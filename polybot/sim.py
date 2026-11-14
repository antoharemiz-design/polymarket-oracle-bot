"""Tick-level backtest on recorder output.

What is simulated
  * Our information is the stream as received locally (receive timestamps).
  * Every action (place, cancel) takes effect `lat_ms` after the decision: this is the full
    round trip (our uplink plus the delay with which we saw the market).
  * Post-only: an order that would cross when it arrives is rejected.
  * Queue: on arrival we join behind the displayed size at our price (times `queue_mult` to
    mimic more competing makers). The queue only shrinks through trades at our level; size
    reductions by cancels are assumed to happen behind us unless the level becomes smaller
    than our position (pessimistic).
  * Fills: a trade at our price fills what is left after the queue ahead; a trade through our
    price fills us in full (we had priority over the level that actually traded).
  * Taker mode: the order is matched `lat_ms + 250` ms after the decision (250 ms is the
    exchange taker delay) at the then-best price if it is still within the limit; taker fee
    0.07 * p * (1 - p) per share.
  * Positions are held to resolution. Outcome: actual resolution when known, otherwise the
    TWAP rule on recorded spot (agrees with actual resolution in 98.4% of markets).

What is not simulated: our own market impact, and reactions of other participants to our quotes.

usage: python -m polybot.sim REC_DIR [--mode maker|mid|taker] [--lat 150] [--queue 1.0]
                                     [--set key=value ...] [--outcomes FILE] [--quiet]
"""
import argparse
import json
import math
import os
import statistics

from .engine import MarketSession, PaperExecutor, Result  # noqa: F401
from .fairvalue import SpotSeries
from .replay import list_markets, merged
from .strategy import Params

GRID_MS = 50


def run_market(rec_dir, start, path, p: Params, mode="maker", lat_ms=150, queue_mult=1.0,
               outcome=None, dur=300, taker_theta=1.0, journal=None):
    spot = SpotSeries()
    ex = PaperExecutor(None, lat_ms, queue_mult)
    ses = MarketSession(start, p, spot, ex, dur, mode, journal, taker_theta)
    ex.book = ses.book
    end_ms = ses.end_ms
    next_dec = ses.start_ms
    for e in merged(rec_dir, start, path, dur):
        t = e[1]
        while next_dec <= t and next_dec < end_ms:
            ex.on_time(next_dec); ses.on_timer(next_dec); next_dec += GRID_MS
        ex.on_time(t)
        k = e[0]
        if k == "S":
            spot.update(t, e[2])
        elif k == "B":
            ses.on_snapshot(e[2], e[3])
        elif k == "P":
            ses.on_level(e[2], e[3], e[4])
        elif k == "T":
            ses.on_trade(e[2], e[3], e[4], t)
    return ses.settle(outcome) if outcome is not None else ses.settle()


def summarize(results, label=""):
    rs = [r for r in results if r.pnl is not None]
    if not rs:
        return {"label": label, "markets": 0}
    pnl = [r.pnl for r in rs]
    shares = sum(r.q_up + r.q_dn for r in rs)
    cum = peak = dd = 0.0
    for x in pnl:
        cum += x; peak = max(peak, cum); dd = min(dd, cum - peak)
    sd = statistics.pstdev(pnl) if len(pnl) > 1 else 0.0
    traded = [r for r in rs if r.fills]
    mo_sh = sum(r.markout_shares for r in rs)
    return {
        "label": label, "markets": len(rs), "traded": len(traded), "fills": sum(r.fills for r in rs),
        "shares": shares, "cost": sum(r.cost for r in rs), "fees": sum(r.fees for r in rs),
        "pnl": sum(pnl), "rebate": sum(r.rebate for r in rs),
        "pnl_per_share_c": 100 * sum(pnl) / shares if shares else 0.0,
        "markout60_per_share_c": 100 * sum(r.markout for r in rs) / mo_sh if mo_sh else 0.0,
        "win_rate": 100 * sum(1 for r in traded if r.pnl > 0) / len(traded) if traded else 0.0,
        "mean": statistics.mean(pnl), "stdev": sd,
        "sharpe_per_market": statistics.mean(pnl) / sd if sd else 0.0,
        "tstat": statistics.mean(pnl) / sd * math.sqrt(len(pnl)) if sd else 0.0,
        "max_dd": dd, "placed": sum(r.placed for r in rs), "canceled": sum(r.canceled for r in rs),
        "rejected": sum(r.rejected for r in rs),
    }


HEADER = (f"{'run':<26}{'mkts':>5}{'fills':>7}{'shares':>8}{'pnl$':>9}{'c/share':>8}{'mo60 c':>8}"
          f"{'rebate$':>8}{'win%':>6}{'t-stat':>7}{'maxDD$':>8}{'cancels':>8}")


def fmt(s):
    if not s.get("markets"):
        return f"{s['label']:<26} no resolved markets"
    return (f"{s['label']:<26}{s['markets']:5d}{s['fills']:7d}{s['shares']:8.0f}{s['pnl']:9.2f}{s['pnl_per_share_c']:8.2f}"
            f"{s['markout60_per_share_c']:8.2f}{s['rebate']:8.2f}{s['win_rate']:6.1f}{s['tstat']:7.2f}{s['max_dd']:8.2f}{s['canceled']:8d}")


def run_all(rec_dir, p, mode="maker", lat_ms=150, queue_mult=1.0, outcomes=None, markets=None, taker_theta=1.0):
    outcomes = outcomes or {}
    out = []
    for start, path in (markets or list_markets(rec_dir)):
        out.append(run_market(rec_dir, start, path, p, mode, lat_ms, queue_mult,
                              outcomes.get(str(start)), taker_theta=taker_theta))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rec_dir")
    ap.add_argument("--mode", default="maker", choices=["maker", "mid", "taker"])
    ap.add_argument("--lat", type=int, default=150)
    ap.add_argument("--queue", type=float, default=1.0)
    ap.add_argument("--theta", type=float, default=1.0, help="taker mode: required edge after fee, cents")
    ap.add_argument("--set", nargs="*", default=[], help="strategy parameters, key=value")
    ap.add_argument("--outcomes", default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    p = Params()
    for kv in a.set:
        k, v = kv.split("=", 1)
        cur = getattr(p, k)
        setattr(p, k, (v.lower() == "true") if isinstance(cur, bool) else type(cur)(v))
    path = a.outcomes or os.path.join(a.rec_dir, "outcomes.json")
    outcomes = json.load(open(path)) if os.path.exists(path) else {}
    results = run_all(a.rec_dir, p, a.mode, a.lat, a.queue, outcomes, taker_theta=a.theta)
    if not a.quiet:
        for r in results:
            if r.pnl is not None:
                print(f"{r.start} outcome={r.outcome}({r.outcome_src}) fills={r.fills:3d} up={r.q_up:6.1f} dn={r.q_dn:6.1f} "
                      f"cost={r.cost:7.2f} pnl={r.pnl:7.2f} placed={r.placed} canceled={r.canceled} rejected={r.rejected}")
    print(HEADER)
    print(fmt(summarize(results, f"{a.mode} lat={a.lat} q={a.queue}")))


if __name__ == "__main__":
    main()
