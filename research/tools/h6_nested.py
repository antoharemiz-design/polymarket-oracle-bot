"""H6 (research/PREREG_H6.md): nested BTC 15m / 5m windows sharing the final TWAP.

usage: h6_nested.py OUT_DIR TAPE_5M.csv BTC_1S.npz SPLIT_EPOCH
"""
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

GAMMA = "https://gamma-api.polymarket.com/markets?"
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


def _one15(s):
    d = get(GAMMA + urllib.parse.urlencode({"slug": f"btc-updown-15m-{s}", "closed": "true"}))
    if d:
        m = d[0]; pr = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        if sorted(pr) == [0.0, 1.0]:
            return str(s), {"cond": m["conditionId"], "winner": pr.index(1.0)}
    return None


def _trades(chunk, by_cond):
    rows, cur = [], None
    while True:
        q = {"condition": ",".join(chunk), "taker_only": "true", "limit": 1000}
        if cur:
            q["cursor"] = cur
        d = get(TRADES + urllib.parse.urlencode(q))
        for r in d.get("data") or []:
            rows.append(f"{by_cond[r['condition_id']]},{r['timestamp']},{r['outcome_index']},{r['side'][0]},{r['price']},{r['size']}\n")
        cur = (d.get("pagination") or {}).get("next_cursor")
        if not cur or not d.get("data"):
            return rows


def fetch15(out, starts):
    from concurrent.futures import ThreadPoolExecutor
    mpath, fpath = f"{out}/m15.json", f"{out}/fills15.csv"
    if os.path.exists(mpath) and os.path.exists(fpath):
        return json.load(open(mpath))
    with ThreadPoolExecutor(8) as ex:
        m15 = dict(x for x in ex.map(_one15, starts) if x)
    json.dump(m15, open(mpath, "w"))
    print(f"15m markets resolved: {len(m15)} of {len(starts)}", flush=True)
    by_cond = {v["cond"]: k for k, v in m15.items()}
    ids = list(by_cond)
    chunks = [ids[i:i + 5] for i in range(0, len(ids), 5)]
    with open(fpath + ".part", "w") as f, ThreadPoolExecutor(8) as ex:
        for n, rows in enumerate(ex.map(lambda c: _trades(c, by_cond), chunks)):
            f.writelines(rows)
            if n % 20 == 0:
                print(f"  trades: {n}/{len(chunks)} chunks", flush=True)
    os.replace(fpath + ".part", fpath)
    return m15


def main():
    out, tape, npz, split = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    os.makedirs(out, exist_ok=True)
    win5 = set()
    for line in open(tape):
        win5.add(int(line.split(",", 1)[0]))
    starts15 = sorted({s - 600 for s in win5 if (s - 600) % 900 == 0})
    m15 = fetch15(out, starts15)
    z = np.load(npz); T0 = int(z["t0"]); C = z["close"]
    twap = lambda a: float(C[a - 60 - T0:a - T0].mean()) if a - 60 - T0 >= 0 and a - T0 <= len(C) else None
    # taker buys per market and second: (seconds into window, outcome taker ends long, price, size)
    def load(path, key_is_start):
        d = collections.defaultdict(list)
        for line in open(path):
            p = line.rstrip("\n").split(",")
            s = int(p[0]); ts, oi, side, px, sz = int(p[1]), int(p[2]), p[3], float(p[4]), float(p[5])
            lo, lp = (oi, px) if side == "B" else (1 - oi, 1 - px)
            d[s].append((ts, lo, lp, sz))
        return d
    f15 = load(f"{out}/fills15.csv", True)
    f5 = collections.defaultdict(list)
    for line in open(tape):
        p = line.split(",")
        if len(p) < 8:
            continue
        s = int(p[0])
        if (s - 600) % 900 == 0 and str(s - 600) in m15:
            ts, oi, side, px, sz = int(p[1]), int(p[2]), p[3], float(p[4]), float(p[5])
            lo, lp = (oi, px) if side == "B" else (1 - oi, 1 - px)
            f5[s].append((ts, lo, lp, sz))
    FEE = lambda p: 0.07 * p * (1 - p)
    stat = {part: {"pairs": 0, "windows": 0, "arb_windows": 0, "arb": [], "dK": []} for part in ("sel", "hold")}
    for s15k, info in m15.items():
        s15 = int(s15k); s5 = s15 + 600
        if s5 not in win5 or s5 not in f5:
            continue
        k15, k5 = twap(s15), twap(s5)
        if k15 is None or k5 is None or abs(k5 - k15) < 10:
            continue
        part = "hold" if s15 >= split else "sel"
        st = stat[part]; st["windows"] += 1; st["dK"].append(abs(k5 - k15))
        # legs: K15 <= K5 -> long 15m Up (0) and 5m Down (1); else long 15m Down (1) and 5m Up (0)
        o15, o5 = (0, 1) if k15 <= k5 else (1, 0)
        a = sorted((ts, lp, sz) for ts, lo, lp, sz in f15.get(s15, []) if lo == o15 and ts >= s5)
        b = sorted((ts, lp, sz) for ts, lo, lp, sz in f5[s5] if lo == o5)
        best = None
        j0 = 0
        for ta, pa, sa in a:
            while j0 < len(b) and b[j0][0] < ta - 2:
                j0 += 1
            j = j0
            while j < len(b) and b[j][0] <= ta + 2:
                pb, sb = b[j][1], b[j][2]
                st["pairs"] += 1
                profit = 1 - pa - pb - FEE(pa) - FEE(pb)
                if profit > 0 and (best is None or profit > best[0]):
                    best = (profit, min(sa, sb))
                j += 1
        if best:
            st["arb_windows"] += 1; st["arb"].append(best)
    for part in ("sel", "hold"):
        st = stat[part]
        arb = st["arb"]
        prof = sorted(p for p, _ in arb)
        print(f"{'selection' if part == 'sel' else 'hold-out'}: windows with |K5-K15| >= $10: {st['windows']}, "
              f"leg pairs within 2 s: {st['pairs']}, windows with a sub-$1 basket after fees: {st['arb_windows']} "
              f"({100 * st['arb_windows'] / max(st['windows'], 1):.1f}%)")
        if arb:
            print(f"   profit per share: median {100 * prof[len(prof) // 2]:.2f} c, max {100 * prof[-1]:.2f} c; "
                  f"size at best pair: median {sorted(s for _, s in arb)[len(arb) // 2]:.0f} shares; "
                  f"sum over windows of profit x size ${sum(p * s for p, s in arb):.2f}")
        if st["dK"]:
            print(f"   median |K5 - K15| ${sorted(st['dK'])[len(st['dK']) // 2]:.0f}")


if __name__ == "__main__":
    main()
