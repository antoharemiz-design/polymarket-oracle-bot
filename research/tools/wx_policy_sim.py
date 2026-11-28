"""Policy backtest of liquidity provision on resolved daily-temperature markets (26 Nov, after the live loss): one
market at a time, the public taker tape and the 1-minute mid, our quotes at the touch, our fills from the tape.

    python research/tools/wx_policy_sim.py DATA_DIR [--policies all | "name|name"] [--share 1.0] [--reward 25] [--out OUT.json]

The touch is rebuilt from the mid (tick 0.01): a mid on a half cent is a one-tick book (bid = mid - 0.005), a mid on
a whole cent a two-tick book (bid = mid - 0.01). Our bid and ask join the touch while it is inside the reward zone
(4.5 c from the mid) and the mid is in [0.10, 0.90]. Never inside the spread (the 25 Nov loss).
A taker trade at or through our price fills us (min of the trade times --share and what is left of our order); an
order refilled after a fill waits `cooldown` before it may grow the position again. Reducing quotes SELL what we hold
at the touch. A position is closed by a taker order at the far touch minus 1 c of slippage when its oldest lot is
older than `hold_h`, when the policy's stop time comes (`stop_h` local hours of the measured day; None = never) and
when the mid leaves [0.10, 0.90]. A market whose own loss passes `market_loss` is closed and left.
Rewards: --reward dollars per $100 of quotes per day while both sides quote (one side: a third), the rate paid on
25 Nov ($14.87 for 5.9 h of $195 quotes = $31) rounded down. Every number is per market lifetime, summed.
"""
import bisect
import collections
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import wx_adverse as A  # noqa: E402
from polybot.lp import wx_local_hours  # noqa: E402

N, CAP, TICK, ZONE = 20.0, 60.0, 0.01, 0.045

POLICIES = {
    # name: stop_h (local hours of the measured day; None = never), start_h (first quote), hold_h, cooldown_s,
    #       vol (max sigma per hour, None = off), market_loss ($, None = off)
    "phase3-like: no stop, 1 h hold":        dict(stop_h=None, start_h=-72, hold_h=1.0, cooldown_s=0, vol=None, market_loss=None),
    "stop at measured day 12 h":             dict(stop_h=12, start_h=-72, hold_h=1.0, cooldown_s=0, vol=None, market_loss=None),
    "stop at measured day 6 h":              dict(stop_h=6, start_h=-72, hold_h=1.0, cooldown_s=0, vol=None, market_loss=None),
    "stop at measured day 0 h":              dict(stop_h=0, start_h=-72, hold_h=1.0, cooldown_s=0, vol=None, market_loss=None),
    "stop 6 h before the day":               dict(stop_h=-6, start_h=-72, hold_h=1.0, cooldown_s=0, vol=None, market_loss=None),
    "stop 12 h before the day":              dict(stop_h=-12, start_h=-72, hold_h=1.0, cooldown_s=0, vol=None, market_loss=None),
    "stop 0 h + cooldown 5 min":             dict(stop_h=0, start_h=-72, hold_h=1.0, cooldown_s=300, vol=None, market_loss=None),
    "stop 0 h + cooldown + vol cap":         dict(stop_h=0, start_h=-72, hold_h=1.0, cooldown_s=300, vol=0.10, market_loss=None),
    "stop 0 h + cooldown + vol + loss $6":   dict(stop_h=0, start_h=-72, hold_h=1.0, cooldown_s=300, vol=0.10, market_loss=6.0),
    "phase 4: stop 0 h, all guards, 2 h hold": dict(stop_h=0, start_h=-72, hold_h=2.0, cooldown_s=300, vol=0.10, market_loss=6.0),
    "phase 4, window -36..0 h":              dict(stop_h=0, start_h=-36, hold_h=2.0, cooldown_s=300, vol=0.10, market_loss=6.0),
    "phase 4, hold 6 h":                     dict(stop_h=0, start_h=-72, hold_h=6.0, cooldown_s=300, vol=0.10, market_loss=6.0),
    "phase 4, 1 tick behind the touch":      dict(stop_h=0, start_h=-72, hold_h=2.0, cooldown_s=300, vol=0.10, market_loss=6.0, behind=1),
    "phase 4, 2 ticks behind the touch":     dict(stop_h=0, start_h=-72, hold_h=2.0, cooldown_s=300, vol=0.10, market_loss=6.0, behind=2),
    "phase 4, stop -12 h, 1 tick behind":    dict(stop_h=-12, start_h=-72, hold_h=2.0, cooldown_s=300, vol=0.10, market_loss=6.0, behind=1),
    "phase 4, vol cap 0.05":                 dict(stop_h=0, start_h=-72, hold_h=2.0, cooldown_s=300, vol=0.05, market_loss=6.0),
    "phase 5: phase 4 + cap N + loss $4 + fair value": dict(stop_h=0, start_h=-72, hold_h=2.0, cooldown_s=300, vol=0.10,
                                                             market_loss=4.0, cap=20.0, fv=(0.05, 0.20)),
    "phase 5, fair value only (one 0.03)":   dict(stop_h=0, start_h=-72, hold_h=2.0, cooldown_s=300, vol=0.10,
                                                  market_loss=4.0, cap=20.0, fv=(0.03, 0.20)),
    "phase 4, position cap N":               dict(stop_h=0, start_h=-72, hold_h=2.0, cooldown_s=300, vol=0.10, market_loss=6.0, cap=20.0),
    "phase 4, cap N, loss $4":               dict(stop_h=0, start_h=-72, hold_h=2.0, cooldown_s=300, vol=0.10, market_loss=4.0, cap=20.0),
    "phase 4, cap N, loss $4, hold 4 h":     dict(stop_h=0, start_h=-72, hold_h=4.0, cooldown_s=300, vol=0.10, market_loss=4.0, cap=20.0),
}


