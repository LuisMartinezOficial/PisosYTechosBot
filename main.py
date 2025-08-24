try:
    candles = ws.candles(symbol, granularity=gran, count=max(LOOKBACK_BARS, ATR_PERIOD + 50))
except Exception as e:
    print(f"[{symbol} {gran}] Reconectando WebSocket por error: {e}")
    ws = DerivWS(DERIV_APP_ID, DERIV_TOKEN)  # abre un socket nuevo
    candles = ws.candles(symbol, granularity=gran, count=max(LOOKBACK_BARS, ATR_PERIOD + 50))
