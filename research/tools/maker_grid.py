"""Step 2 grid: maker inventory policy (skew, directional lean, hold vs intraday close).

Each market is parsed once and every configuration is simulated on the same event list.
Selection is made on markets before SPLIT; markets from SPLIT on are the independent check.
PnL is reported without rebates (noReb), with rebates (pnl) and per share; t is per market.

usage: maker_grid.py REC_DIR SPLIT_EPOCH [workers=4] [--set NAME] [--dump FILE] [--real-fills]
  --real-fills fill model driven by the real maker fills of each transaction (polybot/realfills.py)
  --set NAME   configuration set: "step2" (default) or "momentum" (neighbourhood of the momentum lean)
  --dump FILE  write per-market results {label: {start: [pnl, pnl_ex_rebate, shares]}} for paired tests
"""
import json
import math
import os
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from polybot.realfills import real_events, trades_of   # noqa: E402
from polybot.replay import list_markets, merged   # noqa: E402
from polybot.sim import run_market, summarize     # noqa: E402
from polybot.strategy import Params               # noqa: E402

BASE = dict(clip=10.0, max_net=50.0, max_gross=400.0, fast_ms=500, edge=2.0, edge_cancel=1.0)
S1 = dict(BASE, skew=1.0)
CONFIGS = [
    # label, latency ms, queue multiplier, overrides
    # A. inventory skew (cents of fair shift per clip of net inventory)
    ("skew 0",                 150, 1.0, dict(BASE, skew=0.0)),
    ("skew 0.5 (step-1 base)", 150, 1.0, dict(BASE, skew=0.5)),
    ("skew 1",                 150, 1.0, S1),
    ("skew 2",                 150, 1.0, dict(BASE, skew=2.0)),
    ("skew 4",                 150, 1.0, dict(BASE, skew=4.0)),
    ("skew 2 net<=100",        150, 1.0, dict(BASE, skew=2.0, max_net=100.0)),
    ("skew 1 net<=20",         150, 1.0, dict(S1, max_net=20.0)),
    # B. directional lean on the book mid (negative weight = fade the move)
    ("skew 1 fade mom60 0.2",  150, 1.0, dict(S1, mom_s=60, mom_w=-0.2)),
    ("skew 1 follow mom60 0.2", 150, 1.0, dict(S1, mom_s=60, mom_w=0.2)),
    ("skew 1 fade mom30 0.2",  150, 1.0, dict(S1, mom_s=30, mom_w=-0.2)),
    ("skew 1 fade CCI 1c",     150, 1.0, dict(S1, cci_w=-1.0)),
    ("skew 1 follow CCI 1c",   150, 1.0, dict(S1, cci_w=1.0)),
    # C. quoting distance
    ("skew 1 edge 1",          150, 1.0, dict(S1, edge=1.0, edge_cancel=0.5)),
    ("skew 1 edge 3",          150, 1.0, dict(S1, edge=3.0, edge_cancel=1.5)),
    ("skew 1 edge 4",          150, 1.0, dict(S1, edge=4.0, edge_cancel=2.0)),
    # D. hold to settlement vs intraday close (buy the short side as a taker, fee paid)
    ("skew 1 close at 240s",   150, 1.0, dict(S1, t_flat=240.0)),
    ("skew 1 close at 270s",   150, 1.0, dict(S1, t_flat=270.0)),
    ("skew 1 close at 180s",   150, 1.0, dict(S1, t_off=175.0, t_flat=180.0)),
]


MS1 = dict(S1, mom_s=60, mom_w=0.2)
MOMENTUM = [
    ("skew 1 (no lean)",        150, 1.0, S1),
    ("skew 1 net<=20",          150, 1.0, dict(S1, max_net=20.0)),
    ("follow mom60 w0.1",       150, 1.0, dict(MS1, mom_w=0.1)),
    ("follow mom60 w0.2",       150, 1.0, MS1),
    ("follow mom60 w0.3",       150, 1.0, dict(MS1, mom_w=0.3)),
    ("follow mom60 w0.5",       150, 1.0, dict(MS1, mom_w=0.5)),
    ("follow mom30 w0.2",       150, 1.0, dict(MS1, mom_s=30)),
    ("follow mom120 w0.2",      150, 1.0, dict(MS1, mom_s=120)),
    ("follow mom60 w0.2 skew2", 150, 1.0, dict(MS1, skew=2.0)),
    ("follow mom60 w0.2 net20", 150, 1.0, dict(MS1, max_net=20.0)),
    ("follow mom60 w0.2 q x2",  150, 2.0, MS1),
    ("follow mom60 w0.2 lat250", 250, 1.0, MS1),
    ("skew 1 close at 240s",    150, 1.0, dict(S1, t_flat=240.0)),
    ("follow mom60 close 240",  150, 1.0, dict(MS1, t_flat=240.0)),
]
SETS = {"step2": CONFIGS, "momentum": MOMENTUM}


