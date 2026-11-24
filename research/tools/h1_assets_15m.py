"""H1 on other assets: post-window trading in ETH / SOL / XRP 15m up/down markets.

usage: h1_assets_15m.py OUT_DIR START_EPOCH END_EPOCH asset [asset ...]
"""
import collections
import json
import os
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from h4_fetch_sports import get  # noqa: E402

out, t0, t1 = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
os.makedirs(out, exist_ok=True)


def one(args):
    asset, s = args
    d = get("https://gamma-api.polymarket.com/markets?" + urllib.parse.urlencode({"slug": f"{asset}-updown-15m-{s}", "closed": "true"}))
    if not d:
        return None
    m = d[0]
    pr = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    if sorted(pr) != [0.0, 1.0]:
        return None
    rows, cur = [], None
    while True:
        q = {"condition": m["conditionId"], "taker_only": "true", "limit": 1000}
        if cur:
            q["cursor"] = cur
        t = get("https://data-api.polymarket.com/v2/trades?" + urllib.parse.urlencode(q))
        data = t.get("data") or []
        rows += [(r["timestamp"], r["outcome_index"], r["side"][0], float(r["price"]), float(r["size"])) for r in data
                 if r["timestamp"] >= s + 900]
        cur = (t.get("pagination") or {}).get("next_cursor")
        if not cur or not data or min(r["timestamp"] for r in data) < s + 900:
            break
    return asset, s, pr.index(1.0), rows


starts = list(range(t0 - t0 % 900, t1, 900))
for asset in sys.argv[4:]:
    with ThreadPoolExecutor(8) as ex:
        res = [r for r in ex.map(one, [(asset, s) for s in starts]) if r]
    w_cheap = w_gap = l_q = l_val = 0.0
    mk_cheap = set()
    for _, s, win, rows in res:
        for ts, oi, side, px, sz in rows:
            lo, lp = (oi, px) if side == "B" else (1 - oi, 1 - px)
            if lo == win and lp <= 0.99:
                w_cheap += sz; w_gap += (1 - lp) * sz; mk_cheap.add(s)
            elif lo != win:
                l_q += sz; l_val += lp * sz
    json.dump([(s, w, len(r)) for _, s, w, r in res], open(f"{out}/{asset}_15m.json", "w"))
    print(f"{asset} 15m: markets {len(res)}; winner bought at <= 0.99 after the window: {w_cheap:,.0f} contracts in "
          f"{len(mk_cheap)} markets (gap ${w_gap:,.2f}); loser bought after the window: {l_q:,.0f} contracts, "
          f"maker side earned ${l_val:,.2f} in total", flush=True)
