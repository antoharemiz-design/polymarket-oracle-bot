"""Who makes money on BTC 5m markets? Maker-side profit of every public fill, held to resolution.

For each fill the taker bought (or sold) an outcome at price x. The resting side took the other
side of that bet. Maker profit per share = x - payoff(outcome) for a taker buy, the opposite
for a taker sell. Taker fee 0.07*p*(1-p) is paid on top by the taker and is not included.

usage: tape_study.py TAPE.csv RES.json ACTIVITY.jsonl [exclude_wallet_prefix ...]
"""
import collections
import json
import re
import sys

tape, res_p, act = sys.argv[1:4]
res = json.load(open(res_p))
win = {}
for line in open(act):
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    m = re.match(r"^btc-updown-5m-(\d+)$", r.get("slug") or "")
    if m and res.get(r.get("condition_id"), -1) >= 0:
        win[int(m.group(1))] = res[r["condition_id"]]


def bucket(edges, v):
    for i, (lo, hi) in enumerate(zip(edges, edges[1:])):
        if lo <= v < hi:
            return f"{i:02d}|{lo:g}..{hi:g}"
    return "99|other"


dims = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0]))  # shares, maker pnl, fee, fills
takers = collections.defaultdict(lambda: [0.0, 0.0, 0.0])                                    # notional, taker gross pnl, fee
markets = set()
for line in open(tape):
    p = line.rstrip("\n").split(",")
    if len(p) < 8:
        continue
    start = int(p[0]); w = win.get(start)
    if w is None:
        continue
    ts, oi, side, px, sz, wallet = int(p[1]), int(p[2]), p[3], float(p[4]), float(p[5]), p[6]
    markets.add(start)
    payoff = 1.0 if oi == w else 0.0
    taker_pnl = (payoff - px) if side == "B" else (px - payoff)
    maker = -taker_pnl * sz
    fee = 0.07 * px * (1 - px) * sz
    notional = sz * px
    el = ts - start
    for d, k in (("all", "all"),
                 ("price of traded outcome", bucket([0, .05, .15, .3, .45, .55, .7, .85, .95, 1.01], px)),
                 ("seconds into window (on-chain time)", bucket([-10 ** 6, 0, 30, 60, 120, 180, 240, 270, 300, 10 ** 6], el)),
                 ("fill size, shares", bucket([0, 5.5, 20, 50, 200, 1000, 10 ** 9], sz)),
                 ("taker side", "buy" if side == "B" else "sell")):
        a = dims[d][k]; a[0] += sz; a[1] += maker; a[2] += fee; a[3] += 1
    t = takers[wallet]; t[0] += notional; t[1] += taker_pnl * sz; t[2] += fee

print(f"markets {len(markets)}, fills {dims['all']['all'][3]:,}, shares {dims['all']['all'][0]:,.0f}")
for d, groups in dims.items():
    print(f"\nby {d}")
    print(f"{'bucket':>16}{'fills':>10}{'shares':>13}{'maker pnl $':>13}{'c/share':>9}{'taker fee c/sh':>15}")
    for k in sorted(groups):
        s, m, f, n = groups[k]
        print(f"{k.split('|')[-1]:>16}{n:10,d}{s:13,.0f}{m:13,.0f}{100*m/s:9.2f}{100*f/s:15.2f}")
tl = sorted(takers.items(), key=lambda kv: -kv[1][0])
tot = sum(v[0] for v in takers.values())
print(f"\ntakers: {len(takers):,} wallets; top 10 by notional hold {100*sum(v[0] for _, v in tl[:10])/tot:.1f}% of taker volume")
print(f"{'wallet':>14}{'notional $':>13}{'gross pnl $':>12}{'gross %':>9}{'net of fee %':>13}")
for wlt, (n, g, f) in tl[:12]:
    print(f"{wlt:>14}{n:13,.0f}{g:12,.0f}{100*g/n:9.2f}{100*(g-f)/n:13.2f}")
rest = tl[12:]
n = sum(v[0] for _, v in rest); g = sum(v[1] for _, v in rest); f = sum(v[2] for _, v in rest)
print(f"{'all others':>14}{n:13,.0f}{g:12,.0f}{100*g/n:9.2f}{100*(g-f)/n:13.2f}")
small = [v for _, v in tl if v[0] < 2000]
n = sum(v[0] for v in small); g = sum(v[1] for v in small); f = sum(v[2] for v in small)
print(f"{'<$2k wallets':>14}{n:13,.0f}{g:12,.0f}{100*g/n:9.2f}{100*(g-f)/n:13.2f}   ({len(small):,} wallets)")
