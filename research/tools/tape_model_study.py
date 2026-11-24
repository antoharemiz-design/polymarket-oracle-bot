"""Does a spot-driven fair value separate good maker fills from toxic ones? Large-sample test on the public tape.

For every fill the maker's "model edge" is computed from Binance 1s data that was available
INFO_LAG seconds before the estimated match time:
    edge = P_model(maker's outcome wins) - price the maker paid for it.
Fills are bucketed by that edge; the realized maker profit to resolution is reported with
per-market t-statistics. If the model is useful, profit must rise with the bucket.

usage: tape_model_study.py TAPE.csv RES.json ACTIVITY.jsonl BTC_1S.npz [chain_delay_s=3]
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

LAGS = (1, 3, 6, 10)
SEGS = [(12, 60), (60, 120), (120, 180), (180, 240), (240, 290)]
seg_acc = {L: collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0, 0.0])) for L in LAGS}  # (seg, sign)->market->[pnl, shares, fee]
EDGES = [-1e9, -6, -3, -1, 1, 3, 6, 1e9]
LABELS = ["<-6c", "-6..-3", "-3..-1", "-1..+1", "+1..+3", "+3..+6", ">+6c"]
acc = {L: collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0, 0.0])) for L in LAGS}  # bucket->market->[pnl, shares, paid]
cache = {}
markets = set()
for line in open(tape):
    p = line.rstrip("\n").split(",")
    if len(p) < 8:
        continue
    start = int(p[0]); w = win.get(start)
    if w is None or not spot.covers(start - 1000, start + 300):
        continue
    ts, oi, side, px, sz = int(p[1]) - DELAY, int(p[2]), p[3], float(p[4]), float(p[5])
    if not (start + 12 <= ts < start + 290) or not 0.05 <= px <= 0.95:
        continue
    markets.add(start)
    # the maker ends up long `m_oi` at price `m_px`
    if side == "B":
        m_oi, m_px = 1 - oi, 1 - px
    else:
        m_oi, m_px = oi, px
    pnl = ((1.0 if m_oi == w else 0.0) - m_px) * sz
    for L in LAGS:
        key = (start, ts - L)
        pu = cache.get(key)
        if pu is None:
            pu = cache[key] = p_up(spot, start, ts - L, vol_mult=1.25)
        fair = pu if m_oi == 0 else 1 - pu
        e = 100 * (fair - m_px)
        b = next(i for i in range(len(EDGES) - 1) if EDGES[i] <= e < EDGES[i + 1])
        a = acc[L][b][start]; a[0] += pnl; a[1] += sz; a[2] += sz * m_px
        if abs(e) >= 3:
            sg = next(i for i, (lo, hi) in enumerate(SEGS) if lo <= ts - start < hi)
            a = seg_acc[L][(sg, 1 if e > 0 else -1)][start]
            a[0] += pnl; a[1] += sz; a[2] += sz * 0.07 * m_px * (1 - m_px)
n = len(markets)
print(f"markets {n}; match time = on-chain time - {DELAY}s; fills in seconds 12..290 with price 5..95c")
for L in LAGS:
    print(f"\nmodel information {L}s older than the match")
    print(f"{'model edge':>10}{'shares':>12}{'share%':>8}{'maker $':>11}{'c/share':>9}{'t-stat':>8}")
    tot_sh = sum(v[1] for d in acc[L].values() for v in d.values())
    for b in range(len(LABELS)):
        d = acc[L][b]
        pnl = [d[m][0] if m in d else 0.0 for m in markets]
        sh = sum(v[1] for v in d.values())
        mean = sum(pnl) / n; sd = math.sqrt(sum((x - mean) ** 2 for x in pnl) / n)
        print(f"{LABELS[b]:>10}{sh:12,.0f}{100*sh/tot_sh:8.1f}{sum(pnl):11,.0f}{100*sum(pnl)/sh if sh else 0:9.2f}{(mean/sd*math.sqrt(n) if sd else 0):8.2f}")

print("\n\nBy time in window, fills where |model edge| >= 3c.")
print("  maker+: model favoured the maker  -> realized maker c/share (a model-guided maker keeps these)")
print("  taker+: model favoured the taker  -> realized taker c/share before fee, and after the 0.07*p*(1-p) fee")
for L in LAGS:
    print(f"\nmodel information {L}s older than the match")
    print(f"{'seconds':>10}{'maker+ shares':>15}{'c/share':>9}{'t':>7}  |{'taker+ shares':>15}{'gross c':>9}{'net c':>8}{'t(net)':>8}")
    for sg, (lo, hi) in enumerate(SEGS):
        row = f"{f'{lo}..{hi}':>10}"
        d = seg_acc[L][(sg, 1)]
        pnl = [d[m][0] if m in d else 0.0 for m in markets]; sh = sum(v[1] for v in d.values())
        mean = sum(pnl) / n; sd = math.sqrt(sum((x - mean) ** 2 for x in pnl) / n)
        row += f"{sh:15,.0f}{100*sum(pnl)/sh if sh else 0:9.2f}{(mean/sd*math.sqrt(n) if sd else 0):7.2f}  |"
        d = seg_acc[L][(sg, -1)]
        net = [(-d[m][0] - d[m][2]) if m in d else 0.0 for m in markets]; sh = sum(v[1] for v in d.values())
        gross = -sum(v[0] for v in d.values())
        mean = sum(net) / n; sd = math.sqrt(sum((x - mean) ** 2 for x in net) / n)
        row += f"{sh:15,.0f}{100*gross/sh if sh else 0:9.2f}{100*sum(net)/sh if sh else 0:8.2f}{(mean/sd*math.sqrt(n) if sd else 0):8.2f}"
        print(row)
