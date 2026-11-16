"""H10M: historical markout of the simulated worst-case fills (H10C, quote at the best prices).

markout(h) per contract = (mid at fill + h) - fill price for a bid fill, fill price - mid(t + h) for an ask fill.
usage: h10_markout.py SCRATCH_H10_DIR
"""
import bisect
import json
import statistics
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import h10_backtest as hb  # noqa: E402
import h10_groups as g  # noqa: E402

d = sys.argv[1]
hb.CAP_MULT = 3.0
port = json.load(open(f"{d}/bt/portfolio.json"))["best"]
for name, (sub, t0, tsplit, t1, _) in g.PERIODS.items():
    hb.T_START, hb.T_SPLIT, hb.T_END = t0, tsplit, t1
    hist = json.load(open(f"{d}/{sub}/history.json"))
    res = {"stable": {60: [], 600: [], 3600: []}, "news": {60: [], 600: [], 3600: []}}
    for p in port:
        h = hist.get(p["cond"])
        if not h or not h["mids"]:
            continue
        log = []
        hb.simulate(p, h, 1, log)
        mids = sorted(tuple(x) for x in h["mids"])
        tt = [x[0] for x in mids]
        grp = "news" if g.topic_news(p["q"]) else "stable"
        for part, ts, q, px in log:
            if part != "hold":
                continue
            for hz in (60, 600, 3600):
                i = bisect.bisect_left(tt, ts + hz)
                if i < len(mids):
                    m = mids[i][1]
                    res[grp][hz].append(((m - px) if q > 0 else (px - m), abs(q)))
    print(name)
    for grp, r in res.items():
        line = f"  {grp:<7}"
        for hz, xs in r.items():
            if xs:
                w = sum(q for _, q in xs)
                line += f" | {hz:>4}s: {100 * sum(m * q for m, q in xs) / w:6.2f} c/contract ({len(xs)} fills)"
        print(line)
