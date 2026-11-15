"""Mechanism check for early-window ladders on the public tape (all makers, average queue position).

Maker profit to resolution of every fill matched between 20 s before and 45 s after the window
start, by the distance of the maker's price from the opening price, and by day. The opening
price is the Up price of the last trade before the window start (none: 50).
match time = on-chain time - 3 s. t-statistics per market.

usage: tape_early_study.py TAPE.csv RES.json ACTIVITY.jsonl
"""
import collections
import datetime as dt
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
        rows[s].append((int(p[1]) - DELAY, int(p[2]), p[3], float(p[4]), float(p[5])))
FEE = lambda x: 0.07 * x * (1 - x)
by = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0]))   # key -> market -> [pnl $, shares]
for s, v in rows.items():
    v.sort()
    before = [x for x in v if x[0] < s]
    if before:
        t, oi, side, px, sz = before[-1]
        open_up = 100 * (px if oi == 0 else 1 - px)
    else:
        open_up = 50.0
    up_won = win[s] == 0
    for t, oi, side, px, sz in v:
        el = t - s
        if not -20 <= el < 45:
            continue
        # the maker ends up long outcome m_oi at price m_px
        m_oi, m_px = (1 - oi, 1 - px) if side == "B" else (oi, px)
        payoff = 1.0 if (m_oi == 0) == up_won else 0.0
        pnl = (payoff - m_px + 0.2 * FEE(m_px)) * sz          # with the 20% maker rebate
        m_up = 100 * (m_px if m_oi == 0 else 1 - m_px)          # maker price in Up terms
        dist = abs(m_up - open_up)
        db = "0-3c" if dist < 3 else "3-6c" if dist < 6 else "6-10c" if dist < 10 else ">=10c"
        tb = "pre -20..0" if el < 0 else "0..15" if el < 15 else "15..30" if el < 30 else "30..45"
        day = dt.datetime.fromtimestamp(s, dt.UTC).strftime("%m-%d")
        for key in (("all",), ("dist", db), ("time", tb), ("day", day)):
            a = by[key][s]; a[0] += pnl; a[1] += sz
markets = list(rows)


def line(key, label):
    d = by[key]
    sh = sum(x[1] for x in d.values())
    if sh <= 0:
        return
    pm = [d[m][0] if m in d else 0.0 for m in markets]
    mean = sum(pm) / len(pm); sd = math.sqrt(sum((q - mean) ** 2 for q in pm) / len(pm))
    print(f"{label:>22}{sh:13,.0f}{100 * sum(pm) / sh:9.2f}{sum(pm):11,.0f}{mean / sd * math.sqrt(len(pm)) if sd else 0:7.2f}{len(d):7d}")


print(f"markets {len(markets)}; maker fills from 20 s before to 45 s after the start; PnL to resolution incl. 20% rebate")
print(f"{'bucket':>22}{'shares':>13}{'c/share':>9}{'maker $':>11}{'t':>7}{'mkts':>7}")
line(("all",), "all")
for b in ("0-3c", "3-6c", "6-10c", ">=10c"):
    line(("dist", b), "distance " + b)
for b in ("pre -20..0", "0..15", "15..30", "30..45"):
    line(("time", b), "seconds " + b)
for k in sorted(k for k in by if k[0] == "day"):
    line(k, "day " + k[1])
