"""H10N (research/PREREG_H10N.md): H10C by market group — "news" vs "stable".

usage: h10_groups.py SCRATCH_H10_DIR [reward_mode=mean]
  reward_mode: mean  - mean modelled reward over snapshots 2-7 (competition = upper bound from the book)
               min   - per-market minimum over snapshots 2-7 (measured worst case across the day's snapshots)
               file:PATH - per-market reward/day from a JSON {cond: $/day} (step 2, measured competition)
"""
import calendar
import collections
import glob
import json
import math
import os
import re
import sys
import urllib.parse

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import h10_backtest as hb  # noqa: E402
from h2_negrisk_scan import get  # noqa: E402

NEWS = re.compile(
    r"nvidia|jensen|ian buck|rene haas|kimberly powell|anthropic|openai|\bgpt\b|chatgpt|gemini|claude|fable|muse|"
    r"databricks|ai lab|ai model|arena|livebench|humanity.s last exam|iran|israel|netanyahu|russia|ukraine|"
    r"zelenskyy|hormuz|bab el-mandeb|ukmto|ceasefire|military|\bxi\b|takaichi|syria|out as|leave the trump|"
    r"impeach|arrested|nomination odds|chief of staff|next premier|minister|next israeli government|witkoff|"
    r"trump speak|searched person|mecca agreement", re.I)


# categories the pre-registration lists as "stable" take precedence over a news keyword in the same question
STABLE_FIRST = re.compile(r"\bposts? from|\bposts \d|tweets|interest rate|repo rate|monetary policy rate|free app", re.I)


def topic_news(q):
    return bool(NEWS.search(q or "")) and not STABLE_FIRST.search(q or "")


def ts(y, m, d, h=0):
    return calendar.timegm((y, m, d, h, 0, 0))


PERIODS = {
    "September": ("bt_sep", ts(2026, 9, 1), ts(2026, 9, 1), ts(2026, 10, 1), (ts(2026, 8, 25), ts(2026, 9, 1))),
    "October": ("bt_oct", ts(2026, 10, 1), ts(2026, 10, 1), ts(2026, 11, 1), (ts(2026, 9, 24), ts(2026, 10, 1))),
    "November 8-15": ("bt", ts(2026, 11, 1), ts(2026, 11, 8), ts(2026, 11, 15, 21), (ts(2026, 11, 1), ts(2026, 11, 8))),
}


def prior_mids(d, port, a, b):
    """mids in [a, b) for every portfolio market, taken from whichever history file covers the window."""
    out = collections.defaultdict(list)
    for sub in ("bt", "bt_oct", "bt_sep", "bt_aug"):
        path = f"{d}/{sub}/history.json"
        if os.path.exists(path):
            for c, h in json.load(open(path)).items():
                out[c] += [x for x in h["mids"] if a <= x[0] < b]
    missing = [p for p in port if not out.get(p["cond"])]
    if missing and a < ts(2026, 9, 1):           # August window: fetch once
        os.makedirs(f"{d}/bt_aug", exist_ok=True)
        hist = {}
        for p in port:
            try:
                h = get("https://clob.polymarket.com/prices-history?" + urllib.parse.urlencode(
                    {"market": p["token"], "startTs": a, "endTs": b, "fidelity": 1}))
                mids = [(x["t"], x["p"]) for x in (h.get("history") or []) if a <= x["t"] < b]
            except RuntimeError:
                mids = []
            hist[p["cond"]] = {"mids": mids, "trades": [], "status": None}
            out[p["cond"]] = mids
        json.dump(hist, open(f"{d}/bt_aug/history.json", "w"))
    return out


def vol_class(mids):
    mids = sorted(set(tuple(x) for x in mids))
    if not mids or mids[-1][0] - mids[0][0] < 3 * 86400:
        return "new"
    j, worst = 0, 0.0
    for i, (t, p) in enumerate(mids):
        while mids[j][0] < t - 3600:
            j += 1
        lo = min(x[1] for x in mids[j:i + 1]); hi = max(x[1] for x in mids[j:i + 1])
        worst = max(worst, hi - lo)
    return "news" if worst >= 0.10 else "stable"


def tstat(v):
    v = list(v)
    if len(v) < 2:
        return 0.0
    mean = sum(v) / len(v); sd = math.sqrt(sum((x - mean) ** 2 for x in v) / len(v))
    return mean / sd * math.sqrt(len(v)) if sd else 0.0


