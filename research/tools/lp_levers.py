"""Phase 2 research: which levers raise the liquidity-reward model M1 per dollar of quotes, out of sample.

    python research/tools/lp_levers.py SCAN_DIR [--split HOURS] [--grid NAME] [--json OUT]

Data: research/tools/lp_scan.py snapshots (every rewarded market's YES book every 30 min).
Selection uses the snapshots of the first `split` hours (default 24), exactly like the phase 1B bot
(median M1 over snapshots with 0 where we cannot quote, median competitor levels, choose()). Evaluation uses
the later snapshots: in each one the selected markets are re-quoted with the same rule and size, and M1 is
recomputed from that snapshot's book. Reported per configuration: predicted and realised M1 ($/day), M1 per
$100 of quote capital, CV of the portfolio M1 across evaluation snapshots, largest market share.

Levers:
  capital   budget for quotes: $100 / 200 / 300 / 500 (more markets at the same size, or bigger size)
  size      k x rewards_min_size per market (k = 1, 2, 3, 5)
  markets   at most 5 / 10 / 15 / 20 / 30 markets
  minsize   which rewards_min_size the market may have: <= 20 (phase 1B), <= 50, <= 100, <= 200
  place     join best prices (phase 1B) / improve by one tick / zone edge when best prices are outside the zone
  filter    competitor filter (median levels >= 3) on / off; topic filter (classification A) on / off
  category  fee_type of the market (sports, politics, crypto, ...)

Selection uses snapshots on a 30-minute grid (as pre-registered). CV is over 30-minute means of the evaluation
snapshots (every 10 min since 19 Nov 19:45 UTC), close to the bot's CV over 30-minute means of minute samples.
"""
import dataclasses
import gzip
import json
import math
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polybot import lp  # noqa: E402


def _ts(fname):
    return time.mktime(time.strptime(fname[5:20], "%Y%m%dT%H%M%S")) - time.timezone


def load(scan_dir):
    files = sorted(f for f in os.listdir(scan_dir) if f.startswith("snap_") and f.endswith(".json.gz"))
    metas = sorted(f for f in os.listdir(scan_dir) if f.startswith("meta_") and f.endswith(".json.gz"))
    meta = {}
    for f in metas:                                   # later metadata wins, earlier fills the gaps
        with gzip.open(os.path.join(scan_dir, f), "rt") as fh:
            meta.update(json.load(fh))
    snaps = []
    for f in files:
        with gzip.open(os.path.join(scan_dir, f), "rt") as fh:
            snaps.append(json.load(fh))
    return meta, snaps


# ---------------------------------------------------------------------------------------------------
# one market in one snapshot

def quote(bids, asks, tick, v, place):
    """Our YES bid and ask for a placement rule, or (None, None) if we cannot quote inside the zone."""
    if not bids or not asks:
        return None, None
    bb, ba = bids[0][0], asks[0][0]
    if ba - bb < tick - 1e-9:
        return None, None
    half_c = (ba - bb) / 2 * 100
    if place == "join":
        return (bb, ba) if half_c < v else (None, None)
    if place == "improve":                            # one tick better than the best prices, if the spread allows
        if half_c >= v:
            return None, None
        if ba - bb >= 3 * tick - 1e-9:
            return round(bb + tick, 6), round(ba - tick, 6)
        return bb, ba
    if place == "edge":                               # join inside the zone; if the best prices are outside it,
        if half_c < v:                                # quote inside the spread just within the zone
            return bb, ba
        mid = (bb + ba) / 2
        d = (v / 100) - tick
        if d <= 0:
            return None, None
        b = math.floor((mid - d) / tick + 1e-9) * tick
        a = math.ceil((mid + d) / tick - 1e-9) * tick
        if (a - b) / 2 * 100 >= v:
            b, a = b + tick, a - tick
        if a - b < tick - 1e-9 or (a - b) / 2 * 100 >= v:
            return None, None
        return round(b, 6), round(a, 6)
    raise ValueError(place)


