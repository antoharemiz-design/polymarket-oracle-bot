"""Phase 2 end of test: first-order estimate of income and taker share with the paper fills the 30-second poll
missed (22-23 Nov). Builds on lp_paper_audit.py (order lifetimes, crossing rule, paper position path).

    python research/tools/lp_paper_income.py JOURNAL.jsonl --tape DIR [--hours 48] [--out OUT.json]

For every contract of a public taker trade that crossed one of our standing orders but was not counted by paper
(missed), with its market's journal data:
  markout     (mid 600 s later - our price) x contracts, in our favour; for a fill that cuts the paper position this
              also stands for the later moves of the part of the position it closes (the two offset at 600 s);
  rebate      contracts x fee_rate x p x (1 - p) x rebate_rate (the market's feeSchedule, as paper does);
  closing     reducing contracts save one closing-order contract each, at the market's average closing cost per
              contract; increasing contracts open positions closed by taker at the market's paper rate (taker
              contracts / filled), at the same cost.
Estimate = paper result + markout + rebate + closing, per $100 of quotes per day with the denominator of the
phase 2 report (polybot.lp.phase2). Share closed by taker = (taker - reducing + increasing x rate) / (filled + missed).
First order: after a missed fill the bot would have quoted differently; the replay of the test answers that.
"""
import bisect
import collections
import gzip
import itertools
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import lp_paper_audit as A  # noqa: E402
from polybot import lp  # noqa: E402


