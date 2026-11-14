"""Tick recorder for BTC up/down 5m markets: Polymarket L2 book + trades, Binance and Coinbase spot.

Writes compact gzip CSV, one file per market window plus hourly spot files:

  OUT/pm/btc-updown-5m-<start>.csv.gz
     B,recv_ms,srv_ms,asset_idx,bids_json,asks_json        full book snapshot
     P,recv_ms,srv_ms,asset_idx,side,price,size,bb,ba      level update (size = new total at price)
     T,recv_ms,srv_ms,asset_idx,side,price,size,tx          trade (side = taker side)
     asset_idx: 0 = Up token, 1 = Down token
  OUT/spot/binance-<hour>.csv.gz    recv_ms,bid,ask         (only when the top of book changes)
  OUT/spot/coinbase-<hour>.csv.gz   recv_ms,price,bid,ask

usage: recorder.py OUT_DIR [asset=btc] [tf=5m]
"""
import asyncio
import gzip
import json
import os
import sys
import time
import urllib.request

import websockets

PM_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA = "https://gamma-api.polymarket.com/markets?slug="
BINANCE_WS = "wss://data-stream.binance.vision/ws/btcusdt@bookTicker"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
DUR = {"5m": 300, "15m": 900}


def now_ms():
    return int(time.time() * 1000)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def fetch_market(slug):
    req = urllib.request.Request(GAMMA + slug, headers={"User-Agent": "research-client/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)
    if not d:
        return None
    return json.loads(d[0]["clobTokenIds"])


async def record_market(out, asset, tf, start):
    dur = DUR[tf]
    slug = f"{asset}-updown-{tf}-{start}"
    toks = None
    for _ in range(10):
        try:
            toks = await asyncio.to_thread(fetch_market, slug)
            if toks:
                break
        except Exception as e:
            log(slug, "gamma error", repr(e)[:80])
        await asyncio.sleep(3)
    if not toks:
        log(slug, "no market found"); return
    idx = {toks[0]: 0, toks[1]: 1}
    path = f"{out}/pm/{slug}.csv.gz"
    n = 0
    stop_at = start + dur + 20
    with gzip.open(path, "at", compresslevel=5) as f:
        while time.time() < stop_at:
            try:
                async with websockets.connect(PM_WS, open_timeout=10, ping_interval=None, max_size=2 ** 24) as ws:
                    await ws.send(json.dumps({"assets_ids": toks, "type": "market"}))
                    last_ping = time.time()
                    while time.time() < stop_at:
                        if time.time() - last_ping > 9:
                            await ws.send("PING"); last_ping = time.time(); f.flush()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        if raw == "PONG":
                            continue
                        rcv = now_ms()
                        d = json.loads(raw)
                        for x in (d if isinstance(d, list) else [d]):
                            et = x.get("event_type")
                            if et == "price_change":
                                ts = x.get("timestamp", "")
                                for c in x["price_changes"]:
                                    i = idx.get(c["asset_id"])
                                    if i is not None:
                                        f.write(f"P,{rcv},{ts},{i},{c['side'][0]},{c['price']},{c['size']},{c.get('best_bid','')},{c.get('best_ask','')}\n")
                                        n += 1
                            elif et == "book":
                                i = idx.get(x["asset_id"])
                                if i is not None:
                                    bids = json.dumps([[b["price"], b["size"]] for b in x["bids"]], separators=(",", ":"))
                                    asks = json.dumps([[a["price"], a["size"]] for a in x["asks"]], separators=(",", ":"))
                                    f.write(f"B,{rcv},{x.get('timestamp','')},{i},\"{bids}\",\"{asks}\"\n"); n += 1
                            elif et == "last_trade_price":
                                i = idx.get(x["asset_id"])
                                if i is not None:
                                    f.write(f"T,{rcv},{x.get('timestamp','')},{i},{x['side'][0]},{x['price']},{x.get('size','')},{x.get('transaction_hash','')}\n")
                                    n += 1
            except Exception as e:
                log(slug, "ws error", repr(e)[:100]); await asyncio.sleep(1)
    log(slug, "done, rows", n)


async def market_scheduler(out, asset, tf):
    dur = DUR[tf]
    started = set()
    while True:
        now = time.time()
        cur = int(now) // dur * dur
        for start in (cur, cur + dur):
            if start not in started and now >= start - 30 and now < start + dur - 5:
                started.add(start)
                asyncio.create_task(record_market(out, asset, tf, start))
        await asyncio.sleep(1)


class HourlyGz:
    def __init__(self, out, name):
        self.out, self.name, self.hour, self.f = out, name, None, None
        self.flushed = time.time()

    def write(self, line):
        h = time.strftime("%Y%m%d-%H", time.gmtime())
        if h != self.hour:
            if self.f:
                self.f.close()
            self.hour = h
            self.f = gzip.open(f"{self.out}/spot/{self.name}-{h}.csv.gz", "at", compresslevel=5)
        self.f.write(line)
        if time.time() - self.flushed > 10:   # bound the data lost if the process is killed
            self.f.flush(); self.flushed = time.time()


async def record_binance(out):
    w = HourlyGz(out, "binance"); last = None
    while True:
        try:
            async with websockets.connect(BINANCE_WS, open_timeout=10) as ws:
                log("binance connected")
                async for raw in ws:
                    d = json.loads(raw)
                    k = (d["b"], d["a"])
                    if k != last:
                        last = k
                        w.write(f"{now_ms()},{float(d['b']):.2f},{float(d['a']):.2f}\n")
        except Exception as e:
            log("binance error", repr(e)[:100]); await asyncio.sleep(2)


async def record_coinbase(out):
    w = HourlyGz(out, "coinbase")
    while True:
        try:
            async with websockets.connect(COINBASE_WS, open_timeout=10) as ws:
                await ws.send(json.dumps({"type": "subscribe", "product_ids": ["BTC-USD"], "channels": ["ticker"]}))
                log("coinbase connected")
                async for raw in ws:
                    d = json.loads(raw)
                    if d.get("type") == "ticker":
                        w.write(f"{now_ms()},{d['price']},{d.get('best_bid','')},{d.get('best_ask','')}\n")
        except Exception as e:
            log("coinbase error", repr(e)[:100]); await asyncio.sleep(2)


async def main():
    out = sys.argv[1]
    asset = sys.argv[2] if len(sys.argv) > 2 else "btc"
    tf = sys.argv[3] if len(sys.argv) > 3 else "5m"
    os.makedirs(out + "/pm", exist_ok=True); os.makedirs(out + "/spot", exist_ok=True)
    await asyncio.gather(market_scheduler(out, asset, tf), record_binance(out), record_coinbase(out))


if __name__ == "__main__":
    asyncio.run(main())
