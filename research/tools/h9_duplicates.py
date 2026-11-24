"""H9: the same question listed as different markets (different conditionIds) in different events.

If two markets resolve on the identical question, YES in one + NO in the other pays exactly 1.
Arbitrage when ask_yes(A) + (1 - bid_yes(B)) + fees < 1, sized by walking both books.

usage: h9_duplicates.py OUT.json
"""
import collections
import json
import re
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from h2_negrisk_scan import events, post_books, walk  # noqa: E402


GAME = re.compile(r"-\d{4}-\d{2}-\d{2}(-|$)")


def norm(q):
    return re.sub(r"[^a-z0-9$%.+\-]+", " ", (q or "").lower().replace("−", "-")).strip()


def main():
    groups = collections.defaultdict(list)
    for e in events():
        if not e.get("enableOrderBook"):
            continue
        if GAME.search(e.get("slug") or ""):
            continue                                   # per-game events reuse generic questions ("O/U 2.5")
        for m in e.get("markets", []):
            if not (m.get("active") and not m.get("closed") and m.get("acceptingOrders") and m.get("clobTokenIds")):
                continue
            fee = (m.get("feeSchedule") or {}).get("rate", 0.0) if m.get("feesEnabled") else 0.0
            groups[(norm(m.get("question")), (m.get("endDate") or "")[:10])].append(
                (m["conditionId"], json.loads(m["clobTokenIds"])[0], fee, e.get("slug"), m.get("question")))
    dups = {k: v for k, v in groups.items() if len({x[0] for x in v}) >= 2 and len(k[0]) > 15}
    toks = list({x[1] for v in dups.values() for x in v})
    books = {}
    for i in range(0, len(toks), 100):
        for b in post_books(toks[i:i + 100]):
            books[b["asset_id"]] = b
        time.sleep(0.2)
    found, pairs = [], 0
    for k, v in dups.items():
        for a in v:
            for b in v:
                if a[0] == b[0]:
                    continue
                ba, bb = books.get(a[1]), books.get(b[1])
                if not ba or not bb or not ba["asks"] or not bb["bids"]:
                    continue
                pairs += 1
                asks = sorted((float(x["price"]), float(x["size"])) for x in ba["asks"])
                bids = sorted(((float(x["price"]), float(x["size"])) for x in bb["bids"]), reverse=True)
                best = None
                for q in (1, 5, 10, 25, 50, 100, 250, 500, 1000):
                    c, p = walk(asks, q), walk(bids, q)
                    if c is None or p is None:
                        break
                    fee = q * (a[2] * (c / q) * (1 - c / q) + b[2] * (p / q) * (1 - p / q))
                    profit = q - (c + q - p) - fee
                    if profit > 0:
                        best = (q, profit)
                if best:
                    found.append({"q": a[4], "a": a[3], "b": b[3], "size": best[0], "profit": round(best[1], 4)})
    json.dump({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "groups": len(dups), "pairs": pairs,
               "found": found}, open(sys.argv[1], "w"), indent=1)
    print(f"duplicate questions across markets: {len(dups)} groups; ordered pairs with books: {pairs}; "
          f"arbitrage after fees: {len(found)}, total ${sum(f['profit'] for f in found):.2f}")
    for f in sorted(found, key=lambda x: -x["profit"])[:10]:
        print(f"  ${f['profit']:.2f} size {f['size']} | {f['q'][:70]} | {f['a'][:30]} vs {f['b'][:30]}")
    for k, v in list(dups.items())[:8]:
        print("  example group:", k[0][:60], [x[3][:25] for x in v][:4])


if __name__ == "__main__":
    main()
