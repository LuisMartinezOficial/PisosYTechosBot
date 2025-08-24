# -*- coding: utf-8 -*-
"""
PisosYTechos Bot (Deriv API + Telegram)

- Escanea índices sintéticos (Volatility, Jump, Step…)
- Timeframes: M15, M30, H1, H4, D1
- Detecta pisos/techos SOLO cuando:
    • Hay >=3 toques en el mismo nivel (pivotos claros)
    • El precio actual está pegado al nivel (<=0.1% o <=0.2*ATR)
- SL = ATR * SL_ATR_FACTOR (por detrás del nivel)
- TP = ratio fijo R/R (default 1:10)
- Envía alerta clara a Telegram
"""

import os, time, json, requests, websocket
from collections import Counter

# -------- Config desde ENV --------
DERIV_TOKEN   = os.getenv("DERIV_TOKEN", "")
DERIV_APP_ID  = os.getenv("DERIV_APP_ID", "")
TG_TOKEN      = os.getenv("TG_TOKEN", "")
TG_CHAT       = os.getenv("TG_CHAT", "")

ATR_PERIOD    = int(os.getenv("ATR_PERIOD", 14))
SL_ATR_FACTOR = float(os.getenv("SL_ATR_FACTOR", 1.0))
RR_RATIO      = float(os.getenv("RR_RATIO", 10.0))

SYMBOLS = ["R_10", "R_25", "R_50", "R_75", "R_100", "JD10", "JD25", "JD50", "JD100"]
TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"]

# -------- Utilidades --------
def enviar_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TG_CHAT, "text": msg})
    except Exception as e:
        print("Error enviando Telegram:", e)

def calcular_atr(candles, periodo=14):
    if len(candles) < periodo+1: return 0
    trs = []
    for i in range(1, periodo+1):
        h = candles[-i]["high"]
        l = candles[-i]["low"]
        c_prev = candles[-i-1]["close"]
        tr = max(h-l, abs(h-c_prev), abs(l-c_prev))
        trs.append(tr)
    return sum(trs)/len(trs)

# -------- NUEVA detección simple --------
def niveles_simples(cierres, ventana=200, toques_min=3, toler_rel=0.002):
    """
    Detecta niveles horizontales fuertes:
    - pivotes locales (máximos/mínimos)
    - agrupa por cercanía (0.2%)
    - devuelve solo los >= toques_min
    """
    n = len(cierres)
    if n < 5: return []
    ini = max(2, n-ventana)
    piv = []
    for i in range(ini, n-1):
        a,b,c = cierres[i-1], cierres[i], cierres[i+1]
        if b > a and b > c: piv.append(round(b,2))   # techo
        if b < a and b < c: piv.append(round(b,2))   # piso
    if not piv: return []
    cnt = Counter(piv)
    niveles = [(lvl,cts) for lvl,cts in cnt.items() if cts>=toques_min]
    if not niveles: return []
    niveles.sort(key=lambda x:x[0])
    compact=[]
    for lvl,cts in niveles:
        if not compact or abs(lvl-compact[-1][0])/max(1e-9,lvl) > toler_rel:
            compact.append([lvl,cts])
        else:
            compact[-1][1]+=cts
    return [(lvl,cts) for lvl,cts in compact]

# -------- Websocket Deriv --------
def ws_connect():
    url = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}&l=EN"
    return websocket.create_connection(url, timeout=20)

def obtener_candles(symbol, timeframe="1h", count=300):
    ws = ws_connect()
    req = {
        "ticks_history": symbol,
        "style": "candles",
        "granularity": tf_to_seconds(timeframe),
        "count": count,
        "end": "latest"
    }
    ws.send(json.dumps(req))
    data = json.loads(ws.recv())
    ws.close()
    return data.get("candles", [])

def tf_to_seconds(tf):
    return {"15m":900,"30m":1800,"1h":3600,"4h":14400,"1d":86400}[tf]

# -------- Escaneo principal --------
def escanear():
    for sym in SYMBOLS:
        for tf in TIMEFRAMES:
            candles = obtener_candles(sym, tf)
            if not candles: continue
            cierres = [c["close"] for c in candles]
            precio = cierres[-1]
            atr_val = calcular_atr(candles, ATR_PERIOD)
            niveles = niveles_simples(cierres, ventana=200, toques_min=3, toler_rel=0.002)
            umbral = max(precio*0.001, atr_val*0.2)
            for nivel,toques in niveles:
                if abs(precio-nivel) <= umbral:
                    tipo = "PISO" if precio>=nivel else "TECHO"
                    sl = nivel - SL_ATR_FACTOR*atr_val if tipo=="PISO" else nivel + SL_ATR_FACTOR*atr_val
                    tp = precio + (precio-sl)*RR_RATIO if tipo=="PISO" else precio - (sl-precio)*RR_RATIO
                    emoji = "📈" if tipo=="PISO" else "📉"
                    msg = (
                        f"{emoji} {sym} {tf} | ACERCÁNDOSE a {tipo} ~ {nivel} (toques={toques})\n"
                        f"SL:{round(sl,2)} | TP:{round(tp,2)} | R/R=1:{int(RR_RATIO)} | ATR={round(atr_val,2)}"
                    )
                    enviar_telegram(msg)

# -------- Loop --------
if __name__=="__main__":
    while True:
        try:
            escanear()
        except Exception as e:
            print("Error:", e)
        time.sleep(300)  # cada 5 min
