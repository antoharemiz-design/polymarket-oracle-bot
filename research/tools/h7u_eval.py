"""H7U (research/PREREG_H7U.md): buying the favorite after the scheduled endDate, before UMA resolution.

usage: h7u_eval.py MARKETS_WITH_ENDDATE_DIR SPLIT_EPOCH|none [sample_mod]
"""
import collections
import json
import math
import sys

d = sys.argv[1]
split = None if sys.argv[2] == "none" else int(sys.argv[2])
MOD = int(sys.argv[3]) if len(sys.argv) > 3 else 1
markets = json.load(open(f"{d}/markets.json"))
B = [(0.85, 0.95, 0.01), (0.95, 0.99, 0.01), (0.99, 0.999, 0.001)]
SC = ("base", "fee 0.07", "slip")
acc = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0, 0.0]))   # (part,b,sc) -> event -> pnl,cost,cost*days
events = collections.defaultdict(set)
for line in open(f"{d}/fills.csv"):
    c, ts, oi, side, px, sz = line.rstrip("\n").split(",")
    m = markets.get(c)
    if m is None or (MOD > 1 and int(c[-6:], 16) % MOD):
        continue
    ts, oi, px, sz = int(ts), int(oi), float(px), float(sz)
    if not m["close"] <= ts < m["closed_time"]:
        continue
    part = "all" if split is None else ("hold" if m["close"] >= split else "sel")
    events[part].add(m["event"])
    lo, lp = (oi, px) if side == "B" else (1 - oi, 1 - px)
    b = next((i for i, (a, z, _) in enumerate(B) if a <= lp < z), None)
    if b is None:
        continue
    pay = 1.0 if lo == m["winner"] else 0.0
    days = (m["closed_time"] - ts) / 86400
    for s in SC:
        r = 0.07 if s == "fee 0.07" else m["rate"]
        p = min(lp + (B[b][2] if s == "slip" else 0.0), 0.9999)
        a = acc[(part, b, s)][m["event"]]
        a[0] += sz * (pay - p - r * p * (1 - p)); a[1] += sz * p; a[2] += sz * p * days
for part in sorted(events):
    ev = list(events[part])
    print(f"{part}: events with post-end trading {len(ev)}")
    for b, (lo_, hi_, sl) in enumerate(B):
        line = f"   {lo_:.2f}-{hi_:.3f}"
        for s in SC:
            dct = acc[(part, b, s)]
            cost = sum(v[1] for v in dct.values())
            if not cost:
                continue
            pm = [dct[e][0] if e in dct else 0.0 for e in ev]
            mean = sum(pm) / len(pm); sd = math.sqrt(sum((x - mean) ** 2 for x in pm) / len(pm))
            t = mean / sd * math.sqrt(len(pm)) if sd else 0.0
            cd = sum(v[2] for v in dct.values())
            losers = sum(1 for v in dct.values() if v[0] < 0)
            line += (f" | {s}: ${sum(pm):9.0f} on ${cost:9.0f} = {100 * sum(pm) / cost:6.2f}% t {t:5.2f}"
                     + (f" ev {len(dct)} losing ev {losers} hold {cd / cost:.2f} d" if s == "base" else ""))
        print(line)
