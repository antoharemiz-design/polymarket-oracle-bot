"""H7R: fetch taker fills for markets outside the H7 subsample, from events with no subsample market.

usage: h7r_fetch.py H7_DIR OUT_DIR
"""
import json
import os
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from h4_fetch_sports import get  # noqa: E402

TRADES = "https://data-api.polymarket.com/v2/trades?"
src, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
markets = json.load(open(f"{src}/markets.json"))
sub_events = {m["event"] for c, m in markets.items() if int(c[-6:], 16) % 4 == 0}
keep = {c: m for c, m in markets.items() if int(c[-6:], 16) % 4 and m["event"] not in sub_events}
json.dump(keep, open(f"{out}/markets.json", "w"))
print(f"markets {len(markets)}, subsample events {len(sub_events)}, independent markets {len(keep)} "
      f"in {len({m['event'] for m in keep.values()})} events", flush=True)
fpath = f"{out}/fills.csv"
done = {line.split(",", 1)[0] for line in open(fpath)} if os.path.exists(fpath) else set()
todo = [c for c in keep if c not in done]


def chunk_rows(chunk):
    rows, cur = [], None
    while True:
        q = {"condition": ",".join(chunk), "taker_only": "true", "limit": 1000}
        if cur:
            q["cursor"] = cur
        d = get(TRADES + urllib.parse.urlencode(q))
        data = d.get("data") or []
        rows += [f"{r['condition_id']},{r['timestamp']},{r['outcome_index']},{r['side'][0]},{r['price']},{r['size']}\n" for r in data]
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