def main():
    d = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "mean"
    hb.CAP_MULT = 3.0
    port = json.load(open(f"{d}/bt/portfolio.json"))["best"]
    em = json.load(open(f"{d}/bt/events.json"))
    snaps = [{m["cond"]: m for m in json.load(open(s))["markets"]} for s in sorted(glob.glob(f"{d}/snap[2-7].json"))]
    fixed = json.load(open(mode[5:])) if mode.startswith("file:") else None

    def rday(p, mult=1.0):
        if fixed is not None:
            return fixed.get(p["cond"], 0.0) / mult
        rw = [hb.reward(sn[p["cond"]], "best", mult) if p["cond"] in sn else 0.0 for sn in snaps]
        return (min(rw) if mode == "min" else sum(rw) / len(rw))

    cls_a = {p["cond"]: ("news" if topic_news(p["q"]) else "stable") for p in port}
    print(f"reward mode: {mode}; classification A: {collections.Counter(cls_a.values())}")
    out = {}
    for name, (sub, t0, tsplit, t1, (pa, pb)) in PERIODS.items():
        hb.T_START, hb.T_SPLIT, hb.T_END = t0, tsplit, t1
        hist = json.load(open(f"{d}/{sub}/history.json"))
        pm = prior_mids(d, port, pa, pb)
        cls_b = {p["cond"]: vol_class(pm.get(p["cond"], [])) for p in port}
        part = "hold"
        rows = []
        for p in port:
            h = hist.get(p["cond"])
            r = hb.simulate(p, h, 1) if h else None
            if not r or part not in r:
                continue
            pnl, nb, ns, act, peak = r[part]
            for mult in (1.0, 2.0):
                pass
            rows.append({"p": p, "rew": rday(p) * act, "rew2": rday(p, 2.0) * act, "fill": pnl - 0.01 * act,
                         "n": nb + ns, "peak": peak, "A": cls_a[p["cond"]], "B": cls_b[p["cond"]],
                         "ev": em.get(p["cond"], p["cond"])})
        days = (t1 - (tsplit if name.startswith("November") else t0)) / 86400
        out[name] = rows
        print(f"\n{name} ({days:.1f} days)")
        print(f"  {'group':<14}{'markets':>8}{'events':>7}{'capital':>18}{'reward':>9}{'fills':>9}{'net':>9}{'$/day':>7}{'t(ev)':>7}"
              f"{'  x2: $/day (t)':>16}{'fills/day':>10}")
        for label, key, val in (("A stable", "A", "stable"), ("A news", "A", "news"),
                                ("B stable", "B", "stable"), ("B news", "B", "news"), ("B new", "B", "new"), ("all", None, None)):
            R = [x for x in rows if key is None or x[key] == val]
            if not R:
                print(f"  {label:<14}{0:>8}")
                continue
            byev, byev2 = collections.defaultdict(float), collections.defaultdict(float)
            for x in R:
                byev[x["ev"]] += x["rew"] + x["fill"]; byev2[x["ev"]] += x["rew2"] + x["fill"]
            rew = sum(x["rew"] for x in R); fill = sum(x["fill"] for x in R); net = rew + fill
            cap = sum(x["p"]["cap"] for x in R); peak = sum(x["peak"] for x in R)
            net2 = sum(byev2.values())
            print(f"  {label:<14}{len(R):>8}{len(byev):>7}{f'${cap:,.0f} + ${peak:,.0f}':>18}{rew:9.0f}{fill:9.0f}{net:9.0f}"
                  f"{net / days:7.0f}{tstat(byev.values()):7.2f}{f'{net2 / days:7.0f} ({tstat(byev2.values()):5.2f})':>16}"
                  f"{sum(x['n'] for x in R) / days:10.0f}")
    json.dump({k: [{"cond": x["p"]["cond"], "q": x["p"]["q"], "A": x["A"], "B": x["B"], "rew": x["rew"], "fill": x["fill"],
                    "n": x["n"], "ev": x["ev"]} for x in v] for k, v in out.items()},
              open(f"{d}/groups_{mode.replace('/', '_').replace(':', '_')[:40]}.json", "w"))


if __name__ == "__main__":
    main()
