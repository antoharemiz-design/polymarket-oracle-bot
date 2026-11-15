"""H4 analysis (research/PREREG_H4.md): pre-game taker buys in sports markets by price bucket.

usage: h4_analyze.py DIR [split_iso=2026-10-26T00:00:00Z] [buffer_s=300] [sample_mod=4]
Only the outcome-independent subsample int(conditionId[-6:], 16) % sample_mod == 0 is used.
"""
import calendar
import collections
import json
import math
import sys
import time

d = sys.argv[1]
split = calendar.timegm(time.strptime(sys.argv[2] if len(sys.argv) > 2 else "2026-10-26T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
BUF = int(sys.argv[3]) if len(sys.argv) > 3 else 300
MOD = int(sys.argv[4]) if len(sys.argv) > 4 else 4
markets = json.load(open(f"{d}/markets.json"))
EDGES = [0.01, 0.05, 0.15, 0.30, 0.45, 0.55, 0.70, 0.85, 0.95, 0.99]
LAB = [f"{a:.2f}-{b:.2f}" for a, b in zip(EDGES, EDGES[1:])]
SC = {"base": (None, 0.0), "fee 0.07": (0.07, 0.0), "slip +1c": (None, 0.01)}
# part -> scenario -> bucket -> event -> [pnl $, shares, cost $]
acc = {p: {s: collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0, 0.0])) for s in SC} for p in ("sel", "hold")}
events = {"sel": set(), "hold": set()}
types = collections.Counter()
for line in open(f"{d}/fills.csv"):
    c, ts, oi, side, px, sz = line.rstrip("\n").split(",")
    m = markets.get(c)
    if m is None or int(c[-6:], 16) % MOD:
        continue
    ts, oi, px, sz = int(ts), int(oi), float(px), float(sz)
    if ts > m["start"] - BUF:
        continue
    lo, lp = (oi, px) if side == "B" else (1 - oi, 1 - px)      # outcome the taker ends long, and its price
    if not EDGES[0] <= lp < EDGES[-1]:
        continue
    b = next(i for i in range(len(EDGES) - 1) if EDGES[i] <= lp < EDGES[i + 1])
    part = "hold" if m["start"] >= split else "sel"
    events[part].add(m["event"]); types[m["type"]] += sz * lp
    payoff = 1.0 if lo == m["winner"] else 0.0
    for s, (rate, slip) in SC.items():
        r = m["rate"] if rate is None else rate
        p = min(lp + slip, 0.999)
        a = acc[part][s][b][m["event"]]
        a[0] += sz * (payoff - p - r * p * (1 - p)); a[1] += sz; a[2] += sz * p


def row(part, s, b):
    dct = acc[part][s][b]
    sh = sum(v[1] for v in dct.values()); cost = sum(v[2] for v in dct.values())
    if sh <= 0:
        return None
    ev = list(events[part])
    pm = [dct[e][0] if e in dct else 0.0 for e in ev]
    mean = sum(pm) / len(pm); sd = math.sqrt(sum((x - mean) ** 2 for x in pm) / len(pm))
    return sh, cost, sum(pm), 100 * sum(pm) / cost, (mean / sd * math.sqrt(len(pm)) if sd else 0.0), len(dct)


print(f"markets {len(markets)}; events selection {len(events['sel'])}, hold-out {len(events['hold'])}; "
      f"taker buys at least {BUF} s before game start")
print("notional by market type:", {k: round(v) for k, v in types.most_common(6)})
for part in ("sel", "hold"):
    print(f"\n{'selection' if part == 'sel' else 'hold-out'}: PnL to resolution after fee, % of cost (t clustered by event)")
    print(f"{'bucket':>11}{'shares':>12}{'cost $':>12}{'pnl $':>11}{'ret %':>8}{'t':>7}{'events':>8}" +
          "".join(f"{s + ' ret% (t)':>20}" for s in list(SC)[1:]))
    for b in range(len(LAB)):
        r = row(part, "base", b)
        if not r:
            continue
        extra = ""
        for s in list(SC)[1:]:
            x = row(part, s, b)
            extra += f"{x[3]:12.2f} ({x[4]:5.2f})"
        print(f"{LAB[b]:>11}{r[0]:12.0f}{r[1]:12.0f}{r[2]:11.0f}{r[3]:8.2f}{r[4]:7.2f}{r[5]:8d}{extra}")
