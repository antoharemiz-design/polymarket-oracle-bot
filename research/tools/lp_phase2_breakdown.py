"""Phase 2 paper journal: where the money went, per market (user's question, 21-22 Nov).

    python research/tools/lp_phase2_breakdown.py JOURNAL.jsonl [--hours 48]

Reads the phase 2 journal (<data>/paper/paper.jsonl on the VPS). Per market and in total:
  - contracts filled (maker), split by side; rebates;
  - contracts merged as YES + NO pairs (closed for free by opposite fills);
  - closing orders (taker): count, contracts, cost (against the mid, fee included), by reason (position held 1 h,
    exit window, end near);
  - contracts still open at the end (|YES - NO|), mark-to-market of the market;
  - M1 earned (model), $.
A closing order of q contracts pairs with q filled contracts, and those q pairs are merged too. So:
  closed by taker = taker contracts / contracts filled;
  closed for free = 2 x (merged pairs - taker contracts + unmerged YES+NO pairs at the end) / contracts filled
                    (filled contracts that met an opposite fill of ours);
  open = |YES - NO| at the end.
Only reads the file; nothing on the bot changes.
"""
import collections
import json
import sys


def main():
    path = sys.argv[1]
    hours = float(sys.argv[sys.argv.index("--hours") + 1]) if "--hours" in sys.argv else 48.0
    ev = [json.loads(line) for line in open(path)]
    t0 = min(e["ts"] for e in ev if e["event"] == "start")
    t1 = t0 + hours * 3600
    ev = [e for e in ev if t0 <= e["ts"] <= t1]
    names, pos, pnl_end = {}, {}, {}
    for e in ev:
        if e["event"] == "rebalance":
            for c, m in (e.get("markets") or {}).items():
                names[c] = (m.get("q") or c)[:60]
    m = collections.defaultdict(lambda: collections.Counter())
    reasons = collections.defaultdict(collections.Counter)
    for e in ev:
        c = e.get("mkt")
        if e["event"] == "fill":
            m[c]["fills"] += 1
            m[c]["filled"] += e["size"]
            m[c]["filled_" + e["side"]] += e["size"]
            m[c]["rebate"] += e.get("rebate", 0.0)
        elif e["event"] == "merge":
            m[c]["merged_pairs"] += e["pairs"]
        elif e["event"] == "flatten":
            m[c]["taker_n"] += 1
            m[c]["taker_contracts"] += e["size"]
            m[c]["taker_cost"] += -e.get("cost", 0.0)          # cost is logged as a negative number
            m[c]["taker_fee"] += e.get("fee", 0.0)
            reasons[c][e.get("reason") or "?"] += e["size"]
        elif e["event"] == "m1":
            m[c]["m1_usd"] += e.get("usd", 0.0)
        elif e["event"] == "retired":
            m[c]["retired_pnl"] += e.get("pnl", 0.0)
        elif e["event"] == "pnl":
            pos = e.get("positions") or pos
    hours_run = (max(e["ts"] for e in ev) - t0) / 3600
    rows = []
    for c, x in m.items():
        if not x["filled"] and not x["m1_usd"]:
            continue
        yn = pos.get(c) or [0.0, 0.0]
        x["open"] = abs(yn[0] - yn[1])
        x["free"] = 2 * (max(0.0, x["merged_pairs"] - x["taker_contracts"]) + min(yn[0], yn[1]))
        rows.append((c, x))
    rows.sort(key=lambda r: -r[1]["taker_cost"])
    tot = collections.Counter()
    print(f"journal {path}: {hours_run:.1f} h of quoting from the first start event\n")
    print(f"{'market':60s} {'filled':>7s} {'free':>7s} {'taker':>7s} {'cost $':>7s} {'open':>6s} {'free %':>6s} "
          f"{'taker %':>7s} {'M1 $':>7s} {'rebate $':>8s}")
    for c, x in rows:
        f = x["filled"] or 1e-9
        print(f"{names.get(c, c)[:60]:60s} {x['filled']:7.0f} {x['free']:7.0f} {x['taker_contracts']:7.0f} "
              f"{x['taker_cost']:7.2f} {x['open']:6.0f} {x['free'] / f:6.0%} {x['taker_contracts'] / f:7.0%} "
              f"{x['m1_usd']:7.2f} {x['rebate']:8.2f}")
        tot.update(x)
    f = tot["filled"] or 1e-9
    print(f"{'TOTAL':60s} {tot['filled']:7.0f} {tot['free']:7.0f} {tot['taker_contracts']:7.0f} "
          f"{tot['taker_cost']:7.2f} {tot['open']:6.0f} {tot['free'] / f:6.0%} {tot['taker_contracts'] / f:7.0%} "
          f"{tot['m1_usd']:7.2f} {tot['rebate']:8.2f}")
    per_day = 24 / hours_run if hours_run > 0 else 0.0
    print(f"\nper day: taker closing cost ${tot['taker_cost'] * per_day:.2f} (fees ${tot['taker_fee'] * per_day:.2f}), "
          f"M1 ${tot['m1_usd'] * per_day:.2f}, rebates ${tot['rebate'] * per_day:.2f}; "
          f"closing orders {tot['taker_n']}, contracts {tot['taker_contracts']:.0f}")
    by_reason = collections.Counter()
    for r in reasons.values():
        by_reason.update(r)
    print("taker contracts by reason:", dict(by_reason))
    print("free = filled contracts closed by an opposite fill (merged or held as YES+NO pairs); taker = closed by a "
          "closing order; open = |YES - NO| at the end")


if __name__ == "__main__":
    main()
