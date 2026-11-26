"""Adverse selection of passive liquidity on daily-temperature markets (26 Nov, after the live loss): every public
taker trade is a maker fill at the trade price; its markout (the maker's gain per contract) is measured against the
YES price later (1-minute history) and against the outcome, by the local time of the city relative to the measured day.

    python research/tools/wx_adverse.py DATA_DIR [--out OUT.json]

DATA_DIR: files of research/tools/wx_collect.py. A taker BUY of YES at p (or a SELL of NO at 1 - p) is a maker short
of YES at p; a taker SELL of YES (or a BUY of NO) is a maker long. Markout in cents per contract, size-weighted:
  m5 / m30 / m60: mid 5 / 30 / 60 minutes later minus the fill price (signed by the maker's side);
  res: outcome (1 if YES won, else 0) minus the fill price, signed.
Buckets: hours since local midnight of the measured day (from the question's city and date, polybot.lp.wx_local_hours).
"""
import bisect
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polybot.lp import wx_local_hours  # noqa: E402

EDGES = [-72, -48, -36, -24, -18, -12, -6, 0, 3, 6, 9, 12, 15, 18, 24, 48]


def bucket(h):
    for a, b in zip(EDGES, EDGES[1:]):
        if a <= h < b:
            return f"{a:+d}..{b:+d}"
    return "<-72" if h < EDGES[0] else ">=48"


def outcome(meta):
    try:
        p = json.loads(meta.get("outcomePrices") or "[]")
        y = float(p[0])
    except Exception:
        return None
    return 1.0 if y > 0.98 else 0.0 if y < 0.02 else None


def maker_fills(d):
    """(ts, maker YES delta sign, YES price, size) for every taker trade of the market."""
    out = []
    for t in d["trades"]:
        oi, side, px, z = int(t["outcome_index"]), t["side"], float(t["price"]), float(t["size"])
        buy_yes = (side == "BUY") if oi == 0 else (side != "BUY")
        y = px if oi == 0 else 1 - px
        out.append((int(t["timestamp"]), -1 if buy_yes else 1, y, z))
    return sorted(out)


def load(path):
    d = json.load(open(path))
    res = outcome(d["meta"])
    if res is None or not d["prices"] or not d["trades"]:
        return None
    pts = sorted((int(x["t"]), float(x["p"])) for x in d["prices"])
    return d, res, [t for t, _ in pts], [p for _, p in pts]


def mid_at(ts, pt, pp, t):
    j = bisect.bisect_right(pt, t) - 1
    return pp[j] if j >= 0 else None


def main():
    data = sys.argv[1]
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    agg = {}
    n_mk = 0
    for f in glob.glob(os.path.join(data, "*.json")):
        x = load(f)
        if x is None:
            continue
        d, res, pt, pp = x
        q = d["meta"]["question"]
        n_mk += 1
        for ts, sg, y, z in maker_fills(d):
            h = wx_local_hours(q, ts)
            if h is None:
                continue
            b = bucket(h)
            a = agg.setdefault(b, {"n": 0, "size": 0.0, "usd": 0.0, "m5": 0.0, "m30": 0.0, "m60": 0.0, "res": 0.0,
                                   "w5": 0.0, "w30": 0.0, "w60": 0.0, "mid_ok": 0})
            a["n"] += 1
            a["size"] += z
            a["usd"] += z * (y if sg > 0 else 1 - y)
            a["res"] += sg * (res - y) * z
            for k, dt in (("m5", 300), ("m30", 1800), ("m60", 3600)):
                m = mid_at(ts, pt, pp, ts + dt)
                if m is not None and pt[-1] >= ts + dt:
                    a[k] += sg * (m - y) * z
                    a["w" + k[1:]] += z
    rows = []
    for b in [bucket(e + 0.001) for e in EDGES[:-1]] + ["<-72", ">=48"]:
        a = agg.get(b)
        if not a or a["size"] <= 0:
            continue
        c = lambda k, w: 100 * a[k] / a[w] if a[w] > 0 else float("nan")
        rows.append({"bucket": b, "trades": a["n"], "contracts": round(a["size"]), "usd": round(a["usd"]),
                     "m5_c": round(c("m5", "w5"), 2), "m30_c": round(c("m30", "w30"), 2),
                     "m60_c": round(c("m60", "w60"), 2), "res_c": round(100 * a["res"] / a["size"], 2)})
    print(f"{n_mk} resolved markets")
    print(f"{'local hours of the measured day':32s} {'trades':>7s} {'contracts':>10s} {'m5 c':>7s} {'m30 c':>7s} "
          f"{'m60 c':>7s} {'res c':>7s}")
    for r in rows:
        print(f"{r['bucket']:32s} {r['trades']:7d} {r['contracts']:10d} {r['m5_c']:7.2f} {r['m30_c']:7.2f} "
              f"{r['m60_c']:7.2f} {r['res_c']:7.2f}")
    if out:
        json.dump({"markets": n_mk, "rows": rows}, open(out, "w"), indent=1)


if __name__ == "__main__":
    main()