def evaluate(rw, book, place, k, mid_lo=0.15, mid_hi=0.85):
    """(M1 $/day, competitor levels, quote capital) for one market, or None if we cannot quote."""
    rate, v, mn = rw
    if v <= 0 or mn <= 0 or rate <= 0:
        return None
    tick, bids, asks = book
    b, a = quote(bids, asks, tick, v, place)
    if b is None:
        return None
    mid = (b + a) / 2
    if not mid_lo <= mid <= mid_hi:
        return None
    size = k * mn
    frac = lp.m1_fraction(bids, asks, b, a, size, v, mn)
    return rate * frac, lp.comp_levels(bids, asks, mid, v, mn), size * (b + 1 - a)


# ---------------------------------------------------------------------------------------------------
# configurations

@dataclasses.dataclass
class Conf:
    name: str
    budget: float = 100.0
    k: int = 1
    markets_max: int = 8
    markets_min: int = 5
    max_min: float = 20.0
    place: str = "join"
    comp_min: float = 3.0
    topic: bool = True
    category: str = ""                     # fee_type filter ("" = any)
    exclude: tuple = ()                    # fee_types never quoted (phase 2: economics, finance_prices)
    max_share: float = 0.4
    min_age_days: float = 7.0              # H10N filters; short-dated recurring markets (weather, daily prices)
    min_days_left: float = 14.0            # are excluded by them
    alloc: str = "fixed"                   # fixed: every market at size k; marginal: budget in steps of one
    kmax: int = 5                          # rewards_min_size, each step to the best marginal M1 per dollar


def eligible(meta_c, now, conf):
    if not meta_c or not meta_c.get("active", True) or meta_c.get("game"):
        return False
    if conf.topic and lp.topic_news(meta_c.get("question") or ""):
        return False
    if conf.category and (meta_c.get("fee_type") or "none") != conf.category:
        return False
    if (meta_c.get("fee_type") or "none") in conf.exclude:
        return False
    try:
        if now - lp._ts(meta_c["created"]) < conf.min_age_days * 86400 or lp._ts(meta_c["end"]) - now < conf.min_days_left * 86400:
            return False
    except Exception:
        return False
    return True


def marginal(rows, conf):
    """Budget in steps of one rewards_min_size: each step goes to the option with the best marginal M1 per dollar
    (a new market at k=1 or one more unit on a chosen market). No market takes more than max_share of the budget;
    if one carries more than max_share of the predicted M1, its size is cut by one unit (dropped at k=1) and the
    budget is spread again."""
    banned, kcap = set(), {}
    while True:
        k, events, cap = {}, set(), 0.0
        while True:
            best = None
            for x in rows:
                c = x["cond"]
                if c in banned:
                    continue
                cur = k.get(c, 0)
                if cur >= min(conf.kmax, kcap.get(c, conf.kmax)) or (cur == 0 and (x["event"] in events or len(k) >= conf.markets_max)):
                    continue
                if x["cap_k"][cur] > max(conf.budget * conf.max_share, x["cap_k"][0]):
                    continue
                gain = x["m1_k"][cur] - (x["m1_k"][cur - 1] if cur else 0.0)
                cost = x["cap_k"][cur] - (x["cap_k"][cur - 1] if cur else 0.0)
                if gain <= 0 or cost <= 0 or cap + cost > conf.budget:
                    continue
                if best is None or gain / cost > best[0]:
                    best = (gain / cost, c, cost, x["event"])
            if best is None:
                break
            _, c, cost, e = best
            k[c] = k.get(c, 0) + 1; cap += cost; events.add(e)
        out = []
        for x in rows:
            if x["cond"] in k:
                kk = k[x["cond"]]
                out.append(dict(x, k=kk, m1_usd_day=x["m1_k"][kk - 1], quote_cap=x["cap_k"][kk - 1]))
        tot = sum(x["m1_usd_day"] for x in out)
        top = max(out, key=lambda x: x["m1_usd_day"], default=None)
        if top is None or tot <= 0 or top["m1_usd_day"] / tot <= conf.max_share or len(out) < 2:
            return out
        if top["k"] > 1:
            kcap[top["cond"]] = top["k"] - 1
        else:
            banned.add(top["cond"])


