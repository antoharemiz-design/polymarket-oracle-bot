"""Which maker fills on daily-temperature markets lose (26 Nov): the fills an LP like ours would get (price inside the
reward zone around the mid, mid in [0.10, 0.90]), each weighted by min(size, N = 20), split by features known when
the fill happens.

    python research/tools/wx_adverse2.py DATA_DIR

Features: trade size; the mid's move over the 5 minutes before the trade, signed toward the taker (positive: the
taker trades with the recent move, i.e. we would be the stale side); the realized volatility of the last 30 minutes;
local hours of the measured day; the price level. Markout in cents per contract (maker's side), m1 / m5 / m30 / m60.
"""
import bisect
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import wx_adverse as A  # noqa: E402
from polybot.lp import wx_local_hours  # noqa: E402

N = 20.0
ZONE = 0.045


def sigma30(pt, pp, t):
    j = bisect.bisect_right(pt, t) - 1
    i = bisect.bisect_left(pt, t - 1800)
    xs = pp[i:j + 1]
    if len(xs) < 10:
        return None
    d = [b - a for a, b in zip(xs, xs[1:])]
    m = sum(d) / len(d)
    return math.sqrt(sum((x - m) ** 2 for x in d) / max(1, len(d) - 1) * 60)


def rows(data):
    for f in glob.glob(os.path.join(data, "*.json")):
        x = A.load(f)
        if x is None:
            continue
        d, res, pt, pp = x
        q = d["meta"]["question"]
        for ts, sg, y, z in A.maker_fills(d):
            m0 = A.mid_at(ts, pt, pp, ts - 60)
            if m0 is None or not 0.10 <= m0 <= 0.90 or abs(y - m0) > ZONE:
                continue
            mb = A.mid_at(ts, pt, pp, ts - 360)
            mom = (m0 - mb) * (-sg) if mb is not None else None      # >0: the taker trades with the recent move
            mk = {}
            for k, dt in (("m1", 60), ("m5", 300), ("m30", 1800), ("m60", 3600)):
                if pt[-1] >= ts + dt:
                    mk[k] = sg * (A.mid_at(ts, pt, pp, ts + dt) - y)
            yield {"w": min(z, N), "z": z, "y": y, "mom": mom, "sig": sigma30(pt, pp, ts - 60),
                   "h": wx_local_hours(q, ts), "res": sg * (res - y), **mk}


def table(rs, key, edges, label):
    print(f"\n{label}")
    print(f"{'':>16s} {'fills':>7s} {'our size':>9s} {'m1 c':>7s} {'m5 c':>7s} {'m30 c':>7s} {'m60 c':>7s} {'res c':>7s}")
    for a, b in zip(edges, edges[1:]):
        sel = [r for r in rs if r[key] is not None and a <= r[key] < b]
        if not sel:
            continue
        out = []
        for k in ("m1", "m5", "m30", "m60"):
            w = sum(r["w"] for r in sel if k in r)
            out.append(100 * sum(r["w"] * r[k] for r in sel if k in r) / w if w else float("nan"))
        w = sum(r["w"] for r in sel)
        res = 100 * sum(r["w"] * r["res"] for r in sel) / w
        print(f"{f'{a:g}..{b:g}':>16s} {len(sel):7d} {w:9.0f} " + " ".join(f"{v:7.2f}" for v in out) + f" {res:7.2f}")


def main():
    rs = list(rows(sys.argv[1]))
    print(f"{len(rs)} fills inside the reward zone, mid in [0.10, 0.90]")
    table(rs, "z", [0, 5, 20, 50, 100, 300, 1e9], "taker trade size (contracts)")
    table(rs, "mom", [-1, -0.03, -0.01, -0.002, 0.002, 0.01, 0.03, 1], "mid move over the 5 min before, toward the taker")
    table(rs, "sig", [0, 0.01, 0.02, 0.04, 0.08, 1], "volatility of the last 30 min (price per hour)")
    table(rs, "h", [-72, -24, -12, 0, 6, 12, 18, 24, 48], "local hours of the measured day")
    table(rs, "y", [0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9], "price level")


if __name__ == "__main__":
    main()
