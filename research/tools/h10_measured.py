"""H10M (research/PREREG_H10M.md): measured reward share of a new N-size two-sided quote at the best prices.

usage: h10_measured.py SCRATCH_H10_DIR
Writes reward_M1.json, reward_M2.json, reward_M3.json ({cond: $/day}) and prints the measurement table.
"""
import glob
import json
import statistics
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import h10_backtest as hb  # noqa: E402

d = sys.argv[1]
sel = json.load(open(f"{d}/stable_A.json"))
N = {x["cond"]: x["N"] for x in sel}
sun = [{m["cond"]: m for m in json.load(open(s))["markets"]} for s in sorted(glob.glob(f"{d}/snap[2-7].json"))]
mon = [json.load(open(s))["markets"] for s in sorted(glob.glob(f"{d}/books_mon/book_*.json"))]


def m1_from_book(b):
    """same fields as the Sunday scan, so hb.reward() applies"""
    if not b:
        return None
    sc = lambda lv, side: sum(((b["v"] - dist) / b["v"]) ** 2 * s for p, s in lv
                              for dist in [((b["mid"] - p) if side == "bid" else (p - b["mid"])) * 100]
                              if 0 <= dist < b["v"] and s >= b["min"])
    spread = b["best_ask"][0] - b["best_bid"][0]
    return {"rate": b["rate"], "v": b["v"], "min": b["min"], "mid": b["mid"], "spread": spread, "tick": b["tick"],
            "q1": sc(b["bids"], "bid"), "q2": sc(b["asks"], "ask")}


def m2(b, n):
    """user's definition: our size / (ours + all resting size inside v), min over the two sides"""
    if not b:
        return 0.0
    s = (b["best_ask"][0] - b["best_bid"][0]) / 2 * 100
    if s >= b["v"]:
        return 0.0
    rb = sum(x[1] for x in b["bids"]); ra = sum(x[1] for x in b["asks"])
    return b["rate"] * min(n / (n + rb), n / (n + ra))


r1, r2, r3, stat = {}, {}, {}, []
for x in sel:
    c, n = x["cond"], N[x["cond"]]
    m1s = [hb.reward(sn[c], "best") if c in sn else 0.0 for sn in sun]
    m1s += [hb.reward(m1_from_book(s.get(c)), "best") if s.get(c) else 0.0 for s in mon]
    m2s = [m2(s.get(c), n) for s in mon]
    r1[c] = sum(m1s) / len(m1s); r2[c] = sum(m2s) / len(m2s) if m2s else 0.0; r3[c] = min(m1s)
    lvl = []
    for s in mon:
        b = s.get(c)
        if b:
            lvl.append((b["best_bid"][1] + b["best_ask"][1]) / 2)
    within = [sum(l[1] for l in s[c]["bids"]) + sum(l[1] for l in s[c]["asks"]) for s in mon if s.get(c)]
    stat.append({"cond": c, "n": n, "best_level": statistics.median(lvl) if lvl else None,
                 "within_v": statistics.median(within) if within else None})
for name, r in (("M1", r1), ("M2", r2), ("M3", r3)):
    json.dump(r, open(f"{d}/reward_{name}.json", "w"))
lv = [s for s in stat if s["best_level"]]
share = lambda k: statistics.median(k * s["n"] / (k * s["n"] + s["best_level"]) for s in lv)
share_v = lambda k: statistics.median(k * s["n"] / (k * s["n"] + s["within_v"]) for s in lv if s["within_v"] is not None)
print(f"snapshots: Sunday {len(sun)}, Monday {len(mon)}; markets {len(sel)}")
print(f"resting size at the best price (median over snapshots, then markets): {statistics.median(s['best_level'] for s in lv):,.0f} contracts; "
      f"inside v (both sides): {statistics.median(s['within_v'] for s in lv if s['within_v'] is not None):,.0f}; our N median {statistics.median(s['n'] for s in lv):.0f}")
print(f"our share of the best level: N {share(1):.3f}, 3N {share(3):.3f}; of resting size inside v: N {share_v(1):.3f}, 3N {share_v(3):.3f}")
print(f"portfolio reward $/day: M1 {sum(r1.values()):.0f}, M2 {sum(r2.values()):.0f}, M3 {sum(r3.values()):.0f}; "
      f"Sunday-only mean (as in step 1) {sum(sum(hb.reward(sn[c], 'best') if c in sn else 0 for sn in sun) / len(sun) for c in N):.0f}")
