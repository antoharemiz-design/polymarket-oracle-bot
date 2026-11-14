"""Attribution of a Polymarket wallet's PnL from data-api v2 dumps.

Inputs (JSONL written by fetch.py):
  activity file  - every fill of the wallet (maker and taker) plus REDEEM/MERGE/SPLIT rows
  taker file     - /v2/trades with taker_only=true: the subset of fills where the wallet was taker

Each fill is flagged maker/taker by matching it against the taker file.
Winner of a market is taken from REDEEM rows (payout > 0), so only markets
the wallet redeemed can be scored; the share of unscored volume is printed.

Winners come from a resolutions cache (resolutions.py) when given, else from
REDEEM rows with a payout.

usage: analyze_wallet.py ACTIVITY.jsonl TAKER.jsonl [--fee-rate 0.07] [--resolutions CACHE.json] [--out summary.json]
"""
import collections
import json
import re
import statistics
import sys

DUR = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # tolerate a truncated last line
    return rows


def fill_key(r):
    return (r["transaction_hash"], r["token_id"], r["side"], round(r["price"], 6), round(r["size"], 6))


def family(slug):
    m = re.match(r"^([a-z]+)-updown-(\d+[mh])-(\d+)$", slug)
    if m:
        return f"{m.group(1)}-{m.group(2)}", int(m.group(3)), DUR.get(m.group(2))
    return re.sub(r"[-\d]+$", "", slug)[:40] or "?", None, None


def pct(a, b):
    return 100.0 * a / b if b else 0.0


def table(title, rows, cols):
    print(f"\n{title}")
    print("".join(f"{c:>{w}}" for c, w in cols))
    for r in rows:
        print("".join(f"{v:>{w}}" if isinstance(v, str) else f"{v:>{w}.{p}f}" for v, (c, w), p in r))


