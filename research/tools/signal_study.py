"""Does a spot-driven fair value model explain a wallet's BTC 5m entries?

Part 1: calibration of the model on every market in spot coverage (independent of the wallet).
Part 2: model edge (fair - paid price) at the wallet's fills, for several assumed delays between
        the match and the on-chain timestamp reported by the data API.

usage: signal_study.py ACTIVITY.jsonl TAKER.jsonl RES.json BTC_1S.npz
"""
import collections
import json
import re
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from fairvalue import Spot, p_up  # noqa: E402

act_p, tak_p, res_p, npz = sys.argv[1:5]
res = json.load(open(res_p)); spot = Spot(npz)
key = lambda r: (r["transaction_hash"], r["token_id"], r["side"], round(r["price"], 6), round(r["size"], 6))
taker = collections.Counter(key(json.loads(l)) for l in open(tak_p))
fills, markets = [], {}
for line in open(act_p):
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    m = re.match(r"^btc-updown-5m-(\d+)$", r.get("slug") or "")
    if not m or r["type"] != "TRADE" or r["side"] != "BUY":
        continue
    start = int(m.group(1)); w = res.get(r["condition_id"], -1)
    if w < 0 or not spot.covers(start - 1000, start + 300):
        continue
    k = key(r); tk = taker[k] > 0
    if tk:
        taker[k] -= 1
    markets[start] = w
    fills.append((start, r["timestamp"], r["outcome_index"], r["price"], r["size"], tk, w))
print(f"markets {len(markets)}, buy fills {len(fills)}")

# ---- Part 1: calibration -------------------------------------------------------------------------
for vm in (1.0, 1.25, 1.5):
    bins = collections.defaultdict(lambda: [0, 0.0, 0]); brier = []
    for start, w in markets.items():
        for off in (15, 45, 90, 150, 210, 250, 275, 290):
            p = p_up(spot, start, start + off, vol_mult=vm)
            b = bins[min(int(p * 10), 9)]; b[0] += 1; b[1] += p; b[2] += (w == 0)
            brier.append((p - (w == 0)) ** 2)
    print(f"\ncalibration, vol_mult={vm}: Brier {np.mean(brier):.4f} (coin flip = 0.25)")
    print("  " + "  ".join(f"{b[1]/b[0]:.2f}->{b[2]/b[0]:.2f}" for _, b in sorted(bins.items())) + "   (model p -> realized Up frequency)")
print("\naccuracy of sign(model p - 0.5) by seconds elapsed:")
for off in (15, 45, 90, 150, 210, 250, 275, 290):
    acc = np.mean([(p_up(spot, s, s + off) >= 0.5) == (w == 0) for s, w in markets.items()])
    print(f"  t+{off:3d}s: {100*acc:.1f}%", end="")
print()

# ---- Part 2: model edge at the wallet's fills ----------------------------------------------------
VM = 1.25
print(f"\nmodel edge at fills = model P(bought side) - price paid (vol_mult={VM}); delay = seconds between match and reported timestamp")
print(f"{'role':>6}{'delay':>6}{'fills':>8}{'mean edge':>10}{'edge>0':>8}{'edge>2c':>8}{'corr(edge,win-px)':>19}")
for role in (True, False):
    rr = [f for f in fills if f[5] == role]
    for d in (0, 2, 4, 6, 8, 12, 20):
        e, y, wgt = [], [], []
        for start, t, oi, px, sz, tk, w in rr:
            tt = t - d
            if not start <= tt < start + 300:
                continue
            p = p_up(spot, start, tt, vol_mult=VM)
            e.append((p if oi == 0 else 1 - p) - px); y.append((oi == w) - px); wgt.append(sz)
        e, y, wgt = np.array(e), np.array(y), np.array(wgt)
        print(f"{'taker' if role else 'maker':>6}{d:6d}{len(e):8d}{np.average(e, weights=wgt):10.4f}{100*np.average(e > 0, weights=wgt):8.1f}"
              f"{100*np.average(e > 0.02, weights=wgt):8.1f}{np.corrcoef(e, y)[0, 1]:19.3f}")

# realized pnl per share by model-edge bucket (delay chosen = 4s)
print("\nrealized gross pnl per share by model edge bucket (delay 4s)")
print(f"{'role':>6}{'edge bucket':>16}{'shares':>10}{'avg px':>8}{'gross/share':>12}{'fee/share':>10}")
edges = [-1, -0.10, -0.05, -0.02, 0, 0.02, 0.05, 0.10, 1]
for role in (True, False):
    buck = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for start, t, oi, px, sz, tk, w in fills:
        if tk != role or not start <= t - 4 < start + 300:
            continue
        p = p_up(spot, start, t - 4, vol_mult=VM); e = (p if oi == 0 else 1 - p) - px
        b = buck[max(i for i, lo in enumerate(edges[:-1]) if e >= lo)]
        b[0] += sz; b[1] += sz * px; b[2] += sz * ((oi == w) - px); b[3] += sz * 0.07 * px * (1 - px) * role
    for i in sorted(buck):
        b = buck[i]
        print(f"{'taker' if role else 'maker':>6}{f'{edges[i]:+.2f}..{edges[i+1]:+.2f}':>16}{b[0]:10.0f}{b[1]/b[0]:8.3f}{b[2]/b[0]:12.4f}{b[3]/b[0]:10.4f}")
