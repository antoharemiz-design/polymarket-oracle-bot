"""Does fading recent moves of the market price pay, held to resolution? Public tape, all fills.

For every fill, in Up-token terms: x = change of the Up price over the previous N seconds (from
trades up to one second before), y = payoff_up - price_up in cents (what a buyer of Up at that
price earns at resolution; a buyer of Down earns -y). If y falls as x rises, prices overshoot
and a maker who leans against the move (buys the side that just fell) earns the difference.
t-statistics are clustered by market.

usage: tape_fade_study.py TAPE.csv RES.json ACTIVITY.jsonl
"""
import bisect
import collections
import json
import math
import re
import sys

tape, res_p, act = sys.argv[1:4]
DELAY = 3
res = json.load(open(res_p)); win = {}
for line in open(act):
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    m = re.match(r"^btc-updown-5m-(\d+)$", r.get("slug") or "")
    if m and res.get(r.get("condition_id"), -1) >= 0:
        win[int(m.group(1))] = res[r["condition_id"]]
rows = collections.defaultdict(list)
for line in open(tape):
    p = line.split(",")
    if len(p) < 8:
        continue
    s = int(p[0])
    if s in win:
        oi, px = int(p[2]), float(p[4])
        rows[s].append((int(p[1]) - DELAY, 100 * (px if oi == 0 else 1 - px), float(p[5])))
NS = (10, 30, 60)
EDGES = [-1e9, -20, -8, -3, 3, 8, 20, 1e9]
LAB = ["<-20", "-20..-8", "-8..-3", "-3..+3", "+3..+8", "+8..+20", ">+20"]
acc = {n: collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0])) for n in NS}
for s, v in rows.items():
    v.sort(); ts = [a for a, _, _ in v]
    payoff = 100.0 if win[s] == 0 else 0.0
    for t, pu, sz in v:
        el = t - s
        if not (20 <= el < 280) or not 8 <= pu <= 92:
            continue
        i0 = bisect.bisect_right(ts, t - 1) - 1
        if i0 < 0:
            continue
        p0 = v[i0][1]
        for n in NS:
            i1 = bisect.bisect_right(ts, t - 1 - n) - 1
            if i1 < 0:
                continue
            x = p0 - v[i1][1]
            b = next(i for i in range(len(EDGES) - 1) if EDGES[i] <= x < EDGES[i + 1])
            a = acc[n][b][s]; a[0] += sz * (payoff - pu); a[1] += sz
markets = list(rows)
print(f"markets {len(markets)}; fills 20..280 s into the window, Up price 8..92c")
print("y = payoff - price for a buyer of UP, cents/share, by the Up-price move of the previous N seconds")
for n in NS:
    print(f"\nN = {n} s")
    print(f"{'move c':>10}{'shares':>13}{'y c/sh':>8}{'t':>7}")
    for b in range(len(LAB)):
        d = acc[n][b]
        sh = sum(x[1] for x in d.values())
        if sh == 0:
            continue
        y = sum(x[0] for x in d.values()) / sh
        pm = [d[m][0] / 100 if m in d else 0.0 for m in markets]
        mean = sum(pm) / len(pm); sd = math.sqrt(sum((q - mean) ** 2 for q in pm) / len(pm))
        print(f"{LAB[b]:>10}{sh:13,.0f}{y:8.2f}{mean / sd * math.sqrt(len(pm)) if sd else 0:7.2f}")
