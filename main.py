# ================== WebSocket Deriv con autoreconexión ==================
import threading
import time
import json
import traceback
import websocket

MAX_RETRIES = 8
REQUEST_INTERVAL = 0.35   # segundos entre requests para no saturar
PING_INTERVAL = 25        # cada cuánto mandar ping
RECONNECT_BACKOFF = [1, 2, 4, 8, 12, 20, 30, 45]  # seg

class DerivWS:
    def __init__(self, app_id: str, token: str):
        self.app_id = app_id
        self.token = token
        self.ws = None
        self.last_req_ts = 0.0
        self.last_ping_ts = 0.0
        self.lock = threading.Lock()
        self._connect_and_auth()

    # ---------- conexión / auth ----------
    def _connect_and_auth(self):
        self._safe_close()
        url = f"wss://ws.derivws.com/websockets/v3?app_id={self.app_id}"
        self.ws = websocket.create_connection(url, timeout=20)
        # autorizar
        payload = {"authorize": self.token}
        self.ws.send(json.dumps(payload))
        resp = json.loads(self.ws.recv())
        if "error" in resp:
            raise RuntimeError(f"Auth error: {resp}")
        self.last_req_ts = time.time()
        self.last_ping_ts = time.time()

    def _safe_close(self):
        try:
            if self.ws:
                self.ws.close()
        except:
            pass
        finally:
            self.ws = None

    # ---------- utilidades ----------
    def _throttle(self):
        # respeta REQUEST_INTERVAL entre envíos
        dt = time.time() - self.last_req_ts
        if dt < REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL - dt)

    def _ensure_alive(self):
        # manda ping si toca; si falla, reconecta
        now = time.time()
        try:
            if (now - self.last_ping_ts) >= PING_INTERVAL:
                self._send_raw({"ping": 1}, wait=False)
                self.last_ping_ts = now
        except Exception:
            # ping falló -> reconectar
            self._reconnect()

    def _reconnect(self):
        # reintentos con backoff
        self._safe_close()
        for i, delay in enumerate(RECONNECT_BACKOFF, start=1):
            try:
                self._connect_and_auth()
                return
            except Exception:
                time.sleep(delay)
        # último intento
        self._connect_and_auth()

    def _send_raw(self, payload: dict, wait: bool = True):
        # envío básico con lock + control de cierre
        with self.lock:
            try:
                self._throttle()
                self.ws.send(json.dumps(payload))
                self.last_req_ts = time.time()
                if wait:
                    raw = self.ws.recv()
                    return json.loads(raw)
                return None
            except websocket._exceptions.WebSocketConnectionClosedException:
                # socket cerrado -> reconecta y reintenta una vez
                self._reconnect()
                self._throttle()
                self.ws.send(json.dumps(payload))
                self.last_req_ts = time.time()
                if wait:
                    raw = self.ws.recv()
                    return json.loads(raw)
                return None

    # ---------- API de alto nivel ----------
    def candles(self, symbol: str, granularity: int, count: int = 300):
        # asegúrate de que el socket esté vivo (ping/reconect)
        self._ensure_alive()
        req = {
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": int(count),
            "granularity": int(granularity),
            "style": "candles",
            "end": "latest"
        }
        # Deriv pide "subscribe": 0 para histórico
        req["subscribe"] = 0

        # reintentos en caso de error de red
        err = None
        for _ in range(MAX_RETRIES):
            try:
                resp = self._send_raw(req, wait=True)
                if "error" in resp:
                    # errores de cuota o similares: espera y reintenta
                    time.sleep(1.0)
                    err = resp["error"]
                    continue
                candles = resp.get("candles") or []
                return candles
            except Exception as e:
                err = e
                time.sleep(0.8)
                self._reconnect()
        raise RuntimeError(f"candles() failed: {err}")

    def close(self):
        self._safe_close()
# =======================================================================
