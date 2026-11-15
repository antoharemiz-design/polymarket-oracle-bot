"""Step 4 arbiter: what did REAL passive fills earn at the prices and times our quotes rested?

For each recorded market the simulator runs the strategy and logs the intervals in which each
of its quotes rested (side, price, exchange time). Every real maker fill of every wallet in that
market (Data API v2, taker_only=false minus the taker rows) is timed exactly through its
transaction hash in the recorded trade feed and expressed in Up terms. Then:

  at-price   real maker fills on our side at exactly our price while our quote rested.
             Weight min(size at that price in the transaction, clip): what we could have got
             at most if we had been first in the queue.
  through    transactions that filled real makers BEYOND our price while our quote rested:
             the whole level at our price was consumed, so we would have been filled. Weight clip.

PnL per share to the real resolution (cents), with and without the maker rebate, and the
60 s markout against the recorded book mid. t-statistics are per market. The simulator's own
fills of the same strategy on the same markets are reported in the same categories.

usage: real_maker_arbiter.py REC_DIR SPLIT_EPOCH FRESH_EPOCH [workers=4]
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
from polybot.engine import REBATE_SHARE, fee_c    # noqa: E402
from polybot.replay import list_markets, merged   # noqa: E402
from polybot.sim import run_market                # noqa: E402
from polybot.strategy import Params               # noqa: E402

CONTROL = dict(clip=10.0, max_net=50.0, max_gross=400.0, fast_ms=500, edge=2.0, edge_cancel=1.0, skew=1.0)
CAND = dict(CONTROL, mom_s=60, mom_w=0.2)
STRATS = [("candidate", CAND), ("control", CONTROL), ("candidate join>=20", dict(CAND, join_min=20.0))]
CLIP = 10.0
from polybot.realfills import condition_of, trades_of, up_terms   # noqa: E402,F401


def pnl_c(side, P, up_won):
    return (100.0 * up_won - P) if side == "B" else (P - 100.0 * up_won)


def one_market(args):
    rec, start, path, outcome = args
    events = list(merged(rec, start, path, 300, source="coinbase"))
    srv_of = {e[5]: e[6] for e in events if e[0] == "T" and e[6]}
    tr = trades_of(rec, start)
    takers = collections.Counter((r[0], r[1], r[2], round(r[3], 6), round(r[4], 6)) for r in tr["true"])
    makers = []
    for r in tr["false"]:
        k = (r[0], r[1], r[2], round(r[3], 6), round(r[4], 6))
        if takers[k] > 0:
            takers[k] -= 1; continue
        makers.append(r)
    up_won = 1.0 if outcome == 0 else 0.0
    # event list where the last_trade_price feed is replaced by the real maker fills of each transaction
    by_tx = collections.defaultdict(list)
    for tx, oi, side, price, size in makers:
        by_tx[tx].append(up_terms(oi, side, price) + (size,))
    real_events = []
    for e in events:
        if e[0] == "T":
            if e[5] in by_tx:
                real_events.append(("R", e[1], by_tx[e[5]], e[6]))
        else:
            real_events.append(e)
    out = {}
    for sname, sp in STRATS:
        for qm, tag in ((1.0, ""), (2.0, " q2")):
            rr = run_market(rec, start, path, Params(**sp), "maker", 150, qm, outcome, events=real_events, keep_session=True)
            sim_r = collections.defaultdict(lambda: [0.0, 0.0, 0.0])
            for t, side, price, got, q0, qb, tsz, thr, edge in rr.session.ex.fill_meta:
                k = "through" if thr else "at"
                sim_r[k][0] += got; sim_r[k][1] += got * pnl_c(side, price, up_won)
                sim_r[k][2] += got * REBATE_SHARE * fee_c(price)
            out[sname + " | real-fill FIFO" + tag] = dict(pnl=rr.pnl, noreb=rr.pnl_ex_rebate, shares=rr.q_up + rr.q_dn,
                                                         sim=dict(sim_r))
        r = run_market(rec, start, path, Params(**sp), "maker", 150, 1.0, outcome, events=events, keep_session=True)
        ses = r.session
        lag = ses.min_lag or 0
        gt, gm = ses.grid_t, ses.grid_mid

        def mid_at(t):
            j = bisect.bisect_right(gt, t) - 1
            return gm[j] if j >= 0 and gt[j] >= t - 500 else None
        iv = collections.defaultdict(list)                    # side -> [(live_at, end, price)]
        for side, price, a, b in ses.ex.order_log:
            iv[side].append((a, b, price))
        at_price = collections.defaultdict(float)             # (tx, side, P) -> real size
        through = {}                                          # (tx, side, our P, live_at) -> t
        missing = 0
        for tx, oi, side, price, size in makers:
            if tx not in srv_of:
                missing += 1; continue
            t = srv_of[tx] + lag
            s_, P = up_terms(oi, side, price)
            for a, b, q in iv[s_]:
                if a <= t < b:
                    if q == P:
                        at_price[(tx, s_, P)] += size
                    elif (s_ == "B" and P < q) or (s_ == "S" and P > q):
                        through[(tx, s_, q, a)] = t
        rows = []                                             # (kind, weight, pnl_c, mo60_c, real_size)
        for (tx, s_, P), sz in at_price.items():
            if any(k[0] == tx and k[1] == s_ and k[2] == P for k in through):
                continue                                      # counted as a through event
            t = srv_of[tx] + lag
            m = mid_at(t + 60000)
            rows.append(("at", min(sz, CLIP), pnl_c(s_, P, up_won), None if m is None else (m - P if s_ == "B" else P - m), sz, P))
        for (tx, s_, q, a), t in through.items():
            m = mid_at(t + 60000)
            rows.append(("through", CLIP, pnl_c(s_, q, up_won), None if m is None else (m - q if s_ == "B" else q - m), CLIP, q))
        sim = collections.defaultdict(lambda: [0.0, 0.0, 0.0])          # shares, pnl c, rebate c
        for t, side, price, got, q0, qb, tsz, thr, edge in ses.ex.fill_meta:
            k = "through" if thr else "at"
            sim[k][0] += got; sim[k][1] += got * pnl_c(side, price, up_won)
            sim[k][2] += got * REBATE_SHARE * fee_c(price)
        out[sname] = dict(rows=rows, missing=missing, sim=dict(sim), sim_pnl=r.pnl, sim_noreb=r.pnl_ex_rebate,
                          sim_shares=r.q_up + r.q_dn)
        out[sname + " | trade-feed sim"] = dict(pnl=r.pnl, noreb=r.pnl_ex_rebate, shares=r.q_up + r.q_dn, sim=dict(sim))
    return start, out


def tstat(xs):
    if len(xs) < 3:
        return float("nan")
    m = statistics.mean(xs); s = statistics.stdev(xs)
    return m / s * math.sqrt(len(xs)) if s else 0.0


def main():
    rec, split, fresh = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    workers = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    outcomes = json.load(open(os.path.join(rec, "outcomes.json")))
    markets = [(s, p) for s, p in list_markets(rec) if str(s) in outcomes]
    for s, _ in markets:                                      # fetch serially (rate limits), cache on disk
        trades_of(rec, s)
    res = {}
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for start, out in ex.map(one_market, [(rec, s, p, outcomes[str(s)]) for s, p in markets]):
            res[start] = out
    part = lambda s: "fresh" if s >= fresh else ("indep" if s >= split else "sel")
    print(f"markets: {len(markets)} (selection {sum(part(s) == 'sel' for s, _ in markets)}, independent "
          f"{sum(part(s) == 'indep' for s, _ in markets)}, fresh {sum(part(s) == 'fresh' for s, _ in markets)}); "
          f"maker rows without a recorded trade: {sum(res[s]['candidate']['missing'] for s in res)}")
    for sname, _ in STRATS:
        print(f"\n=== {sname} ===")
        print(f"{'sample':>7}{'kind':>9}{'events':>8}{'shares':>8}{'c/sh':>7}{'+reb':>7}{'mo60':>7}{'$+reb':>9}{'t':>6}"
              f"   |{'sim sh':>7}{'c/sh':>7}{'+reb':>7}")
        for pt in ("sel", "indep", "fresh", "all"):
            ms = [s for s, _ in markets if pt == "all" or part(s) == pt]
            if not ms:
                continue
            for kind in ("at", "through", "both"):
                per = []; sh = pn = rb = mo = mosh = 0.0; n = 0
                ssh = spn = srb = 0.0
                for s in ms:
                    o = res[s][sname]
                    tot = 0.0
                    for k_, w, p, m, _, P in o["rows"]:
                        if kind != "both" and k_ != kind:
                            continue
                        reb = REBATE_SHARE * fee_c(P)
                        n += 1; sh += w; pn += w * p; rb += w * reb
                        tot += w * (p + reb) / 100.0
                        if m is not None:
                            mo += w * m; mosh += w
                    per.append(tot)
                    for k in (("at", "through") if kind == "both" else (kind,)):
                        if k in o["sim"]:
                            ssh += o["sim"][k][0]; spn += o["sim"][k][1]; srb += o["sim"][k][2]
                if sh <= 0:
                    continue
                print(f"{pt:>7}{kind:>9}{n:8d}{sh:8.0f}{pn / sh:7.2f}{(pn + rb) / sh:7.2f}{(mo / mosh if mosh else 0):7.2f}"
                      f"{sum(per):9.2f}{tstat(per):6.1f}   |{ssh:7.0f}{(spn / ssh if ssh else 0):7.2f}{((spn + srb) / ssh if ssh else 0):7.2f}")
        tot_sim = sum(res[s][sname]["sim_pnl"] for s, _ in markets)
        print(f"  simulator PnL with rebates, all markets: ${tot_sim:.2f}")
    print("\n=== back-of-queue PnL: trade-feed simulator vs simulator driven by the real maker fills ===")
    print(f"{'strategy | fill model':<46}" + "".join(f"{pt + ' $ (t)':>17}" for pt in ("sel", "indep", "fresh", "all"))
          + f"{'noReb all':>11}{'c/sh':>7}{'at c/sh':>9}{'thr c/sh':>9}{'thr %':>7}")
    keys = [k for k in res[markets[0][0]] if " | " in k]
    for k in keys:
        row = f"{k:<46}"
        for pt in ("sel", "indep", "fresh", "all"):
            xs = [res[s][k]["pnl"] for s, _ in markets if pt == "all" or part(s) == pt]
            row += f"{sum(xs):>10.2f} ({tstat(xs):4.1f})" if xs else f"{'-':>17}"
        sh = sum(res[s][k]["shares"] for s, _ in markets)
        nr = sum(res[s][k]["noreb"] for s, _ in markets)
        pn = sum(res[s][k]["pnl"] for s, _ in markets)
        cat = {c: [sum(res[s][k]["sim"].get(c, [0, 0, 0])[i] for s, _ in markets) for i in range(3)] for c in ("at", "through")}
        f = lambda c: (cat[c][1] + cat[c][2]) / cat[c][0] if cat[c][0] else 0.0
        thr = cat["through"][0] / max(cat["at"][0] + cat["through"][0], 1e-9)
        row += f"{nr:>11.2f}{100 * pn / sh if sh else 0:>7.2f}{f('at'):>9.2f}{f('through'):>9.2f}{100 * thr:>7.1f}"
        print(row)


if __name__ == "__main__":
    main()
