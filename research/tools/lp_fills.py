"""Phase 2 research: fill risk of reward quoting as the quote size grows, and the maker rebate on those fills.

    python research/tools/lp_fills.py SCAN_DIR OUT.json [--top 150] [--random 100] [--days 7]

Markets: from the latest lp_scan snapshot, those the selection could pick (classification A, >= 7 days old,
>= 14 days left, not a game, rewards_min_size <= 200, quotable at the best prices, >= 3 competitor levels):
the `top` best by M1 per dollar plus `random` others.

For each market, the last `days` days of public taker trades (data-api) and the price history (~1 min):
worst case, we are first in the queue at the best price on both sides, so every taker trade fills us for
min(trade size, our size). Our size = k x rewards_min_size, k = 1, 2, 3, 5.
  contracts per day at each k, markout per contract after 60 s, 600 s, 1 h and to the last price of the history
  (bid fill: mid(t+h) - price; ask fill: price - mid(t+h)), maker rebate = fee_rate x p x (1 - p) x rebate_rate.
"""
import bisect
import json
import os
import random
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from polybot import lp  # noqa: E402
import lp_levers as L  # noqa: E402

KS = (1, 2, 3, 5)


def trades(cond, since):
    d = lp._req(f"{lp.DATA}/v2/trades?" + urllib.parse.urlencode({"condition": cond, "taker_only": "true", "limit": 1000}))
    out = []
    for r in d.get("data") or []:
        if r["timestamp"] < since:
            continue
        oi, side, px, sz = r["outcome_index"], r["side"][0], float(r["price"]), float(r["size"])
        buy_yes, y = (side == "B", px) if oi == 0 else (side != "B", 1 - px)
        out.append((r["timestamp"], buy_yes, y, sz))
    return out


def mids(token, since, until):
    d = lp._req(f"{lp.CLOB}/prices-history?" + urllib.parse.urlencode({"market": token, "startTs": int(since),
                                                                        "endTs": int(until), "fidelity": 1}))
    h = sorted((x["t"], x["p"]) for x in d.get("history") or [])
    return [t for t, _ in h], [p for _, p in h]


def main():
    scan, out_path = sys.argv[1], sys.argv[2]
    arg = lambda k, d: type(d)(sys.argv[sys.argv.index(k) + 1]) if k in sys.argv else d
    top, rnd, days = arg("--top", 150), arg("--random", 100), arg("--days", 7)
    cats = arg("--categories", "")                    # e.g. weather_fees,finance_prices_fees: short-dated study
    meta, snaps = L.load(scan)
    s = snaps[-1]
    conf = L.Conf("fills", max_min=200) if not cats else L.Conf("fills", max_min=200, min_age_days=0, min_days_left=0.04)
    rows = []
    for c, r in s["rw"].items():
        if r[2] > conf.max_min or c not in s["books"] or not L.eligible(meta.get(c), s["ts"], conf):
            continue
        if cats and (meta[c].get("fee_type") or "none") not in cats.split(","):
            continue
        e = L.evaluate(r, s["books"][c], "join", 1)
        if e and e[1] >= 3 and e[0] > 0:
            rows.append((e[0] / e[2], c, r, e))
    rows.sort(reverse=True)
    pick = rows[:top] + random.Random(7).sample(rows[top:], min(rnd, max(0, len(rows) - top)))
    now = time.time()
    since = now - days * 86400
    res = []
    for i, (ratio, c, r, e) in enumerate(pick):
        m = meta[c]
        try:
            tr = trades(c, since)
            tt, pp = mids(m["token"], since, now + 3600) if tr else ([], [])
        except Exception as ex:
            print(f"{c[:10]} failed: {ex!r:.120}", flush=True)
            continue
        mn = r[2]
        rec = {"cond": c, "q": (m.get("question") or "")[:90], "fee_type": m.get("fee_type"), "min": mn, "rate": r[0],
               "m1_per_usd": ratio, "rank": i, "top": i < top, "n_trades": len(tr),
               "contracts_day": {k: sum(min(z, k * mn) for _, _, _, z in tr) / days for k in KS},
               "trade_size_median": sorted(z for *_, z in tr)[len(tr) // 2] if tr else None}
        mk = {60: [], 600: [], 3600: [], "last": []}
        rebate = []
        fr, rr = m.get("fee_rate") or 0.0, m.get("rebate_rate") or 0.0
        for ts, buy_yes, y, z in tr:
            q = min(z, mn)
            rebate.append((fr * y * (1 - y) * rr * 100, q))
            for h in (60, 600, 3600, "last"):
                j = len(tt) - 1 if h == "last" else bisect.bisect_left(tt, ts + h)
                if 0 <= j < len(tt) and tt[j] > ts:
                    mid = pp[j]
                    # a taker buying YES lifts our ask (we sell YES); a taker selling YES hits our bid (we buy YES)
                    mk[h].append((((y - mid) if buy_yes else (mid - y)) * 100, q))
        w = lambda xs: (sum(v * q for v, q in xs) / sum(q for _, q in xs)) if xs and sum(q for _, q in xs) else None
        rec.update(markout60_c=w(mk[60]), markout600_c=w(mk[600]), markout3600_c=w(mk[3600]), markout_last_c=w(mk["last"]),
                   last_mid=pp[-1] if pp else None, rebate_c=w(rebate),
                   worse_than_2c_600=(sum(q for v, q in mk[600] if v < -2.1) / sum(q for _, q in mk[600])) if mk[600] else None)
        res.append(rec)
        if i % 25 == 0:
            print(f"{i}/{len(pick)} done", flush=True)
    with open(out_path, "w") as f:
        json.dump(res, f)
    print(f"{len(res)} markets written to {out_path}")


if __name__ == "__main__":
    main()
