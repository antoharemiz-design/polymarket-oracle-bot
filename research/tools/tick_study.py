"""Microstructure study on recorded ticks.

1. Lead-lag: does (spot-model fair value - book mid) predict the next move of the book mid?
2. Maker markouts: profit per share of the resting side of every trade at several horizons.
3. Guard value: markouts split by the model edge the maker had `lat` ms before the trade.

usage: tick_study.py REC_DIR [lat_ms=150] [skip_first_n=0]
"""
import bisect
import collections
import sys

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from polybot.book import UpBook            # noqa: E402
from polybot.fairvalue import FairValue, SpotSeries  # noqa: E402
from polybot.replay import list_markets, merged      # noqa: E402

rec = sys.argv[1]
LAT = int(sys.argv[2]) if len(sys.argv) > 2 else 150
SKIP = int(sys.argv[3]) if len(sys.argv) > 3 else 0
GRID = 100
H = (1000, 5000, 30000)

ll = collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0])   # horizon -> n, sx, sy, sxx, sxy
mk = collections.defaultdict(lambda: [0.0] + [0.0] * (len(H) + 1))  # bucket -> shares, sum markouts..., sum spread
used = 0
for start, path in list_markets(rec)[SKIP:]:
    spot = SpotSeries(); fv = FairValue(spot, start); book = UpBook()
    grid_t, grid_mid, grid_p = [], [], []
    trades = []
    nxt = start * 1000
    end_ms = (start + 300) * 1000
    for e in merged(rec, start, path):
        t = e[1]
        while t >= nxt and nxt <= end_ms:          # sample state on the grid before applying the event
            if book.ready and spot.ready(start):
                bb, ba = book.best_bid(), book.best_ask()
                p = fv.p_up(nxt)
                if p is not None and 0 < bb < ba < 100:
                    grid_t.append(nxt); grid_mid.append((bb + ba) / 2.0); grid_p.append(100 * p)
            nxt += GRID
        k = e[0]
        if k == "S":
            spot.update(t, e[2])
        elif k == "B":
            book.snapshot(e[2], e[3])
        elif k == "P":
            if book.ready:
                book.change(e[2], e[3], e[4])
        elif k == "T" and start * 1000 <= t < end_ms:
            trades.append((t, e[2], e[3], e[4]))
    if len(grid_t) < 1000:
        continue
    used += 1
    at = lambda tt: bisect.bisect_right(grid_t, tt) - 1
    # 1. lead-lag on the grid, sampled once per second
    for i in range(0, len(grid_t), 10):
        x = grid_p[i] - grid_mid[i]
        if abs(grid_mid[i] - 50) > 40:
            continue
        for h in H:
            j = at(grid_t[i] + h)
            if j > i and grid_t[j] - grid_t[i] >= h - GRID:
                y = grid_mid[j] - grid_mid[i]
                a = ll[h]; a[0] += 1; a[1] += x; a[2] += y; a[3] += x * x; a[4] += x * y
    # 2/3. markouts for the resting side of each trade
    for t, side, px, sz in trades:
        i0 = at(t - LAT)
        if i0 < 0 or sz <= 0 or not 5 <= px <= 95:
            continue
        sign = 1 if side == "B" else -1            # taker bought Up at px -> maker sold Up at px
        edge = sign * (px - grid_p[i0])            # maker's model edge LAT ms before the trade, cents
        spread = sign * (px - grid_mid[i0])
        b = "edge<-2c" if edge < -2 else "-2..0c" if edge < 0 else "0..2c" if edge < 2 else "edge>2c"
        outs = []
        for h in H:
            j = at(t + h)
            if j < 0 or grid_t[j] < t + h - 2 * GRID:
                outs = None; break
            outs.append(sign * (px - grid_mid[j]))
        if outs is None:
            continue
        for key in (b, "ALL"):
            a = mk[key]; a[0] += sz
            for n, o in enumerate(outs):
                a[1 + n] += sz * o
            a[1 + len(H)] += sz * spread

print(f"markets used: {used}; latency assumed for the maker's information: {LAT} ms")
print("\n1) lead-lag: future change of book mid (cents) regressed on (model fair - mid) now")
for h in H:
    n, sx, sy, sxx, sxy = ll[h]
    if n > 10:
        beta = (sxy - sx * sy / n) / (sxx - sx * sx / n)
        vx = sxx / n - (sx / n) ** 2
        print(f"   horizon {h/1000:4.0f}s: n={n:6d} beta={beta:5.2f}  (model-mid gap stdev {vx ** 0.5:.2f}c)")
print("\n2) maker markout per share in cents (positive = resting side made money), by model edge before the trade")
print(f"{'bucket':>10}{'shares':>10}{'share%':>8}{'spread':>8}" + "".join(f"{f'+{h//1000}s':>8}" for h in H))
tot = mk["ALL"][0] or 1
for key in ("edge<-2c", "-2..0c", "0..2c", "edge>2c", "ALL"):
    a = mk[key]
    if a[0] > 0:
        print(f"{key:>10}{a[0]:10.0f}{100*a[0]/tot:8.1f}{a[1+len(H)]/a[0]:8.2f}" + "".join(f"{a[1+n]/a[0]:8.2f}" for n in range(len(H))))
