"""Paginated fetch of Polymarket data-api v2 feeds into compact JSONL."""
import json, sys, time, urllib.request, urllib.parse, urllib.error
B = "https://data-api.polymarket.com/v2"
W = None  # wallet, set from argv
KEEP = ("timestamp","type","side","size","usdc_size","price","slug","outcome","outcome_index","condition_id","transaction_hash","token_id")
def get(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-client/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 ** i); continue
            raise
        except Exception:
            time.sleep(2 ** i)
    raise RuntimeError("failed " + url)
def fetch(route, params, out, max_pages=100000):
    assert W, "wallet not set"
    n = 0; cursor = None; pages = 0
    with open(out, "w") as f:
        while pages < max_pages:
            q = dict(params); q["user"] = W
            if cursor: q["cursor"] = cursor; q.pop("limit", None)
            d = get(f"{B}/{route}?" + urllib.parse.urlencode(q))
            rows = d.get("data") or []
            lo = int(params.get("start", 0)); hi = int(params.get("end", 1 << 62))
            oob = [r for r in rows if not lo <= r["timestamp"] <= hi]
            rows = [r for r in rows if lo <= r["timestamp"] <= hi]
            for r in rows:
                f.write(json.dumps({k: r[k] for k in KEEP if k in r}) + "\n")
            n += len(rows); pages += 1
            cursor = (d.get("pagination") or {}).get("next_cursor")
            if not cursor or not rows or oob: break
    return n
if __name__ == "__main__":  # usage: fetch.py WALLET ROUTE OUT key=value ...
    W, route, out = sys.argv[1], sys.argv[2], sys.argv[3]
    params = dict(a.split("=", 1) for a in sys.argv[4:])
    t = time.time(); n = fetch(route, params, out)
    print(f"{route} {params} -> {n} rows in {time.time()-t:.1f}s")
