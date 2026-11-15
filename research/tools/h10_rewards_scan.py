"""H10 step 1: what share of the liquidity-rewards pool a small two-sided quote would get right now.

Scoring (Polymarket docs): an order at distance s (cents) from the midpoint inside the max spread v
scores S = ((v - s) / v)^2 * size; orders below the market's min size do not count.
Q_one = scores of bids, Q_two = scores of asks (the unified YES book already contains mirrored NO orders).
A maker's Q_min = min(Q_one, Q_two); with the midpoint in [0.10, 0.90] a one-sided maker gets max(Q)/3.
The pool rate_per_day is split pro rata to Q_min over the day's samples.
Competition from the aggregate book is bounded above by (Q_one + Q_two) / 2 (proof in research/07).

usage: h10_rewards_scan.py REWARDS.json OUT.json
"""
import json
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from h2_negrisk_scan import get, post_books  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com/markets?"


def meta(chunk):
    q = urllib.parse.urlencode([("condition_ids", c) for c in chunk] + [("limit", len(chunk))])
    return get(GAMMA + q)


def score_side(levels, mid, v, min_size, side):
    tot = 0.0
    for p, s in levels:
        dist = (mid - p if side == "bid" else p - mid) * 100
        if dist < 0 or dist >= v or s < min_size:
            continue
        tot += ((v - dist) / v) ** 2 * s
    return tot


def main():
    rows = json.load(open(sys.argv[1]))
    cids = [r["condition_id"] for r in rows]
    with ThreadPoolExecutor(6) as ex:
        ms = [m for part in ex.map(meta, [cids[i:i + 40] for i in range(0, len(cids), 40)]) for m in part]
    info = {m["conditionId"]: m for m in ms if m.get("clobTokenIds")}
    tok = {c: json.loads(info[c]["clobTokenIds"])[0] for c in cids if c in info}
    toks = list(tok.values())
    books = {}
    for i in range(0, len(toks), 100):
        for b in post_books(toks[i:i + 100]):
            books[b["asset_id"]] = b
        time.sleep(0.2)
    now = time.time()
    out = []
    for r in rows:
        c = r["condition_id"]
        if c not in tok or tok[c] not in books:
            continue
        m, b = info[c], books[tok[c]]
        bids = sorted(((float(x["price"]), float(x["size"])) for x in b["bids"]), reverse=True)
        asks = sorted((float(x["price"]), float(x["size"])) for x in b["asks"])
        if not bids or not asks:
            continue
        mid = (bids[0][0] + asks[0][0]) / 2
        v, mn = float(r["rewards_max_spread"]), float(r["rewards_min_size"])
        q1, q2 = score_side(bids, mid, v, mn, "bid"), score_side(asks, mid, v, mn, "ask")
        end = m.get("endDate")
        try:
            end_ts = time.mktime(time.strptime(end[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone if end else None
        except ValueError:
            end_ts = None
        gs = m.get("gameStartTime")
        live = False
        if gs:
            g = time.mktime(time.strptime(gs[:19], "%Y-%m-%d %H:%M:%S")) - time.timezone
            live = g <= now
        out.append({"cond": c, "token": tok[c], "q": m.get("question"), "slug": m.get("slug"), "rate": r["total_daily_rate"],
                    "v": v, "min": mn, "mid": mid, "spread": asks[0][0] - bids[0][0], "q1": q1, "q2": q2,
                    "tick": float(b.get("tick_size") or 0.01), "end_days": (end_ts - now) / 86400 if end_ts else None,
                    "sports": bool(gs), "live": live, "vol": m.get("volumeNum"), "neg_risk": bool(m.get("negRisk")),
                    "fees": bool(m.get("feesEnabled"))})
    json.dump({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "markets": out}, open(sys.argv[2], "w"))
    print(f"rewarded markets {len(rows)}, with metadata {len(tok)}, with two-sided books {len(out)}")


if __name__ == "__main__":
    main()
