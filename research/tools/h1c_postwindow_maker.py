"""H1c: after the 5m window ends the outcome is known; lottery buyers buy the loser at ~1c. A maker
bid of 0.99 on the winner (= ask 0.01 on the loser, the same price level in the unified book)
earns 1c per share without outcome risk. Question: what does a NEW order placed d seconds after
the end receive, given the size already resting at that level (FIFO on real maker fills)?

Inputs: recorded markets (book until end + 20 s) and all real fills of those markets with block
timestamps and wallets (fetched and cached as <slug>.fills_ts.json).
Match time of a fill = recorded server time of its transaction when recorded, else block time - 3 s.

usage: h1c_postwindow_maker.py REC_DIR [clip=100]
"""
import collections
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from polybot.book import UpBook                    # noqa: E402
from polybot.realfills import condition_of, up_terms   # noqa: E402
from polybot.replay import list_markets, market_events  # noqa: E402

API = "https://data-api.polymarket.com/v2/trades?"


def get(url):
    for i in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-client/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception:
            time.sleep(2 ** i)
    raise RuntimeError(url)


def fills_ts(rec, start):
    path = f"{rec}/pm/btc-updown-5m-{start}.fills_ts.json"
    if os.path.exists(path):
        return json.load(open(path))
    cond = condition_of(rec, start)
    out = {}
    for to in ("false", "true"):
        rows, cur = [], None
        while True:
            q = {"condition": cond, "limit": 1000, "taker_only": to}
            if cur:
                q = {"condition": cond, "cursor": cur, "taker_only": to}
            d = get(API + urllib.parse.urlencode(q))
            rows += [[r["transaction_hash"], r["outcome_index"], r["side"], r["price"], r["size"], r["timestamp"],
                      r["proxy_wallet"]] for r in d.get("data") or []]
            cur = (d.get("pagination") or {}).get("next_cursor")
            if not cur or not d.get("data"):
                break
        out[to] = rows
    json.dump(out, open(path, "w"))
    return out


def main():
    rec = sys.argv[1]
    clip = float(sys.argv[2]) if len(sys.argv) > 2 else 100.0
    outcomes = json.load(open(os.path.join(rec, "outcomes.json")))
    DS = (2, 5, 10, 20)
    res = collections.defaultdict(list)            # d -> per market (our fill shares, level fills after d, queue at d)
    makers = collections.Counter(); takers = collections.Counter(); level_total = []
    for start, path in list_markets(rec):
        if str(start) not in outcomes:
            continue
        w = outcomes[str(start)]
        side, P = ("B", 99) if w == 0 else ("S", 1)        # winner bid 0.99 in Up terms
        end = (start + 300) * 1000
        # displayed size at the level at end + d (exchange time; receive time when no server stamp)
        book = UpBook(); snap = {}; srv_of = {}; pending = list(DS)
        for e in market_events(path):
            t = e[-1] or e[1]
            while pending and t >= end + 1000 * pending[0]:
                snap[pending.pop(0)] = book.size(side, P) if book.ready else None
            if e[0] == "B":
                book.snapshot(e[2], e[3])
            elif e[0] == "P" and book.ready:
                book.change(e[2], e[3], e[4])
            elif e[0] == "T":
                srv_of[e[5]] = e[6]
        for d in pending:
            snap[d] = book.size(side, P) if book.ready else None
        f = fills_ts(rec, start)
        tk = collections.Counter((r[0], r[1], r[2], round(r[3], 6), round(r[4], 6)) for r in f["true"])
        level = []                                          # (match time ms, size, maker wallet)
        for r in f["false"]:
            k = (r[0], r[1], r[2], round(r[3], 6), round(r[4], 6))
            if tk[k] > 0:
                tk[k] -= 1
                if r[5] * 1000 >= end:
                    takers[r[6]] += r[4]
                continue
            s_, p_ = up_terms(r[1], r[2], r[3])
            if s_ == side and p_ == P:
                t = srv_of.get(r[0]) or (r[5] - 3) * 1000
                if t >= end:
                    level.append((t, r[4], r[6]))
                    makers[r[6]] += r[4]
        level.sort()
        level_total.append(sum(x[1] for x in level))
        for d in DS:
            q = snap.get(d)
            if q is None:
                continue
            after = sum(sz for t, sz, _ in level if t >= end + 1000 * d)
            got = min(clip, max(0.0, after - q))            # FIFO, cancels ahead of us ignored (pessimistic)
            res[d].append((got, after, q))
    n = len(level_total)
    tot = sum(level_total)
    print(f"markets: {n}; post-window maker fills at the winner-0.99 level: {tot:,.0f} shares "
          f"(median per market {sorted(level_total)[n // 2]:.0f}); clip {clip:.0f}")
    print(f"{'placed at':>10}{'queue ahead (median)':>22}{'fills after (median)':>22}{'mkts filled':>13}{'our shares':>12}"
          f"{'$ / market':>12}{'t':>7}")
    for d in DS:
        rows = res[d]
        if not rows:
            continue
        got = [g for g, _, _ in rows]
        pnl = [g * 0.01 for g in got]
        mean = sum(pnl) / len(pnl); sd = math.sqrt(sum((x - mean) ** 2 for x in pnl) / len(pnl))
        qs = sorted(q for _, _, q in rows); af = sorted(a for _, a, _ in rows)
        print(f"{f'end + {d} s':>10}{qs[len(qs) // 2]:22.0f}{af[len(af) // 2]:22.0f}{sum(1 for g in got if g > 0):13d}"
              f"{sum(got):12.0f}{mean:12.3f}{(mean / sd * math.sqrt(len(pnl)) if sd else 0):7.2f}")
    mt = sum(makers.values())
    print(f"maker wallets on that level: {len(makers)}; top 3 hold "
          f"{100 * sum(v for _, v in makers.most_common(3)) / max(mt, 1):.0f}%: {[(k[:10], round(v)) for k, v in makers.most_common(3)]}")


if __name__ == "__main__":
    main()
