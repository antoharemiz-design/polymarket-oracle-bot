"""Fetch closed non-sports, non-crypto-up/down binary markets and their taker fills for H7.

usage: h7_fetch_general.py OUT_DIR CLOSED_MIN CLOSED_MAX [SAMPLE_MOD=4]
Writes OUT_DIR/markets.json and OUT_DIR/fills.csv (cond,ts,outcome_index,side,price,size).
"""
import calendar
import json
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from h4_fetch_sports import get  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com/events/keyset?"
TRADES = "https://data-api.polymarket.com/v2/trades?"
SKIP_SLUG = re.compile(r"updown|up-or-down|above-on|price-on|-above-|what-price-will", re.I)


def ts_of(s):
    s = s.replace("T", " ")[:19]
    return calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M:%S"))


def main():
    out, cmin, cmax = sys.argv[1], ts_of(sys.argv[2]), ts_of(sys.argv[3])
    mod = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    os.makedirs(out, exist_ok=True)
    mpath = f"{out}/markets.json"
    if os.path.exists(mpath):
        markets = json.load(open(mpath))
    else:
        markets, cur, pages = {}, None, 0
        while True:
            q = {"closed": "true", "end_date_min": "2026-09-15T00:00:00Z", "volume_min": 20000, "limit": 100}
            if cur:
                q["after_cursor"] = cur
            d = get(GAMMA + urllib.parse.urlencode(q))
            pages += 1
            for e in d.get("events") or []:
                tags = {t.get("slug") for t in (e.get("tags") or [])}
                if "sports" in tags or "esports" in tags or SKIP_SLUG.search(e.get("slug") or ""):
                    continue
                for m in e.get("markets", []):
                    try:
                        prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                        outs = json.loads(m.get("outcomes") or "[]")
                    except (ValueError, TypeError):
                        continue
                    if len(outs) != 2 or sorted(prices) != [0.0, 1.0] or not m.get("closedTime"):
                        continue
                    if (m.get("volumeNum") or 0) < 5000 or m.get("gameStartTime"):
                        continue
                    ct = ts_of(m["closedTime"])
                    if not cmin <= ct < cmax:
                        continue
                    rate = (m.get("feeSchedule") or {}).get("rate", 0.0) if m.get("feesEnabled") else 0.0
                    markets[m["conditionId"]] = {"event": e["id"], "title": e.get("title"), "q": m.get("question"),
                                                 "close": ct, "winner": prices.index(1.0), "rate": rate,
                                                 "neg_risk": bool(e.get("negRisk")),
                                                 "tags": sorted(tags)[:8]}
            cur = d.get("next_cursor")
            if pages % 20 == 0:
                print(f"  gamma pages {pages}, markets {len(markets)}", flush=True)
            if not cur or not d.get("events"):
                break
        json.dump(markets, open(mpath, "w"))
    print(f"markets: {len(markets)}", flush=True)
    fpath = f"{out}/fills.csv"
    done = set()
    if os.path.exists(fpath):
        done = {line.split(",", 1)[0] for line in open(fpath)}
    todo = [c for c in markets if c not in done and int(c[-6:], 16) % mod == 0]

    def chunk_rows(chunk):
        rows, cur = [], None
        while True:
            q = {"condition": ",".join(chunk), "taker_only": "true", "limit": 1000}
            if cur:
                q["cursor"] = cur
            d = get(TRADES + urllib.parse.urlencode(q))
            data = d.get("data") or []
            rows += [f"{r['condition_id']},{r['timestamp']},{r['outcome_index']},{r['side'][0]},{r['price']},{r['size']}\n"
                     for r in data]
            cur = (d.get("pagination") or {}).get("next_cursor")
            if not cur or not data:
                return rows

    chunks = [todo[i:i + 10] for i in range(0, len(todo), 10)]
    with open(fpath, "a") as f, ThreadPoolExecutor(6) as ex:
        for n, rows in enumerate(ex.map(chunk_rows, chunks)):
            f.writelines(rows); f.flush()
            if n % 25 == 0:
                print(f"  {min((n + 1) * 10, len(todo))}/{len(todo)} markets fetched", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
