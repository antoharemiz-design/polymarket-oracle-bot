"""Run several simulator configurations over all recorded markets, in parallel.

usage: sweep.py REC_DIR [min_start] [max_start]
Each line of CONFIGS: label, mode, latency ms, queue multiplier, strategy overrides, taker theta.
"""
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from polybot.replay import list_markets          # noqa: E402
from polybot.sim import HEADER, fmt, run_market, summarize  # noqa: E402
from polybot.strategy import Params              # noqa: E402

BASE = dict(clip=10.0, max_net=50.0, max_gross=400.0)
MM = dict(edge=2.0, edge_cancel=1.0)
CONFIGS = [
    # --- hypothesis 3: passive market making ---------------------------------------------------
    ("mm book-mid only e=1",      "mid",   150, 1.0, dict(edge=1.0, edge_cancel=0.5), 0),
    ("mm book-mid only e=2",      "mid",   150, 1.0, MM, 0),
    ("mm fast e=1",               "maker", 150, 1.0, dict(edge=1.0, edge_cancel=0.5), 0),
    ("mm fast e=2",               "maker", 150, 1.0, MM, 0),
    ("mm fast e=3",               "maker", 150, 1.0, dict(edge=3.0, edge_cancel=1.5), 0),
    ("mm fast e=2 win=250ms",     "maker", 150, 1.0, dict(MM, fast_ms=250), 0),
    ("mm fast e=2 win=1000ms",    "maker", 150, 1.0, dict(MM, fast_ms=1000), 0),
    ("mm fast e=2 k=1.5",         "maker", 150, 1.0, dict(MM, fast_k=1.5), 0),
    ("mm fast e=2 first 120s",    "maker", 150, 1.0, dict(MM, t_off=120.0), 0),
    ("mm fast e=2 net<=20",       "maker", 150, 1.0, dict(MM, max_net=20.0), 0),
    # sensitivity: latency and competition
    ("mm fast e=2 lat=200",       "maker", 200, 1.0, MM, 0),
    ("mm fast e=2 lat=250",       "maker", 250, 1.0, MM, 0),
    ("mm fast e=2 lat=350",       "maker", 350, 1.0, MM, 0),
    ("mm fast e=2 queue x2",      "maker", 150, 2.0, MM, 0),
    ("mm fast e=2 queue x4",      "maker", 150, 4.0, MM, 0),
    # --- hypothesis 2: taking stale quotes on the fast spot signal ------------------------------
    ("taker th=1c",               "taker", 150, 1.0, dict(), 1.0),
    ("taker th=2c",               "taker", 150, 1.0, dict(), 2.0),
    ("taker th=3c",               "taker", 150, 1.0, dict(), 3.0),
    ("taker th=2c win=250ms",     "taker", 150, 1.0, dict(fast_ms=250), 2.0),
    ("taker th=2c win=1000ms",    "taker", 150, 1.0, dict(fast_ms=1000), 2.0),
    ("taker th=2c lat=200",       "taker", 200, 1.0, dict(), 2.0),
    ("taker th=2c lat=250",       "taker", 250, 1.0, dict(), 2.0),
    ("taker th=2c lat=350",       "taker", 350, 1.0, dict(), 2.0),
    ("taker th=2c lat=600",       "taker", 600, 1.0, dict(), 2.0),
]


def job(args):
    rec, start, path, cfg, outcome = args
    label, mode, lat, qm, over, theta = cfg
    p = Params(**{**BASE, **over})
    return label, run_market(rec, start, path, p, mode, lat, qm, outcome, taker_theta=theta)


def main():
    rec = sys.argv[1]
    lo = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    hi = int(sys.argv[3]) if len(sys.argv) > 3 else 1 << 62
    op = os.path.join(rec, "outcomes.json")
    outcomes = json.load(open(op)) if os.path.exists(op) else {}
    markets = [(s, p) for s, p in list_markets(rec) if lo <= s < hi and str(s) in outcomes]
    jobs = [(rec, s, p, cfg, outcomes[str(s)]) for cfg in CONFIGS for s, p in markets]
    res = {c[0]: [] for c in CONFIGS}
    with ProcessPoolExecutor(max_workers=3) as ex:
        for label, r in ex.map(job, jobs, chunksize=2):
            res[label].append(r)
    print(f"{len(markets)} markets with actual outcomes, {markets[0][0]}..{markets[-1][0]}")
    print(HEADER)
    for c in CONFIGS:
        print(fmt(summarize(sorted(res[c[0]], key=lambda r: r.start), c[0])))


if __name__ == "__main__":
    main()