def main():
    path = sys.argv[1]
    arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
    tape_dir, hours, out_path = arg("--tape", None), float(arg("--hours", 48.0)), arg("--out", None)
    op = gzip.open if path.endswith(".gz") else open
    ev = [json.loads(line) for line in op(path, "rt")]
    rep = lp.phase2(ev, hours=hours)
    t0 = min(e["ts"] for e in ev if e["event"] == "start")
    ev = [e for e in ev if t0 <= e["ts"] <= t0 + hours * 3600]
    t1 = max(e["ts"] for e in ev)
    meta = {}
    for e in ev:
        if e["event"] == "rebalance":
            for c, m in (e.get("markets") or {}).items():
                meta[c] = m
    lives = collections.defaultdict(list)
    for lt in A.lifetimes(ev):
        lives[lt[0]].append(lt)
    paper = collections.defaultdict(list)
    for e in ev:
        if e["event"] == "fill":
            paper[(e["mkt"], e["side"])].append((e.get("fill_ts", e["ts"]), e["price"], e["size"]))
    rows, tot = [], collections.Counter()
    for c, lts in lives.items():
        m = meta.get(c) or {}
        local = os.path.join(tape_dir, f"{c}.jsonl") if tape_dir else None
        if local and os.path.exists(local):
            raw = [json.loads(line) for line in open(local)]
        else:
            raw = A.req(f"{A.DATA}/v2/trades?" + urllib.parse.urlencode(
                {"condition": c, "taker_only": "true", "limit": 1000})).get("data") or []
        tape = sorted({(r["timestamp"], r["outcome_index"], r["side"][0], float(r["price"]), float(r["size"]),
                        r.get("transaction_hash")) for r in raw})
        hist_t, hist_p = [], []
        if m.get("yes"):
            try:
                h = A.req(f"{A.CLOB}/prices-history?" + urllib.parse.urlencode(
                    {"market": m["yes"], "startTs": int(t0 - 3600), "endTs": int(t1 + 3600), "fidelity": 1}))
                hh = sorted((x["t"], x["p"]) for x in h.get("history") or [])
                hist_t, hist_p = [t for t, _ in hh], [p for _, p in hh]
            except Exception:
                pass
        price_at = lambda t: hist_p[bisect.bisect_right(hist_t, t) - 1] if hist_t and t >= hist_t[0] else None
        cross = []
        for _, side, price, size, ta, tb in lts:
            left = size
            for ts, oi, sd, px, sz, _h in tape:
                if ts + 1 < ta or ts > tb or left <= 1e-9:
                    continue
                if A.crosses(oi, sd, px, price if side == "bid" else None, price if side == "ask" else None) == side:
                    q = min(left, sz)
                    left -= q
                    cross.append((ts, side, price, q))
        used, missed = set(), []
        for ts, side, price, q in cross:
            hit = next((i for i, (t, p, z) in enumerate(paper.get((c, side), []))
                        if (side, i) not in used and abs(t - ts) <= 1 and abs(p - price) < 1e-9), None)
            if hit is None:
                missed.append((ts, side, price, q))
            else:
                used.add((side, hit))
        path_ = sorted((e.get("fill_ts", e["ts"]), e["size"] if e["side"] == "bid" else -e["size"])
                       for e in ev if e.get("mkt") == c and e["event"] in ("fill", "flatten"))
        pt, cum = [t for t, _ in path_], list(itertools.accumulate(d for _, d in path_))
        filled = sum(e["size"] for e in ev if e.get("mkt") == c and e["event"] == "fill")
        flat = [e for e in ev if e.get("mkt") == c and e["event"] == "flatten"]
        taker = sum(e["size"] for e in flat)
        tcost = -sum(e.get("cost", 0.0) for e in flat)
        rate = taker / filled if filled else 0.0
        x = collections.Counter(filled=filled, taker=taker, taker_cost=tcost)
        for ts, side, price, q in missed:
            j = bisect.bisect_left(pt, ts) - 1
            net = cum[j] if j >= 0 else 0.0
            d = q if side == "bid" else -q
            red = min(q, abs(net)) if net * d < 0 else 0.0
            later = price_at(ts + 600)
            x["missed"] += q
            x["reduce"] += red
            x["increase"] += q - red
            if later is not None:
                x["markout"] += ((later - price) if side == "bid" else (price - later)) * q
                x["markout_q"] += q
            p = price if side == "bid" else 1 - price
            x["rebate"] += q * float(m.get("fee_rate") or 0.0) * p * (1 - p) * float(m.get("rebate_rate") or 0.0)
        x["cost_per"] = tcost / taker if taker else None
        rows.append((c, (m.get("q") or c)[:56], x))
        tot.update({k: v for k, v in x.items() if k != "cost_per"})
    avg_cost = tot["taker_cost"] / tot["taker"] if tot["taker"] else 0.0
    closing = 0.0
    for c, name, x in rows:
        cp = x["cost_per"] if x["cost_per"] is not None else avg_cost
        rate = x["taker"] / x["filled"] if x["filled"] else tot["taker"] / tot["filled"]
        x["closing"] = min(x["reduce"], x["taker"]) * cp - x["increase"] * rate * cp
        x["taker_est"] = max(0.0, x["taker"] - x["reduce"]) + x["increase"] * rate
        closing += x["closing"]
        tot["taker_est"] += x["taker_est"]
    days = rep["hours"] / 24 if rep.get("hours") else None
    per100 = lambda usd: usd * 100 / rep["quote_cap_usd"] / days if rep.get("quote_cap_usd") and days else None
    delta = tot["markout"] + tot["rebate"] + closing
    res = {"paper": {k: rep.get(k) for k in ("hours", "quote_cap_usd", "m1_per100", "rebates_per100", "mtm_per100",
                                               "net_per100", "cv", "max_share_last24", "fact_over_pred", "verdict",
                                               "flatten_contracts", "flatten_cost_usd", "contracts")},
           "missed_contracts": tot["missed"], "reduce": tot["reduce"], "increase": tot["increase"],
           "markout_usd": tot["markout"], "markout_c_per_contract": 100 * tot["markout"] / tot["markout_q"] if tot["markout_q"] else None,
           "rebate_usd": tot["rebate"], "closing_usd": closing, "delta_usd": delta, "delta_per100": per100(delta),
           "net_per100_est": (rep["net_per100"] + per100(delta)) if rep.get("net_per100") is not None and per100(delta) is not None else None,
           "taker_share_paper": tot["taker"] / tot["filled"] if tot["filled"] else None,
           "taker_share_est": tot["taker_est"] / (tot["filled"] + tot["missed"]) if tot["filled"] + tot["missed"] else None,
           "markets": {c: dict(x, q=name) for c, name, x in rows}}
    print(json.dumps({k: v for k, v in res.items() if k != "markets"}, indent=1, default=str))
    print(f"\n{'market':56s} {'filled':>7s} {'taker':>6s} {'missed':>7s} {'reduce':>7s} {'incr':>6s} {'markout $':>9s} "
          f"{'rebate $':>8s} {'closing $':>9s}")
    for c, name, x in sorted(rows, key=lambda r: -r[2]["missed"]):
        if x["filled"] or x["missed"]:
            print(f"{name:56s} {x['filled']:7.0f} {x['taker']:6.0f} {x['missed']:7.0f} {x['reduce']:7.0f} {x['increase']:6.0f} "
                  f"{x['markout']:9.2f} {x['rebate']:8.2f} {x['closing']:9.2f}")
    if out_path:
        json.dump(res, open(out_path, "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
