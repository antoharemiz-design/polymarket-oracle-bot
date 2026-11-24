"""Fetch resolution payouts for condition ids into a JSON cache {condition_id: winning_outcome_index}.

usage: resolutions.py CACHE.json ACTIVITY.jsonl [ACTIVITY.jsonl ...]
Reads every condition id seen in TRADE rows, fetches the ones missing from the cache
(20 per request, /v2/resolutions). Unresolved or split payouts are stored as -1.
"""
import json
import os
import sys
import time
import urllib.request

URL = "https://data-api.polymarket.com/v2/resolutions?condition="


def get(url):
    for i in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-client/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception:
            time.sleep(2 ** i)
    raise RuntimeError("failed " + url)


def main():
    cache_path, files = sys.argv[1], sys.argv[2:]
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    want = set()
    for p in files:
        for line in open(p):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type", "TRADE") == "TRADE" and r.get("condition_id"):
                want.add(r["condition_id"])
    todo = sorted(want - set(cache))
    print(f"{len(want)} conditions, {len(todo)} to fetch")
    for i in range(0, len(todo), 20):
        chunk = todo[i:i + 20]
        rows = get(URL + ",".join(chunk)).get("data") or []
        for r in rows:
            pay = r.get("payouts") or []
            win = [j for j, v in enumerate(pay) if v and v == max(pay)]
            cache[r["condition_id"]] = win[0] if r.get("status") == "resolved" and len(win) == 1 else -1
        for c in chunk:
            cache.setdefault(c, -1)
        if (i // 20) % 50 == 49:
            json.dump(cache, open(cache_path, "w"))
    json.dump(cache, open(cache_path, "w"))
    print("resolved:", sum(1 for v in cache.values() if v >= 0), "unresolved:", sum(1 for v in cache.values() if v < 0))


if __name__ == "__main__":
    main()
