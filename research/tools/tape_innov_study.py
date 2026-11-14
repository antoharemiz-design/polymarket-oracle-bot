"""Is a recent spot move a usable toxicity signal for a maker? Large-sample test on the public tape.

Signal for each fill: change of the model probability of the MAKER's outcome over the H seconds
that ended LAG seconds before the estimated match (no market price is used, only spot).
A strongly negative value means spot just moved against the resting side.
Reported: realized maker profit to resolution per bucket, t-statistics clustered by market.

usage: tape_innov_study.py TAPE.csv RES.json ACTIVITY.jsonl BTC_1S.npz [chain_delay_s=3]
"""
import collections
import json
import math
import re
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from fairvalue import Spot, p_up  # noqa: E402

tape, res_p, act, npz = sys.argv[1:5]
DELAY = int(sys.argv[5]) if len(sys.argv) > 5 else 3
spot = Spot(npz)
res = json.load(open(res_p)); win = {}
for line in open(act):
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    m = re.match(r"^btc-updown-5m-(\d+)$", r.get("slug") or "")
    if m and res.get(r.get("condition_id"), -1) >= 0:
        win[int(m.group(1))] = res[r["condition_id"]]

CASES = [(1, 3), (1, 10), (3, 10), (1, 30)]           # (LAG, H)
EDGES = [-1e9, -6, -3, -1, 1, 3, 6, 1e9]
LABELS = ["<-6c", "-6..-3", "-3..-1", "-1..+1", "+1..+3", "+3..+6", ">+6c"]
acc = {c: collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0])) for c in CASES}
cache = {}
markets = set()


def pu(start, t):
    k = (start, t)
    v = cache.get(k)
    if v is None:
        v = cache[k] = p_up(spot, start, t, vol_mult=1.25)
    return v


for line in open(tape):
    p = line.rstrip("\n").split(",")
    if len(p) < 8:
        continue
    start = int(p[0]); w = win.get(start)
    if w is None or not spot.covers(start - 1000, start + 300):
        continue
    ts, oi, side, px, sz = int(p[1]) - DELAY, int(p[2]), p[3], float(p[4]), float(p[5])
    if not (start + 45 <= ts < start + 290) or not 0.05 <= px <= 0.95:
        continue
    markets.add(start)
    if side == "B":
        m_oi, m_px = 1 - oi, 1 - px
    else:
        m_oi, m_px = oi, px
    pnl = ((1.0 if m_oi == w else 0.0) - m_px) * sz
    for lag, h in CASES:
        a, b = pu(start, ts - lag), pu(start, ts - lag - h)
        x = 100 * ((a - b) if m_oi == 0 else (b - a))
        k = next(i for i in range(len(EDGES) - 1) if EDGES[i] <= x < EDGES[i + 1])
        e = acc[(lag, h)][k][start]; e[0] += pnl; e[1] += sz
n = len(markets)
print(f"markets {n}; match time = on-chain time - {DELAY}s; fills in seconds 45..290, price 5..95c")
for lag, h in CASES:
    print(f"\nmodel move over the {h}s that ended {lag}s before the match, in favour of the maker's side")
    print(f"{'move':>10}{'shares':>12}{'share%':>8}{'maker $':>11}{'c/share':>9}{'t-stat':>8}")
    tot = sum(v[1] for d in acc[(lag, h)].values() for v in d.values())
    for k in range(len(LABELS)):
        d = acc[(lag, h)][k]
        pnl = [d[m][0] if m in d else 0.0 for m in markets]
        sh = sum(v[1] for v in d.values())
        mean = sum(pnl) / n; sd = math.sqrt(sum((x - mean) ** 2 for x in pnl) / n)
        print(f"{LABELS[k]:>10}{sh:12,.0f}{100*sh/tot:8.1f}{sum(pnl):11,.0f}{100*sum(pnl)/sh if sh else 0:9.2f}{(mean/sd*math.sqrt(n) if sd else 0):8.2f}")
