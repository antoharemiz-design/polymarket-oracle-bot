"""H3: monotonicity arbitrage across strike ladders, from a live snapshot.

Inside an event whose markets differ only by a numeric threshold:
  "above / reach / hit / at least / more than"  P(K) must fall as K rises
  "below / dip / under / less than"             P(K) must rise as K rises
For an "above"-type pair K1 < K2, YES(K1) + NO(K2) pays at least 1 in every state; it costs
ask_yes(K1) + (1 - bid_yes(K2)). Arbitrage when ask_yes(K1) < bid_yes(K2) after both taker fees.
(For "below"-type the roles of K1 and K2 swap.)

usage: h3_ladder_scan.py OUT.json
"""
import json
import re
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from h2_negrisk_scan import events, post_books, walk  # noqa: E402

UP = re.compile(r"\b(above|reach|hit|at least|more than|over|exceed|greater than|or more|or higher)\b", re.I)
DOWN = re.compile(r"\b(below|dip|under|less than|fall to|drop to|or lower|or less)\b", re.I)
NUM = re.compile(r"(-?\$?\d[\d,]*\.?\d*)\s*([kKmMbBtT%]?)")
LOW_EVENT = re.compile(r"\b(how low|lowest|dip|bottom|fall to|drop to)\b", re.I)
HIGH_EVENT = re.compile(r"\b(how high|highest|peak|top)\b", re.I)


def threshold(m):
    txt = (m.get("groupItemTitle") or "") or m.get("question", "")
    hit = NUM.search(txt.replace("−", "-"))
    if not hit:
        return None
    try:
        v = float(hit.group(1).replace("$", "").replace(",", ""))
    except ValueError:
        return None
    return v * {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}.get(hit.group(2).lower(), 1)


def main():
    ladders = []
    for e in events():
        if e.get("negRisk") or not e.get("enableOrderBook"):
            continue
        ms = [m for m in e.get("markets", []) if m.get("active") and not m.get("closed") and m.get("acceptingOrders")]
        if len(ms) < 2:
            continue
        groups = {"up": [], "down": []}
        ok = True
        ev_low, ev_high = bool(LOW_EVENT.search(e.get("title", ""))), bool(HIGH_EVENT.search(e.get("title", "")))
        for m in ms:
            txt = (m.get("question", "") + " " + (m.get("groupItemTitle") or ""))
            low = bool(re.search(r"\(LOW\)|↓", txt)) or bool(DOWN.search(txt))
            high = bool(re.search(r"\(HIGH\)|↑", txt)) or (bool(UP.search(txt)) and not low)
            if not re.search(r"\(LOW\)|\(HIGH\)|↓|↑", txt) and ev_low != ev_high:
                low, high = ev_low, ev_high            # the event title says which way "hit" points
            if low == high:
                ok = False; break
            k = threshold(m)
            if k is None:
                ok = False; break
            fee = (m.get("feeSchedule") or {}).get("rate", 0.0) if m.get("feesEnabled") else 0.0
            groups["down" if low else "up"].append((k, json.loads(m["clobTokenIds"])[0], fee, m.get("question", "")))
        if not ok:
            continue
        for kind, rows in groups.items():
            if len(rows) >= 2 and len({r[0] for r in rows}) == len(rows):
                ladders.append((e, kind, sorted(rows)))
    tokens = [r[1] for _, _, rows in ladders for r in rows]
    books = {}
    for i in range(0, len(tokens), 100):
        for b in post_books(tokens[i:i + 100]):
            books[b["asset_id"]] = b
        time.sleep(0.3)
    found, pairs = [], 0
    for e, kind, rows in ladders:
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                lo, hi = rows[i], rows[j]
                # "above": YES(lo) is worth more than YES(hi) -> buy YES(lo), buy NO(hi) = sell YES(hi) at its bid
                rich, cheap = (hi, lo) if kind == "up" else (lo, hi)
                bc, br = books.get(cheap[1]), books.get(rich[1])
                if not bc or not br or not bc["asks"] or not br["bids"]:
                    continue
                pairs += 1
                asks = sorted((float(a["price"]), float(a["size"])) for a in bc["asks"])
                bids = sorted(((float(b["price"]), float(b["size"])) for b in br["bids"]), reverse=True)
                best = None
                for q in (1, 5, 10, 25, 50, 100, 250, 500, 1000):
                    c_yes = walk(asks, q); p_yes = walk(bids, q)
                    if c_yes is None or p_yes is None:
                        break
                    a, b = c_yes / q, p_yes / q
                    fee = q * (cheap[2] * a * (1 - a) + rich[2] * b * (1 - b))
                    cost = c_yes + (q - p_yes)
                    profit = q - cost - fee
                    if profit > 0:
                        best = (q, profit, cost)
                if best:
                    found.append({"event": e["title"], "kind": kind, "cheap": cheap[3], "rich": rich[3], "size": best[0],
                                  "profit": round(best[1], 4), "cost": round(best[2], 2), "end": e.get("endDate")})
    json.dump({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ladders": len(ladders), "pairs": pairs,
               "found": found}, open(sys.argv[1], "w"), indent=1)
    print(f"threshold ladders: {len(ladders)} ({sum(1 for l in ladders if l[1] == 'up')} up, "
          f"{sum(1 for l in ladders if l[1] == 'down')} down); pairs with both sides quoted: {pairs}")
    print(f"monotonicity violations after fees: {len(found)}; total profit ${sum(f['profit'] for f in found):.2f}")
    for f in sorted(found, key=lambda x: -x["profit"])[:12]:
        print(f"  ${f['profit']:7.2f} on ${f['cost']:8.2f} size {f['size']:4d} end {str(f['end'])[:10]} | {f['event'][:50]} | "
              f"{f['cheap'][:40]} vs {f['rich'][:40]}")


if __name__ == "__main__":
    main()
