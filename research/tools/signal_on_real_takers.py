"""Validate the fast spot signal on REAL taker fills, independent of the execution model.

Every recorded trade was a real marketable order that was submitted about (250 ms taker delay +
network) before it matched. For each one, compute what the bot's signal said at the moment that
order must have been sent, using only data received locally by then:
    edge = fair - touch - fee      (touch = best price on the taker's side as seen at that moment)
exactly as the bot decides. Markouts are measured from the price the real taker actually paid.
If the signal is real, real takers who happened to trade when edge >= threshold must have made
money on the following seconds; and since these fills did happen, the liquidity was obtainable.

usage: signal_on_real_takers.py REC_DIR [send_lead_ms=400] [fast_ms=250] [min_start] [max_start]
"""
import bisect
import collections
import json
import math
import os
import sys

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from polybot.book import UpBook                       # noqa: E402
from polybot.engine import fee_c                      # noqa: E402
from polybot.fairvalue import FairValue, SpotSeries   # noqa: E402
from polybot.replay import list_markets, merged       # noqa: E402
from polybot.strategy import Fair                     # noqa: E402

rec = sys.argv[1]
LEAD = int(sys.argv[2]) if len(sys.argv) > 2 else 400
FAST = int(sys.argv[3]) if len(sys.argv) > 3 else 250
LO = int(sys.argv[4]) if len(sys.argv) > 4 else 0
HI = int(sys.argv[5]) if len(sys.argv) > 5 else 1 << 62
outcomes = json.load(open(os.path.join(rec, "outcomes.json")))
GRID = 50
H = (1000, 5000, 30000)
EDGES = [-1e9, -2, 0, 1, 2, 4, 1e9]
LABELS = ["<-2c", "-2..0", "0..1", "1..2", "2..4", ">=4c"]
acc = collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])   # n, shares, m1, m5, m30, fee, settle, settle_sh
per_mkt = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0]))  # bucket -> market -> [m5 - fee dollars, shares]
used = 0
for start, path in list_markets(rec):
    if not (LO <= start < HI) or str(start) not in outcomes:
        continue
    spot = SpotSeries(); fv = FairValue(spot, start); book = UpBook(); fair = Fair(1.0)
    gt, gmid, gfair, gbb, gba = [], [], [], [], []
    trades = []
    min_lag = None
    nxt, end_ms = start * 1000, (start + 300) * 1000
    for e in merged(rec, start, path):
        t = e[1]
        while t >= nxt and nxt <= end_ms:
            if book.ready and spot.ready(start):
                bb, ba = book.best_bid(), book.best_ask()
                comp = fv.components(nxt)
                if comp is not None and comp[1] > 0 and 0 < bb < ba < 100:
                    f, _ = fair.update(nxt, comp, bb, ba, spot.move(nxt, FAST), fv.sens(nxt))
                    gt.append(nxt); gmid.append((bb + ba) / 2.0); gfair.append(f); gbb.append(bb); gba.append(ba)
            nxt += GRID
        k = e[0]
        if k == "S":
            spot.update(t, e[2]); continue
        srv = e[-1]
        if srv:
            lag = t - srv
            min_lag = lag if min_lag is None or lag < min_lag else min_lag
        tau = srv + min_lag if srv else t
        if k == "B":
            book.snapshot(e[2], e[3])
        elif k == "P":
            if book.ready:
                book.change(e[2], e[3], e[4])
        elif k == "T":
            trades.append((tau, e[2], e[3], e[4]))
    if len(gt) < 1000:
        continue
    used += 1
    w = outcomes[str(start)]
    at = lambda tt: bisect.bisect_right(gt, tt) - 1
    for tau, side, px, sz in trades:
        el = (tau - start * 1000) / 1000
        if not (5 <= el <= 235) or sz <= 0:
            continue
        i0 = at(tau - LEAD)
        if i0 < 0 or gt[i0] < tau - LEAD - 300:
            continue
        mid0 = gmid[i0]
        if not 12 <= mid0 <= 88:
            continue
        sign = 1 if side == "B" else -1
        # the bot only takes at the touch it saw: skip real trades at a worse price than that
        if (side == "B" and px > gba[i0]) or (side == "S" and px < gbb[i0]):
            continue
        fee = fee_c(px)
        # the bot's decision rule uses the touch it sees when deciding, not the price obtained later
        touch = gba[i0] if side == "B" else gbb[i0]
        edge = sign * (gfair[i0] - touch) - fee_c(touch)
        outs = []
        for h in H:
            j = at(tau + h)
            outs.append(sign * (gmid[j] - px) if j >= 0 and gt[j] >= tau + h - 300 else None)
        if None in outs:
            continue
        b = next(i for i in range(len(EDGES) - 1) if EDGES[i] <= edge < EDGES[i + 1])
        a = acc[b]
        a[0] += 1; a[1] += sz; a[2] += sz * outs[0]; a[3] += sz * outs[1]; a[4] += sz * outs[2]; a[5] += sz * fee
        a[6] += sz * sign * ((100.0 if w == 0 else 0.0) - px); a[7] += sz
        pm = per_mkt[b][start]; pm[0] += sz * (outs[1] - fee) / 100.0; pm[1] += sz
print(f"markets: {used}; real taker fills between 5 s and 235 s; signal evaluated {LEAD} ms before the match, fast window {FAST} ms")
print("taker markout in cents per share vs the book mid after the fill; net5 = +5s markout minus the fee actually due")
print(f"{'signal edge':>11}{'fills':>8}{'shares':>10}{'share%':>8}{'+1s':>7}{'+5s':>7}{'+30s':>7}{'fee':>6}{'net5':>7}{'t':>6}{'settle':>8}")
tot = sum(a[1] for a in acc.values()) or 1
for b in range(len(LABELS)):
    a = acc[b]
    if a[1] <= 0:
        continue
    d = per_mkt[b]
    net = 100 * sum(v[0] for v in d.values()) / a[1]
    se = 100 * math.sqrt(sum((v[0] - net / 100 * v[1]) ** 2 for v in d.values())) / a[1] if len(d) > 2 else 0.0
    print(f"{LABELS[b]:>11}{a[0]:8d}{a[1]:10.0f}{100*a[1]/tot:8.1f}{a[2]/a[1]:7.2f}{a[3]/a[1]:7.2f}{a[4]/a[1]:7.2f}{a[5]/a[1]:6.2f}"
          f"{net:7.2f}{(net/se if se else 0):6.1f}{a[6]/a[7]:8.2f}")
