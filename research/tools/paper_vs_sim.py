"""Stage 5 check: does paper trading on the live stream agree with the backtest of the same markets?

Reads `settle` records from a paper journal and re-runs the simulator on the recorder files of the
same markets with the parameters stored in the journal's `start` record.

usage: paper_vs_sim.py JOURNAL.jsonl REC_DIR
"""
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 3)[0])
from polybot.replay import list_markets      # noqa: E402
from polybot.sim import run_market           # noqa: E402
from polybot.strategy import Params          # noqa: E402

journal, rec = sys.argv[1], sys.argv[2]
start_rec, settles, fills = None, {}, {}
for line in open(journal):
    r = json.loads(line)
    if r["event"] == "start":
        start_rec = r; settles, fills = {}, {}       # only the last run in the file
    elif r["event"] == "settle":
        settles[r["market"]] = r
    elif r["event"] == "fill":
        fills.setdefault(r["market"], []).append(r)
p = Params(**start_rec["params"])
mode, theta, lat, qm = start_rec.get("strategy", "maker"), start_rec.get("taker_theta", 1.0), start_rec["lat_ms"], start_rec["queue_mult"]
paths = dict(list_markets(rec))
print(f"strategy={mode} theta={theta} lat={lat}ms; markets settled in paper: {len(settles)}")
print(f"{'market':>12}{'out':>4} | {'paper fills':>11}{'shares':>8}{'cost$':>8}{'pnl$':>8} | {'sim fills':>9}{'shares':>8}{'cost$':>8}{'pnl$':>8}")
tot = [0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0]
n = 0
for m in sorted(settles):
    s = settles[m]
    if m not in paths or s["pnl"] is None:
        continue
    r = run_market(rec, m, paths[m], p, mode, lat, qm, s["outcome"], taker_theta=theta)
    n += 1
    row = [s["fills"], s["up"] + s["dn"], s["cost"], s["pnl"], r.fills, r.q_up + r.q_dn, r.cost, r.pnl]
    tot = [a + b for a, b in zip(tot, row)]
    print(f"{m:>12}{s['outcome']:>4} | {row[0]:11d}{row[1]:8.1f}{row[2]:8.2f}{row[3]:8.2f} | {row[4]:9d}{row[5]:8.1f}{row[6]:8.2f}{row[7]:8.2f}")
print(f"{'TOTAL':>12}{n:>4} | {tot[0]:11d}{tot[1]:8.1f}{tot[2]:8.2f}{tot[3]:8.2f} | {tot[4]:9d}{tot[5]:8.1f}{tot[6]:8.2f}{tot[7]:8.2f}")
if tot[5] > 0 and tot[1] > 0:
    print(f"shares traded: paper/sim = {tot[1] / tot[5]:.2f};  pnl per share: paper {100 * tot[3] / tot[1]:.2f}c, sim {100 * tot[7] / tot[5]:.2f}c")
