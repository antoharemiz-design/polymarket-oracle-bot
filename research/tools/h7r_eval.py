"""H7R evaluation (research/PREREG_H7R.md): cell W3 (1-24 h before close) x price 0.85-0.95 on independent events.

usage: h7r_eval.py DIR [lo=0.85] [hi=0.95]
"""
import collections
import json
import math
import sys

d = sys.argv[1]
LO = float(sys.argv[2]) if len(sys.argv) > 2 else 0.85
HI = float(sys.argv[3]) if len(sys.argv) > 3 else 0.95
markets = json.load(open(f"{d}/markets.json"))
SC = {"base": (None, 0.0), "fee 0.07": (0.07, 0.0), "slip +1c": (None, 0.01)}
ev = {s: collections.defaultdict(lambda: [0.0, 0.0, 0.0]) for s in SC}     # pnl, cost, cost*days
all_events = {m["event"] for m in markets.values()}
title, elections = {}, set()
for line in open(f"{d}/fills.csv"):
    c, ts, oi, side, px, sz = line.rstrip("\n").split(",")
    m = markets.get(c)
    if m is None:
        continue
    ts, oi, px, sz = int(ts), int(oi), float(px), float(sz)
    left = m["close"] - ts
    if not 3600 <= left < 86400:
        continue
    lo, lp = (oi, px) if side == "B" else (1 - oi, 1 - px)
    if not LO <= lp < HI:
        continue
    title[m["event"]] = m["title"]
    if "elections" in m["tags"]:
        elections.add(m["event"])
    pay = 1.0 if lo == m["winner"] else 0.0
    for s, (rate, slip) in SC.items():
        r = m["rate"] if rate is None else rate
        p = min(lp + slip, 0.999)
        a = ev[s][m["event"]]
        a[0] += sz * (pay - p - r * p * (1 - p)); a[1] += sz * p; a[2] += sz * p * left / 86400


def stat(dct, events):
    pm = [dct[e][0] if e in dct else 0.0 for e in events]
    mean = sum(pm) / len(pm); sd = math.sqrt(sum((x - mean) ** 2 for x in pm) / len(pm))
    cost = sum(dct[e][1] for e in events if e in dct); cd = sum(dct[e][2] for e in events if e in dct)
    return sum(pm), cost, 100 * sum(pm) / cost, mean / sd * math.sqrt(len(pm)), sum(1 for e in events if e in dct), cd / cost


base = ev["base"]
top5 = {e for e, _ in sorted(base.items(), key=lambda x: -x[1][0])[:5]}
print(f"independent markets {len(markets)} in {len(all_events)} events; cell W3 x {LO:.2f}-{HI:.2f}")
for name, evs in (("all", all_events), ("without elections", all_events - elections), ("without top-5 events", all_events - top5)):
    for s in SC:
        pnl, cost, ret, t, n, hold = stat(ev[s], list(evs))
        print(f"  {name:>21} {s:>9}: pnl ${pnl:10.0f} on ${cost:10.0f} = {ret:6.2f}%  t {t:5.2f}  events traded {n:4d}  hold {hold:.2f} d")
print("  top-5 events:", [(round(base[e][0]), title[e][:40]) for e in top5])
losers = sorted(base.items(), key=lambda x: x[1][0])[:5]
print("  worst 5 events:", [(round(v[0]), title[e][:40]) for e, v in losers])
