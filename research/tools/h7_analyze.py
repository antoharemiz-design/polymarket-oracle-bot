"""H7 analysis (research/PREREG_H7.md): taker buys by time to close x price bucket, non-sports markets.

usage: h7_analyze.py DIR [split_iso=2026-10-26T00:00:00Z] [sample_mod=4]
"""
import calendar
import collections
import json
import math
import sys
import time

d = sys.argv[1]
split = calendar.timegm(time.strptime(sys.argv[2] if len(sys.argv) > 2 else "2026-10-26T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
MOD = int(sys.argv[3]) if len(sys.argv) > 3 else 4
markets = json.load(open(f"{d}/markets.json"))
EDGES = [0.01, 0.05, 0.15, 0.30, 0.45, 0.55, 0.70, 0.85, 0.95, 0.99]
LAB = [f"{a:.2f}-{b:.2f}" for a, b in zip(EDGES, EDGES[1:])]
WIN = [("W1 >7d", 7 * 86400, 10 ** 9), ("W2 1-7d", 86400, 7 * 86400), ("W3 1-24h", 3600, 86400)]
SC = {"base": (None, 0.0), "fee 0.07": (0.07, 0.0), "slip +1c": (None, 0.01)}
# part -> scenario -> (window, bucket) -> event -> [pnl $, shares, cost $, cost*days]
acc = {p: {s: collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])) for s in SC}
       for p in ("sel", "hold")}
events = {"sel": set(), "hold": set()}
for line in open(f"{d}/fills.csv"):
    c, ts, oi, side, px, sz = line.rstrip("\n").split(",")
    m = markets.get(c)
    if m is None or int(c[-6:], 16) % MOD:
        continue
    ts, oi, px, sz = int(ts), int(oi), float(px), float(sz)
    left = m["close"] - ts
    w = next((i for i, (_, lo, hi) in enumerate(WIN) if lo <= left < hi), None)
    if w is None:
        continue
    lo_, lp = (oi, px) if side == "B" else (1 - oi, 1 - px)
    if not EDGES[0] <= lp < EDGES[-1]:
        continue
    b = next(i for i in range(len(EDGES) - 1) if EDGES[i] <= lp < EDGES[i + 1])
    part = "hold" if m["close"] >= split else "sel"
    events[part].add(m["event"])
    payoff = 1.0 if lo_ == m["winner"] else 0.0
    for s, (rate, slip) in SC.items():
        r = m["rate"] if rate is None else rate
        p = min(lp + slip, 0.999)
        a = acc[part][s][(w, b)][m["event"]]
        a[0] += sz * (payoff - p - r * p * (1 - p)); a[1] += sz; a[2] += sz * p; a[3] += sz * p * left / 86400


def row(part, s, key):
    dct = acc[part][s][key]
    sh = sum(v[1] for v in dct.values()); cost = sum(v[2] for v in dct.values())
    if sh <= 0:
        return None
    ev = list(events[part])
    pm = [dct[e][0] if e in dct else 0.0 for e in ev]
    mean = sum(pm) / len(pm); sd = math.sqrt(sum((x - mean) ** 2 for x in pm) / len(pm))
    cd = sum(v[3] for v in dct.values())
    return dict(sh=sh, cost=cost, pnl=sum(pm), ret=100 * sum(pm) / cost, t=(mean / sd * math.sqrt(len(pm)) if sd else 0.0),
                ev=len(dct), days=cd / cost, per_day=100 * sum(pm) / cd if cd else 0.0)


nm = sum(1 for c in markets if int(c[-6:], 16) % MOD == 0)
print(f"markets in subsample {nm}; events selection {len(events['sel'])}, hold-out {len(events['hold'])}")
for part in ("sel", "hold"):
    print(f"\n{'selection' if part == 'sel' else 'hold-out'}: PnL to resolution after fee (t clustered by event)")
    print(f"{'window':>9}{'bucket':>11}{'shares':>11}{'cost $':>11}{'pnl $':>10}{'ret %':>8}{'t':>7}{'ev':>5}"
          f"{'hold d':>7}{'%/day':>7}" + "".join(f"{s + ' ret%(t)':>18}" for s in list(SC)[1:]))
    for w in range(len(WIN)):
        for b in range(len(LAB)):
            r = row(part, "base", (w, b))
            if not r:
                continue
            extra = ""
            for s in list(SC)[1:]:
                x = row(part, s, (w, b))
                extra += f"{x['ret']:10.2f} ({x['t']:5.2f})"
            print(f"{WIN[w][0]:>9}{LAB[b]:>11}{r['sh']:11.0f}{r['cost']:11.0f}{r['pnl']:10.0f}{r['ret']:8.2f}{r['t']:7.2f}"
                  f"{r['ev']:5d}{r['days']:7.2f}{r['per_day']:7.3f}{extra}")
