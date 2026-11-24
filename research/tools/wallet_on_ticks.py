"""Place one wallet's fills into recorded tick data: what did the book and the model look like?

The public tape gives the wallet's fills with transaction hashes; the recorder stored every trade
with the same hash and a millisecond match time. For each of the wallet's TAKER fills this prints
the edge it had against the book mid and against the spot model `lat` ms before the match, and
the markout afterwards.

usage: wallet_on_ticks.py REC_DIR WALLET [lat_ms=150]
"""
import bisect
import collections
import gzip
import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from polybot.book import UpBook                       # noqa: E402
from polybot.fairvalue import FairValue, SpotSeries   # noqa: E402
from polybot.replay import list_markets, merged       # noqa: E402
from polybot.strategy import Fair                     # noqa: E402

rec, wallet = sys.argv[1], sys.argv[2].lower()
LAT = int(sys.argv[3]) if len(sys.argv) > 3 else 150
GRID = 100


def get(u):
    req = urllib.request.Request(u, headers={"User-Agent": "research-client/1.0"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def wallet_fills(start, end, taker_only):
    out, cursor = [], None
    while True:
        q = {"user": wallet, "start": start, "end": end, "limit": 1000, "taker_only": "true" if taker_only else "false"}
        if cursor:
            q["cursor"] = cursor; q.pop("limit")
        d = get("https://data-api.polymarket.com/v2/trades?" + urllib.parse.urlencode(q))
        rows = [r for r in d.get("data") or [] if start <= r["timestamp"] <= end]
        out += rows
        cursor = (d.get("pagination") or {}).get("next_cursor")
        if not cursor or len(rows) < len(d.get("data") or [1]):
            break
    return out


ms = list_markets(rec)
lo, hi = ms[0][0], ms[-1][0] + 400
taker_tx = {r["transaction_hash"] for r in wallet_fills(lo, hi, True)}
all_fills = wallet_fills(lo, hi, False)
by_tx = collections.defaultdict(list)
for r in all_fills:
    by_tx[r["transaction_hash"]].append(r)
print(f"wallet fills in recorded span: {len(all_fills)} ({sum(1 for r in all_fills if r['transaction_hash'] in taker_tx)} as taker)")
outcomes = json.load(open(os.path.join(rec, "outcomes.json"))) if os.path.exists(os.path.join(rec, "outcomes.json")) else {}
H = (1000, 5000, 30000)
agg = collections.defaultdict(lambda: [0.0] * 9)   # role -> shares, edge_mid, edge_model, m1, m5, m30, settle, settle_sh, fills
spread_hist = collections.Counter()
for start, path in ms:
    spot = SpotSeries(); fv = FairValue(spot, start); book = UpBook(); fair = Fair(1.0)
    gt, gmid, gf, gsp = [], [], [], []
    trades = []
    nxt, end_ms = start * 1000, (start + 300) * 1000
    for e in merged(rec, start, path):
        t = e[1]
        while t >= nxt and nxt <= end_ms:
            if book.ready and spot.ready(start):
                bb, ba = book.best_bid(), book.best_ask()
                comp = fv.components(nxt)
                if comp is not None and comp[1] > 0 and 0 < bb < ba < 100:
                    f, _ = fair.update(nxt, comp, bb, ba, spot.move(nxt, 500), fv.sens(nxt))
                    gt.append(nxt); gmid.append((bb + ba) / 2.0); gf.append(f); gsp.append(ba - bb)
            nxt += GRID
        k = e[0]
        if k == "S":
            spot.update(t, e[2])
        elif k == "B":
            book.snapshot(e[2], e[3])
        elif k == "P":
            if book.ready:
                book.change(e[2], e[3], e[4])
        elif k == "T":
            trades.append(e)
    if len(gt) < 500:
        continue
    w = outcomes.get(str(start))
    at = lambda tt: bisect.bisect_right(gt, tt) - 1
    for _, t, tside, px, sz, tx, _srv in trades:
        if tx not in by_tx:
            continue
        i0 = at(t - LAT)
        if i0 < 0 or gt[i0] < t - LAT - 500:
            continue
        for r in by_tx[tx]:
            # the wallet bought outcome `oi` at r.price; express in Up terms: sign +1 = long Up
            oi, p_c, shares = r["outcome_index"], 100 * r["price"], r["size"]
            sign = 1 if oi == 0 else -1
            px_up = p_c if oi == 0 else 100 - p_c
            role = "taker" if tx in taker_tx else "maker"
            a = agg[role]
            outs = []
            for h in H:
                j = at(t + h)
                outs.append(sign * (gmid[j] - px_up) if j >= 0 and gt[j] >= t + h - 2 * GRID else None)
            if None in outs:
                continue
            a[0] += shares; a[8] += 1
            a[1] += shares * sign * (gmid[i0] - px_up)
            a[2] += shares * sign * (gf[i0] - px_up)
            for q in range(3):
                a[3 + q] += shares * outs[q]
            if w is not None:
                a[6] += shares * sign * ((100.0 if w == 0 else 0.0) - px_up); a[7] += shares
            if role == "taker":
                spread_hist[int(gsp[i0])] += shares
print(f"\nwallet's edge per share in cents, information {LAT} ms before the match (positive = good for the wallet)")
print(f"{'role':>6}{'fills':>7}{'shares':>9}{'vs mid':>8}{'vs model':>9}{'+1s':>7}{'+5s':>7}{'+30s':>7}{'settle':>8}")
for role in ("taker", "maker"):
    a = agg[role]
    if a[0]:
        print(f"{role:>6}{a[8]:7.0f}{a[0]:9.0f}{a[1]/a[0]:8.2f}{a[2]/a[0]:9.2f}{a[3]/a[0]:7.2f}{a[4]/a[0]:7.2f}{a[5]/a[0]:7.2f}{(a[6]/a[7] if a[7] else 0):8.2f}")
tot = sum(spread_hist.values()) or 1
print("taker fills by book spread just before (cents: share of volume):", {k: f"{100*v/tot:.0f}%" for k, v in sorted(spread_hist.items())})
