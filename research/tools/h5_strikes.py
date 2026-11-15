"""H5 (research/PREREG_H5.md): daily "Bitcoin above $K" markets against a lognormal model.

usage: h5_strikes.py OUT_DIR BTC_1S.npz SPLIT_ISO
"""
import calendar
import collections
import gzip
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import numpy as np

GAMMA = "https://gamma-api.polymarket.com/events/keyset?"
TRADES = "https://data-api.polymarket.com/v2/trades?"


def get(url):
    for i in range(7):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-client/1.0", "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
        except Exception:
            time.sleep(min(2 ** i, 30))
    raise RuntimeError(url)


def fetch(out):
    mpath, fpath = f"{out}/markets.json", f"{out}/fills.csv"
    if os.path.exists(mpath) and os.path.exists(fpath):
        return json.load(open(mpath))
    markets, cur = {}, None
    while True:
        q = {"closed": "true", "tag_slug": "crypto", "title_search": "Bitcoin above", "limit": 100,
             "end_date_min": "2026-10-15T00:00:00Z", "end_date_max": "2026-11-13T00:00:00Z"}
        if cur:
            q["after_cursor"] = cur
        d = get(GAMMA + urllib.parse.urlencode(q))
        for e in d.get("events") or []:
            if not re.match(r"^Bitcoin above ___ on", e.get("title", "")):
                continue
            for m in e.get("markets", []):
                k = re.search(r"\$([\d,]+)", m.get("question", ""))
                prices = json.loads(m.get("outcomePrices") or "[]")
                if not k or [float(x) for x in prices] not in ([1.0, 0.0], [0.0, 1.0]):
                    continue
                end = calendar.timegm(time.strptime(m["endDate"][:19], "%Y-%m-%dT%H:%M:%S"))
                markets[m["conditionId"]] = {"event": e["id"], "K": float(k.group(1).replace(",", "")), "end": end,
                                             "yes_won": float(prices[0]) == 1.0}
        cur = d.get("next_cursor")
        if not cur or not d.get("events"):
            break
    json.dump(markets, open(mpath, "w"))
    ids = list(markets)
    with open(fpath, "w") as f:
        for i in range(0, len(ids), 20):
            chunk = ids[i:i + 20]; cur = None
            while True:
                q = {"condition": ",".join(chunk), "taker_only": "true", "limit": 1000}
                if cur:
                    q = {"condition": ",".join(chunk), "taker_only": "true", "cursor": cur}
                d = get(TRADES + urllib.parse.urlencode(q))
                for r in d.get("data") or []:
                    f.write(f"{r['condition_id']},{r['timestamp']},{r['outcome_index']},{r['side'][0]},{r['price']},{r['size']}\n")
                cur = (d.get("pagination") or {}).get("next_cursor")
                if not cur or not d.get("data"):
                    break
    return markets


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def main():
    out, npz, split_iso = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out, exist_ok=True)
    split = calendar.timegm(time.strptime(split_iso, "%Y-%m-%dT%H:%M:%SZ"))
    markets = fetch(out)
    z = np.load(npz); T0 = int(z["t0"]); S = z["close"]
    lr = np.diff(np.log(S)); c2 = np.concatenate([[0.0], np.cumsum(lr * lr)])
    VOLWIN = 6 * 3600

    def model_yes(t, K, end):
        i = t - T0
        if i - VOLWIN < 0 or i >= len(S):
            return None
        var_s = (c2[i] - c2[i - VOLWIN]) / VOLWIN
        tau = end - t
        sd = math.sqrt(var_s * tau)
        if sd <= 0:
            return None
        return phi(math.log(S[i] / K) / sd - sd / 2)
    THETAS = (0.02, 0.05, 0.10)
    SC = {"base": (0, 0.0), "slip +1c": (0, 0.01), "spot 5 min stale": (300, 0.0)}
    acc = {p: {s: {th: collections.defaultdict(lambda: [0.0, 0.0, 0.0]) for th in THETAS} for s in SC} for p in ("sel", "hold")}
    events = {"sel": set(), "hold": set()}; n_fill = 0
    for line in open(f"{out}/fills.csv"):
        c, ts, oi, side, px, sz = line.rstrip("\n").split(",")
        m = markets.get(c)
        if not m:
            continue
        ts, oi, px, sz = int(ts), int(oi), float(px), float(sz)
        if not (m["end"] - 24 * 3600 <= ts <= m["end"] - 1800):
            continue
        lo, lp = (oi, px) if side == "B" else (1 - oi, 1 - px)        # outcome 0 = Yes
        if not 0.02 <= lp <= 0.98:
            continue
        part = "hold" if m["end"] >= split else "sel"
        events[part].add(m["event"]); n_fill += 1
        won = (lo == 0) == m["yes_won"]
        for s, (stale, slip) in SC.items():
            py = model_yes(ts - stale, m["K"], m["end"])
            if py is None:
                continue
            pm = py if lo == 0 else 1 - py
            p = min(lp + slip, 0.999)
            edge = pm - p - 0.07 * p * (1 - p)
            for th in THETAS:
                if edge >= th:
                    a = acc[part][s][th][m["event"]]
                    a[0] += sz * ((1.0 if won else 0.0) - p - 0.07 * p * (1 - p)); a[1] += sz; a[2] += sz * p
    print(f"markets {len(markets)}; fills in window {n_fill}; events selection {len(events['sel'])}, hold-out {len(events['hold'])}")
    for part in ("sel", "hold"):
        print(f"\n{'selection' if part == 'sel' else 'hold-out'}")
        print(f"{'scenario':<18}{'theta':>6}{'shares':>9}{'cost $':>9}{'pnl $':>9}{'ret %':>8}{'t':>7}{'events':>7}")
        for s in SC:
            for th in THETAS:
                dct = acc[part][s][th]
                sh = sum(v[1] for v in dct.values()); cost = sum(v[2] for v in dct.values())
                if sh <= 0:
                    print(f"{s:<18}{th:6.2f}{0:9d}"); continue
                ev = list(events[part]); pm = [dct[e][0] if e in dct else 0.0 for e in ev]
                mean = sum(pm) / len(pm); sd = math.sqrt(sum((x - mean) ** 2 for x in pm) / len(pm))
                print(f"{s:<18}{th:6.2f}{sh:9.0f}{cost:9.0f}{sum(pm):9.0f}{100 * sum(pm) / cost:8.2f}"
                      f"{(mean / sd * math.sqrt(len(pm)) if sd else 0):7.2f}{len(dct):7d}")


if __name__ == "__main__":
    main()
