# -*- coding: utf-8 -*-
"""
PisosYTechos Bot (Deriv API + Telegram)
- Escanea TODOS los Volatility (incl. 1s), Step, Multi Step y Jump.
- Timeframes: M15, M30, H1, H4, D1.
- Detecta pisos/techos por clusters de pivotes (fractales).
- Solo envía ALERTAS DE ACERCAMIENTO (no ruptura) cuando el nivel tenga >= 2 toques.
- SL = ATR * SL_ATR_FACTOR (detrás del nivel).
- TP único = R/R (default 1:10).
- Notifica por Telegram.
"""
import os, time, json, requests, websocket

# ---------- Config desde ENV ----------
DERIV_TOKEN    = os.getenv("DERIV_TOKEN", "").strip()
DERIV_APP_ID   = os.getenv("DERIV_APP_ID", "1089").strip()  # default public app_id
TG_TOKEN       = os.getenv("TG_TOKEN", "").strip()
TG_CHAT        = os.getenv("TG_CHAT", "").strip()

ATR_PERIOD     = int(os.getenv("ATR_PERIOD", "14"))
SL_ATR_FACTOR  = float(os.getenv("SL_ATR_FACTOR", "1.0"))
RR_RATIO       = float(os.getenv("RR_RATIO", "10"))
PIVOT_K        = int(os.getenv("PIVOT_K", "2"))
TOL_PCT        = float(os.getenv("TOL_PCT", "0.0015"))
TOL_ATR_MULT   = float(os.getenv("TOL_ATR_MULT", "0.5"))
COOLDOWN_SEC   = int(os.getenv("COOLDOWN_SEC", "180"))
BARS           = int(os.getenv("BARS", "350"))
USE_TF         = [x.strip() for x in os.getenv("USE_TF", "M15,M30,H1,H4,D1").split(",") if x.strip()]

GRAN = {"M15":900, "M30":1800, "H1":3600, "H4":14400, "D1":86400}

# ---------- Utilidades ----------
def tg(text):
    if not TG_TOKEN or not TG_CHAT:
        print("TG not configured:", text)
        return
    try:
        requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                     params={"chat_id": TG_CHAT, "text": text})
    except Exception as e:
        print("TG error:", e)

def ws_connect():
    url = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
    ws = websocket.create_connection(url, timeout=20)
    if DERIV_TOKEN:
        ws.send(json.dumps({"authorize": DERIV_TOKEN}))
        auth = json.loads(ws.recv())
        if "error" in auth:
            raise RuntimeError("Auth error: " + str(auth))
    return ws

def ws_call(ws, payload):
    ws.send(json.dumps(payload))
    data = json.loads(ws.recv())
    if "error" in data:
        raise RuntimeError("WS error: " + str(data["error"]))
    return data

def get_active_symbols(ws):
    data = ws_call(ws, {"active_symbols":"brief","product_type":"basic"})
    syms = data.get("active_symbols", [])
    out = []
    for s in syms:
        name = (s.get("display_name","") + " " + s.get("symbol","")).lower()
        if ("volatility" in name) or (" jump" in name) or ("jump " in name) or (" step" in name) or ("multi step" in name):
            out.append(s["symbol"])
    return sorted(set(out))

def get_candles(ws, symbol, granularity, count):
    req = {"ticks_history": symbol,
           "style": "candles",
           "granularity": granularity,
           "count": count,
           "end": "latest"}
    data = ws_call(ws, req)
    return data.get("candles", [])

def calc_atr(candles, period=14):
    if len(candles) < period+1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        cp= float(candles[i-1]["close"])
        tr = max(h - l, abs(h - cp), abs(l - cp))
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period

def pivots(candles, k=2):
    highs, lows = [], []
    for i in range(k, len(candles)-k):
        H = float(candles[i]["high"]); L = float(candles[i]["low"])
        if all(H > float(candles[j]["high"]) for j in range(i-k, i)) and \
           all(H > float(candles[j]["high"]) for j in range(i+1, i+k+1)):
            highs.append((int(candles[i]["epoch"]), H))
        if all(L < float(candles[j]["low"]) for j in range(i-k, i)) and \
           all(L < float(candles[j]["low"]) for j in range(i+1, i+k+1)):
            lows.append((int(candles[i]["epoch"]), L))
    return highs, lows

