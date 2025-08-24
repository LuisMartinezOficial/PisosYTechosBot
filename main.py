# -*- coding: utf-8 -*-
"""
PisosYTechos Bot (Deriv API + Telegram)

• Escanea Volatility, Jump (1Hz), Step y Multi-Step.
• Timeframes: M15, M30, H1, H4, D1.
• Detecta PISOS/TECHOS claros por clusters de pivotes/wicks (“rechazos”).
• Solo envía ALERTAS DE ACERCAMIENTO (no rompe-niveles).
• SL = ATR × SL_ATR_FACTOR (por detrás del nivel)
• TP con R/R fijo (default 1:10)
• Notifica por Telegram con nombre legible del instrumento.

Requiere variables de entorno:
DERIV_APP_ID, TG_TOKEN, TG_CHAT
Opcionales:
ATR_PERIOD, SL_ATR_FACTOR, RR_RATIO, MIN_TOUCHES, NEAR_ATR, MAX_LEVELS_PER_TF
"""

import os, time, json, math, traceback
import requests
import websocket

# ------------ Config desde ENV ------------
DERIV_TOKEN     = os.getenv("DERIV_TOKEN", "")
DERIV_APP_ID    = os.getenv("DERIV_APP_ID", "")
TG_TOKEN        = os.getenv("TG_TOKEN", "")
TG_CHAT         = os.getenv("TG_CHAT", "")

ATR_PERIOD      = int(os.getenv("ATR_PERIOD", "14"))
SL_ATR_FACTOR   = float(os.getenv("SL_ATR_FACTOR", "1.0"))
RR_RATIO        = float(os.getenv("RR_RATIO", "10"))

# Sensibilidad de niveles / alertas
MIN_TOUCHES         = int(os.getenv("MIN_TOUCHES", "5"))     # rechazos mínimos para considerar nivel
NEAR_ATR            = float(os.getenv("NEAR_ATR", "1.2"))    # cuántos ATRs cuenta como “acercándose”
MAX_LEVELS_PER_TF   = int(os.getenv("MAX_LEVELS_PER_TF", "1"))

# Timeframes a escanear
TF_MAP = {
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

# Se rellenan al arrancar
SYMBOLS = []
NOMBRE_LEGIBLE = {}


# ------------ Utilidades HTTP/WS ------------
def tg_send(text):
    """Envía mensaje a Telegram."""
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": text}
        )
    except Exception:
        pass


def ws_call(payload, timeout=25):
    """Llamada corta al WS de Deriv (conecta, manda, recibe, cierra)."""
    url = "wss://ws.derivws.com/websockets/v3?app_id=" + str(DERIV_APP_ID)
    ws = websocket.create_connection(url, timeout=timeout)
    ws.send(json.dumps(payload))
    raw = ws.recv()
    ws.close()
    return json.loads(raw)


# ------------ Descubrir símbolos + nombres ------------
def descubrir_simbolos_y_nombres():
    """
    Devuelve (lista_simbolos, dict_nombre_legible)
    Incluye Volatility, Jump, Step y Multi-Step cuando estén disponibles.
    """
    try:
        resp = ws_call({"active_symbols": "brief", "product_type": "basic"})
        items = resp.get("active_symbols", []) if isinstance(resp, dict) else []
    except Exception:
        items = []

    simbolos, nombres = [], {}
    SUBMERCADOS_OK = {
        "Volatility Indices",
        "Jump Indices",
        "Step Indices",
        "Multi Step Indices",
    }
    for it in items:
        # Algunos tenants usan submarket o submarket_display_name
        submarket = it.get("submarket") or it.get("submarket_display_name") or ""
        if submarket in SUBMERCADOS_OK:
            s = it.get("symbol")
            dn = it.get("display_name") or s
            if s:
                simbolos.append(s)
                nombres[s] = dn

    # Respaldo si no devolvió nada
    if not simbolos:
        simbolos = [
            # Volatility
            "R_10", "R_25", "R_50", "R_75", "R_100",
            # Jump (1Hz)
            "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
            # Step / Multi (nombres pueden variar por cuenta)
            "STEP_INDEX", "STEP_INDEX_2", "STEP_INDEX_3", "STEP_INDEX_4", "STEP_INDEX_5",
            "MULTI_STEP_INDEX_1", "MULTI_STEP_INDEX_2", "MULTI_STEP_INDEX_3",
        ]
        for s in simbolos:
            nombres[s] = s

    return simbolos, nombres


