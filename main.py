# -*- coding: utf-8 -*-
"""
PisosYTechos Bot (Deriv API + Telegram) — versión Nivel PRO

➤ Escanea VOLATILITY (incluye 1HZ series).
➤ Timeframes: M15, M30, H1, H4, D1.
➤ Detecta SÓLO pisos/techos FUERTES y CLAROS:
    - Clusters de pivotes (mínimo de toques).
    - Confluencia por ATR (nivel dentro de k*ATR).
    - Rechazo con mecha (mecha/cuerpo alto).
    - Entrada con momentum (aceleración previa al nivel).

➤ Envia ALERTAS de ACERCAMIENTO (no ruptura) por Telegram.
"""

import os
import time
import json
import math
import traceback
import threading
from collections import defaultdict, deque

import websocket  # websocket-client
import requests  # Telegram

# ------------- Config desde ENV (con defaults sensatos) -------------
DERIV_TOKEN   = os.getenv("DERIV_TOKEN", "")
DERIV_APP_ID  = os.getenv("DERIV_APP_ID", "")
TG_TOKEN      = os.getenv("TG_TOKEN", "")
TG_CHAT       = os.getenv("TG_CHAT", "")

ATR_PERIOD        = int(os.getenv("ATR_PERIOD", "14"))
SL_ATR_FACTOR     = float(os.getenv("SL_ATR_FACTOR", "1.0"))
RR_RATIO          = float(os.getenv("RR_RATIO", "10"))
MIN_TOUCHES       = int(os.getenv("MIN_TOUCHES", "3"))          # mínimo de toques para que sea nivel fuerte
MAX_DISTANCE_PCT  = float(os.getenv("MAX_DISTANCE_PCT", "0.20")) # 0.20% (tolerancia de cluster)
LOOKBACK_BARS     = int(os.getenv("LOOKBACK_BARS", "250"))      # muestra para análisis por TF

# Símbolos volátiles (puedes agregar/quitar)
SYMBOLS = [
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V", "JD10", "JD25", "JD50", "JD75", "JD100"
]

# Timeframes Deriv (en segundos)
TIMEFRAMES = {
    "M15":  15 * 60,
    "M30":  30 * 60,
    "H1":   60 * 60,
    "H4":   4  * 60 * 60,
    "D1":   24 * 60 * 60,
}

DERIV_WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

