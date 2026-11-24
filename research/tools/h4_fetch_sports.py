"""Fetch closed sports markets (with game start times) and their taker fills for H4.

usage: h4_fetch_sports.py OUT_DIR END_MIN END_MAX [SAMPLE_MOD=4]
Only markets with int(conditionId[-6:], 16) % SAMPLE_MOD == 0 are fetched (outcome-independent subsample).
Writes OUT_DIR/markets.json and OUT_DIR/fills.csv (cond,ts,outcome_index,side,price,size).
"""
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com/events/keyset?"
TRADES = "https://data-api.polymarket.com/v2/trades?"


def get(url):
    for i in range(7):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-client/1.0", "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
        except Exception:
            time.sleep(min(2 ** i, 30))
    raise RuntimeError(url)


def main():
    out, emin, emax = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out, exist_ok=True)
    mpath = f"{out}/markets.json"
    if os.path.exists(mpath):
        markets = json.load(open(mpath))
    else:
        markets, cur = {}, None
        while True:
            q = {"closed": "true", "tag_slug": "sports", "end_date_min": emin, "end_date_max": emax,
                 "volume_min": 20000, "limit": 100}
            if cur:
                q["after_cursor"] = cur
            d = get(GAMMA + urllib.parse.urlencode(q))
            for e in d.get("events") or []:
                for m in e.get("markets", []):
                    try:
                        prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                        outs = json.loads(m.get("outcomes") or "[]")
                    except (ValueError, TypeError):
                        continue
                    if len(outs) != 2 or sorted(prices) != [0.0, 1.0] or not m.get("gameStartTime"):
                        continue
                    if (m.get("volumeNum") or 0) < 5000:
                        continue
                    gs = time.mktime(time.strptime(m["gameStartTime"][:19], "%Y-%m-%d %H:%M:%S")) - time.timezone
                    rate = (m.get("feeSchedule") or {}).get("rate", 0.0) if m.get("feesEnabled") else 0.0
                    markets[m["conditionId"]] = {"event": e["id"], "title": e.get("title"), "q": m.get("question"),
                                                 "type": m.get("sportsMarketType"), "start": gs,
                                                 "winner": prices.index(1.0), "rate": rate,
                                                 "tags": [t.get("slug") for t in (e.get("tags") or [])][:6]}
            cur = d.get("next_cursor")
            if not cur or not d.get("events"):
                break
        json.dump(markets, open(mpath, "w"))
    print(f"markets: {len(markets)}", flush=True)
    fpath = f"{out}/fills.csv"
    done = set()
    if os.path.exists(fpath):
        done = {line.split(",", 1)[0] for line in open(fpath)}
    mod = int(sys.argv[4]) if len(sys.argv) > 4 else 4
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

    from concurrent.futures import ThreadPoolExecutor
    chunks = [todo[i:i + 20] for i in range(0, len(todo), 20)]
    # each chunk is written only when complete, so an interrupted run never leaves a partial market behind
    with open(fpath, "a") as f, ThreadPoolExecutor(6) as ex:
        for n, rows in enumerate(ex.map(chunk_rows, chunks)):
            f.writelines(rows); f.flush()
            if n % 25 == 0:
                print(f"  {min((n + 1) * 20, len(todo))}/{len(todo)} markets fetched", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
