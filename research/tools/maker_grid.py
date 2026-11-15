"""Step 2 grid: maker inventory policy (skew, directional lean, hold vs intraday close).

Each market is parsed once and every configuration is simulated on the same event list.
Selection is made on markets before SPLIT; markets from SPLIT on are the independent check.
PnL is reported without rebates (noReb), with rebates (pnl) and per share; t is per market.

usage: maker_grid.py REC_DIR SPLIT_EPOCH [workers=4] [--set NAME] [--dump FILE] [--real-fills] [--fillstats]
  --fillstats  also report resting orders, the share of them that filled and median time to first fill
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
CTL = S1
CND = MS1
SHARE = [
    ("control",                     150, 1.0, CTL),
    ("control share<50%",           150, 1.0, dict(CTL, max_share=0.50)),
    ("control share<25%",           150, 1.0, dict(CTL, max_share=0.25)),
    ("control share<10%",           150, 1.0, dict(CTL, max_share=0.10)),
    ("control share<25% keep50%",   150, 1.0, dict(CTL, max_share=0.25, keep_share=0.50)),
    ("control share<10% keep25%",   150, 1.0, dict(CTL, max_share=0.10, keep_share=0.25)),
    ("control share<25% edge1",     150, 1.0, dict(CTL, max_share=0.25, edge=1.0, edge_cancel=0.5)),
    ("control share<25% edge3",     150, 1.0, dict(CTL, max_share=0.25, edge=3.0, edge_cancel=1.5)),
    ("candidate",                   150, 1.0, CND),
    ("candidate share<25%",         150, 1.0, dict(CND, max_share=0.25)),
    ("candidate share<10%",         150, 1.0, dict(CND, max_share=0.10)),
    ("candidate share<25% keep50%", 150, 1.0, dict(CND, max_share=0.25, keep_share=0.50)),
    ("control share<25% queue x2",  150, 2.0, dict(CTL, max_share=0.25)),
    ("control share<25% lat350",    350, 1.0, dict(CTL, max_share=0.25)),
    ("candidate share<25% queue x2", 150, 2.0, dict(CND, max_share=0.25)),
    ("candidate share<25% lat350",  350, 1.0, dict(CND, max_share=0.25)),
]
AUG = 33.6     # $/day per BTC 5m market if the ended August program ($300k/month) were active
REWARDS = [
    ("control rate 0 (actual)",          150, 1.0, CTL),
    ("control rate Aug",                 150, 1.0, dict(CTL, reward_rate=AUG)),
    ("control clip50 rate 0",            150, 1.0, dict(CTL, clip=50.0, max_net=250.0, max_gross=2000.0)),
    ("control clip50 rate Aug",          150, 1.0, dict(CTL, clip=50.0, max_net=250.0, max_gross=2000.0, reward_rate=AUG)),
    ("control clip50 rate 10xAug",       150, 1.0, dict(CTL, clip=50.0, max_net=250.0, max_gross=2000.0, reward_rate=10 * AUG)),
    ("candidate clip50 rate Aug",        150, 1.0, dict(CND, clip=50.0, max_net=250.0, max_gross=2000.0, reward_rate=AUG)),
    ("candidate <25% clip50 rate Aug",   150, 1.0, dict(CND, clip=50.0, max_net=250.0, max_gross=2000.0, max_share=0.25, reward_rate=AUG)),
    ("cand <10% clip50 rate 10xAug",     150, 1.0, dict(CND, clip=50.0, max_net=250.0, max_gross=2000.0, max_share=0.10, reward_rate=10 * AUG)),
]
LAD = dict(clip=10.0, max_net=60.0, max_gross=400.0, lad_place=5.0, lad_d0=2, lad_step=3, lad_levels=4, lad_stop=240.0)
LADDER = [
    ("ladder base",                   150, 1.0, LAD, "ladder"),
    ("ladder pre-start -20s",         150, 1.0, dict(LAD, lad_place=-20.0), "ladder"),
    ("ladder near d0=1 step2 L5",     150, 1.0, dict(LAD, lad_d0=1, lad_step=2, lad_levels=5), "ladder"),
    ("ladder deep d0=5 step5",        150, 1.0, dict(LAD, lad_d0=5, lad_step=5), "ladder"),
    ("ladder rearm",                  150, 1.0, dict(LAD, lad_rearm=True), "ladder"),
    ("ladder stop 290s",              150, 1.0, dict(LAD, lad_stop=290.0), "ladder"),
    ("ladder stop 180s",              150, 1.0, dict(LAD, lad_stop=180.0), "ladder"),
    ("ladder share<25%",              150, 1.0, dict(LAD, max_share=0.25), "ladder"),
    ("ladder pre -20 share<25%",      150, 1.0, dict(LAD, lad_place=-20.0, max_share=0.25), "ladder"),
    ("ladder net<=30",                150, 1.0, dict(LAD, max_net=30.0), "ladder"),
    ("ladder pre -20 rearm",          150, 1.0, dict(LAD, lad_place=-20.0, lad_rearm=True), "ladder"),
    ("ladder base queue x2",          150, 2.0, LAD, "ladder"),
    ("ladder base lat350",            350, 1.0, LAD, "ladder"),
]
RA = dict(LAD, lad_rearm=True)
LADDER2 = [
    ("rearm base",                    150, 1.0, RA, "ladder"),
    ("rearm deep d0=5 step5",         150, 1.0, dict(RA, lad_d0=5, lad_step=5), "ladder"),
    ("rearm d0=3 step4",              150, 1.0, dict(RA, lad_d0=3, lad_step=4), "ladder"),
    ("rearm levels 2",                150, 1.0, dict(RA, lad_levels=2), "ladder"),
    ("rearm levels 6",                150, 1.0, dict(RA, lad_levels=6), "ladder"),
    ("rearm net<=30",                 150, 1.0, dict(RA, max_net=30.0), "ladder"),
    ("rearm net<=120",                150, 1.0, dict(RA, max_net=120.0), "ladder"),
    ("rearm share<25%",               150, 1.0, dict(RA, max_share=0.25), "ladder"),
    ("rearm stop 180s",               150, 1.0, dict(RA, lad_stop=180.0), "ladder"),
    ("rearm stop 270s",               150, 1.0, dict(RA, lad_stop=270.0), "ladder"),
    ("rearm place 30s",               150, 1.0, dict(RA, lad_place=30.0), "ladder"),
    ("rearm deep queue x2",           150, 2.0, dict(RA, lad_d0=5, lad_step=5), "ladder"),
    ("rearm deep lat350",             350, 1.0, dict(RA, lad_d0=5, lad_step=5), "ladder"),
    ("rearm base queue x2",           150, 2.0, RA, "ladder"),
    ("rearm base lat350",             350, 1.0, RA, "ladder"),
]
EARLY = [
    ("control first 30s",             150, 1.0, dict(CTL, t_off=30.0)),
    ("control first 60s",             150, 1.0, dict(CTL, t_off=60.0)),
    ("candidate first 30s",           150, 1.0, dict(CND, t_off=30.0)),
    ("candidate first 60s",           150, 1.0, dict(CND, t_off=60.0)),
    ("control first 30s share<25%",   150, 1.0, dict(CTL, t_off=30.0, max_share=0.25)),
    ("ladder rearm stop 45s",         150, 1.0, dict(RA, lad_stop=45.0), "ladder"),
    ("ladder rearm pre-20 stop 45s",  150, 1.0, dict(RA, lad_place=-20.0, lad_stop=45.0), "ladder"),
    ("control first 30s queue x2",    150, 2.0, dict(CTL, t_off=30.0)),
    ("control first 30s lat350",      350, 1.0, dict(CTL, t_off=30.0)),
]
# research/PREREGISTRATION_2.md, frozen 2026-11-15 08:30 UTC
P1 = dict(RA, lad_place=-20.0, lad_stop=45.0)
P2 = dict(RA, lad_stop=45.0)
P3 = dict(CTL, t_off=30.0)
P4 = dict(RA, lad_d0=3, lad_step=4)
P5 = dict(CND, join_min=20.0)
P6 = dict(CND, max_share=0.10)
PREREG = []
for name, cfg, mode in (("P1 ladder rearm pre-20 stop 45s", P1, "ladder"), ("P2 ladder rearm stop 45s", P2, "ladder"),
                        ("P3 control first 30s", P3, "maker"), ("P4 ladder rearm d0=3 step4", P4, "ladder"),
                        ("P5 candidate join>=20", P5, "maker"), ("P6 candidate share<10%", P6, "maker"),
                        ("R  control", CTL, "maker")):
    PREREG += [(name, 150, 1.0, cfg, mode), (name + " q x2", 150, 2.0, cfg, mode), (name + " lat350", 350, 1.0, cfg, mode)]
SETS = {"step2": CONFIGS, "momentum": MOMENTUM, "share": SHARE, "rewards": REWARDS, "ladder": LADDER, "ladder2": LADDER2,
        "early": EARLY, "prereg": PREREG}


REAL = False


def one_market(args):
    rec, start, path, outcome, configs, real = args
    events = list(merged(rec, start, path, 300, source="coinbase"))
    if real:
        events = real_events(events, rec, start)
    out = []
    for label, lat, qm, over, *mode in configs:
        r = run_market(rec, start, path, Params(**over), mode[0] if mode else "maker", lat, qm, outcome, events=events)
        out.append((label, r))
    return start, out


FILLSTATS = False


def line(label, rs):
    s = summarize(sorted(rs, key=lambda r: r.start), label)
    if not s.get("markets") or not s["shares"]:
        return " " * (64 + (30 if FILLSTATS else 0))
    inv = statistics.mean(abs(r.q_up - r.q_dn) for r in rs)
    out = (f"{s['fills']:6d}{s['shares']:7.0f}{s['pnl_ex_rebate']:9.2f}{s['tstat_ex_rebate']:6.1f}{s['rebate']:7.2f}"
           f"{s['pnl']:9.2f}{s['tstat']:6.1f}{s['pnl_per_share_c']:7.2f}{inv:6.0f}")
    if FILLSTATS:
        out += f"{sum(r.reward for r in rs):8.2f}"
        live = sum(r.orders_live for r in rs); filled = sum(r.orders_filled for r in rs)
        ttf = sorted(x for r in rs for x in r.ttf)
        out += f"{live:6d}{100 * filled / max(live, 1):6.1f}{(ttf[len(ttf) // 2] / 1000 if ttf else 0):6.1f}{'':>4}"
    return out


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
    if "--fillstats" in args:
        args.remove("--fillstats")
        global FILLSTATS
        FILLSTATS = True
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
    if FILLSTATS:
        hdr += f"{'rew$':>8}{'orders':>6}{'fill%':>6}{'ttf s':>6}{'':>4}"
    wd = 64 + (30 if FILLSTATS else 0)
    print(f"{'configuration':<26}| {'selection sample':^{wd}}| {'independent sample':^{wd}}")
    print(f"{'':<26}| {hdr}| {hdr}")
    for c in configs:
        print(f"{c[0]:<26}| {line(c[0], res[c[0]]['in'])}| {line(c[0], res[c[0]]['out'])}")
    if dump:
        json.dump({c[0]: {r.start: [r.pnl, r.pnl_ex_rebate, r.q_up + r.q_dn]
                          for part in ("in", "out") for r in res[c[0]][part]} for c in configs}, open(dump, "w"))
    proxies = sum(1 for c in configs for part in ("in", "out") for r in res[c[0]][part] if r.outcome_src != "actual")
    print("noReb$ = PnL to resolution after fees, without rebates; pnl$ = with maker rebates; t = per-market t-statistic;")
    if FILLSTATS:
        print("orders = quotes that rested; fill% = share of them with any fill; ttf s = median seconds from resting to first fill;")
    print(f"c/sh = pnl$ per share; |net| = mean |Up - Down| shares left at settlement. Proxy-settled results: {proxies}")


if __name__ == "__main__":
    main()
