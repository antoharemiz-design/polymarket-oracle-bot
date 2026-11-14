"""Live market data: Polymarket market channel and Binance spot, normalized like polybot.replay."""
import asyncio
import json
import time

import websockets

PM_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = "wss://data-stream.binance.vision/ws/{symbol}@bookTicker"


def now_ms() -> int:
    return int(time.time() * 1000)


async def spot_feed(on_mid, symbol: str = "btcusdt", log=print, on_alive=None):
    """Calls on_mid(local_ms, mid) on every top-of-book change and on_alive(local_ms) on every
    message (a quiet market sends unchanged quotes; that is not a stale feed). Reconnects forever."""
    last = None
    while True:
        try:
            async with websockets.connect(BINANCE_WS.format(symbol=symbol), open_timeout=10) as ws:
                log("spot feed connected")
                async for raw in ws:
                    d = json.loads(raw)
                    t = now_ms()
                    if on_alive is not None:
                        on_alive(t)
                    k = (d["b"], d["a"])
                    if k != last:
                        last = k
                        on_mid(t, (float(d["b"]) + float(d["a"])) / 2.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"spot feed error: {e!r}"); await asyncio.sleep(1)


async def market_feed(token_up: str, token_down: str, handler, until_s: float, log=print):
    """Feeds one market into `handler` until `until_s` (epoch seconds).

    handler.on_snapshot(bids, asks), handler.on_level(side, price_c, size),
    handler.on_trade(taker_side, price_c, size, local_ms), handler.on_feed(local_ms) on every message.
    Only Up-token rows are used; the Down book is the mirror image.
    """
    while time.time() < until_s:
        try:
            async with websockets.connect(PM_WS, open_timeout=10, ping_interval=None, max_size=2 ** 24) as ws:
                await ws.send(json.dumps({"assets_ids": [token_up, token_down], "type": "market"}))
                last_ping = time.time()
                while time.time() < until_s:
                    if time.time() - last_ping > 9:
                        await ws.send("PING"); last_ping = time.time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    if raw == "PONG":
                        continue
                    t = now_ms()
                    d = json.loads(raw)
                    handler.on_feed(t)
                    for x in (d if isinstance(d, list) else [d]):
                        et = x.get("event_type")
                        if et == "price_change":
                            for c in x["price_changes"]:
                                if c["asset_id"] == token_up:
                                    handler.on_level("B" if c["side"] == "BUY" else "S",
                                                     int(round(float(c["price"]) * 100)), float(c["size"]))
                        elif et == "book":
                            if x["asset_id"] == token_up:
                                handler.on_snapshot([[b["price"], b["size"]] for b in x["bids"]],
                                                    [[a["price"], a["size"]] for a in x["asks"]])
                        elif et == "last_trade_price":
                            side = "B" if x["side"] == "BUY" else "S"
                            pc = int(round(float(x["price"]) * 100))
                            if x["asset_id"] == token_down:
                                side = "S" if side == "B" else "B"; pc = 100 - pc
                            elif x["asset_id"] != token_up:
                                continue
                            handler.on_trade(side, pc, float(x.get("size") or 0), t)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"market feed error: {e!r}"); await asyncio.sleep(0.5)
