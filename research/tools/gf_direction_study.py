"""Which signal drives a wallet's directional accumulation? Tested on its real fills.

For every BUY fill of the wallet (match time = on-chain time - 3 s) the direction is +1 for Up,
-1 for Down. Candidate signals, all computed from data available one second before the fill:
  spot momentum      Binance 1 s close change over N seconds, bps
  spot CCI(20)       on 5 s bars of the Binance close
  market momentum    change of the Up price over N seconds, cents (public tape, all participants)
  market CCI(20)     on 5 s bars of the Up price
  Up price level     distance of the Up price from 50 cents
Reported: share-weighted mean direction by signal quintile (+1 = all Up), the correlation, and
per market whether the final net position (Up - Down shares) sided with the winner.

usage: gf_direction_study.py ACTIVITY.jsonl TAKER.jsonl TAPE.csv RES.json BTC_1S.npz
"""
import bisect
import collections
import json
import math
import re
import sys

import numpy as np

act_p, tak_p, tape_p, res_p, npz = sys.argv[1:6]
DELAY = 3
res = json.load(open(res_p))
z = np.load(npz); T0 = int(z["t0"]); CLOSE = z["close"]
T_END = T0 + len(CLOSE)

# ---- spot signals, vectorized -------------------------------------------------------------------
BAR = 5
nb = len(CLOSE) // BAR
bars = CLOSE[BAR - 1::BAR][:nb]                       # close of each 5 s bar


def cci(x, n=20):
    w = np.lib.stride_tricks.sliding_window_view(x, n)
    sma = w.mean(axis=1)
    mad = np.abs(w - sma[:, None]).mean(axis=1)
    out = np.full(len(x), np.nan)
    out[n - 1:] = (x[n - 1:] - sma) / (0.015 * np.maximum(mad, 1e-9))
    return out


SPOT_CCI = cci(bars)


def spot_mom(sec, n):
    i = sec - T0
    return 1e4 * (CLOSE[i] / CLOSE[i - n] - 1)


def spot_cci(sec):
    b = (sec - T0 + 1) // BAR - 1                       # last completed bar
    return SPOT_CCI[b] if b >= 0 else np.nan


# ---- market Up-price paths from the public tape -------------------------------------------------
path = collections.defaultdict(list)                   # start -> [(ts, up_price_cents)]
for line in open(tape_p):
    p = line.split(",")
    if len(p) < 8:
        continue
    start, ts, oi, px = int(p[0]), int(p[1]) - DELAY, int(p[2]), float(p[4])
    path[start].append((ts, 100 * (px if oi == 0 else 1 - px)))
for k in path:
    path[k].sort()
pts = {k: [a for a, _ in v] for k, v in path.items()}


def mkt_price(start, sec):
    v = path.get(start)
    if not v:
        return None
    i = bisect.bisect_right(pts[start], sec) - 1
    return v[i][1] if i >= 0 else None


def mkt_cci(start, sec, n=20):
    xs = []
    for b in range(n - 1, -1, -1):
        x = mkt_price(start, sec - BAR * b)
        if x is None:
            return None
        xs.append(x)
    xs = np.array(xs); sma = xs.mean(); mad = np.abs(xs - sma).mean()
    return (xs[-1] - sma) / (0.015 * mad) if mad > 1e-9 else 0.0


# ---- wallet fills -------------------------------------------------------------------------------
key = lambda r: (r["transaction_hash"], r["token_id"], r["side"], round(r["price"], 6), round(r["size"], 6))
taker = collections.Counter()
for line in open(tak_p):
    r = json.loads(line)
    if T0 + 400 <= r["timestamp"] < T_END:
        taker[key(r)] += 1
SIGS = ["spot mom 10s", "spot mom 30s", "spot mom 60s", "spot CCI20x5s",
        "mkt mom 10s", "mkt mom 30s", "mkt mom 60s", "mkt CCI20x5s", "Up price - 50"]
obs = {s: {"maker": [], "taker": []} for s in SIGS}   # (signal, direction, shares)
net = collections.defaultdict(float); gross = collections.defaultdict(float); winner = {}
mkt_n = 0
for line in open(act_p):
    r = json.loads(line)
    if r["type"] != "TRADE" or r["side"] != "BUY" or not (T0 + 400 <= r["timestamp"] < T_END):
        continue
    m = re.match(r"^btc-updown-5m-(\d+)$", r.get("slug") or "")
    if not m:
        continue
    start = int(m.group(1))
    k = key(r); role = "taker" if taker[k] > 0 else "maker"
    if role == "taker":
        taker[k] -= 1
    sec = r["timestamp"] - DELAY - 1                     # information one second before the match
    d = 1 if r["outcome_index"] == 0 else -1
    sz = r["size"]
    net[start] += d * sz; gross[start] += sz
    w = res.get(r["condition_id"], -1)
    if w >= 0:
        winner[start] = w
    vals = {"spot mom 10s": spot_mom(sec, 10), "spot mom 30s": spot_mom(sec, 30), "spot mom 60s": spot_mom(sec, 60),
            "spot CCI20x5s": spot_cci(sec)}
    if start in path:
        p0 = mkt_price(start, sec)
        for n in (10, 30, 60):
            p1 = mkt_price(start, sec - n)
            vals[f"mkt mom {n}s"] = None if p0 is None or p1 is None else p0 - p1
        vals["mkt CCI20x5s"] = mkt_cci(start, sec)
        vals["Up price - 50"] = None if p0 is None else p0 - 50
    for s, v in vals.items():
        if v is not None and not (isinstance(v, float) and math.isnan(v)):
            obs[s][role].append((v, d, sz))

print("share-weighted mean direction of the wallet's buys (+1 = all Up, -1 = all Down) by signal quintile")
print(f"{'signal':>15}{'role':>7}{'fills':>8}" + "".join(f"{f'Q{q+1}':>15}" for q in range(5)) + f"{'corr':>7}")
for s in SIGS:
    for role in ("maker", "taker"):
        o = obs[s][role]
        if len(o) < 500:
            continue
        a = np.array(o)
        qs = np.quantile(a[:, 0], [0.2, 0.4, 0.6, 0.8])
        b = np.searchsorted(qs, a[:, 0])
        cells = []
        for q in range(5):
            mask = b == q
            dirm = np.average(a[mask, 1], weights=a[mask, 2])
            lo = a[mask, 0].min(); hi = a[mask, 0].max()
            cells.append(f"{dirm:+.2f} [{lo:.0f},{hi:.0f}]" if abs(hi) >= 10 or abs(lo) >= 10 else f"{dirm:+.2f} [{lo:.1f},{hi:.1f}]")
        c = np.corrcoef(a[:, 0], a[:, 1])[0, 1]
        print(f"{s:>15}{role:>7}{len(o):8d}" + "".join(f"{x:>15}" for x in cells) + f"{c:7.3f}")

# ---- per market: did the net lean side with the winner? -----------------------------------------
lean = [(net[s] / gross[s], winner[s]) for s in net if s in winner and gross[s] > 0]
strong = [(x, w) for x, w in lean if abs(x) >= 0.2]
hit = sum(1 for x, w in strong if (x > 0) == (w == 0))
print(f"\nmarkets: {len(lean)}; final net / gross: median |lean| {np.median([abs(x) for x, _ in lean]):.2f}")
print(f"markets with |lean| >= 0.2: {len(strong)}; lean sided with the winner in {hit} = {100 * hit / max(len(strong), 1):.1f}%"
      f"  (binomial z = {(hit - len(strong) / 2) / math.sqrt(len(strong) / 4):.1f})")