# ------------ Datos de velas + indicadores ------------
def pedir_velas(symbol, granularity, count=500):
    """
    Pide velas OHLC a Deriv (candles).
    Retorna lista de dicts con epoch, open, high, low, close.
    """
    payload = {
        "ticks_history": symbol,
        "adjust_start_time": 1,
        "count": count,
        "granularity": granularity,
        "style": "candles",
    }
    data = ws_call(payload)
    c = data.get("candles") or []
    # normalizamos
    velas = []
    for it in c:
        velas.append({
            "t": it.get("epoch"),
            "o": float(it.get("open")),
            "h": float(it.get("high")),
            "l": float(it.get("low")),
            "c": float(it.get("close")),
        })
    return velas


def calc_atr(velas, period=14):
    """ATR simple sobre high/low/close."""
    if len(velas) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(velas)):
        h = velas[i]["h"]; l = velas[i]["l"]; pc = velas[i-1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    trs = trs[-period:]
    return sum(trs) / max(1, len(trs))


# ------------ Detección de pisos/techos claros ------------
def pivotes(velas, win=3):
    """
    Devuelve listas de índices que son pivote alto y pivote bajo (locales).
    win=3 -> compara 3 velas a cada lado.
    """
    highs, lows = [], []
    n = len(velas)
    for i in range(win, n - win):
        h = velas[i]["h"]; l = velas[i]["l"]
        if all(h >= velas[i-k]["h"] for k in range(1, win+1)) and all(h >= velas[i+k]["h"] for k in range(1, win+1)):
            highs.append(i)
        if all(l <= velas[i-k]["l"] for k in range(1, win+1)) and all(l <= velas[i+k]["l"] for k in range(1, win+1)):
            lows.append(i)
    return highs, lows


def cluster_niveles(precios, tolerancia):
    """
    Agrupa precios cercanos (|a-b| <= tolerancia).
    Retorna lista de (nivel, cantidad, elementos).
    """
    if not precios:
        return []
    precios = sorted(precios)
    grupos = []
    grp = [precios[0]]
    for x in precios[1:]:
        if abs(x - grp[-1]) <= tolerancia:
            grp.append(x)
        else:
            grupos.append(grp)
            grp = [x]
    grupos.append(grp)

    res = []
    for g in grupos:
        nivel = sum(g) / len(g)
        res.append((nivel, len(g), g))
    # Ordena por cantidad desc
    res.sort(key=lambda x: x[1], reverse=True)
    return res


def detectar_niveles(velas, atr, min_touches=5, tol_atr=0.3):
    """
    Encuentra niveles de soporte (pisos) y resistencia (techos) “claros”.
    • min_touches: rechazos mínimos.
    • tol_atr: ancho de cluster en múltiplos de ATR.
    Retorna dict: {'pisos': [(nivel, toques)], 'techos': [...]}
    """
    hs, ls = pivotes(velas, win=3)

    highs_prec = [velas[i]["h"] for i in hs]
    lows_prec  = [velas[i]["l"] for i in ls]

    tol = max(1e-9, atr * tol_atr)
    c_hi = cluster_niveles(highs_prec, tol)
    c_lo = cluster_niveles(lows_prec,  tol)

    techos = [(nivel, cnt) for (nivel, cnt, _) in c_hi if cnt >= min_touches]
    pisos  = [(nivel, cnt) for (nivel, cnt, _) in c_lo if cnt >= min_touches]

    return {"pisos": techos if False else pisos, "techos": techos}


# ------------ Formato de mensajes ------------
def nombre_amigable(symbol):
    return NOMBRE_LEGIBLE.get(symbol, symbol)


def formatear_alerta(symbol, timeframe, tipo, nivel, toques, sl, tp, atr, evidencias=None):
    """
    tipo: 'PISO' o 'TECHO'
    evidencias: int o None
    """
    titulo = f"📈 {nombre_amigable(symbol)} {timeframe} | ACERCÁNDOSE a {tipo} ~\n"
    linea1 = f"Nivel ~ {nivel:.2f} (toques={toques})\n"
    rr = RR_RATIO
    linea2 = f"SL:{sl:.2f} | TP:{tp:.2f} | R/R=1:{int(rr)} | ATR={atr:.2f}\n"
    linea3 = f"Evidencias: {evidencias} rechazos claros" if evidencias is not None else ""
    return titulo + linea1 + linea2 + linea3


# ------------ Lógica de escaneo ------------
def escanear_symbol_tf(symbol, tf_name, gran):
    """Escanea un símbolo en un timeframe y, si hay acercamiento, envía alertas."""
    velas = pedir_velas(symbol, granularity=gran, count=500)
    if len(velas) < ATR_PERIOD + 20:
        return

    atr = calc_atr(velas, ATR_PERIOD)
    if atr <= 0:
        return

    niveles = detectar_niveles(velas, atr, min_touches=MIN_TOUCHES, tol_atr=0.35)
    close = velas[-1]["c"]

    # Distancia para considerar “acercándose”
    near_dist = max(1e-9, atr * NEAR_ATR)

    enviados = 0

    # Pisos (soportes) cerca por debajo del precio
    candidatos_piso = [x for x in niveles["pisos"] if (0 <= (close - x[0]) <= near_dist)]
    # Techos (resistencias) cerca por encima del precio
    candidatos_techo = [x for x in niveles["techos"] if (0 <= (x[0] - close) <= near_dist)]

    candidatos_piso.sort(key=lambda x: abs(close - x[0]))
    candidatos_techo.sort(key=lambda x: abs(x[0] - close))

    for (nivel, toques) in candidatos_piso[:MAX_LEVELS_PER_TF]:
        sl = nivel - SL_ATR_FACTOR * atr
        tp = close + RR_RATIO * (close - sl)  # compra desde zona de soporte
        msg = formatear_alerta(symbol, tf_name, "PISO", nivel, toques, sl, tp, atr, evidencias=toques)
        tg_send(msg)
        enviados += 1

    for (nivel, toques) in candidatos_techo[:MAX_LEVELS_PER_TF]:
        sl = nivel + SL_ATR_FACTOR * atr
        tp = close - RR_RATIO * (sl - close)  # venta desde zona de resistencia
        msg = formatear_alerta(symbol, tf_name, "TECHO", nivel, toques, sl, tp, atr, evidencias=toques)
        tg_send(msg)
        enviados += 1

    return enviados


def escanear_y_alertar():
    total = 0
    for symbol in SYMBOLS:
        for tf_name, gran in TF_MAP.items():
            try:
                enviados = escanear_symbol_tf(symbol, tf_name, gran)
                if enviados:
                    total += enviados
            except Exception:
                traceback.print_exc()
                time.sleep(1)
    if total:
        print(f"Enviadas {total} alertas.")
    else:
        print("Sin acercamientos claros.")


# ------------ Main loop ------------
def run():
    if not DERIV_APP_ID or not TG_TOKEN or not TG_CHAT:
        raise RuntimeError("Faltan variables: DERIV_APP_ID, TG_TOKEN, TG_CHAT")

    # Descubrir símbolos y nombres
    global SYMBOLS, NOMBRE_LEGIBLE
    try:
        SYMBOLS, NOMBRE_LEGIBLE = descubrir_simbolos_y_nombres()
        print(f"🔎 Símbolos a escanear: {len(SYMBOLS)}")
        if SYMBOLS:
            print("Ejemplos:", [NOMBRE_LEGIBLE.get(s, s) for s in SYMBOLS[:8]])
    except Exception:
        traceback.print_exc()
        # Si falló, quedarán listas por defecto dentro de la función

    print("✅ Bot iniciado. Escaneando niveles…")
    tg_send("🤖 PisosYTechos Bot iniciado.")

    try:
        while True:
            escanear_y_alertar()
            time.sleep(120)  # cada 2 minutos
    except KeyboardInterrupt:
        pass
    except Exception:
        print("Fallo crítico:")
        traceback.print_exc()
        time.sleep(5)


if __name__ == "__main__":
    run()
