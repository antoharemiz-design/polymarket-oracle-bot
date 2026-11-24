"""H1: buying the already-determined winner after the 5m window ends (and in the final seconds).

Public tape fills, match time = block time - 3 s. Winner for trading = spot proxy with |TWAP move|
>= MIN_BPS (Binance 1 s closes), PnL by the real resolution. A "take" is a taker buy of the winner
or a taker sale of the loser (same economics: the taker ends long the winner at p_w).

usage: h1_postwindow.py TAPE.csv RES.json ACTIVITY.jsonl BTC_1S.npz SPLIT_EPOCH
"""
import collections
import json
import math
import re
import sys

import numpy as np

tape, res_p, act, npz, split = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
DELAY, MIN_BPS = 3, 1.0
z = np.load(npz); T0 = int(z["t0"]); C = z["close"]
res = json.load(open(res_p)); win = {}
for line in open(act):
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    m = re.match(r"^btc-updown-5m-(\d+)$", r.get("slug") or "")
    if m and res.get(r.get("condition_id"), -1) >= 0:
        win[int(m.group(1))] = res[r["condition_id"]]


def proxy(s):
    if s - 60 - T0 < 0 or s + 300 - T0 > len(C):
        return None
    k0 = C[s - 60 - T0:s - T0].mean(); k1 = C[s + 240 - T0:s + 300 - T0].mean()
    if abs(k1 / k0 - 1) * 1e4 < MIN_BPS:
        return None
    return 0 if k1 >= k0 else 1


FEE = lambda p: 0.07 * p * (1 - p)
rows = collections.defaultdict(list)
for line in open(tape):
    p = line.split(",")
    if len(p) >= 8 and int(p[0]) in win:
        rows[int(p[0])].append((int(p[1]) - DELAY - int(p[0]), int(p[2]), p[3], float(p[4]), float(p[5]), p[6]))
PHASES = {"final 285-300 s": (285, 300), "post +0-2 s": (300, 302), "post +2-5 s": (302, 305),
          "post +5-10 s": (305, 310), "post +10 s and later": (310, 10 ** 6)}
stat = {part: {ph: collections.defaultdict(lambda: [0.0, 0.0]) for ph in PHASES} for part in ("sel", "hold")}
wallets = collections.Counter()
prices = collections.Counter()
skipped = collections.Counter()
for s, v in rows.items():
    w = win[s]; pw = proxy(s)
    part = "hold" if s >= split else "sel"
    if pw is None:
        skipped[part] += 1; continue
    for el, oi, side, px, sz, wal in v:
        # the taker ends long the proxy winner?
        if side == "B" and oi == pw:
            p_w = px
        elif side == "S" and oi != pw:
            p_w = 1 - px
        else:
            continue
        if p_w > 0.99 + 1e-9:
            continue
        for ph, (lo, hi) in PHASES.items():
            if lo <= el < hi:
                if ph.startswith("final") and p_w > 0.97 + 1e-9:
                    break
                payoff = 1.0 if pw == w else 0.0
                a = stat[part][ph][s]; a[0] += sz * (payoff - p_w - FEE(p_w)); a[1] += sz
                if ph.startswith("post"):
                    wallets[wal] += sz; prices[round(p_w, 3)] += sz
                break
for part in ("sel", "hold"):
    ms = [s for s in rows if (s >= split) == (part == "hold")]
    n_days = len({s // 86400 for s in ms})
    print(f"\n{part}: {len(ms)} markets over {n_days} days; skipped (|move| < {MIN_BPS} bp or no spot): {skipped[part]}")
    print(f"{'phase':<24}{'shares':>10}{'c/share':>9}{'$ total':>10}{'t':>7}{'mkts':>6}{'$/day':>8}")
    for ph in PHASES:
        d = stat[part][ph]
        sh = sum(x[1] for x in d.values())
        if sh <= 0:
            print(f"{ph:<24}{0:>10}"); continue
        pm = [d[m][0] if m in d else 0.0 for m in ms]
        mean = sum(pm) / len(pm); sd = math.sqrt(sum((q - mean) ** 2 for q in pm) / len(pm))
        print(f"{ph:<24}{sh:10.0f}{100 * sum(pm) / sh:9.2f}{sum(pm):10.2f}{(mean / sd * math.sqrt(len(pm)) if sd else 0):7.2f}"
              f"{len(d):6d}{sum(pm) / n_days:8.2f}")
tot = sum(wallets.values())
print(f"\npost-window winner takes at <= 0.99: {tot:,.0f} shares by {len(wallets)} wallets; "
      f"top 3 hold {100 * sum(v for _, v in wallets.most_common(3)) / max(tot, 1):.0f}%")
print("price distribution (share of post-window volume):",
      {k: f"{100 * v / tot:.1f}%" for k, v in sorted(prices.items(), key=lambda kv: -kv[1])[:8]})
