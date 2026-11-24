"""How often does MarketSession.spot_proxy_outcome agree with the real resolution?

Part A: every market of the public tape, spot = Binance 1s closes (the only historical source).
Part B: recorded markets, spot = the recorded Coinbase and Binance ticks (the live code path).
The proxy is exercised through the same method the simulator uses.

usage: proxy_agreement.py TAPE.csv RES.json ACTIVITY.jsonl BTC_1S.npz REC_DIR
"""
import json
import os
import re
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from polybot.engine import MarketSession, PaperExecutor   # noqa: E402
from polybot.fairvalue import SpotSeries                  # noqa: E402
from polybot.replay import list_markets, spot_events      # noqa: E402
from polybot.strategy import Params                       # noqa: E402

tape, res_p, act, npz, rec = sys.argv[1:6]
res = json.load(open(res_p)); win = {}
for line in open(act):
    try:
        r = json.loads(line)
    except json.JSONDecodeError:
        continue
    m = re.match(r"^btc-updown-5m-(\d+)$", r.get("slug") or "")
    if m and res.get(r.get("condition_id"), -1) >= 0:
        win[int(m.group(1))] = res[r["condition_id"]]
starts = sorted({int(line.split(",", 1)[0]) for line in open(tape) if line[:1].isdigit()})
z = np.load(npz); t0 = int(z["t0"]); S = z["close"]


def proxy_from_closes(start):
    spot = SpotSeries()
    for sec in range(start - 70, start + 301):
        spot.update((sec + 1) * 1000 - 1, float(S[sec - t0]))     # close of second `sec`
    ses = MarketSession(start, Params(), spot, PaperExecutor(None))
    return ses.spot_proxy_outcome()


n = agree = 0
by_move = {}
for s in starts:
    w = win.get(s)
    if w is None or s - 80 < t0 or s + 301 - t0 >= len(S):
        continue
    p = proxy_from_closes(s)
    if p is None:
        continue
    n += 1; agree += (p == w)
    k0 = S[s - 60 - t0:s - t0].mean(); k1 = S[s + 240 - t0:s + 300 - t0].mean()
    bps = abs(k1 / k0 - 1) * 1e4
    b = "<0.5" if bps < 0.5 else "0.5-1" if bps < 1 else "1-2" if bps < 2 else "2-5" if bps < 5 else ">=5"
    a = by_move.setdefault(b, [0, 0]); a[0] += 1; a[1] += (p == w)
print(f"A. tape markets with a known resolution and spot coverage: {n}")
print(f"   proxy agrees with the real resolution: {agree} = {100 * agree / n:.2f}%  (disagrees in {100 * (n - agree) / n:.2f}%)")
print("   by size of the TWAP move (bps):  " + "  ".join(f"{k}: {v[1]}/{v[0]} ({100*v[1]/v[0]:.0f}%)" for k, v in sorted(by_move.items(), key=lambda kv: ["<0.5", "0.5-1", "1-2", "2-5", ">=5"].index(kv[0]))))

outcomes = json.load(open(os.path.join(rec, "outcomes.json")))
for source in ("coinbase", "binance"):
    n = agree = miss = 0
    for start, _ in list_markets(rec):
        w = outcomes.get(str(start))
        if w is None:
            continue
        spot = SpotSeries()
        for _, t, mid in spot_events(rec, (start - 400) * 1000, (start + 302) * 1000, source):
            spot.update(t, mid)
        if spot.sec0 is None or spot.sec0 > start - 60:
            miss += 1; continue
        p = MarketSession(start, Params(), spot, PaperExecutor(None)).spot_proxy_outcome()
        if p is None:
            miss += 1; continue
        n += 1; agree += (p == w)
    print(f"B. recorded markets, {source}: {agree}/{n} agree ({100 * agree / max(n, 1):.1f}%); {miss} without enough spot")