REAL = False


def one_market(args):
    rec, start, path, outcome, configs, real = args
    events = list(merged(rec, start, path, 300, source="coinbase"))
    if real:
        events = real_events(events, rec, start)
    out = []
    for label, lat, qm, over in configs:
        r = run_market(rec, start, path, Params(**over), "maker", lat, qm, outcome, events=events)
        out.append((label, r))
    return start, out


def line(label, rs):
    s = summarize(sorted(rs, key=lambda r: r.start), label)
    if not s.get("markets") or not s["shares"]:
        return " " * 64
    inv = statistics.mean(abs(r.q_up - r.q_dn) for r in rs)
    return (f"{s['fills']:6d}{s['shares']:7.0f}{s['pnl_ex_rebate']:9.2f}{s['tstat_ex_rebate']:6.1f}{s['rebate']:7.2f}"
            f"{s['pnl']:9.2f}{s['tstat']:6.1f}{s['pnl_per_share_c']:7.2f}{inv:6.0f}")


def main():
    args = sys.argv[1:]
    dump = None
    configs = CONFIGS
    if "--dump" in args:
        i = args.index("--dump"); dump = args[i + 1]; del args[i:i + 2]
    real = "--real-fills" in args
    if real:
        args.remove("--real-fills")
    if "--set" in args:
        i = args.index("--set"); configs = SETS[args[i + 1]]; del args[i:i + 2]
    rec, split = args[0], int(args[1])
    workers = int(args[2]) if len(args) > 2 else 4
    outcomes = json.load(open(os.path.join(rec, "outcomes.json")))
    markets = [(s, p) for s, p in list_markets(rec) if str(s) in outcomes]
    res = {c[0]: {"in": [], "out": []} for c in configs}
    with ProcessPoolExecutor(max_workers=workers) as ex:
        if real:
            for s, _ in markets:
                trades_of(rec, s)
        for start, out in ex.map(one_market, [(rec, s, p, outcomes[str(s)], configs, real) for s, p in markets]):
            for label, r in out:
                res[label]["out" if start >= split else "in"].append(r)
    n_in = sum(1 for s, _ in markets if s < split)
    print(f"markets: {n_in} for selection, {len(markets) - n_in} independent; all settled on real resolutions; "
          f"fill model: {'real maker fills (FIFO)' if real else 'trade feed'}")
    hdr = f"{'fills':>6}{'sh':>7}{'noReb$':>9}{'t':>6}{'reb$':>7}{'pnl$':>9}{'t':>6}{'c/sh':>7}{'|net|':>6}"
    print(f"{'configuration':<26}| {'selection sample':^64}| {'independent sample':^64}")
    print(f"{'':<26}| {hdr}| {hdr}")
    for c in configs:
        print(f"{c[0]:<26}| {line(c[0], res[c[0]]['in'])}| {line(c[0], res[c[0]]['out'])}")
    if dump:
        json.dump({c[0]: {r.start: [r.pnl, r.pnl_ex_rebate, r.q_up + r.q_dn]
                          for part in ("in", "out") for r in res[c[0]][part]} for c in configs}, open(dump, "w"))
    proxies = sum(1 for c in configs for part in ("in", "out") for r in res[c[0]][part] if r.outcome_src != "actual")
    print("noReb$ = PnL to resolution after fees, without rebates; pnl$ = with maker rebates; t = per-market t-statistic;")
    print(f"c/sh = pnl$ per share; |net| = mean |Up - Down| shares left at settlement. Proxy-settled results: {proxies}")


if __name__ == "__main__":
    main()
