"""In-sample vs out-of-sample evaluation of a fixed list of configurations.

The configurations were chosen on markets that started before SPLIT; markets from SPLIT on were
recorded afterwards and were not looked at while choosing. Edge criterion: 5 s markout minus the
taker fee (taker) or 5 s markout (maker) must be positive with t > 2 out of sample.

usage: validate.py REC_DIR SPLIT_EPOCH
"""
import json
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from polybot.replay import list_markets   # noqa: E402
from polybot.sim import run_market, summarize  # noqa: E402
from polybot.strategy import Params       # noqa: E402

BASE = dict(clip=10.0, max_net=50.0, max_gross=400.0)
CONFIGS = [
    # label, mode, latency, queue mult, overrides, theta
    ("taker win=250 th=1 (primary)", "taker", 150, 1.0, dict(fast_ms=250), 1.0),
    ("taker win=250 th=2",           "taker", 150, 1.0, dict(fast_ms=250), 2.0),
    ("taker win=250 th=3",           "taker", 150, 1.0, dict(fast_ms=250), 3.0),
    ("taker win=250 th=1 lat=200",   "taker", 200, 1.0, dict(fast_ms=250), 1.0),
    ("taker win=250 th=1 lat=250",   "taker", 250, 1.0, dict(fast_ms=250), 1.0),
    ("taker win=250 th=1 lat=350",   "taker", 350, 1.0, dict(fast_ms=250), 1.0),
    ("taker win=500 th=2",           "taker", 150, 1.0, dict(fast_ms=500), 2.0),
    ("maker fast e=2",               "maker", 150, 1.0, dict(fast_ms=500, edge=2.0, edge_cancel=1.0), 0),
    ("maker fast e=2 queue x2",      "maker", 150, 2.0, dict(fast_ms=500, edge=2.0, edge_cancel=1.0), 0),
    ("maker fast e=2 lat=350",       "maker", 350, 1.0, dict(fast_ms=500, edge=2.0, edge_cancel=1.0), 0),
    ("maker book-mid only e=2",      "mid",   150, 1.0, dict(edge=2.0, edge_cancel=1.0), 0),
]


def job(a):
    rec, s, path, cfg, outcome = a
    label, mode, lat, qm, over, theta = cfg
    return label, s, run_market(rec, s, path, Params(**{**BASE, **over}), mode, lat, qm, outcome, taker_theta=theta)


def main():
    rec, split = sys.argv[1], int(sys.argv[2])
    outcomes = json.load(open(os.path.join(rec, "outcomes.json")))
    markets = [(s, p) for s, p in list_markets(rec) if str(s) in outcomes]
    jobs = [(rec, s, p, c, outcomes[str(s)]) for c in CONFIGS for s, p in markets]
    res = {c[0]: {"in": [], "out": []} for c in CONFIGS}
    with ProcessPoolExecutor(max_workers=3) as ex:
        for label, s, r in ex.map(job, jobs, chunksize=2):
            res[label]["out" if s >= split else "in"].append(r)
    n_in = sum(1 for s, _ in markets if s < split); n_out = len(markets) - n_in
    print(f"markets: {n_in} before the split (used to choose), {n_out} after (not used)")
    hdr = f"{'fills':>6}{'sh':>6}{'net5':>7}{'t':>6}{'noReb$':>8}{'t':>6}{'reb$':>7}{'pnl$':>8}{'t':>6}{'DD$':>7}"
    print(f"{'configuration':<30}| {'chosen on':^66}| {'independent check':^66}")
    print(f"{'':<30}| {hdr} | {hdr}")
    proxies = 0
    for c in CONFIGS:
        row = f"{c[0]:<30}"
        for part in ("in", "out"):
            s = summarize(sorted(res[c[0]][part], key=lambda r: r.start))
            if not s.get("markets") or not s["shares"]:
                row += "| " + " " * 66; continue
            net = s["markout5_per_share_c"] - s["fee_per_share_c"]
            t = net / s["markout5_se_c"] if s["markout5_se_c"] else 0.0
            row += (f"| {s['fills']:6d}{s['shares']:6.0f}{net:7.2f}{t:6.1f}{s['pnl_ex_rebate']:8.2f}{s['tstat_ex_rebate']:6.1f}"
                    f"{s['rebate']:7.2f}{s['pnl']:8.2f}{s['tstat']:6.1f}{s['max_dd']:7.1f} ")
            proxies += s["proxy_outcomes"]
        print(row)
    print("net5 = markout vs book mid 5 s after the fill minus taker fee, cents/share; noReb$ = held to resolution, after fees,")
    print("without maker rebates; pnl$ = with rebates (20% of own maker fee-equivalent); t = per-market t-statistic")
    print(f"markets settled on the spot proxy instead of the real resolution: {proxies}")


if __name__ == "__main__":
    main()
