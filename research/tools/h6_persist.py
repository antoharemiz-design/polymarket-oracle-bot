"""H6 follow-up: does the nested-window mispricing survive long enough for a slow taker?

For each 15m window with |K5 - K15| >= $10 (strikes from Binance 1 s TWAP), the bundle
"long 15m side that pays whenever the last 5m side does not" costs p15 + p5 + fees and pays 1 + [T between strikes].
Signal at t0 = the first second where printed taker prices of both legs (within 2 s) form a bundle
cheaper than 1 - margin after fees. Execution: the next prints of each leg at t >= t0 + DELAY s
(within 3 s of each other), size = min of the two prints. PnL = payoff - cost - fees, payoff from
Binance TWAP (ambiguous |T - K| < $5 counted as payoff 1, the guaranteed minimum).

usage: h6_persist.py H6_DIR TAPE_5M.csv BTC_1S.npz SPLIT_EPOCH
"""
import collections
import json
import math
import sys

import numpy as np

d, tape, npz, split = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
m15 = json.load(open(f"{d}/m15.json"))
z = np.load(npz); T0 = int(z["t0"]); C = z["close"]
twap = lambda a: float(C[a - 60 - T0:a - T0].mean()) if a - 60 - T0 >= 0 and a - T0 <= len(C) else None
FEE = lambda p: 0.07 * p * (1 - p)


def load(path, keep):
    out = collections.defaultdict(list)
    for line in open(path):
        p = line.rstrip("\n").split(",")
        s = int(p[0])
        if not keep(s):
            continue
        ts, oi, side, px, sz = int(p[1]), int(p[2]), p[3], float(p[4]), float(p[5])
        lo, lp = (oi, px) if side == "B" else (1 - oi, 1 - px)
        out[s].append((ts, lo, lp, sz))
    return out


f15 = load(f"{d}/fills15.csv", lambda s: True)
f5 = load(tape, lambda s: (s - 600) % 900 == 0 and str(s - 600) in m15)


def run(margin, delay):
    res = {"sel": [], "hold": []}
    persist = {"sel": [], "hold": []}
    for k, info in m15.items():
        s15 = int(k); s5 = s15 + 600
        if s5 not in f5:
            continue
        k15, k5, T = twap(s15), twap(s5), twap(s15 + 900)
        if None in (k15, k5, T) or abs(k5 - k15) < 10:
            continue
        o15, o5 = (0, 1) if k15 <= k5 else (1, 0)
        lo_k, hi_k = min(k15, k5), max(k15, k5)
        between = lo_k + 5 < T < hi_k - 5
        a = sorted((ts, lp, sz) for ts, lo, lp, sz in f15.get(s15, []) if lo == o15 and s5 <= ts < s15 + 900)
        b = sorted((ts, lp, sz) for ts, lo, lp, sz in f5[s5] if lo == o5 and ts < s5 + 300)
        if not a or not b:
            continue
        arb_secs = []
        j0 = 0
        for ta, pa, sa in a:
            while j0 < len(b) and b[j0][0] < ta - 2:
                j0 += 1
            j = j0
            while j < len(b) and b[j][0] <= ta + 2:
                pb = b[j][1]
                if 1 - pa - pb - FEE(pa) - FEE(pb) > margin:
                    arb_secs.append(max(ta, b[j][0]))
                j += 1
        if not arb_secs:
            continue
        part = "hold" if s15 >= split else "sel"
        t0 = min(arb_secs)
        persist[part].append((max(arb_secs) - t0, len(set(arb_secs))))
        # execution after the delay: first print of each leg at >= t0 + delay, within 3 s of each other
        A = [x for x in a if x[0] >= t0 + delay]
        B = [x for x in b if x[0] >= t0 + delay]
        if not A or not B or abs(A[0][0] - B[0][0]) > 3:
            res[part].append((k, 0.0, 0.0, 0.0))     # signal, no execution
            continue
        pa, sa = A[0][1], A[0][2]; pb, sb = B[0][1], B[0][2]
        q = min(sa, sb)
        pnl = (1 + (1 if between else 0)) - pa - pb - FEE(pa) - FEE(pb)
        res[part].append((k, q, pnl, pa + pb))
    return res, persist


split_name = {"sel": "selection", "hold": "hold-out"}
for margin in (0.0, 0.02):
    for delay in (0, 1, 2):
        res, persist = run(margin, delay)
        for part in ("sel", "hold"):
            r = res[part]
            ex = [x for x in r if x[1] > 0]
            pnl_sh = [x[2] for x in ex]
            mean = sum(pnl_sh) / len(pnl_sh) if pnl_sh else 0
            sd = math.sqrt(sum((x - mean) ** 2 for x in pnl_sh) / len(pnl_sh)) if pnl_sh else 0
            t = mean / sd * math.sqrt(len(pnl_sh)) if sd else 0
            dollars = sum(x[1] * x[2] for x in ex)
            neg = sum(1 for x in pnl_sh if x < 0)
            line = (f"margin {margin:.2f} delay {delay}s {split_name[part]:>9}: signals {len(r):3d}, executed {len(ex):3d}, "
                    f"PnL/share mean {100 * mean:6.2f} c (t {t:5.2f}), losing {neg:2d}, $ at print size {dollars:7.2f}")
            print(line)
        if margin == 0.0 and delay == 0:
            for part in ("sel", "hold"):
                p = sorted(persist[part])
                if p:
                    durs = [x[0] for x in p]; secs = [x[1] for x in p]
                    print(f"   persistence {split_name[part]}: arb visible for median {durs[len(durs) // 2]} s "
                          f"(p25 {durs[len(durs) // 4]}, p75 {durs[3 * len(durs) // 4]}), distinct seconds median "
                          f"{sorted(secs)[len(secs) // 2]}")
