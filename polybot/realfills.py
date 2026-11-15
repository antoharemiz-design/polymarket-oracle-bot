"""Real maker fills for recorded markets, used as the reference fill model in backtests.

The live market feed reports one `last_trade_price` per transaction, which hides how far a
sweep went through the book. The Data API lists every maker fill of every transaction, so after
the fact we know exactly how much traded at each price. `real_events` replaces each recorded
trade event with ("R", recv_ms, [(side, price_c, size), ...], srv_ms) in Up terms, and
engine.PaperExecutor.on_real_fills runs FIFO against that volume (research/06, step 4).
"""
import collections
import json
import os
import time
import urllib.parse
import urllib.request

API = "https://data-api.polymarket.com/v2/trades?"
GAMMA = "https://gamma-api.polymarket.com/markets?slug="


def _get(url):
    for i in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polybot/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception:
            time.sleep(2 ** i)
    raise RuntimeError("failed " + url)


def condition_of(rec, start, slug_prefix="btc-updown-5m"):
    meta = f"{rec}/pm/{slug_prefix}-{start}.json"
    if os.path.exists(meta):
        return json.load(open(meta))["condition_id"]
    cond = _get(GAMMA + f"{slug_prefix}-{start}&closed=true")[0]["conditionId"]
    json.dump({"condition_id": cond}, open(meta, "w"))
    return cond


def trades_of(rec, start, slug_prefix="btc-updown-5m"):
    """{"false": all fills, "true": taker fills}; rows [tx, outcome_index, side, price, size]. Cached on disk."""
    path = f"{rec}/pm/{slug_prefix}-{start}.trades.json"
    if os.path.exists(path):
        return json.load(open(path))
    cond = condition_of(rec, start, slug_prefix)
    out = {}
    for to in ("false", "true"):
        rows, cur = [], None
        while True:
            q = {"condition": cond, "limit": 1000, "taker_only": to}
            if cur:
                q = {"condition": cond, "cursor": cur, "taker_only": to}
            d = _get(API + urllib.parse.urlencode(q))
            rows += [[r["transaction_hash"], r["outcome_index"], r["side"], r["price"], r["size"]] for r in d.get("data") or []]
            cur = (d.get("pagination") or {}).get("next_cursor")
            if not cur or not d.get("data"):
                break
        out[to] = rows
    json.dump(out, open(path, "w"))
    return out


def up_terms(oi, side, price):
    """A maker fill as (side code, Up price in cents): "B" = long Up at P, "S" = long Down at 100 - P."""
    x = round(100 * price)
    if oi == 0:
        return ("B", x) if side == "BUY" else ("S", x)
    return ("S", 100 - x) if side == "BUY" else ("B", 100 - x)


def maker_rows(tr):
    takers = collections.Counter((r[0], r[1], r[2], round(r[3], 6), round(r[4], 6)) for r in tr["true"])
    out = []
    for r in tr["false"]:
        k = (r[0], r[1], r[2], round(r[3], 6), round(r[4], 6))
        if takers[k] > 0:
            takers[k] -= 1
        else:
            out.append(r)
    return out


def real_events(events, rec, start):
    """Replace recorded trade events by the real maker fills of the same transactions."""
    by_tx = collections.defaultdict(list)
    for tx, oi, side, price, size in maker_rows(trades_of(rec, start)):
        by_tx[tx].append(up_terms(oi, side, price) + (size,))
    out = []
    for e in events:
        if e[0] == "T":
            if e[5] in by_tx:
                out.append(("R", e[1], by_tx[e[5]], e[6]))
        else:
            out.append(e)
    return out
