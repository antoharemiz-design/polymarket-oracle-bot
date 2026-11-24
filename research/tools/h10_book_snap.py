"""H10M: repeated book snapshots of selected rewarded markets, keeping every level inside the max spread.

usage: h10_book_snap.py CONDS.json OUT_DIR COUNT INTERVAL_S
CONDS.json: list of {"cond", "token"}.
"""
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from h2_negrisk_scan import get, post_books  # noqa: E402


def rewards_config(conds):
    rows, cur = {}, None
    while True:
        d = get("https://clob.polymarket.com/rewards/markets/current?" + urllib.parse.urlencode({"next_cursor": cur} if cur else {}))
        for r in d.get("data") or []:
            if r["condition_id"] in conds:
                rows[r["condition_id"]] = r
        cur = d.get("next_cursor")
        if not cur or cur == "LTE=" or not d.get("data"):
            return rows


def main():
    sel = json.load(open(sys.argv[1]))
    out, count, every = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    os.makedirs(out, exist_ok=True)
    conds = {x["cond"]: x["token"] for x in sel}
    for k in range(count):
        t0 = time.time()
        cfg = rewards_config(set(conds))
        books = {}
        toks = list(conds.values())
        for i in range(0, len(toks), 100):
            for b in post_books(toks[i:i + 100]):
                books[b["asset_id"]] = b
        snap = {}
        for c, tok in conds.items():
            b, r = books.get(tok), cfg.get(c)
            if not b or not r or not b["bids"] or not b["asks"]:
                snap[c] = None
                continue
            bids = sorted(((float(x["price"]), float(x["size"])) for x in b["bids"]), reverse=True)
            asks = sorted((float(x["price"]), float(x["size"])) for x in b["asks"])
            mid = (bids[0][0] + asks[0][0]) / 2
            v = float(r["rewards_max_spread"])
            snap[c] = {"rate": r["total_daily_rate"], "v": v, "min": float(r["rewards_min_size"]), "mid": mid,
                       "tick": float(b.get("tick_size") or 0.01),
                       "bids": [l for l in bids if (mid - l[0]) * 100 < v], "asks": [l for l in asks if (l[0] - mid) * 100 < v],
                       "best_bid": bids[0], "best_ask": asks[0]}
        name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        json.dump({"time": name, "markets": snap}, open(f"{out}/book_{name}.json", "w"))
        print(f"{name}: {sum(1 for v in snap.values() if v)} markets with books", flush=True)
        if k < count - 1:
            time.sleep(max(0, every - (time.time() - t0)))


if __name__ == "__main__":
    main()
