"""Which price rule reproduces actual BTC 5m resolutions? Tested on Binance 1s closes.

usage: resolution_rule.py ACTIVITY.jsonl RES.json BTC_1S.npz
"""
import json
import re
import sys

import numpy as np

act, res, npz = sys.argv[1:4]
res = json.load(open(res))
z = np.load(npz); t0 = int(z["t0"]); S = z["close"]
P = lambda t: S[t - t0]            # close of second t  (price at instant t+1)
M = lambda a, b: S[a - t0:b - t0].mean()  # mean of closes of seconds [a, b)

markets = {}
for line in open(act):
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    m = re.match(r"^btc-updown-5m-(\d+)$", r.get("slug") or "")
    if m and r["condition_id"] in res and res[r["condition_id"]] >= 0:
        markets[int(m.group(1))] = res[r["condition_id"]]  # 0 = Up won, 1 = Down won
rows = [(s, w) for s, w in sorted(markets.items()) if s - 120 >= t0 and s + 300 < t0 + len(S)]
print(f"markets with known winner inside spot coverage: {len(rows)}; Up won in {100*np.mean([w == 0 for _, w in rows]):.1f}%")
rules = {
    "spot(end) >= spot(start)":                  lambda s: P(s + 299) >= P(s - 1),
    "twap60(end) >= twap60(start)":              lambda s: M(s + 240, s + 300) >= M(s - 60, s),
    "twap60(end) >= spot(start)":                lambda s: M(s + 240, s + 300) >= P(s - 1),
    "twap(whole window) >= spot(start)":         lambda s: M(s, s + 300) >= P(s - 1),
    "twap(whole window) >= twap60(start)":       lambda s: M(s, s + 300) >= M(s - 60, s),
    "twap30(end) >= twap30(start)":              lambda s: M(s + 270, s + 300) >= M(s - 30, s),
}
for name, f in rules.items():
    hit = np.mean([(0 if f(s) else 1) == w for s, w in rows])
    print(f"  {name:40} agrees with actual outcome: {100*hit:.2f}%")
# how decisive is the margin when the best rule disagrees?
best = rules["spot(end) >= spot(start)"]
miss = [abs(P(s + 299) / P(s - 1) - 1) * 1e4 for s, w in rows if (0 if best(s) else 1) != w]
allm = [abs(P(s + 299) / P(s - 1) - 1) * 1e4 for s, w in rows]
print(f"  |5m move| bps: median all {np.median(allm):.2f}; median where spot rule disagrees {np.median(miss) if miss else float('nan'):.2f}")
