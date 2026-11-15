"""Attach the scheduled endDate (known before trading) to H7 / H7R market files, to remove the look-ahead
of windows measured from closedTime (known only after resolution).

usage: h7_enddates.py MARKETS_IN.json MARKETS_OUT.json
Output keeps every field; "close" becomes the scheduled endDate, "closed_time" keeps the actual close.
Markets whose endDate is missing are dropped.
"""
import calendar
import json
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from h4_fetch_sports import get  # noqa: E402

src, dst = sys.argv[1], sys.argv[2]
markets = json.load(open(src))
cids = list(markets)


def meta(chunk):
    q = urllib.parse.urlencode([("condition_ids", c) for c in chunk] + [("limit", len(chunk)), ("closed", "true")])
    return [(m["conditionId"], m.get("endDate")) for m in get("https://gamma-api.polymarket.com/markets?" + q)]


with ThreadPoolExecutor(6) as ex:
    ends = dict(x for part in ex.map(meta, [cids[i:i + 40] for i in range(0, len(cids), 40)]) for x in part)
out, early = {}, 0
for c, m in markets.items():
    e = ends.get(c)
    if not e:
        continue
    ts = calendar.timegm(time.strptime(e[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"))
    mm = dict(m); mm["closed_time"] = m["close"]; mm["close"] = ts
    early += ts - m["close"] > 86400
    out[c] = mm
json.dump(out, open(dst, "w"))
print(f"markets {len(markets)}, with endDate {len(out)}; resolved more than 1 day before the scheduled end: {early}")