def main():
    args = sys.argv[1:]
    fee_rate = 0.07
    out = None
    if "--fee-rate" in args:
        i = args.index("--fee-rate"); fee_rate = float(args[i + 1]); del args[i:i + 2]
    if "--out" in args:
        i = args.index("--out"); out = args[i + 1]; del args[i:i + 2]
    res = None
    if "--resolutions" in args:
        i = args.index("--resolutions"); res = json.load(open(args[i + 1])); del args[i:i + 2]
    act, tak = load(args[0]), load(args[1])

    taker_keys = collections.Counter(fill_key(r) for r in tak)
    types = collections.Counter(r["type"] for r in act)
    print("activity rows by type:", dict(types))

    # winner per market = winning outcome_index
    winner = {}
    if res is not None:
        winner = {c: w for c, w in res.items() if w >= 0}
    else:
        for r in act:
            if r["type"] == "REDEEM" and r.get("usdc_size", 0) > 0 and r["size"] > 0:
                winner[r["condition_id"]] = r["outcome_index"]

    fills = []
    for r in act:
        if r["type"] != "TRADE":
            continue
        k = fill_key(r)
        is_taker = taker_keys[k] > 0
        if is_taker:
            taker_keys[k] -= 1
        fam, start, dur = family(r["slug"])
        fills.append(dict(r, taker=is_taker, fam=fam, start=start, dur=dur))
    unmatched = sum(taker_keys.values())
    n = len(fills)
    notional = sum(f["size"] * f["price"] for f in fills)
    print(f"fills: {n}, notional ${notional:,.0f}; taker rows not matched to activity: {unmatched}")

    by = collections.defaultdict(lambda: [0, 0.0])
    for f in fills:
        for k in (("side", f["side"]), ("role", "taker" if f["taker"] else "maker"), ("fam", f["fam"])):
            by[k][0] += 1; by[k][1] += f["size"] * f["price"]
    for dim in ("side", "role", "fam"):
        print(f"\nby {dim}:")
        for (d, v), (c, usd) in sorted(by.items(), key=lambda x: -x[1][1]):
            if d == dim:
                print(f"  {v:28} fills {c:8d} ({pct(c, n):5.1f}%)  notional ${usd:12,.0f} ({pct(usd, notional):5.1f}%)")

    # ---- per-fill attribution for BUY fills held to resolution --------------------------
    sells = [f for f in fills if f["side"] == "SELL"]
    buys = [f for f in fills if f["side"] == "BUY"]
    scored = [f for f in buys if f["condition_id"] in winner]
    print(f"\nBUY fills scored against a known winner: {len(scored)} of {len(buys)} "
          f"({pct(sum(f['size']*f['price'] for f in scored), sum(f['size']*f['price'] for f in buys)):.1f}% of buy notional); "
          f"SELL fills: {len(sells)}")

    def agg(rows):
        cost = sum(f["size"] * f["price"] for f in rows)
        pay = sum(f["size"] for f in rows if f["outcome_index"] == winner[f["condition_id"]])
        fee = sum(f["size"] * fee_rate * f["price"] * (1 - f["price"]) for f in rows if f["taker"])
        sh = sum(f["size"] for f in rows)
        return dict(n=len(rows), cost=cost, shares=sh, gross=pay - cost, fee=fee, net=pay - cost - fee,
                    avg_px=cost / sh if sh else 0, win=pay / sh if sh else 0)

    def show(title, groups):
        print(f"\n{title}")
        print(f"{'bucket':>16}{'fills':>9}{'cost$':>12}{'avgPx':>7}{'winRate':>8}{'gross$':>10}{'fee$':>9}{'net$':>10}{'gross%':>8}{'net%':>7}")
        for name, rows in groups:
            if not rows:
                continue
            a = agg(rows)
            print(f"{name:>16}{a['n']:9d}{a['cost']:12,.0f}{a['avg_px']:7.3f}{a['win']:8.3f}{a['gross']:10,.0f}{a['fee']:9,.0f}"
                  f"{a['net']:10,.0f}{pct(a['gross'], a['cost']):8.2f}{pct(a['net'], a['cost']):7.2f}")

    show("ALL scored buys (winRate = share-weighted probability the bought side won)", [("all", scored)])
    show("by role", [("maker", [f for f in scored if not f["taker"]]), ("taker", [f for f in scored if f["taker"]])])
    edges = [0, .05, .15, .3, .45, .55, .7, .85, .95, 1.01]
    for role in ("maker", "taker"):
        rr = [f for f in scored if f["taker"] == (role == "taker")]
        show(f"{role} buys by price", [(f"{lo:.2f}-{hi:.2f}", [f for f in rr if lo <= f["price"] < hi]) for lo, hi in zip(edges, edges[1:])])
    timed = [f for f in scored if f["start"] and f["dur"]]
    tb = [0, .1, .25, .5, .75, .9, 1.0, 9]
    for role in ("maker", "taker"):
        rr = [f for f in timed if f["taker"] == (role == "taker")]
        show(f"{role} buys by elapsed fraction of the window",
             [(f"{lo:.2f}-{hi:.2f}", [f for f in rr if lo <= (f["timestamp"] - f["start"]) / f["dur"] < hi]) for lo, hi in zip(tb, tb[1:])])
    show("by market family", [(fam, [f for f in scored if f["fam"] == fam]) for fam in sorted({f["fam"] for f in scored})])

    # ---- per-market structure -------------------------------------------------------------
    mk = collections.defaultdict(lambda: collections.defaultdict(lambda: [0.0, 0.0, 0.0]))  # outcome -> shares, cost, fee
    for f in scored:
        o = mk[f["condition_id"]][f["outcome_index"]]
        o[0] += f["size"]; o[1] += f["size"] * f["price"]
        if f["taker"]:
            o[2] += f["size"] * fee_rate * f["price"] * (1 - f["price"])
    pnl, pair_pnl, resid_pnl, both, imb, paircost = [], 0.0, 0.0, 0, [], []
    for cid, o in mk.items():
        w = winner[cid]
        cost = sum(v[1] for v in o.values()); fee = sum(v[2] for v in o.values())
        pay = o[w][0] if w in o else 0.0
        pnl.append(pay - cost - fee)
        if len(o) == 2:
            both += 1
            (qa, ca, _), (qb, cb, _) = o.values()
            q = min(qa, qb)
            pc = ca / qa + cb / qb
            paircost.append(pc)
            pp = q * (1 - pc)
            pair_pnl += pp
            resid_pnl += pay - cost - pp
            imb.append(abs(qa - qb) / max(qa, qb))
        else:
            resid_pnl += pay - cost
    m = len(pnl)
    wins = sum(1 for x in pnl if x > 0)
    print(f"\nPER MARKET ({m} scored markets)")
    print(f"  bought both outcomes: {both} ({pct(both, m):.1f}%)")
    print(f"  market win rate (net pnl > 0): {pct(wins, m):.1f}%")
    print(f"  net pnl/market: mean {statistics.mean(pnl):.2f}, median {statistics.median(pnl):.2f}, "
          f"stdev {statistics.pstdev(pnl):.2f}, worst {min(pnl):.0f}, best {max(pnl):.0f}")
    if paircost:
        pcs = sorted(paircost)
        print(f"  pair cost (avg px A + avg px B): median {statistics.median(pcs):.4f}, "
              f"p10 {pcs[len(pcs)//10]:.4f}, p90 {pcs[9*len(pcs)//10]:.4f}; share below 1.00: {pct(sum(1 for x in pcs if x < 1), len(pcs)):.1f}%")
        print(f"  inventory imbalance at expiry |A-B|/max: median {statistics.median(imb):.3f}, mean {statistics.mean(imb):.3f}")
        print(f"  gross pnl split: matched pairs ${pair_pnl:,.0f}  |  unmatched residual ${resid_pnl:,.0f}")
    if out:
        json.dump({"markets": m, "market_win_rate": pct(wins, m), "pair_pnl": pair_pnl, "resid_pnl": resid_pnl,
                   "all": agg(scored)}, open(out, "w"), indent=1)


if __name__ == "__main__":
    main()
