"""Lead-lag between spot and the Polymarket book on recorded ticks.

x = (spot move over the last `lookback` ms) x model delta, in cents
y = move of the book mid over the next `lead` ms
Both are observed on the local receive clock, sampled every 300 ms.

usage: leadlag.py REC_DIR
"""
import bisect
import math
import sys
from statistics import NormalDist

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from polybot.book import UpBook                       # noqa: E402
from polybot.fairvalue import FairValue, SpotSeries   # noqa: E402
from polybot.replay import list_markets, merged       # noqa: E402

rec = sys.argv[1]
LBS, LEADS = (250, 500, 1000, 2000), (300, 1000, 3000)
nd = NormalDist()
stats, used = {}, {}
for source in ("coinbase", "binance"):
    for start, path in list_markets(rec):
        spot = SpotSeries(); fv = FairValue(spot, start); book = UpBook()
        st, sp, gt, gm, gx = [], [], [], [], []
        nxt = start * 1000 + 5000
        for e in merged(rec, start, path, source=source):
            t = e[1]
            while t >= nxt and nxt < (start + 285) * 1000:
                if book.ready and spot.ready(start) and st:
                    bb, ba = book.best_bid(), book.best_ask()
                    comp = fv.components(nxt)
                    if comp and comp[1] > 0 and 15 < bb < ba < 85:
                        mid = (bb + ba) / 2
                        delta = 100 * nd.pdf(nd.inv_cdf(mid / 100)) / comp[1] * fv.sens(nxt)
                        row = {}
                        for lb in LBS:
                            i = bisect.bisect_right(st, nxt - lb) - 1
                            row[lb] = (sp[-1] - sp[i]) * delta if i >= 0 else 0.0
                        gt.append(nxt); gm.append(mid); gx.append(row)
                nxt += 100
            if e[0] == "S":
                spot.update(t, e[2]); st.append(t); sp.append(e[2])
            elif e[0] == "B":
                book.snapshot(e[2], e[3])
            elif e[0] == "P" and book.ready:
                book.change(e[2], e[3], e[4])
        if len(gt) > 500:
            used[source] = used.get(source, 0) + 1
        for lb in LBS:
            for lead in LEADS:
                a = stats.setdefault((source, lb, lead), [0, 0.0, 0.0, 0.0, 0.0, 0.0])
                for i in range(0, len(gt), 3):
                    j = bisect.bisect_left(gt, gt[i] + lead)
                    if j >= len(gt) or gt[j] - gt[i] > lead + 200:
                        continue
                    x, y = gx[i][lb], gm[j] - gm[i]
                    a[0] += 1; a[1] += x; a[2] += y; a[3] += x * x; a[4] += y * y; a[5] += x * y
print("markets used:", used)
print(f"{'source':>9}{'lookback':>9}{'lead':>6}{'n':>8}{'corr':>7}{'beta':>7}{'sd(x) c':>9}")
for (src, lb, ld), (n, sx, sy, sxx, syy, sxy) in sorted(stats.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
    cov = sxy / n - sx * sy / n / n; vx = sxx / n - (sx / n) ** 2; vy = syy / n - (sy / n) ** 2
    print(f"{src:>9}{lb:9d}{ld:6d}{n:8d}{cov / math.sqrt(vx * vy):7.3f}{cov / vx:7.3f}{math.sqrt(vx):9.2f}")
