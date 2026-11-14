"""Microstructure study on recorded ticks: which fair value best separates good maker fills from toxic ones?

For every recorded trade, the resting side's edge is computed from what a maker could know
`lat` ms before the trade, under several fair-value definitions:
    mid          book mid only (no model)
    raw          spot model level
    hl=N         spot model with the strike offset averaged over an N-second half-life
Trades are bucketed by that edge. Reported per bucket: maker markout per share (cents) against
the book mid 1 s / 5 s / 30 s later and against the final outcome.

usage: tick_study.py REC_DIR [lat_ms=150]
"""
import bisect
import collections
import json
import os
import sys

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from polybot.book import UpBook                       # noqa: E402
from polybot.fairvalue import FairValue, SpotSeries   # noqa: E402
from polybot.replay import list_markets, merged       # noqa: E402
from polybot.strategy import Fair                     # noqa: E402

rec = sys.argv[1]
LAT = int(sys.argv[2]) if len(sys.argv) > 2 else 150
GRID = 100
H = (1000, 5000, 30000)
HLS = (5, 20, 60)
NAMES = ["mid", "raw"] + [f"hl={h}" for h in HLS]
outcomes = json.load(open(os.path.join(rec, "outcomes.json"))) if os.path.exists(os.path.join(rec, "outcomes.json")) else {}
EDGES = [-1e9, -3, -1, 1, 3, 1e9]
LABELS = ["<-3c", "-3..-1", "-1..+1", "+1..+3", ">+3c"]
# name -> bucket -> [shares, m1, m5, m30, settle_sum, settle_shares]
mk = {nm: collections.defaultdict(lambda: [0.0] * 6) for nm in NAMES}
per_mkt = {nm: collections.defaultdict(lambda: collections.defaultdict(float)) for nm in NAMES}   # bucket -> market -> settle pnl
used = 0
for start, path in list_markets(rec):
    spot = SpotSeries(); fv = FairValue(spot, start); book = UpBook()
    fairs = [Fair(h) for h in HLS]
    gt, gmid, gf = [], [], []
    trades = []
    nxt, end_ms = start * 1000, (start + 300) * 1000
    for e in merged(rec, start, path):
        t = e[1]
        while t >= nxt and nxt <= end_ms:
            if book.ready and spot.ready(start):
                bb, ba = book.best_bid(), book.best_ask()
                comp = fv.components(nxt)
                if comp is not None and comp[1] > 0 and 0 < bb < ba < 100:
                    mid = (bb + ba) / 2.0
                    vals = [mid, None]
                    for f in fairs:
                        fair, raw = f.update(nxt, comp, bb, ba)
                        vals.append(fair); vals[1] = mid + raw
                    gt.append(nxt); gmid.append(mid); gf.append(vals)
            nxt += GRID
        k = e[0]
        if k == "S":
            spot.update(t, e[2])
        elif k == "B":
            book.snapshot(e[2], e[3])
        elif k == "P":
            if book.ready:
                book.change(e[2], e[3], e[4])
        elif k == "T" and start * 1000 + 10000 <= t < end_ms - 10000:
            trades.append((t, e[2], e[3], e[4]))
    if len(gt) < 1000:
        continue
    used += 1
    w = outcomes.get(str(start))
    at = lambda tt: bisect.bisect_right(gt, tt) - 1
    for t, side, px, sz in trades:
        i0 = at(t - LAT)
        if i0 < 0 or sz <= 0 or not 8 <= px <= 92 or gt[i0] < t - LAT - 500:
            continue
        sign = 1 if side == "B" else -1          # taker bought Up at px: the maker sold Up at px
        outs = []
        for h in H:
            j = at(t + h)
            outs.append(sign * (px - gmid[j]) if j >= 0 and gt[j] >= t + h - 2 * GRID else None)
        settle = None if w is None else sign * (px - (100.0 if w == 0 else 0.0))
        for n, nm in enumerate(NAMES):
            edge = sign * (px - gf[i0][n])
            b = next(i for i in range(len(EDGES) - 1) if EDGES[i] <= edge < EDGES[i + 1])
            a = mk[nm][b]
            if None not in outs:
                a[0] += sz
                for q in range(3):
                    a[1 + q] += sz * outs[q]
            if settle is not None:
                a[4] += sz * settle; a[5] += sz
                per_mkt[nm][b][start] += sz * settle / 100.0

print(f"markets used: {used} ({sum(1 for s, _ in list_markets(rec) if str(s) in outcomes)} with a known outcome); maker information is {LAT} ms old")
print("maker markout per share in cents; positive = the resting side made money")
import math
for nm in NAMES:
    print(f"\nfair value: {nm}")
    print(f"{'maker edge':>10}{'shares':>9}{'share%':>8}{'+1s':>7}{'+5s':>7}{'+30s':>7}{'settle':>8}{'settle $':>10}{'t':>6}")
    tot = sum(a[0] for a in mk[nm].values()) or 1
    for b in range(len(LABELS)):
        a = mk[nm][b]
        if a[0] <= 0:
            continue
        d = per_mkt[nm][b]; ms = list(d.values())
        t = 0.0
        if len(ms) > 2:
            mean = sum(ms) / len(ms); sd = math.sqrt(sum((x - mean) ** 2 for x in ms) / len(ms))
            t = mean / sd * math.sqrt(len(ms)) if sd else 0.0
        print(f"{LABELS[b]:>10}{a[0]:9.0f}{100*a[0]/tot:8.1f}{a[1]/a[0]:7.2f}{a[2]/a[0]:7.2f}{a[3]/a[0]:7.2f}"
              f"{(a[4]/a[5] if a[5] else 0):8.2f}{sum(ms):10.0f}{t:6.1f}")
