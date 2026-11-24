"""Maker-side profit by time in window, with per-market t-statistics.

Per market and bucket: maker pnl ($) = sum over fills of shares * (maker side payoff - price).
The t-statistic treats markets as independent observations.

usage: tape_time_study.py TAPE.csv RES.json ACTIVITY.jsonl [chain_delay_s=3]
"""
import collections
import json
import math
import re
import sys

tape, res_p, act = sys.argv[1:4]
DELAY = int(sys.argv[4]) if len(sys.argv) > 4 else 3      # on-chain timestamp lags the match by a few seconds
res = json.load(open(res_p)); win = {}
for line in open(act):
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    m = re.match(r"^btc-updown-5m-(\d+)$", r.get("slug") or "")
    if m and res.get(r.get("condition_id"), -1) >= 0:
        win[int(m.group(1))] = res[r["condition_id"]]

T_EDGES = [-10 ** 6, 0, 15, 30, 60, 90, 120, 180, 240, 270, 300]
P_EDGES = [0.0, 0.2, 0.4, 0.6, 0.8, 1.01]
tb = lambda v: next((i for i in range(len(T_EDGES) - 1) if T_EDGES[i] <= v < T_EDGES[i + 1]), None)
pb = lambda v: next((i for i in range(len(P_EDGES) - 1) if P_EDGES[i] <= v < P_EDGES[i + 1]), None)
by_t = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0]))       # bucket -> market -> [pnl, shares]
by_tp = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0]))
markets = set()
for line in open(tape):
    p = line.rstrip("\n").split(",")
    if len(p) < 8:
        continue
    start = int(p[0]); w = win.get(start)
    if w is None:
        continue
    markets.add(start)
    ts, oi, side, px, sz = int(p[1]) - DELAY, int(p[2]), p[3], float(p[4]), float(p[5])
    payoff = 1.0 if oi == w else 0.0
    maker = -((payoff - px) if side == "B" else (px - payoff)) * sz
    # normalize price to the maker's bought outcome: maker of a taker-buy at px effectively buys the other side at 1-px
    maker_px = 1 - px if side == "B" else px
    i, j = tb(ts - start), pb(maker_px)
    if i is None:
        continue
    a = by_t[i][start]; a[0] += maker; a[1] += sz
    if j is not None:
        a = by_tp[(i, j)][start]; a[0] += maker; a[1] += sz
n = len(markets)


def stat(d):
    pnl = [d[m][0] if m in d else 0.0 for m in markets]
    sh = sum(v[1] for v in d.values())
    mean = sum(pnl) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in pnl) / n)
    return sum(pnl), sh, (mean / sd * math.sqrt(n) if sd else 0.0)


print(f"markets {n}; match time = on-chain time - {DELAY}s")
print(f"\n{'seconds':>12}{'shares':>13}{'maker pnl $':>13}{'c/share':>9}{'t-stat':>8}")
for i in range(len(T_EDGES) - 1):
    tot, sh, t = stat(by_t[i])
    lab = f"{max(T_EDGES[i], -99):g}..{T_EDGES[i+1]:g}"
    print(f"{lab:>12}{sh:13,.0f}{tot:13,.0f}{100*tot/sh if sh else 0:9.2f}{t:8.2f}")
print("\nmaker c/share (t-stat) by time and by the price of the outcome the maker ends up holding")
print(f"{'seconds':>12}" + "".join(f"{f'{P_EDGES[j]:g}-{min(P_EDGES[j+1],1):g}':>16}" for j in range(len(P_EDGES) - 1)))
for i in range(len(T_EDGES) - 1):
    row = f"{f'{max(T_EDGES[i], -99):g}..{T_EDGES[i+1]:g}':>12}"
    for j in range(len(P_EDGES) - 1):
        tot, sh, t = stat(by_tp[(i, j)])
        row += f"{(f'{100*tot/sh:+.2f} ({t:+.1f})' if sh else '-'):>16}"
    print(row)
