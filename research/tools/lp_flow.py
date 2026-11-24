"""Phase 3 research: one-sided taker flow, related markets, passive exit (user's questions, 22 Nov).

    python research/tools/lp_flow.py CONDS.json OUT.json [--since EPOCH] [--size 20]

CONDS.json: list of condition ids (e.g. the markets the phase 2 bot quoted, or a simulator's choice).
Public taker trades of each market (data-api, newest 1000) since `since` (default: 48 h ago). Per market:
  - flow: share of contracts (min(trade, size), worst case: every trade fills us) where the taker BOUGHT YES
    (bought YES or sold NO); imbalance = |buy share - 0.5| x 2 (0 = balanced, 1 = all one way);
  - passive exit: after a fill on one side (we hold the position), time until the next taker trade in the opposite
    direction (that trade would fill a reducing quote at the best price; one tick better puts us first in the
    queue). Share of fills with such a trade within 30 and 60 minutes, median wait;
  - taker wallets (proxy_wallet), for relatedness: pairs of markets whose taker sets overlap (Jaccard) or whose
    event slugs share words.
"""
import bisect
import itertools
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polybot import lp  # noqa: E402


def main():
    conds, out = json.load(open(sys.argv[1])), sys.argv[2]
    since = float(sys.argv[sys.argv.index("--since") + 1]) if "--since" in sys.argv else time.time() - 48 * 3600
    size = float(sys.argv[sys.argv.index("--size") + 1]) if "--size" in sys.argv else 20.0
    res, wallets = {}, {}
    for c in conds:
        try:
            d = lp._req(f"{lp.DATA}/v2/trades?" + urllib.parse.urlencode({"condition": c, "taker_only": "true", "limit": 1000}))
        except Exception as e:
            print(f"{c[:10]} failed: {e!r:.100}", flush=True)
            continue
        tr = sorted((r for r in d.get("data") or [] if r["timestamp"] >= since), key=lambda r: r["timestamp"])
        if not tr:
            res[c] = {"trades": 0}
            continue
        rows = []
        for r in tr:
            buy_yes = (r["side"] == "BUY") == (r["outcome_index"] == 0)
            rows.append((r["timestamp"], buy_yes, min(float(r["size"]), size), r.get("proxy_wallet")))
        q = sum(z for _, _, z, _ in rows)
        buy = sum(z for _, b, z, _ in rows if b)
        ts_buy = [t for t, b, _, _ in rows if b]
        ts_sell = [t for t, b, _, _ in rows if not b]
        waits = []
        for t, b, z, _ in rows:
            opp = ts_sell if b else ts_buy          # a taker buying YES lifts our ask -> we are short -> need a seller
            j = bisect.bisect_right(opp, t)
            waits.append(((opp[j] - t) if j < len(opp) else None, z))
        w = lambda lim: sum(z for x, z in waits if x is not None and x <= lim) / q
        known = sorted(x for x, _ in waits if x is not None)
        wallets[c] = {wl for *_, wl in rows if wl}
        res[c] = {"q": tr[0].get("title"), "event_slug": tr[0].get("event_slug"), "trades": len(rows),
                  "contracts": q, "hours": (rows[-1][0] - rows[0][0]) / 3600 if len(rows) > 1 else 0.0,
                  "buy_yes_share": buy / q, "imbalance": abs(buy / q - 0.5) * 2,
                  "exit_within_30m": w(1800), "exit_within_60m": w(3600),
                  "exit_median_min": known[len(known) // 2] / 60 if known else None,
                  "takers": len(wallets[c])}
    pairs = []
    for a, b in itertools.combinations([c for c in wallets if wallets[c]], 2):
        j = len(wallets[a] & wallets[b]) / len(wallets[a] | wallets[b])
        wa = set(re.findall(r"[a-z]{4,}", (res[a].get("event_slug") or "")))
        wb = set(re.findall(r"[a-z]{4,}", (res[b].get("event_slug") or "")))
        if j >= 0.1 or len(wa & wb) >= 2:
            pairs.append({"a": a, "b": b, "qa": res[a].get("q"), "qb": res[b].get("q"), "jaccard": j,
                          "shared_slug_words": sorted(wa & wb)})
    json.dump({"since": since, "size": size, "markets": res, "related": pairs}, open(out, "w"), indent=1)
    for c, r in sorted(res.items(), key=lambda kv: -(kv[1].get("imbalance") or 0)):
        if not r.get("trades"):
            continue
        print(f"{(r['q'] or c)[:58]:58s} trades {r['trades']:4d} contracts {r['contracts']:6.0f} buyYES {r['buy_yes_share']:4.0%} "
              f"imbalance {r['imbalance']:4.0%} exit<=30m {r['exit_within_30m']:4.0%} <=60m {r['exit_within_60m']:4.0%} "
              f"median {r['exit_median_min'] if r['exit_median_min'] is None else round(r['exit_median_min'])} min takers {r['takers']}")
    for p in sorted(pairs, key=lambda p: -p["jaccard"])[:20]:
        print(f"related: J={p['jaccard']:.2f} {p['shared_slug_words']} | {(p['qa'] or '')[:45]} <-> {(p['qb'] or '')[:45]}")


if __name__ == "__main__":
    main()
