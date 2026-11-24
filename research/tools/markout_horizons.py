"""Step A: markout of maker fills at 5/60/120/300 s, front of queue vs back of queue.

Groups (same markets, same strategies as real_maker_arbiter.py):
  front     real maker fills on our side at our price while our quote rested (weight
            min(size, clip)) plus transactions that went through our price (weight clip):
            what a quote at the FRONT of the queue would have received
  back      fills of the simulator driven by the real maker fills (FIFO, our order joins
            behind the displayed size): what our quote at the BACK of the queue received
Markout at horizon h, cents per share, for the side bought: book mid at fill + h minus the price
paid; when fill + h is past the end of the 5-minute window, the settlement value (0/100) is
used, so the 300 s horizon is almost always the resolution. t-statistics are per market.

Also: back-of-queue fills by our share of the price level when the order started resting,
share = clip / (clip + displayed size at our price).

usage: markout_horizons.py REC_DIR [workers=4]
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
from polybot.engine import REBATE_SHARE, fee_c                      # noqa: E402
from polybot.realfills import maker_rows, real_events, trades_of, up_terms   # noqa: E402
from polybot.replay import list_markets, merged                      # noqa: E402
from polybot.sim import run_market                                   # noqa: E402
from polybot.strategy import Params                                  # noqa: E402

CONTROL = dict(clip=10.0, max_net=50.0, max_gross=400.0, fast_ms=500, edge=2.0, edge_cancel=1.0, skew=1.0)
CAND = dict(CONTROL, mom_s=60, mom_w=0.2)
STRATS = [("candidate", CAND), ("control", CONTROL), ("candidate join>=20", dict(CAND, join_min=20.0))]
H = (5, 60, 120, 300)
CLIP = 10.0


def one_market(args):
    rec, start, path, outcome = args
    events = list(merged(rec, start, path, 300, source="coinbase"))
    revents = real_events(events, rec, start)
    srv_of = {e[5]: e[6] for e in events if e[0] == "T" and e[6]}
    makers = maker_rows(trades_of(rec, start))
    up_won = 100.0 if outcome == 0 else 0.0
    end_ms = (start + 300) * 1000
    out = {}
    for sname, sp in STRATS:
        r = run_market(rec, start, path, Params(**sp), "maker", 150, 1.0, outcome, events=events, keep_session=True)
        ses = r.session
        gt, gm, lag = ses.grid_t, ses.grid_mid, ses.min_lag or 0

        def marks(t, side, P):
            out_ = []
            for h in H:
                th = t + 1000 * h
                if th >= end_ms - 1000:
                    v = up_won
                else:
                    j = bisect.bisect_right(gt, th) - 1
                    v = gm[j] if j >= 0 else None
                out_.append(None if v is None else (v - P if side == "B" else P - v))
            out_.append(up_won - P if side == "B" else P - up_won)   # settlement
            return out_
        iv = collections.defaultdict(list)
        for side, price, a, b, *_ in ses.ex.order_log:
            iv[side].append((a, b, price))
        at, thr = collections.defaultdict(float), {}
        for tx, oi, side, price, size in makers:
            if tx not in srv_of:
                continue
            t = srv_of[tx] + lag
            s_, P = up_terms(oi, side, price)
            for a, b, q in iv[s_]:
                if a <= t < b:
                    if q == P:
                        at[(tx, s_, P)] += size
                    elif (s_ == "B" and P < q) or (s_ == "S" and P > q):
                        thr[(tx, s_, q)] = t
        front = []
        for (tx, s_, P), sz in at.items():
            if (tx, s_, P) in thr:
                continue
            front.append((min(sz, CLIP), P, marks(srv_of[tx] + lag, s_, P), None))
        for (tx, s_, q), t in thr.items():
            front.append((CLIP, q, marks(t, s_, q), None))
        rb = run_market(rec, start, path, Params(**sp), "maker", 150, 1.0, outcome, events=revents, keep_session=True)
        sb = rb.session
        gt, gm = sb.grid_t, sb.grid_mid
        back = []
        for t, side, price, got, q0, qb, tsz, through, edge in sb.ex.fill_meta:
            back.append((got, price, marks(t, side, price), CLIP / (CLIP + q0)))
        out[sname] = dict(front=front, back=back)
    return start, out


def tstat(xs):
    if len(xs) < 3:
        return float("nan")
    m = statistics.mean(xs); s = statistics.stdev(xs)
    return m / s * math.sqrt(len(xs)) if s else 0.0


def report(label, per_market):
    """per_market: {start: [(w, P, marks, share), ...]}"""
    sh = sum(w for rows in per_market.values() for w, *_ in rows)
    if sh <= 0:
        return
    cells = []
    for i in range(len(H) + 1):
        tot = sum(w * m[i] for rows in per_market.values() for w, P, m, _ in rows if m[i] is not None)
        n = sum(w for rows in per_market.values() for w, P, m, _ in rows if m[i] is not None)
        per = [sum(w * m[i] for w, P, m, _ in rows if m[i] is not None) / 100.0 for rows in per_market.values()]
        cells.append(f"{tot / n:7.2f} ({tstat(per):4.1f})")
    reb = sum(w * REBATE_SHARE * fee_c(P) for rows in per_market.values() for w, P, *_ in rows) / sh
    print(f"{label:<40}{sh:8.0f}" + "".join(f"{c:>15}" for c in cells) + f"{reb:7.2f}")


def main():
    rec = sys.argv[1]
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    outcomes = json.load(open(os.path.join(rec, "outcomes.json")))
    markets = [(s, p) for s, p in list_markets(rec) if str(s) in outcomes]
    for s, _ in markets:
        trades_of(rec, s)
    res = {}
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for start, out in ex.map(one_market, [(rec, s, p, outcomes[str(s)]) for s, p in markets]):
            res[start] = out
    print(f"markets: {len(markets)}; markout in cents per share (t per market); reb = maker rebate per share")
    print(f"{'strategy / group':<40}{'shares':>8}" + "".join(f"{f'+{h}s':>15}" for h in H) + f"{'settle':>15}{'reb':>7}")
    for sname, _ in STRATS:
        for g in ("front", "back"):
            report(f"{sname} / {g}", {s: res[s][sname][g] for s in res})
    print("\nback-of-queue fills by our share of the level when the order started resting (all strategies pooled)")
    print(f"{'share':<40}{'shares':>8}" + "".join(f"{f'+{h}s':>15}" for h in H) + f"{'settle':>15}{'reb':>7}")
    for lo, hi in ((0, 0.10), (0.10, 0.25), (0.25, 0.50), (0.50, 1.01)):
        report(f"share {lo:.0%}-{min(hi, 1):.0%}",
               {s: [x for sn, _ in STRATS for x in res[s][sn]["back"] if lo <= x[3] < hi] for s in res})


if __name__ == "__main__":
    main()