def cluster_levels(points, tol):
    points = sorted([p for (_,p) in points])
    clusters = []
    for p in points:
        placed = False
        for c in clusters:
            if abs(p - c["level"]) <= tol:
                c["level"] = (c["level"]*c["n"] + p) / (c["n"] + 1)
                c["n"] += 1
                placed = True
                break
        if not placed:
            clusters.append({"level": p, "n": 1})
    return [(round(c["level"], 2), c["n"]) for c in clusters]

_last_alert = {}
def should_alert(key, cooldown=180):
    now = int(time.time())
    last = _last_alert.get(key, 0)
    if now - last >= cooldown:
        _last_alert[key] = now
        return True
    return False

def run():
    ws = ws_connect()
    syms = get_active_symbols(ws)
    tg("🤖 PisosYTechos Bot iniciado. Símbolos: " + ", ".join(syms[:8]) + ("..." if len(syms)>8 else ""))
    while True:
        try:
            for sym in syms:
                for tf in USE_TF:
                    gran = GRAN.get(tf)
                    if not gran: 
                        continue
                    cs = get_candles(ws, sym, gran, BARS)
                    if len(cs) < 50: 
                        time.sleep(0.2); 
                        continue
                    close = float(cs[-1]["close"])
                    prev_close = float(cs[-2]["close"])

                    atr = calc_atr(cs, ATR_PERIOD)
                    tol = max(close * TOL_PCT, atr * TOL_ATR_MULT, 0.1)

                    highs, lows = pivots(cs, PIVOT_K)
                    high_clusters = [(lvl, n) for (lvl, n) in cluster_levels(highs, tol) if n >= 2]
                    low_clusters  = [(lvl, n) for (lvl, n) in cluster_levels(lows,  tol) if n >= 2]

                    for lvl, n in high_clusters:
                        in_zone = (close <= lvl) and (abs(close - lvl) <= tol)
                        was_out = not ((prev_close <= lvl) and (abs(prev_close - lvl) <= tol))
                        approaching = close > prev_close
                        if in_zone and was_out and approaching:
                            key = (sym, tf, "TOP", lvl)
                            if should_alert(key, COOLDOWN_SEC):
                                sl   = lvl + (atr * SL_ATR_FACTOR)
                                risk = abs(sl - lvl)
                                tp   = lvl - (risk * RR_RATIO)
                                msg = f"📈 {sym} {tf} | ACERCÁNDOSE a TECHO ~ {lvl:.2f} (toques={n})\nSL:{sl:.2f} | TP:{tp:.2f} | R/R=1:{int(RR_RATIO)} | ATR={atr:.2f}"
                                tg(msg)

                    for lvl, n in low_clusters:
                        in_zone = (close >= lvl) and (abs(close - lvl) <= tol)
                        was_out = not ((prev_close >= lvl) and (abs(prev_close - lvl) <= tol))
                        approaching = close < prev_close
                        if in_zone and was_out and approaching:
                            key = (sym, tf, "BOT", lvl)
                            if should_alert(key, COOLDOWN_SEC):
                                sl   = lvl - (atr * SL_ATR_FACTOR)
                                risk = abs(lvl - sl)
                                tp   = lvl + (risk * RR_RATIO)
                                msg = f"📉 {sym} {tf} | ACERCÁNDOSE a PISO ~ {lvl:.2f} (toques={n})\nSL:{sl:.2f} | TP:{tp:.2f} | R/R=1:{int(RR_RATIO)} | ATR={atr:.2f}"
                                tg(msg)

                    time.sleep(0.2)

            time.sleep(5)
        except Exception as e:
            print("Loop error:", e)
            try: ws.close()
            except: pass
            time.sleep(3)
            ws = ws_connect()
            syms = get_active_symbols(ws)

if __name__ == "__main__":
    run()
