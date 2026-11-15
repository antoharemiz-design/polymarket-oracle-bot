"""H2: arbitrage inside negative-risk (multi-outcome) events, from a live snapshot of all books.

Exactly one outcome of a neg-risk event resolves YES.
  buy all YES   cost sum(ask_i) for one share of each outcome, pays 1        (only if the basket covers
                every outcome: no augmented events, no open outcome without a book, closed ones resolved NO)
  buy all NO    cost sum(1 - bid_i) = N - sum(bid_i), pays N - 1           (safe even if outcomes are added)
Each leg pays the taker fee rate * p * (1 - p) of its market. The basket is sized by walking every
leg's book; profit is reported for the largest size with a non-negative marginal basket.

usage: h2_negrisk_scan.py OUT.json
"""
import gzip
import json
import sys
import time
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com/events/keyset?"
BOOKS = "https://clob.polymarket.com/books"


def get(url):
    for i in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-client/1.0", "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
        except Exception:
            time.sleep(2 ** i)
    raise RuntimeError(url)


def post_books(tokens):
    body = json.dumps([{"token_id": t} for t in tokens]).encode()
    for i in range(6):
        try:
            req = urllib.request.Request(BOOKS, data=body, headers={"User-Agent": "research-client/1.0",
                                                                    "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception:
            time.sleep(2 ** i)
    raise RuntimeError("books")


def events():
    out, cur = [], None
    while True:
        q = {"closed": "false", "limit": 100}
        if cur:
            q["after_cursor"] = cur
        d = get(GAMMA + urllib.parse.urlencode(q))
        out += d.get("events") or []
        cur = d.get("next_cursor")
        if not cur or not d.get("events"):
            break
    return out


def walk(levels, q):
    """Cost of q shares walking (price, size) levels from the best; None if the book is too thin."""
    cost, left = 0.0, q
    for p, s in levels:
        take = min(left, s); cost += take * p; left -= take
        if left <= 1e-9:
            return cost
    return None


def main():
    evs = [e for e in events() if e.get("negRisk") and e.get("enableOrderBook")]
    legs = []
    for e in evs:
        allm = e.get("markets", [])
        ms = [m for m in allm if m.get("active") and not m.get("closed") and m.get("acceptingOrders")]
        if len(ms) < 2:
            continue
        # the buy-all-YES basket is complete only if every open outcome is a tradable leg and every
        # closed outcome already resolved NO; otherwise an outcome outside the basket can win
        def resolved_no(m):
            try:
                return [float(x) for x in json.loads(m.get("outcomePrices") or "[]")] == [0.0, 1.0]
            except (ValueError, TypeError):
                return False
        complete = all((m in ms) or (m.get("closed") and resolved_no(m)) for m in allm)
        e["_complete"] = complete and not e.get("negRiskAugmented")
        fs = lambda m: (m.get("feeSchedule") or {}).get("rate", 0.0) if m.get("feesEnabled") else 0.0
        legs.append((e, [(json.loads(m["clobTokenIds"])[0], fs(m), m.get("question", "")) for m in ms]))
    tokens = [t for _, ls in legs for t, _, _ in ls]
    books = {}
    for i in range(0, len(tokens), 100):
        for b in post_books(tokens[i:i + 100]):
            books[b["asset_id"]] = b
        time.sleep(0.3)
    found = []
    stats = {"events": len(legs), "legs": len(tokens), "yes_checked": 0, "no_checked": 0}
    for e, ls in legs:
        bk = [books.get(t) for t, _, _ in ls]
        if any(b is None for b in bk):
            continue
        asks = [sorted((float(a["price"]), float(a["size"])) for a in b["asks"]) for b in bk]
        bids = [sorted(((float(x["price"]), float(x["size"])) for x in b["bids"]), reverse=True) for b in bk]
        rates = [r for _, r, _ in ls]
        n = len(ls)
        days = max((time.mktime(time.strptime(e["endDate"][:19], "%Y-%m-%dT%H:%M:%S")) - time.time()) / 86400, 0.0) \
            if e.get("endDate") else None
        # buy all YES
        if e.get("_complete") and all(asks):
            stats["yes_checked"] += 1
            best = None
            for q in (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500):
                legs_cost = [walk(a, q) for a in asks]
                if any(c is None for c in legs_cost):
                    break
                fee = sum(r * (c / q) * (1 - c / q) * q for r, c in zip(rates, legs_cost))
                profit = q - sum(legs_cost) - fee
                if profit > 0:
                    best = (q, profit, sum(legs_cost))
            if best:
                found.append({"type": "buy_all_yes", "event": e["title"], "slug": e["slug"], "n": n, "size": best[0],
                              "profit": round(best[1], 4), "cost": round(best[2], 2), "days": days,
                              "sum_best_ask": round(sum(a[0][0] for a in asks), 4)})
        # buy all NO = sell YES at the bids of every leg
        if all(bids):
            stats["no_checked"] += 1
            best = None
            for q in (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500):
                proceeds = [walk(b, q) for b in bids]
                if any(c is None for c in proceeds):
                    break
                no_cost = sum(q - c for c in proceeds)                     # NO price = 1 - YES bid
                fee = sum(r * (c / q) * (1 - c / q) * q for r, c in zip(rates, proceeds))
                profit = (n - 1) * q - no_cost - fee
                if profit > 0:
                    best = (q, profit, no_cost)
            if best:
                found.append({"type": "buy_all_no", "event": e["title"], "slug": e["slug"], "n": n, "size": best[0],
                              "profit": round(best[1], 4), "cost": round(best[2], 2), "days": days,
                              "sum_best_bid": round(sum(b[0][0] for b in bids), 4)})
    json.dump({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stats": stats, "found": found},
              open(sys.argv[1], "w"), indent=1)
    print(stats)
    print(f"arbitrage baskets found: {len(found)}")
    for f in sorted(found, key=lambda x: -x["profit"])[:15]:
        ann = (f["profit"] / f["cost"]) * 365 / f["days"] * 100 if f["days"] else float("nan")
        print(f"  {f['type']:<12} n={f['n']:3d} size={f['size']:5d} profit=${f['profit']:8.2f} on ${f['cost']:9.2f} "
              f"({100 * f['profit'] / f['cost']:.2f}%, {f['days'] or 0:6.1f} d, {ann:7.1f}%/yr)  {f['event'][:60]}")


if __name__ == "__main__":
    main()
