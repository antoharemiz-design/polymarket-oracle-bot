"""Step 3: execution calibration for the pre-registered maker candidate and its control.

1. Stress grid: queue x1/x2/x3, competition haircuts (uniform and good-fills-only), latency,
   intraday close with walk-the-book vs holding. PnL with and without rebates, per sample.
2. Competition at the moment of our simulated fills: queue ahead when we joined, whether the
   trade went through our price, and the markout of the fill by those buckets.

usage: exec_calibration.py REC_DIR SPLIT_EPOCH [FRESH_EPOCH] [workers=4] [--dump FILE] [--join]
       --join  run the join-only variants (quote only at prices where shares already rest)
       markets >= FRESH_EPOCH form the pre-registered fresh sample (reported separately)
"""
import bisect
import collections
import json
import math
import os
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from polybot.replay import list_markets, merged   # noqa: E402
from polybot.sim import run_market, summarize     # noqa: E402
from polybot.strategy import Params               # noqa: E402

CONTROL = dict(clip=10.0, max_net=50.0, max_gross=400.0, fast_ms=500, edge=2.0, edge_cancel=1.0, skew=1.0)
CAND = dict(CONTROL, mom_s=60, mom_w=0.2)
VARIANTS = [
    # label, latency, queue mult, fill_share, share_good, extra overrides
    ("base",                    150, 1.0, 1.0, 1.0, {}),
    ("queue x2",                150, 2.0, 1.0, 1.0, {}),
    ("queue x3",                150, 3.0, 1.0, 1.0, {}),
    ("fill share 0.5",          150, 1.0, 0.5, 1.0, {}),
    ("good fills 0.5",          150, 1.0, 1.0, 0.5, {}),
    ("queue x2 + good 0.5",     150, 2.0, 1.0, 0.5, {}),
    ("queue x3 + good 0.25",    150, 3.0, 1.0, 0.25, {}),
    ("latency 250",             250, 1.0, 1.0, 1.0, {}),
    ("latency 350",             350, 1.0, 1.0, 1.0, {}),
    ("close 240s, walk 3c",     150, 1.0, 1.0, 1.0, dict(t_flat=240.0, flat_slip=3)),
]
JOIN = [
    ("join>=1",                 150, 1.0, 1.0, 1.0, dict(join_min=1.0)),
    ("join>=20",                150, 1.0, 1.0, 1.0, dict(join_min=20.0)),
    ("join>=20 queue x3",       150, 3.0, 1.0, 1.0, dict(join_min=20.0)),
    ("join>=20 queue x2 good 0.5", 150, 2.0, 1.0, 0.5, dict(join_min=20.0)),
    ("join>=20 queue x3 good 0.25", 150, 3.0, 1.0, 0.25, dict(join_min=20.0)),
    ("join>=20 latency 350",    350, 1.0, 1.0, 1.0, dict(join_min=20.0)),
]
STRATS = [("candidate", CAND), ("control", CONTROL)]


def one_market(args):
    rec, start, path, outcome = args
    events = list(merged(rec, start, path, 300, source="coinbase"))
    out, meta = [], []
    for sname, sp in STRATS:
        for vlabel, lat, qm, fs, sg, extra in VARIANTS:
            r = run_market(rec, start, path, Params(**{**sp, **extra}), "maker", lat, qm, outcome,
                           events=events, fill_share=fs, share_good=sg, keep_session=(vlabel == "base"))
            if vlabel == "base":
                ses = r.session
                gt, gm = ses.grid_t, ses.grid_mid
                for t, side, price, got, q0, qb, tsz, through, edge in ses.ex.fill_meta:
                    sign = 1 if side == "B" else -1
                    mo = []
                    for h in (5000, 60000):
                        j = bisect.bisect_right(gt, t + h) - 1
                        mo.append(sign * (gm[j] - price) if j >= 0 and gt[j] >= t + h - 300 else None)
                    paid = price if side == "B" else 100 - price
                    won = (outcome == 0) == (side == "B")
                    meta.append((sname, start, got, q0, tsz, through, edge, mo[0], mo[1], (100.0 if won else 0.0) - paid))
                stale = ses.ex.stale_trades
                del r.session
                out.append((sname, vlabel, r, stale))
            else:
                out.append((sname, vlabel, r, 0))
    return start, out, meta


def tstat(xs):
    if len(xs) < 3:
        return 0.0
    m = statistics.mean(xs); s = statistics.stdev(xs)
    return m / s * math.sqrt(len(xs)) if s else 0.0


