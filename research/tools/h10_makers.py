"""H10M: how many distinct maker addresses actually got filled in each market over the last N days.

Maker rows = rows of /v2/trades?taker_only=false that are not the taker's own row (matched by tx hash + wallet).
usage: h10_makers.py CONDS.json OUT.json [days=7]
"""
import collections
import json
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from h2_negrisk_scan import get  # noqa: E402

sel = json.load(open(sys.argv[1]))
days = float(sys.argv[3]) if len(sys.argv) > 3 else 7
since = time.time() - days * 86400


def rows(cond, taker_only):
    out, cur = [], None
    while True:
        q = {"condition": cond, "taker_only": taker_only, "limit": 1000}
        if cur:
            q["cursor"] = cur
        d = get("https://data-api.polymarket.com/v2/trades?" + urllib.parse.urlencode(q))
        data = d.get("data") or []
        out += [r for r in data if r["timestamp"] >= since]
        cur = (d.get("pagination") or {}).get("next_cursor")
        if not cur or not data or min(r["timestamp"] for r in data) < since:
            return out


def one(x):
    taker = {(r["transaction_hash"], r["proxy_wallet"]) for r in rows(x["cond"], "true")}
    vol = collections.Counter()
    for r in rows(x["cond"], "false"):
        if (r["transaction_hash"], r["proxy_wallet"]) not in taker:
            vol[r["proxy_wallet"]] += float(r["size"])
    tot = sum(vol.values())
    top3 = sum(v for _, v in vol.most_common(3))
    return x["cond"], {"makers": len(vol), "maker_volume": tot, "top3_share": top3 / tot if tot else None}


with ThreadPoolExecutor(6) as ex:
    res = dict(ex.map(one, sel))
json.dump(res, open(sys.argv[2], "w"))
mk = sorted(v["makers"] for v in res.values())
sh = sorted(v["top3_share"] for v in res.values() if v["top3_share"] is not None)
print(f"markets {len(res)}; distinct maker addresses filled in {days:.0f} days: median {mk[len(mk) // 2]}, "
      f"p25 {mk[len(mk) // 4]}, p75 {mk[3 * len(mk) // 4]}, zero in {sum(1 for m in mk if m == 0)} markets; "
      f"top-3 makers' share of maker volume: median {sh[len(sh) // 2]:.2f}")