def run(conf, meta, sel, ev):
    """Select on snapshots `sel`, evaluate on `ev`. Returns a result dict."""
    now = sel[0]["ts"]
    cands = [c for c, r in sel[0]["rw"].items() if r[2] <= conf.max_min and eligible(meta.get(c), now, conf)]
    ks = range(1, conf.kmax + 1) if conf.alloc == "marginal" else [conf.k]
    rows = []
    for c in cands:
        vals = {k: [] for k in ks}
        for s in sel:
            r, bk = s["rw"].get(c), s["books"].get(c)
            for k in ks:
                vals[k].append(evaluate(r, bk, conf.place, k) if r and bk else None)
        k0 = ks[0]
        seen = [x for x in vals[k0] if x]
        if not seen:
            continue
        comp = lp.median([x[1] if x else 0 for x in vals[k0]])
        m1_k = [lp.median([x[0] if x else 0.0 for x in vals[k]]) for k in ks]
        cap_k = [st.median(x[2] for x in vals[k] if x) for k in ks]
        if comp + 1e-9 < conf.comp_min or m1_k[0] <= 0:
            continue
        rows.append({"cond": c, "event": (meta.get(c) or {}).get("event", c), "m1_usd_day": m1_k[0],
                     "quote_cap": cap_k[0], "comp": comp, "m1_k": m1_k, "cap_k": cap_k, "k": ks[0]})
    if conf.alloc == "marginal":
        out = marginal(rows, conf)
    else:
        cfg = lp.LPConfig(markets_max=conf.markets_max, quote_budget_usd=conf.budget, max_share=conf.max_share)
        out = lp.choose(rows, cfg)
    res = {"conf": dataclasses.asdict(conf), "candidates": len(rows), "selected": [x["cond"] for x in out],
           "ks": [x["k"] for x in out],
           "n": len(out), "cap": sum(x["quote_cap"] for x in out), "pred": sum(x["m1_usd_day"] for x in out)}
    if not out or not ev:
        return res
    port, per = [], {x["cond"]: [] for x in out}
    for s in ev:
        tot = 0.0
        for x in out:
            r, bk = s["rw"].get(x["cond"]), s["books"].get(x["cond"])
            e = evaluate(r, bk, conf.place, x["k"]) if r and bk else None
            m = e[0] if e else 0.0
            per[x["cond"]].append(m); tot += m
        port.append(tot)
    mean = sum(port) / len(port)
    # CV over 30-minute window means (closer to the bot's CV, which averages minute samples per window)
    t_ev = ev[0]["ts"]
    win = {}
    for s_, p_ in zip(ev, port):
        win.setdefault(int((s_["ts"] - t_ev) // 1800), []).append(p_)
    wm = [sum(v) / len(v) for v in win.values()]
    mw = sum(wm) / len(wm)
    sd = (sum((p - mw) ** 2 for p in wm) / len(wm)) ** 0.5
    totm = sum(sum(v) for v in per.values())
    res.update(real=mean, real_per100=mean * 100 / res["cap"] if res["cap"] else None,
               pred_per100=res["pred"] * 100 / res["cap"] if res["cap"] else None,
               ratio=mean / res["pred"] if res["pred"] else None, cv=sd / mean if mean else None,
               max_share=max(sum(v) / totm for v in per.values()) if totm else None,
               zero_share=sum(1 for v in per.values() for m in v if m == 0) / (len(out) * len(ev)))
    return res


GRIDS = {
    "base": [Conf("phase 1B rule ($100, 5-8 markets, k=1, min<=20, join)")],
    "capital": [Conf(f"budget ${b}, k=1, up to {m} markets", budget=b, markets_max=m)
                for b, m in ((100, 8), (200, 15), (300, 20), (500, 30))]
               + [Conf(f"budget ${b}, k={k}, up to 8 markets", budget=b, k=k)
                  for b, k in ((200, 2), (300, 3), (500, 5))],
    "markets": [Conf(f"budget $500, up to {m} markets", budget=500, markets_max=m) for m in (5, 10, 15, 20, 30)],
    "alloc": [Conf(f"budget ${b}, marginal sizes k<=5, up to {m} markets", budget=b, alloc="marginal", markets_max=m)
              for b, m in ((100, 8), (200, 15), (300, 20), (500, 30))]
             + [Conf(f"budget ${b}, marginal, min_size<=100, improve", budget=b, alloc="marginal", markets_max=30,
                     max_min=100, place="improve") for b in (100, 300, 500)],
    "share": [Conf(f"budget ${b}, max share {int(ms * 100)}%", budget=b, max_share=ms, markets_max=m)
              for b, m in ((100, 8), (300, 20)) for ms in (0.4, 1.0)],
    "minsize": [Conf(f"budget ${b}, min_size <= {mm}", budget=b, max_min=mm, markets_max=30)
                for b in (100, 300, 500) for mm in (20, 50, 100, 200)],
    "place": [Conf(f"budget ${b}, place={p}", budget=b, place=p, markets_max=30) for b in (100, 300) for p in ("join", "improve", "edge")],
    "filter": [Conf(f"budget $300, comp>={c}, topic filter {t}", budget=300, comp_min=c, topic=t, markets_max=30)
               for c in (0, 3) for t in (True, False)],
}


def category_grid(meta):
    cats = sorted({(m.get("fee_type") or "none") for m in meta.values()})
    return [Conf(f"category {c}, budget $300", budget=300, category=c, markets_max=30, markets_min=1) for c in cats]


TOXIC = ("economics_fees", "finance_prices_fees", "mentions_fees", "culture_fees")   # user's decision, 20 Nov


def phase2_grid(meta):
    """User's phase 2 rules (20 Nov): no age / days-left filters, min_size up to 200, economics, finance prices,
    mentions and culture excluded, size scaled per market (marginal, k <= 5), no market above 40% of predicted M1,
    $300 / $500.
    Kept from phase 1B: topic filter (classification A), >= 3 competitor levels. Sensitivity rows change one rule."""
    p2 = dict(alloc="marginal", kmax=5, max_min=200, min_age_days=0, min_days_left=0, exclude=TOXIC,
              markets_max=40, max_share=0.4)
    out = [Conf("phase 1B rule ($100, 5 markets, k=1, min<=20, >=7d old, >=14d left)")]
    for b in (100, 300, 500):
        out.append(Conf(f"PHASE 2 rules, budget ${b}", budget=b, **p2))
    for b in (300, 500):
        out += [Conf(f"  sens ${b}: no size scaling (k=1)", budget=b, **dict(p2, kmax=1)),
                Conf(f"  sens ${b}: long-dated only (>=7d old, >=14d left)", budget=b, **dict(p2, min_age_days=7, min_days_left=14)),
                Conf(f"  sens ${b}: min_size <= 20 only", budget=b, **dict(p2, max_min=20)),
                Conf(f"  sens ${b}: culture + mentions allowed", budget=b, **dict(p2, exclude=TOXIC[:2])),
                Conf(f"  sens ${b}: no competitor filter", budget=b, **dict(p2, comp_min=0)),
                Conf(f"  sens ${b}: no topic filter", budget=b, **dict(p2, topic=False)),
                Conf(f"  sens ${b}: improve price by 1 tick", budget=b, **dict(p2, place="improve")),
                Conf(f"  sens ${b}: weather only", budget=b, **dict(p2, category="weather_fees", markets_min=1))]
    return out


def horizon_grid(meta):
    """Short-dated markets: no age / days-left filters (still: topic filter, >= 1 day left, comp >= 3)."""
    out = [Conf(f"budget ${b}, any horizon, all categories", budget=b, markets_max=30, min_age_days=0,
                min_days_left=1, max_min=100) for b in (100, 300, 500)]
    for c in ("weather_fees", "finance_prices_fees", "crypto_fees_v2", "culture_fees", "economics_fees"):
        out.append(Conf(f"category {c}, any horizon, budget $300", budget=300, category=c, markets_max=30,
                        markets_min=1, min_age_days=0, min_days_left=1, max_min=100))
    return out


def run_rolling(conf, meta, snaps, lookback_h, step_h, start_h):
    """Re-select every step_h hours on the trailing lookback_h hours (30-minute grid), evaluate on the next step_h
    hours; the portfolio changes at each step, as a bot with rolling selection would. Returns the evaluation of all
    steps together: mean M1, M1 per $100 of the average quote capital, CV of 30-minute means, largest share."""
    t0 = snaps[0]["ts"]
    t, end = t0 + start_h * 3600, snaps[-1]["ts"]
    steps, port_by_win, per, caps, preds, turnover, prev, khours = [], {}, {}, [], [], [], None, {}
    while t < end:
        sel = []
        for s_ in snaps:
            if t - lookback_h * 3600 <= s_["ts"] < t and (not sel or s_["ts"] - sel[-1]["ts"] >= 1790):
                sel.append(s_)
        ev = [s_ for s_ in snaps if t <= s_["ts"] < t + step_h * 3600]
        if len(sel) < 4 or not ev:
            t += step_h * 3600
            continue
        r = run(conf, meta, sel, ev)
        caps.append(r["cap"]); preds.append(r["pred"])
        cur = set(r["selected"])
        if prev is not None:
            turnover.append(len(cur - prev) / max(1, len(cur)))
        prev = cur
        # re-evaluate per snapshot to pool the windows across steps
        chosen = list(zip(r["selected"], r["ks"]))
        for c, k in chosen:                           # market-hours at size k (for the fill cost: lp_net.py)
            kh = khours.setdefault(c, {})
            kh[k] = kh.get(k, 0.0) + step_h
        for s_ in ev:
            tot = 0.0
            for c, k in chosen:
                rr, bk = s_["rw"].get(c), s_["books"].get(c)
                e = evaluate(rr, bk, conf.place, k) if rr and bk else None
                m = e[0] if e else 0.0
                per[c] = per.get(c, 0.0) + m; tot += m
            port_by_win.setdefault(int((s_["ts"] - t0) // 1800), []).append(tot)
        steps.append(r)
        t += step_h * 3600
    if not port_by_win:
        return {"conf": dataclasses.asdict(conf), "steps": 0}
    wm = [sum(v) / len(v) for v in port_by_win.values()]
    mean = sum(wm) / len(wm)
    sd = (sum((x - mean) ** 2 for x in wm) / len(wm)) ** 0.5
    cap = sum(caps) / len(caps)
    totm = sum(per.values())
    return {"conf": dataclasses.asdict(conf), "steps": len(steps), "cap": cap, "pred": sum(preds) / len(preds),
            "real": mean, "real_per100": mean * 100 / cap if cap else None, "cv": sd / mean if mean else None,
            "max_share": max(per.values()) / totm if totm else None, "turnover": sum(turnover) / len(turnover) if turnover else None,
            "n": sum(r["n"] for r in steps) / len(steps), "selected": sorted({c for r in steps for c in r["selected"]}),
            "hours": (len(port_by_win) * 0.5), "khours": khours,
            "m1_by_market": {c: v / max(1, sum(len(x) for x in port_by_win.values())) * len(port_by_win) / len(port_by_win) for c, v in per.items()}}


def main():
    scan = sys.argv[1]
    split = float(sys.argv[sys.argv.index("--split") + 1]) if "--split" in sys.argv else 24.0
    grid = sys.argv[sys.argv.index("--grid") + 1] if "--grid" in sys.argv else "all"
    meta, snaps = load(scan)
    t0 = snaps[0]["ts"]
    sel = []                                          # selection: a 30-minute grid, as pre-registered
    for s in snaps:
        if s["ts"] < t0 + split * 3600 and (not sel or s["ts"] - sel[-1]["ts"] >= 1790):
            sel.append(s)
    ev = [s for s in snaps if s["ts"] >= t0 + split * 3600]
    print(f"{len(snaps)} snapshots: selection {len(sel)} ({split:g} h), evaluation {len(ev)}", flush=True)
    if "--rolling" in sys.argv:                      # --rolling LOOKBACK_H,STEP_H,START_H
        lb, step, start = (float(x) for x in sys.argv[sys.argv.index("--rolling") + 1].split(","))
        print(f"rolling: re-select every {step:g} h on the last {lb:g} h, from hour {start:g}")
        confs = phase2_grid(meta) if grid in ("phase2", "all") else GRIDS[grid]
        results = []
        f = lambda x, p="{:.2f}": "—" if x is None else p.format(x)
        for conf in confs:
            r = run_rolling(conf, meta, snaps, lb, step, start)
            results.append(r)
            if not r.get("steps"):
                print(f"{conf.name:55s} no steps"); continue
            print(f"{conf.name:55s} steps {r['steps']:2d} n {r['n']:4.1f} cap ${r['cap']:6.0f} pred ${r['pred']:7.2f} "
                  f"real ${f(r['real'])} ({f(r['real_per100'])}/100) cv {f(r['cv'], '{:.0%}')} "
                  f"share {f(r['max_share'], '{:.0%}')} turnover {f(r['turnover'], '{:.0%}')}", flush=True)
        if "--json" in sys.argv:
            with open(sys.argv[sys.argv.index("--json") + 1], "w") as fh:
                json.dump(results, fh)
        if "--conds-out" in sys.argv:                 # markets chosen by the main phase 2 rows, for lp_fills --conds
            sel = sorted({c for r in results if r.get("steps") and r["conf"]["name"].startswith("PHASE 2") for c in r["selected"]})
            with open(sys.argv[sys.argv.index("--conds-out") + 1], "w") as fh:
                json.dump(sel, fh)
            print(f"{len(sel)} markets chosen by the PHASE 2 rows written for the fill study")
        return
    names = list(GRIDS) + ["category", "horizon"] if grid == "all" else grid.split(",")
    results = []
    for g in names:
        confs = (category_grid(meta) if g == "category" else horizon_grid(meta) if g == "horizon"
                 else phase2_grid(meta) if g == "phase2" else GRIDS[g])
        print(f"\n## {g}")
        for conf in confs:
            r = run(conf, meta, sel, ev)
            r["grid"] = g
            results.append(r)
            f = lambda x, p="{:.2f}": "—" if x is None else p.format(x)
            print(f"{conf.name:55s} cand {r['candidates']:5d} n {r['n']:2d} cap ${r['cap']:6.0f} pred ${r['pred']:7.2f} "
                  f"({f(r.get('pred_per100'))}/100) real ${f(r.get('real'))} ({f(r.get('real_per100'))}/100) "
                  f"ratio {f(r.get('ratio'))} cv {f(r.get('cv'), '{:.0%}')} share {f(r.get('max_share'), '{:.0%}')} "
                  f"zero {f(r.get('zero_share'), '{:.0%}')}", flush=True)
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w") as fh:
            json.dump(results, fh)


if __name__ == "__main__":
    main()
