"""Phase 2 research: how toxic are fills as a short market approaches its end (for the exit-before-resolution rule).

    python research/tools/lp_endgame.py CLOSED_MARKETS.json OUT.json [--sample 250] [--size 20]

CLOSED_MARKETS.json: gamma market objects of markets that already closed (e.g. weather, last 4 days).
For each sampled market: public taker trades and the price history (~1 min). Worst case, every taker trade fills
our quote of `size` contracts (min(trade, size)). Per bucket of hours before the market's endDate:
  markout after 1 h (mid(t+1h) vs our price) and to resolution (final outcome price 0/1 vs our price), in cents
  per contract; contracts filled. Bid fill: value - price; ask fill: price - value.
"""
import bisect
import json
import os
import random
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polybot import lp  # noqa: E402

BUCKETS = ((-1e9, 0, "after end"), (0, 1, "0-1"), (1, 3, "1-3"), (3, 6, "3-6"), (6, 12, "6-12"), (12, 24, "12-24"),
           (24, 48, "24-48"), (48, 1e9, ">48"))


def bucket(h):
    return next(name for lo, hi, name in BUCKETS if lo <= h < hi)


def main():
    src, out = sys.argv[1], sys.argv[2]
    n = int(sys.argv[sys.argv.index("--sample") + 1]) if "--sample" in sys.argv else 250
    size = float(sys.argv[sys.argv.index("--size") + 1]) if "--size" in sys.argv else 20.0
    markets = json.load(open(src))
    random.Random(11).shuffle(markets)
    agg, done = {}, 0
    for m in markets:
        if done >= n:
            break
        try:
            final = [float(x) for x in json.loads(m["outcomePrices"])]
            if sorted(final) != [0.0, 1.0]:
                continue                                  # not cleanly resolved yet
            y_final = final[0]                            # YES outcome value
            t_end = lp._ts(m["endDate"])
            tok = json.loads(m["clobTokenIds"])[0]
            d = lp._req(f"{lp.DATA}/v2/trades?" + urllib.parse.urlencode({"condition": m["conditionId"], "taker_only": "true", "limit": 1000}))
            tr = d.get("data") or []
            if not tr:
                continue
            t0 = min(r["timestamp"] for r in tr) - 60
            h = lp._req(f"{lp.CLOB}/prices-history?" + urllib.parse.urlencode({"market": tok, "startTs": int(t0), "endTs": int(t_end + 6 * 3600), "fidelity": 1}))
            hist = sorted((x["t"], x["p"]) for x in h.get("history") or [])
        except Exception as e:
            print(f"skip {m.get('conditionId', '')[:10]}: {e!r:.80}", flush=True)
            continue
        tt, pp = [t for t, _ in hist], [p for _, p in hist]
        for r in tr:
            oi, side, px, sz = r["outcome_index"], r["side"][0], float(r["price"]), float(r["size"])
            buy_yes, y = (side == "B", px) if oi == 0 else (side != "B", 1 - px)
            q = min(sz, size)
            b = bucket((t_end - r["timestamp"]) / 3600)
            a = agg.setdefault(b, {"q": 0.0, "mk1h": 0.0, "q1h": 0.0, "mkres": 0.0, "markets": set()})
            a["q"] += q; a["markets"].add(m["conditionId"])
            a["mkres"] += ((y - y_final) if buy_yes else (y_final - y)) * 100 * q
            j = bisect.bisect_left(tt, r["timestamp"] + 3600)
            if 0 <= j < len(tt):
                a["mk1h"] += ((y - pp[j]) if buy_yes else (pp[j] - y)) * 100 * q
                a["q1h"] += q
        done += 1
        if done % 50 == 0:
            print(f"{done}/{n}", flush=True)
    res = {b: {"contracts": a["q"], "markets": len(a["markets"]), "markout_1h_c": a["mk1h"] / a["q1h"] if a["q1h"] else None,
               "markout_resolution_c": a["mkres"] / a["q"] if a["q"] else None} for b, a in agg.items()}
    json.dump({"sampled": done, "size": size, "buckets": res}, open(out, "w"), indent=1)
    print(f"{done} markets")
    for _, _, name in BUCKETS[::-1]:
        if name in res:
            r = res[name]
            f = lambda x: "  —  " if x is None else f"{x:+6.2f}"
            print(f"  {name:>9s} h before end: contracts {r['contracts']:9.0f}  markets {r['markets']:4d}  "
                  f"markout 1h {f(r['markout_1h_c'])} c  to resolution {f(r['markout_resolution_c'])} c")


if __name__ == "__main__":
    main()
