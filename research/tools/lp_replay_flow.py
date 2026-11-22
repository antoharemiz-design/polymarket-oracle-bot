"""Phase 3 research: do flow filters close the gap the exit rule leaves? (22 Nov)

    python research/tools/lp_replay_flow.py MARKETS.json REPLAY_OUT.json --from EPOCH [--lookback-h 24]

Takes the per-market results of lp_replay.py and the public taker tape of each market in the `lookback` hours BEFORE
the replay window (what a rolling selection would have known at the start), and recomputes the variant totals on the
markets each filter keeps. Filters (thresholds fixed before looking at the replay results, see PHASE3_PLAN.md):
  one_sided:  share of contracts bought YES (taker bought YES or sold NO) outside [20%, 80%] -> excluded;
  opp60:      share of contracts (each trade capped at N) followed by an opposite-direction trade within 60 min >= 75%
              (a reducing quote one tick inside is first in the queue for that trade);
  size:       N <= the smaller side's contracts per 30 min over the lookback (one fill can be undone within 30 min);
  all:        the three together.
A market with no trades in the lookback fails opp60 and size. The filter only drops markets; M1 kept is shown.
"""
import bisect
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polybot import lp  # noqa: E402


def flow(c, t0, t1, n):
    d = lp._req(f"{lp.DATA}/v2/trades?" + urllib.parse.urlencode({"condition": c, "taker_only": "true", "limit": 1000}))
    tr = sorted((r for r in d.get("data") or [] if t0 <= r["timestamp"] < t1), key=lambda r: r["timestamp"])
    if not tr:
        return {"trades": 0}
    rows = [(r["timestamp"], (r["side"] == "BUY") == (r["outcome_index"] == 0), float(r["size"])) for r in tr]
    cap = sum(min(z, n) for _, _, z in rows)
    buy = sum(z for _, b, z in rows if b)
    tot = sum(z for _, _, z in rows)
    ts_b = [t for t, b, _ in rows if b]
    ts_s = [t for t, b, _ in rows if not b]
    ok = 0.0
    for t, b, z in rows:
        opp = ts_s if b else ts_b
        j = bisect.bisect_right(opp, t)
        if j < len(opp) and opp[j] - t <= 3600:
            ok += min(z, n)
    halves = (t1 - t0) / 1800
    return {"trades": len(rows), "buy_yes_share": buy / tot, "opp60": ok / cap,
            "side_per_30m": min(buy, tot - buy) / halves, "oldest_ok": d.get("data") and len(d["data"]) < 1000 or
            min(r["timestamp"] for r in d["data"]) <= t0}


def main():
    mfile, rfile = sys.argv[1], sys.argv[2]
    t_from = float(sys.argv[sys.argv.index("--from") + 1])
    lb = float(sys.argv[sys.argv.index("--lookback-h") + 1]) if "--lookback-h" in sys.argv else 24.0
    res = json.load(open(rfile))
    first = next(iter(res.values()))
    fl = {}
    for spec in json.load(open(mfile)):
        c = spec["cond"]
        if c not in first:
            continue
        fl[c] = flow(c, t_from - lb * 3600, t_from, first[c]["N"])
        f = fl[c]
        print(f"{first[c]['q'][:44]:44s} N {first[c]['N']:5.0f} lookback trades {f['trades']:4d}"
              + (f" buyYES {f['buy_yes_share']:4.0%} opp60 {f['opp60']:4.0%} side/30m {f['side_per_30m']:6.1f}"
                 f"{'' if f['oldest_ok'] else ' (tape cut)'}" if f["trades"] else ""), flush=True)
    keep = {
        "none": lambda c, f: True,
        "one_sided": lambda c, f: not f["trades"] or 0.2 <= f["buy_yes_share"] <= 0.8,
        "opp60": lambda c, f: f["trades"] > 0 and f["opp60"] >= 0.75,
        "size": lambda c, f: f["trades"] > 0 and first[c]["N"] <= f["side_per_30m"],
    }
    keep["all"] = lambda c, f: all(keep[k](c, f) for k in ("one_sided", "opp60", "size"))
    out = {}
    print()
    for kname, fn in keep.items():
        cs = [c for c in fl if fn(c, fl[c])]
        for vname, rs in res.items():
            sel = [rs[c] for c in cs if c in rs]
            f = sum(r["filled"] for r in sel)
            tk = sum(r["taker_contracts"] for r in sel)
            row = {"markets": len(sel), "filled": f, "taker": tk, "share": tk / f if f else None,
                   "cost": sum(r["taker_cost"] for r in sel), "m1": sum(r["m1_usd"] for r in sel),
                   "rebate": sum(r["rebate"] for r in sel), "pnl": sum(r["pnl"] for r in sel)}
            out.setdefault(kname, {})[vname] = row
            print(f"{kname:10s} {vname:24s} markets {len(sel):2d} filled {f:6.0f} taker {tk:5.0f} share "
                  f"{(row['share'] or 0):5.1%} cost ${row['cost']:6.2f} M1 ${row['m1']:6.2f} pnl ${row['pnl']:7.2f}")
        print()
    json.dump({"flow": fl, "totals": out}, open(rfile.replace(".json", "_flow.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
