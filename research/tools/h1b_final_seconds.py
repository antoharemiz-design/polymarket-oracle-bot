"""H1b: final 15 s of the 5m window, buying the side the TWAP model rates >= P_MIN at price <= P_MAX.

The model uses Binance 1 s closes up to one second before the match (no look-ahead); PnL by the real
resolution after the taker fee. Also prints the price distribution of all post-window trades.

usage: h1b_final_seconds.py TAPE.csv RES.json ACTIVITY.jsonl BTC_1S.npz SPLIT_EPOCH
"""
import collections
import json
import math
import re
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from fairvalue import Spot, p_up  # noqa: E402

tape, res_p, act, npz, split = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5])
DELAY = 3
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
FEE = lambda p: 0.07 * p * (1 - p)
GRID = [(0.99, 0.97), (0.98, 0.95), (0.95, 0.90), (0.995, 0.98)]
acc = {g: {"sel": collections.defaultdict(lambda: [0.0, 0.0]), "hold": collections.defaultdict(lambda: [0.0, 0.0])} for g in GRID}
post = collections.Counter(); markets = {"sel": set(), "hold": set()}
cache = {}
for line in open(tape):
    p = line.split(",")
    if len(p) < 8:
        continue
    s = int(p[0]); w = win.get(s)
    if w is None or not spot.covers(s - 1000, s + 300):
        continue
    part = "hold" if s >= split else "sel"
    markets[part].add(s)
    tau = int(p[1]) - DELAY; el = tau - s
    oi, side, px, sz = int(p[2]), p[3], float(p[4]), float(p[5])
    # what the taker ended up long: outcome `lo` at price `lp`
    lo, lp = (oi, px) if side == "B" else (1 - oi, 1 - px)
    if el >= 300:
        post[("winner" if lo == w else "loser", round(lp, 3))] += sz
        continue
    if not 285 <= el < 300:
        continue
    k = (s, tau - 1)
    pu = cache.get(k)
    if pu is None:
        pu = cache[k] = p_up(spot, s, tau - 1, vol_mult=1.25)
    pm = pu if lo == 0 else 1 - pu
    for g in GRID:
        if pm >= g[0] and lp <= g[1]:
            a = acc[g][part][s]; a[0] += sz * ((1.0 if lo == w else 0.0) - lp - FEE(lp)); a[1] += sz
print("H1b: taker buys in the final 15 s of the side the model rates >= P_MIN, at price <= P_MAX (no look-ahead)")
print(f"{'P_MIN / P_MAX':<16}{'part':>6}{'mkts':>6}{'shares':>9}{'c/share':>9}{'$ total':>9}{'t':>7}{'win%':>6}")
for g in GRID:
    for part in ("sel", "hold"):
        d = acc[g][part]; ms = markets[part]
        sh = sum(x[1] for x in d.values())
        if sh <= 0:
            print(f"{f'{g[0]} / {g[1]}':<16}{part:>6}{0:>6}"); continue
        pm = [d[m][0] if m in d else 0.0 for m in ms]
        mean = sum(pm) / len(pm); sd = math.sqrt(sum((q - mean) ** 2 for q in pm) / len(pm))
        wins = sum(1 for m in d if d[m][0] > 0)
        print(f"{f'{g[0]} / {g[1]}':<16}{part:>6}{len(d):6d}{sh:9.0f}{100 * sum(pm) / sh:9.2f}{sum(pm):9.2f}"
              f"{(mean / sd * math.sqrt(len(pm)) if sd else 0):7.2f}{100 * wins / len(d):6.0f}")
tot = sum(post.values())
print(f"\npost-window trades (match time >= 300 s): {tot:,.0f} shares; what takers ended long, by price:")
top = sorted(post.items(), key=lambda kv: -kv[1])[:10]
print("  " + ", ".join(f"{k[0]}@{k[1]}: {100 * v / tot:.1f}%" for k, v in top))