def score(dist_c, v=4.5):
    return ((v - dist_c) / v) ** 2 if dist_c < v else 0.0


S_REF = score(1.0)


def touch(m):
    """(bid, ask) of the book from its mid (tick 0.01)."""
    c = round(m * 200)                                # half cents
    if c % 2:                                         # x.xx5: one-tick book
        return (c - 1) / 200, (c + 1) / 200
    return round(m - TICK, 4), round(m + TICK, 4)


def sigma(pp, j, n=30):
    xs = pp[max(0, j - n):j + 1]
    if len(xs) < 10:
        return None
    d = [b - a for a, b in zip(xs, xs[1:])]
    mu = sum(d) / len(d)
    return math.sqrt(sum((x - mu) ** 2 for x in d) / max(1, len(d) - 1) * 60)


def run_market(d, res, pt, pp, pol, share, reward_rate, fair=None):
    """fair: [(ts, p)] of polybot.wxfair (research/tools/h11_eval.py evaluate --out); with pol["fv"] = (one, pull)
    the quotes follow it as in the bot (phase 5): |edge| >= pull nothing (and a position against it is closed),
    edge >= one no ask, edge <= -one no bid; no fair value: no quotes."""
    q = d["meta"]["question"]
    CAP = pol.get("cap", 60.0)
    fv = pol.get("fv")
    ft = [x[0] for x in fair] if fair else []
    trades = A.maker_fills(d)                          # (ts, maker sign, YES price, size)
    tt = [t for t, *_ in trades]
    out = collections.Counter()
    pos, cash, lots = 0.0, 0.0, []                     # lots: [ts, signed qty]
    cool = {"bid": 0, "ask": 0}
    closed = False
    k = 0
    days = set()

    def close(ts, m, why):
        nonlocal pos, cash, lots
        if abs(pos) < 1e-9:
            return
        b, a = touch(m)
        px = (b - TICK) if pos > 0 else (a + TICK)     # sell into the bid / buy at the ask, 1 c of slippage
        px = min(max(px, 0.001), 0.999)
        cost = abs(pos) * abs(m - px)
        cash += pos * px
        out["taker_contracts"] += abs(pos)
        out["taker_cost"] += cost
        out["closes_" + why] += 1
        pos, lots = 0.0, []

    start = None
    for j in range(len(pt)):
        t, m = pt[j], pp[j]
        h = wx_local_hours(q, t)
        if h is None:
            return None
        if h < pol["start_h"]:
            continue
        if start is None:
            start = t
        nxt = pt[j + 1] if j + 1 < len(pt) else t + 60
        stop = pol["stop_h"] is not None and h >= pol["stop_h"]
        out_of_range = not 0.10 <= m <= 0.90
        if closed or stop or out_of_range:
            close(t, m, "stop" if stop else "range" if out_of_range else "loss")
            if stop or closed:
                break
            continue
        if lots and t - lots[0][0] >= pol["hold_h"] * 3600:
            close(t, m, "hold")
        if pol["market_loss"] is not None and cash + pos * m < -pol["market_loss"]:
            close(t, m, "loss")
            closed = True
            break
        b, a = touch(m)
        back = pol.get("behind", 0) * TICK
        b, a = round(b - back, 4), round(a + back, 4)
        sig = sigma(pp, j) if pol["vol"] is not None else None
        calm = sig is None or sig <= pol["vol"]
        quote = {}
        edge = None
        if fv is not None:
            jf = bisect.bisect_right(ft, t) - 1
            edge = fair[jf][1] - m if jf >= 0 else None
            if edge is not None and abs(edge) >= fv[1] and edge * pos < 0:
                close(t, m, "fair")
        if (m - b) <= ZONE:
            grow_ok = pos < CAP - 1e-9 and calm and t >= cool["bid"]
            if pos < -1e-9 or grow_ok:                 # bid reduces a short, or opens/grows a long
                quote["bid"] = b
        if (a - m) <= ZONE:
            grow_ok = -pos < CAP - 1e-9 and calm and t >= cool["ask"]
            if pos > 1e-9 or grow_ok:
                quote["ask"] = a
        if fv is not None:
            if edge is None or abs(edge) >= fv[1]:
                quote = {}
            elif edge >= fv[0]:
                quote.pop("ask", None)
            elif edge <= -fv[0]:
                quote.pop("bid", None)
        dt = (nxt - t) / 60
        sc = [score(abs(m - quote[sd]) * 100) / S_REF for sd in quote]
        two = (sum(sc) / 2 if len(sc) == 2 and min(sc) * 3 >= max(sc) else max(sc) / 3 if sc else 0.0)
        out["reward"] += reward_rate / 100 * (N * 1.0) / 1440 * dt * two
        out["quote_min"] += dt if quote else 0
        days.add(int(t // 86400))
        rem = {"bid": N if "bid" in quote else 0.0, "ask": N if "ask" in quote else 0.0}
        k = bisect.bisect_left(tt, t, lo=k)
        while k < len(trades) and trades[k][0] < nxt:
            ts, sg, y, z = trades[k]
            k += 1
            side = "bid" if sg > 0 else "ask"
            if side not in quote or rem[side] <= 1e-9:
                continue
            px = quote[side]
            if (side == "bid" and y > px + 1e-9) or (side == "ask" and y < px - 1e-9):
                continue                               # traded at a better price than ours: not us
            through = (side == "bid" and y < px - 1e-9) or (side == "ask" and y > px + 1e-9)
            qty = min(rem[side], z if through else z * share)
            if side == "bid" and pos >= 0:
                qty = min(qty, CAP - pos)
            if side == "ask" and pos <= 0:
                qty = min(qty, CAP + pos)
            if qty <= 1e-9:
                continue
            rem[side] -= qty
            grows = (side == "bid" and pos >= 0) or (side == "ask" and pos <= 0)
            dq = qty if side == "bid" else -qty
            cash -= dq * px
            pos += dq
            out["fills"] += 1
            out["contracts"] += qty
            mk = A.mid_at(ts, pt, pp, ts + 60)
            if mk is not None:
                out["m1_usd"] += (mk - px) * dq
            if grows:
                lots.append([ts, dq])
                if pol["cooldown_s"]:
                    cool[side] = ts + pol["cooldown_s"]
            else:                                       # FIFO
                left = abs(dq)
                while left > 1e-9 and lots:
                    take = min(left, abs(lots[0][1]))
                    lots[0][1] -= math.copysign(take, lots[0][1])
                    left -= take
                    if abs(lots[0][1]) < 1e-9:
                        lots.pop(0)
                if left > 1e-9:
                    lots.append([ts, math.copysign(left, dq)])
    if pos != 0:                                       # still open at the end of the data: at the outcome
        cash += pos * res
        out["held_to_outcome"] += abs(pos)
    out["trading"] = cash
    out["days"] = len(days)
    return out


def main():
    data = sys.argv[1]
    arg = lambda k, dflt: type(dflt)(sys.argv[sys.argv.index(k) + 1]) if k in sys.argv else dflt
    share, reward = arg("--share", 1.0), arg("--reward", 25.0)
    names = list(POLICIES) if arg("--policies", "all") == "all" else arg("--policies", "").split("|")
    outp = arg("--out", "")
    fair_all = json.load(open(arg("--fair", ""))).get("series", {}) if arg("--fair", "") else {}
    d_from, d_to = arg("--from", "0000"), arg("--to", "9999")
    mk = []
    for f in sorted(glob.glob(os.path.join(data, "*.json"))):
        x = A.load(f)
        if x is not None and d_from <= (x[0]["meta"].get("event_end") or "")[:10] <= d_to:
            mk.append(x)
    print(f"{len(mk)} resolved markets; queue share {share}; reward ${reward} per $100 of quotes per day")
    res_all = {}
    for name in names:
        pol = POLICIES[name]
        tot = collections.Counter()
        per_day = collections.defaultdict(float)
        n = 0
        for d, res, pt, pp in mk:
            if pol.get("fv") is not None and d["meta"]["conditionId"] not in fair_all:
                continue                               # markets the fair value model does not value: left out
            o = run_market(d, res, pt, pp, pol, share, reward, fair_all.get(d["meta"]["conditionId"]))
            if o is None:
                continue
            n += 1
            tot.update(o)
            day = d["meta"].get("event_end", "")[:10]
            per_day[day] += o["trading"] + o["reward"]
        quote_usd_days = tot["quote_min"] / 1440 * N          # ~$20 of quotes per market while quoting
        net = tot["trading"] + tot["reward"]
        per100 = net / (quote_usd_days / 100) if quote_usd_days else float("nan")
        days = sorted(per_day.values())
        r = {"markets": n, "fills": tot["fills"], "contracts": round(tot["contracts"]),
             "m1_c": round(100 * tot["m1_usd"] / tot["contracts"], 2) if tot["contracts"] else None,
             "trading_usd": round(tot["trading"], 2), "reward_usd": round(tot["reward"], 2), "net_usd": round(net, 2),
             "taker_cost_usd": round(tot["taker_cost"], 2), "held_to_outcome": round(tot["held_to_outcome"]),
             "quote_usd_days": round(quote_usd_days, 1), "net_per_100_day": round(per100, 2),
             "trading_per_100_day": round(tot["trading"] / (quote_usd_days / 100), 2) if quote_usd_days else None,
             "losing_event_days": f"{sum(1 for v in days if v < 0)}/{len(days)}"}
        res_all[name] = r
        print(f"{name:42s} fills {r['fills']:6d} c/ct m1 {r['m1_c']:5.2f}  trading ${r['trading_usd']:8.2f}  "
              f"reward ${r['reward_usd']:8.2f}  net/$100/day {r['net_per_100_day']:6.2f} (trading {r['trading_per_100_day']:6.2f})  "
              f"to outcome {r['held_to_outcome']}")
    if outp:
        json.dump(res_all, open(outp, "w"), indent=1)


if __name__ == "__main__":
    main()