# -------------------- Utilidades --------------------
def send_telegram(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print("TG no configurado: ", text[:120])
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print("Error enviando a Telegram:", e)

def percent_dist(a, b):
    return abs(a - b) / ((a + b) / 2.0) * 100.0

def atr_from_candles(candles, period=14):
    # True Range clásica con prev close
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i-1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < period:
        return None
    # ATR simple
    return sum(trs[-period:]) / period

def pivot_points(candles, left=2, right=2):
    """
    Detecta pivotes simples (alto/bajo local). Devuelve lista de dicts:
      {"idx": i, "price": nivel, "type": "piso"|"techo"}
    """
    pivs = []
    for i in range(left, len(candles) - right):
        highs = [candles[i - j]["high"] for j in range(left, 0, -1)] + [candles[i]["high"]] + [candles[i + j]["high"] for j in range(1, right + 1)]
        lows  = [candles[i - j]["low"]  for j in range(left, 0, -1)] + [candles[i]["low"]]  + [candles[i + j]["low"]  for j in range(1, right + 1)]
        c = candles[i]
        if c["high"] == max(highs):
            pivs.append({"idx": i, "price": c["high"], "type": "techo"})
        if c["low"] == min(lows):
            pivs.append({"idx": i, "price": c["low"], "type": "piso"})
    return pivs

def cluster_levels(pivots, max_pct, last_close):
    """
    Agrupa pivotes por cercanía porcentual (max_pct). Devuelve niveles:
      [{"price": nivel_medio, "type": "piso"/"techo", "touches": n, "members": [...]}]
    Filtra por mayoría de tipo (si mezcla, toma el dominante).
    """
    clusters = []
    used = set()
    for i, p in enumerate(pivots):
        if i in used:
            continue
        group = [p]
        used.add(i)
        for j in range(i + 1, len(pivots)):
            if j in used:
                continue
            if percent_dist(p["price"], pivots[j]["price"]) <= max_pct:
                group.append(pivots[j])
                used.add(j)
        # Tipo dominante
        types = defaultdict(int)
        for m in group:
            types[m["type"]] += 1
        t = "piso" if types["piso"] >= types["techo"] else "techo"
        price = sum(m["price"] for m in group) / len(group)
        clusters.append({
            "price": price,
            "type": t,
            "touches": len(group),
            "members": group
        })
    # Orden por cercanía al precio actual (priorizar niveles relevantes)
    clusters.sort(key=lambda x: abs(x["price"] - last_close))
    return clusters

def wick_rejection_score(candle, level_type, near_level=True):
    """
    Mide rechazo de mecha:
      - techo: mecha superior larga y cuerpo pequeño
      - piso:  mecha inferior larga y cuerpo pequeño
    Devuelve score [0..1]. >0.6 = buen rechazo.
    """
    h, l, o, c = candle["high"], candle["low"], candle["open"], candle["close"]
    body = abs(c - o)
    range_ = max(1e-9, h - l)
    upper_w = h - max(o, c)
    lower_w = min(o, c) - l

    if range_ <= 0:
        return 0.0

    if level_type == "techo":
        # Queremos mecha arriba grande y cuerpo pequeño
        score = (upper_w / range_) * 0.7 + (1 - body / range_) * 0.3
    else:
        # piso: mecha abajo grande
        score = (lower_w / range_) * 0.7 + (1 - body / range_) * 0.3

    # Si no estuvo “cerca del nivel” se penaliza un poco
    if not near_level:
        score *= 0.8
    return max(0.0, min(1.0, score))

def momentum_into_level(candles, idx, look=5):
    """
    Evalúa si venía con aceleración (velas consecutivas en la dirección del nivel).
    Devuelve True/False.
    """
    if idx - look < 0:
        return False
    ups, downs = 0, 0
    for i in range(idx - look, idx):
        if candles[i]["close"] > candles[i]["open"]:
            ups += 1
        else:
            downs += 1
    # momentum si mayoría en una dirección
    return (ups >= int(0.7 * look)) or (downs >= int(0.7 * look))

def near_level(price, level, atr, k_atr=0.6, max_pct=0.20):
    """Cerca si está dentro de k*ATR o dentro del % máximo (lo que sea más estricto)."""
    cond_atr = (atr is not None) and (abs(price - level) <= k_atr * atr)
    cond_pct = percent_dist(price, level) <= max_pct
    return cond_atr or cond_pct

# -------------------- Cliente Deriv --------------------
class DerivWS:
    def __init__(self, app_id, token):
        self.url = f"wss://ws.derivws.com/websockets/v3?app_id={app_id}"
        self.token = token
        self.ws = None
        self.lock = threading.Lock()
        self.req_id = 1
        self.connected = False
        self._connect_and_auth()

    def _connect_and_auth(self):
        self.ws = websocket.create_connection(self.url, timeout=20)
        self.connected = True
        self._send({"authorize": self.token})
        auth = self._recv()
        if "error" in auth:
            raise RuntimeError("Auth error: " + str(auth))
        print("Deriv autorizado")

    def _send(self, payload):
        with self.lock:
            payload["req_id"] = self.req_id
            self.req_id += 1
            self.ws.send(json.dumps(payload))

    def _recv(self):
        raw = self.ws.recv()
        return json.loads(raw)

    def candles(self, symbol, granularity, count=200):
        """
        Devuelve lista de velas dict: time, open, high, low, close
        """
        # Deriv permite máximo 5000 por request, usamos count razonable:
        self._send({
            "ticks_history": symbol,
            "style": "candles",
            "granularity": granularity,
            "count": count,
            "end": "latest",
        })
        data = self._recv()
        if "error" in data:
            raise RuntimeError(str(data["error"]))
        cs = data.get("candles", [])
        out = []
        for c in cs:
            out.append({
                "time": c["epoch"],
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
            })
        return out

    def close(self):
        try:
            self.ws.close()
        except:
            pass
        self.connected = False

# -------------------- Detección de niveles fuertes --------------------
def detectar_niveles_fuertes(candles, min_touches, max_pct, atr):
    """
    1) pivotes -> 2) cluster -> 3) filtra por toques + rechazo + momentum
    Devuelve lista de niveles dict:
      {"type": 'piso'|'techo', "price": nivel, "touches": n, "evidences": [...]}
    """
    if len(candles) < ATR_PERIOD + 10:
        return []

    pivs = pivot_points(candles, left=2, right=2)
    last_close = candles[-1]["close"]
    clusters = cluster_levels(pivs, max_pct, last_close)

    out = []
    for cl in clusters:
        if cl["touches"] < min_touches:
            continue

        # evidencia de rechazo + momentum cerca del nivel
        evidences = []
        good_hits = 0
        for m in cl["members"]:
            i = m["idx"]
            cndl = candles[i]
            # estaba cerca del nivel?
            near = near_level(cndl["close"], cl["price"], atr, k_atr=0.6, max_pct=max_pct)
            score = wick_rejection_score(cndl, cl["type"], near_level=near)
            mom = momentum_into_level(candles, i, look=5)
            if near and score >= 0.6 and mom:
                good_hits += 1
                evidences.append({"idx": i, "score": round(score, 2)})

        # exigimos que al menos 2 toques sean “buenos”
        if good_hits >= 2:
            out.append({
                "type": cl["type"],
                "price": cl["price"],
                "touches": cl["touches"],
                "evidences": evidences
            })

    # ordena por cercanía al precio actual
    out.sort(key=lambda x: abs(x["price"] - candles[-1]["close"]))
    return out

# -------------------- Lógica principal --------------------
def escanear_y_alertar(ws: DerivWS):
    for symbol in SYMBOLS:
        for tf_name, gran in TIMEFRAMES.items():
            try:
                candles = ws.candles(symbol, granularity=gran, count=max(LOOKBACK_BARS, ATR_PERIOD + 50))
                if len(candles) < ATR_PERIOD + 10:
                    continue

                atr = atr_from_candles(candles, ATR_PERIOD)
                if atr is None or atr <= 0:
                    continue

                niveles = detectar_niveles_fuertes(
                    candles=candles,
                    min_touches=MIN_TOUCHES,
                    max_pct=MAX_DISTANCE_PCT,
                    atr=atr
                )

                if not niveles:
                    continue

                last = candles[-1]
                last_price = last["close"]

                for lv in niveles[:3]:  # máx 3 por tf/símbolo para no spamear
                    # solo avisar si estamos ACERCÁNDONOS (no ruptura)
                    # acercándose = precio aún no cruzó el nivel y distancia cae
                    dist_pct = percent_dist(last_price, lv["price"])
                    if dist_pct > MAX_DISTANCE_PCT:
                        continue
                    tipo = "PISO" if lv["type"] == "piso" else "TECHO"

                    # SL/TP estimados (atr-based + R/R único)
                    sl = lv["price"] - SL_ATR_FACTOR * atr if lv["type"] == "piso" else lv["price"] + SL_ATR_FACTOR * atr
                    tp = lv["price"] + RR_RATIO * (lv["price"] - sl) if lv["type"] == "piso" else lv["price"] - RR_RATIO * (sl - lv["price"])

                    msg = (
                        f"📈 <b>{symbol} {tf_name}</b> | <b>ACERCÁNDOSE a {tipo}</b>\n"
                        f"Nivel ~ <b>{lv['price']:.2f}</b> (toques={lv['touches']})\n"
                        f"SL:<code>{sl:.2f}</code> | TP:<code>{tp:.2f}</code> | R/R=1:{int(RR_RATIO)} | ATR={atr:.2f}\n"
                        f"Evidencias: {len(lv['evidences'])} rechazos claros"
                    )
                    print(msg)
                    send_telegram(msg)

            except Exception as e:
                print(f"[{symbol} {tf_name}] Error:", e)
                traceback.print_exc()
                # Si algo raro, esperamos un poco
                time.sleep(2)

def run():
    if not DERIV_APP_ID or not DERIV_TOKEN:
        raise RuntimeError("Faltan DERIV_APP_ID o DERIV_TOKEN")
    ws = None
    try:
        ws = DerivWS(DERIV_APP_ID, DERIV_TOKEN)
        print("✅ Bot iniciado. Escaneando niveles fuertes…")
        # Ciclo principal: escanea cada N minutos (por defecto 2)
        while True:
            escanear_y_alertar(ws)
            time.sleep(120)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("Fallo crítico:", e)
        traceback.print_exc()
        time.sleep(5)
    finally:
        if ws:
            ws.close()

if __name__ == "__main__":
    run()