def main():
    args = sys.argv[1:]
    dump = None
    if "--dump" in args:
        i = args.index("--dump"); dump = args[i + 1]; del args[i:i + 2]
    if "--join" in args:
        args.remove("--join")
        VARIANTS[:] = [VARIANTS[0]] + JOIN
    rec, split = args[0], int(args[1])
    fresh = int(args[2]) if len(args) > 2 else 1 << 62
    workers = int(args[3]) if len(args) > 3 else 4
    outcomes = json.load(open(os.path.join(rec, "outcomes.json")))
    markets = [(s, p) for s, p in list_markets(rec) if str(s) in outcomes]
    part = lambda s: "fresh" if s >= fresh else ("indep" if s >= split else "sel")
    res = collections.defaultdict(list)          # (strategy, variant, part) -> results
    per = collections.defaultdict(dict)          # (strategy, variant) -> {start: pnl}
    meta, stale = [], collections.Counter()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for start, out, m in ex.map(one_market, [(rec, s, p, outcomes[str(s)]) for s, p in markets]):
            for sname, vlabel, r, st in out:
                res[(sname, vlabel, part(start))].append(r)
                res[(sname, vlabel, "all")].append(r)
                per[(sname, vlabel)][start] = (r.pnl, r.pnl_ex_rebate)
                stale[sname] += st
            meta += m
    parts = ["sel", "indep", "fresh", "all"]
    counts = {p: sum(1 for s, _ in markets if part(s) == p) for p in parts[:3]}
    counts["all"] = len(markets)
    print(f"markets: selection {counts['sel']}, independent {counts['indep']}, fresh (pre-registered) {counts['fresh']}; "
          f"all settled on real resolutions")
    print(f"trades ignored because they printed before our order rested (base runs): {dict(stale)}")
    print("\n1) PnL with rebates, $ (t per market); noReb = without rebates")
    hdr = "".join(f"{p + ' pnl (t)':>17}" for p in parts)
    print(f"{'strategy / variant':<38}{hdr}{'all noReb':>11}{'reb':>7}{'c/sh':>7}")
    for sname, _ in STRATS:
        for v in VARIANTS:
            row = f"{sname + ' / ' + v[0]:<38}"
            for p in parts:
                rs = res[(sname, v[0], p)]
                if not rs:
                    row += f"{'-':>17}"; continue
                xs = [r.pnl for r in rs]
                row += f"{sum(xs):>10.2f} ({tstat(xs):4.1f})"
            s = summarize(res[(sname, v[0], "all")])
            row += f"{s['pnl_ex_rebate']:>11.2f}{s['rebate']:>7.2f}{s['pnl_per_share_c']:>7.2f}"
            print(row)
    print("\n   paired: candidate minus control, $ per market (t)")
    for v in VARIANTS:
        row = f"   {v[0]:<35}"
        for p in parts:
            ks = [k for k in per[("candidate", v[0])] if (p == "all" or part(k) == p)]
            d = [per[("candidate", v[0])][k][0] - per[("control", v[0])][k][0] for k in ks]
            row += f"{(statistics.mean(d) if d else 0):>10.2f} ({tstat(d):4.1f})"
        print(row)
    print("\n2) competition at our simulated fills (base runs, both strategies), markouts in cents per share")
    print(f"{'bucket':<38}{'shares':>8}{'share%':>8}{'+5s':>7}{'+60s':>7}{'settle':>8}")
    tot = sum(m[2] for m in meta) or 1

    def show(label, rows):
        sh = sum(m[2] for m in rows)
        if sh <= 0:
            return
        f = lambda i: sum(m[2] * m[i] for m in rows if m[i] is not None) / max(sum(m[2] for m in rows if m[i] is not None), 1e-9)
        print(f"{label:<38}{sh:8.0f}{100 * sh / tot:8.1f}{f(7):7.2f}{f(8):7.2f}{f(9):8.2f}")

    show("all fills", meta)
    show("trade went through our price", [m for m in meta if m[5]])
    show("filled at our price", [m for m in meta if not m[5]])
    for lo, hi, lab in ((0, 0.01, "joined first at a new price (queue 0)"), (0.01, 50, "queue ahead 0-50"),
                        (50, 300, "queue ahead 50-300"), (300, 1e12, "queue ahead > 300")):
        show(lab, [m for m in meta if lo <= m[3] < hi])
    show("edge vs fair >= 0 at the fill (looked good)", [m for m in meta if m[6] is not None and m[6] >= 0])
    show("edge vs fair < 0 at the fill (looked toxic)", [m for m in meta if m[6] is not None and m[6] < 0])
    for sname, _ in STRATS:
        show(f"strategy: {sname}", [m for m in meta if m[0] == sname])
    if dump:
        json.dump({f"{k[0]}|{k[1]}": v for k, v in per.items()}, open(dump, "w"))


if __name__ == "__main__":
    main()
