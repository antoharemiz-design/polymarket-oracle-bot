"""Live market data: Polymarket market channel and Binance spot, normalized like polybot.replay."""
import asyncio
import json
import time

import websockets

PM_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WS = "wss://data-stream.binance.vision/ws/{symbol}@bookTicker"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"


def now_ms() -> int:
    return int(time.time() * 1000)


async def spot_feed(on_mid, symbol: str = "btc", log=print, on_alive=None, source: str = "coinbase"):
    """Calls on_mid(local_ms, mid) on every top-of-book change and on_alive(local_ms) on every
    message (a quiet market sends unchanged quotes; that is not a stale feed). Reconnects forever.

    Coinbase is the default: on recorded data it had no gaps and predicted the Polymarket book
    slightly better than the Binance mirror reachable from restricted regions.
    """
    last = None
    while True:
        try:
            if source == "coinbase":
                url, sub = COINBASE_WS, {"type": "subscribe", "product_ids": [f"{symbol.upper()}-USD"], "channels": ["ticker"]}
            else:
                url, sub = BINANCE_WS.format(symbol=symbol.lower() + "usdt"), None
            async with websockets.connect(url, open_timeout=10) as ws:
                if sub:
                    await ws.send(json.dumps(sub))
                log(f"spot feed connected ({source})")
                async for raw in ws:
                    d = json.loads(raw)
                    if source == "coinbase":
                        if d.get("type") != "ticker" or not d.get("best_bid") or not d.get("best_ask"):
                            continue
                        k = (d["best_bid"], d["best_ask"])
                    else:
                        k = (d["b"], d["a"])
                    t = now_ms()
                    if on_alive is not None:
                        on_alive(t)
                    if k != last:
                        last = k
                        on_mid(t, (float(k[0]) + float(k[1])) / 2.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"spot feed error: {e!r}"); await asyncio.sleep(1)


async def market_feed(token_up: str, token_down: str, handler, until_s: float, log=print):
    """Feeds one market into `handler` until `until_s` (epoch seconds).

    handler.on_feed(local_ms) is called for every message and handler.feed(event) for every
    normalized event, in the same tuple format as polybot.replay (Up-token terms; the Down book
    is the mirror image and is ignored).
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
                        srv = int(x.get("timestamp") or 0)
                        if et == "price_change":
                            for c in x["price_changes"]:
                                if c["asset_id"] == token_up:
                                    handler.feed(("P", t, "B" if c["side"] == "BUY" else "S",
                                                  int(round(float(c["price"]) * 100)), float(c["size"]), srv))
                        elif et == "book":
                            if x["asset_id"] == token_up:
                                handler.feed(("B", t, [[b["price"], b["size"]] for b in x["bids"]],
                                              [[a["price"], a["size"]] for a in x["asks"]], srv))
                        elif et == "last_trade_price":
                            side = "B" if x["side"] == "BUY" else "S"
                            pc = int(round(float(x["price"]) * 100))
                            if x["asset_id"] == token_down:
                                side = "S" if side == "B" else "B"; pc = 100 - pc
                            elif x["asset_id"] != token_up:
                                continue
                            handler.feed(("T", t, side, pc, float(x.get("size") or 0), x.get("transaction_hash", ""), srv))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"market feed error: {e!r}"); await asyncio.sleep(0.5)
