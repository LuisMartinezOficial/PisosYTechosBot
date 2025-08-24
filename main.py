# -*- coding: utf-8 -*-
"""
PisosYTechos Bot (Deriv API + Telegram)
- Escanea TODOS los índices (Volatility, Step, MultiStep, Jump)
- Timeframes: M15, M30, H1, H4, D1.
- Detecta pisos/techos más claros (clusters con rechazos).
- Envío de ALERTAS a Telegram.
- SL = ATR * SL_ATR_FACTOR (detrás del nivel).
- TP = R/R (default 1:10).
"""

import os, time, json, requests, traceback
import numpy as np

# -------- Config desde Railway --------
DERIV_TOKEN   = os.getenv("DERIV_TOKEN", "")
DERIV_APP_ID  = os.getenv("DERIV_APP_ID", "")
TG_TOKEN      = os.getenv("TG_TOKEN", "")
TG_CHAT       = os.getenv("TG_CHAT", "")

ATR_PERIOD    = int(os.getenv("ATR_PERIOD", 14))
SL_ATR_FACTOR = float(os.getenv("SL_ATR_FACTOR", 1.0))
RR_RATIO      = float(os.getenv("RR_RATIO", 10.0))
MIN_TOUCHES   = int(os.getenv("MIN_TOUCHES", 2))     # mínimo de toques
MIN_RECHAZOS  = int(os.getenv("MIN_RECHAZOS", 2))    # mínimo de rechazos claros

# -------- Funciones utilitarias --------
def deriv_call(payload):
    """Consulta al API de Deriv (HTTP, no WS persistente)."""
    url = f"https://api.deriv.com/api/v3"
    headers = {"Content-Type":"application/json"}
    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload))
        return r.json()
    except Exception as e:
        print("Error HTTP:", e)
        return {}

def enviar_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TG_CHAT, "text": msg, "parse_mode":"HTML"})
    except:
        pass

def calcular_atr(candles, period=14):
    closes = [c['close'] for c in candles]
    highs  = [c['high']  for c in candles]
    lows   = [c['low']   for c in candles]
    trs = []
    for i in range(1,len(candles)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    return np.mean(trs[-period:]) if len(trs)>=period else np.mean(trs)

# -------- Nueva lógica de pisos/techos --------
def detectar_pisos_techos(precios, min_touches=2, min_rechazos=2, tol=0.001):
    niveles = []
    for i in range(2, len(precios)-2):
        p = precios[i]
        if precios[i-2] > p and precios[i-1] > p and precios[i+1] > p and precios[i+2] > p:
            niveles.append(("piso", p))
        if precios[i-2] < p and precios[i-1] < p and precios[i+1] < p and precios[i+2] < p:
            niveles.append(("techo", p))

    clusters = []
    for tipo, px in niveles:
        found = False
        for c in clusters:
            if abs(c["nivel"]-px) <= c["nivel"]*tol and c["tipo"]==tipo:
                c["toques"] += 1
                c["precios"].append(px)
                found = True
                break
        if not found:
            clusters.append({"tipo":tipo,"nivel":px,"toques":1,"precios":[px]})

    clusters = [c for c in clusters if c["toques"]>=min_touches]

    for c in clusters:
        rechazos = sum([1 for px in c["precios"] if abs(px-c["nivel"])<=c["nivel"]*tol])
        c["rechazos"] = rechazos

    clusters = [c for c in clusters if c["rechazos"]>=min_rechazos]
    return clusters

# -------- Escaneo --------
def escanear_y_alertar(symbol, tf="M15"):
    try:
        granularities = {"M15":900,"M30":1800,"H1":3600,"H4":14400,"D1":86400}
        req = {
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": 200,
            "granularity": granularities[tf],
            "style": "candles",
            "end": "latest"
        }
        data = deriv_call(req)
        candles = data.get("candles", [])
        if len(candles)<30: return

        precios = [c["close"] for c in candles]
        atr = calcular_atr(candles, ATR_PERIOD)
        niveles = detectar_pisos_techos(precios, MIN_TOUCHES, MIN_RECHAZOS)

        for n in niveles:
            tipo, nivel, toques, rechazos = n["tipo"], n["nivel"], n["toques"], n["rechazos"]
            ultimo = precios[-1]

            if tipo=="piso" and ultimo>nivel:
                SL = nivel - SL_ATR_FACTOR*atr
                TP = ultimo + RR_RATIO*(ultimo-nivel)
                enviar_telegram(f"📉 {symbol} {tf} | ACERCÁNDOSE a PISO ~ {nivel:.2f} (toques={toques})\n"
                                f"SL:{SL:.2f} | TP:{TP:.2f} | R/R=1:{int(RR_RATIO)} | ATR={atr:.2f}\n"
                                f"Evidencias: {rechazos} rechazos claros")
            elif tipo=="techo" and ultimo<nivel:
                SL = nivel + SL_ATR_FACTOR*atr
                TP = ultimo - RR_RATIO*(nivel-ultimo)
                enviar_telegram(f"📈 {symbol} {tf} | ACERCÁNDOSE a TECHO ~ {nivel:.2f} (toques={toques})\n"
                                f"SL:{SL:.2f} | TP:{TP:.2f} | R/R=1:{int(RR_RATIO)} | ATR={atr:.2f}\n"
                                f"Evidencias: {rechazos} rechazos claros")

    except Exception as e:
        print(f"[{symbol} {tf}] Error:",e)
        traceback.print_exc()

# -------- Main --------
def run():
    print("✅ Bot iniciado. Escaneando...")
    timeframes = ["M15","M30","H1","H4","D1"]
    indices = ["R_10","R_25","R_50","R_75","R_100",
               "JD10","JD25","JD50","JD75","JD100",
               "1HZ10V","1HZ25V","1HZ50V","1HZ75V","1HZ100V",
               "STEPINDEX","MULTISTEPINDEX"]
    while True:
        for sym in indices:
            for tf in timeframes:
                escanear_y_alertar(sym, tf)
                time.sleep(2)
        time.sleep(60)

if __name__=="__main__":
    run()
