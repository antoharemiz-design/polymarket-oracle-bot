"""How much weight does the spot model deserve against the traded price?

Regression over public fills, in Up-token terms:
    payoff_up - price_up = w * (model_up - price_up) + noise
w = 0 means the traded price already contains everything the model knows; w = 1 means the
model alone is the right fair value. Standard errors are clustered by market.

usage: tape_blend.py TAPE.csv RES.json ACTIVITY.jsonl BTC_1S.npz [chain_delay_s=3] [info_lag_s=1]
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
LAG = int(sys.argv[6]) if len(sys.argv) > 6 else 1
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
SEGS = [(12, 60), (60, 120), (120, 180), (180, 240), (240, 290), (12, 290)]
VOLS = (1.0, 1.25, 1.5)
# per (seg, vol): market -> [sum w x y, sum w x x]
acc = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0]))
cache = {}
for line in open(tape):
    p = line.rstrip("\n").split(",")
    if len(p) < 8:
        continue
    start = int(p[0]); w = win.get(start)
    if w is None or not spot.covers(start - 1000, start + 300):
        continue
    ts, oi, px, sz = int(p[1]) - DELAY, int(p[2]), float(p[4]), float(p[5])
    el = ts - start
    if not (12 <= el < 290) or not 0.05 <= px <= 0.95:
        continue
    px_up = px if oi == 0 else 1 - px
    y = (1.0 if w == 0 else 0.0) - px_up
    for vm in VOLS:
        key = (start, ts - LAG, vm)
        pu = cache.get(key)
        if pu is None:
            pu = cache[key] = p_up(spot, start, ts - LAG, vol_mult=vm)
        x = pu - px_up
        for si, (lo, hi) in enumerate(SEGS):
            if lo <= el < hi:
                a = acc[(si, vm)][start]; a[0] += sz * x * y; a[1] += sz * x * x
print(f"info lag {LAG}s before estimated match (on-chain - {DELAY}s)")
print(f"{'seconds':>10}" + "".join(f"{f'w @vol x{vm}':>20}" for vm in VOLS))
for si, (lo, hi) in enumerate(SEGS):
    row = f"{f'{lo}..{hi}':>10}"
    for vm in VOLS:
        d = acc[(si, vm)]
        sxy = sum(v[0] for v in d.values()); sxx = sum(v[1] for v in d.values())
        b = sxy / sxx
        se = math.sqrt(sum((v[0] - b * v[1]) ** 2 for v in d.values())) / sxx   # cluster-robust
        row += f"{f'{b:.2f} +- {se:.2f}':>20}"
    print(row)
