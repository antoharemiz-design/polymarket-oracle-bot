"""Download the public trade tape (every fill, taker side) for BTC 5m markets.

Condition ids come from a wallet activity dump that traded those markets (any active wallet works).
Output CSV rows: start,ts,outcome_index,side,price,size,taker_wallet,tx
  start = window start (epoch s), ts = on-chain block time (epoch s), side = taker side.

Re-running with the same OUT.csv resumes: markets already present are skipped
(the newest 20 present are re-fetched, since the last batch may be incomplete).

usage: fetch_tape.py ACTIVITY.jsonl OUT.csv FROM_EPOCH TO_EPOCH
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request

B = "https://data-api.polymarket.com/v2/trades?"


def get(url):
    for i in range(7):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-client/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception:
            time.sleep(min(2 ** i, 30))
    raise RuntimeError("failed " + url)


def main():
    act, out, lo, hi = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    conds = {}
    for line in open(act):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = re.match(r"^btc-updown-5m-(\d+)$", r.get("slug") or "")
        if m and lo <= int(m.group(1)) < hi and r.get("condition_id"):
            conds[r["condition_id"]] = int(m.group(1))
    ids = sorted(conds, key=conds.get)
    import os
    if os.path.exists(out):
        have = sorted({int(line.split(",", 1)[0]) for line in open(out) if line[:1].isdigit()})
        keep = set(have[:-20])
        tmp = out + ".tmp"
        with open(out) as src, open(tmp, "w") as dst:
            for line in src:
                if line[:1].isdigit() and int(line.split(",", 1)[0]) in keep:
                    dst.write(line)
        os.replace(tmp, out)
        ids = [c for c in ids if conds[c] not in keep]
        print(f"resuming: {len(keep)} markets kept", flush=True)
    print(f"{len(ids)} markets to fetch", flush=True)
    n = 0
    with open(out, "a") as f:
        for i in range(0, len(ids), 20):
            params = {"condition": ",".join(ids[i:i + 20]), "limit": 1000}
            cursor = None
            while True:
                q = dict(params)
                if cursor:
                    q["cursor"] = cursor; q.pop("limit")
                d = get(B + urllib.parse.urlencode(q))
                rows = d.get("data") or []
                for r in rows:
                    f.write(f"{conds[r['condition_id']]},{r['timestamp']},{r['outcome_index']},{r['side'][0]},{r['price']},{r['size']},{r['proxy_wallet'][:12]},{r['transaction_hash'][:14]}\n")
                n += len(rows)
                cursor = (d.get("pagination") or {}).get("next_cursor")
                if not cursor or not rows:
                    break
            if (i // 20) % 10 == 0:
                print(f"{i + 20}/{len(ids)} markets, {n} rows", flush=True)
    print("done", n, flush=True)


if __name__ == "__main__":
    main()
