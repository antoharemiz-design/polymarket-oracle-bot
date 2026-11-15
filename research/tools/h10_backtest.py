"""H10 (research/PREREG_H10.md): liquidity-rewards quoting — reward side from snapshots, cost side from real fills.

usage:
  h10_backtest.py fetch SNAP1.json OUT_DIR            portfolio from snapshot 1, then trades + 1-min mids + status
  h10_backtest.py eval  OUT_DIR SNAP2.json [SNAP3.json ...]
"""
import calendar
import collections
import json
import math
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from h2_negrisk_scan import get  # noqa: E402

T_START = int(os.environ.get("H10_T_START", calendar.timegm((2026, 11, 1, 0, 0, 0))))
T_SPLIT = int(os.environ.get("H10_T_SPLIT", calendar.timegm((2026, 11, 8, 0, 0, 0))))
T_END = int(os.environ.get("H10_T_END", calendar.timegm((2026, 11, 15, 21, 0, 0))))   # env overrides: H10L (October)
CAPITAL = 10000.0
CAP_MULT = float(os.environ.get("H10_CAP_MULT", "0"))   # H10C: position limit in units of N; 0 = no limit (H10)
CONFIGS = ("best", "behind")


def eligible(m):
    return (not m["live"] and not re.search(r"updown|up-or-down", m["slug"] or "") and m["end_days"] is not None
            and m["end_days"] >= 2 and 0.10 <= m["mid"] <= 0.90)


def quote(m, cfg):
    """distance s (cents) of our quote from mid, score multiplier S, capital per market; None if outside v."""
    tick = m["tick"]
    s = (m["spread"] / 2 + (tick if cfg == "behind" else 0.0)) * 100
    if s >= m["v"]:
        return None
    S = ((m["v"] - s) / m["v"]) ** 2
    cap = m["min"] * (1 - m["spread"] - (2 * tick if cfg == "behind" else 0.0))
    return s, S, cap


def reward(m, cfg, comp_mult=1.0):
    q = quote(m, cfg)
    if not q:
        return 0.0
    s, S, _ = q
    my = S * m["min"]
    comp = comp_mult * (m["q1"] + m["q2"]) / 2
    return m["rate"] * my / (comp + my)


def portfolio(snap, cfg):
    rows = []
    for m in snap["markets"]:
        if not eligible(m):
            continue
        q = quote(m, cfg)
        if not q or q[2] <= 0:
            continue
        rows.append((reward(m, cfg) / q[2], m, q))
    rows.sort(key=lambda r: -r[0])
    out, cap = [], 0.0
    for _, m, q in rows:
        if cap >= CAPITAL:
            break
        out.append({"cond": m["cond"], "token": m["token"], "q": m["q"], "s": q[0], "N": m["min"], "cap": q[2],
                    "tick": m["tick"]})
        cap += q[2]
    return out


def fetch(snap_path, out):
    os.makedirs(out, exist_ok=True)
    snap = json.load(open(snap_path))
    port = {cfg: portfolio(snap, cfg) for cfg in CONFIGS}
    if os.path.exists(f"{out}/portfolio.json"):
        port = json.load(open(f"{out}/portfolio.json"))      # the portfolio fixed by snapshot 1 is reused
    json.dump(port, open(f"{out}/portfolio.json", "w"))
    allm = {p["cond"]: p for cfg in CONFIGS for p in port[cfg]}
    print({cfg: (len(port[cfg]), round(sum(p["cap"] for p in port[cfg]))) for cfg in CONFIGS}, "union", len(allm), flush=True)

    def one(cond):
        p = allm[cond]
        trades, cur = [], None
        while True:
            q = {"condition": cond, "taker_only": "true", "limit": 1000}
            if cur:
                q["cursor"] = cur
            d = get("https://data-api.polymarket.com/v2/trades?" + urllib.parse.urlencode(q))
            data = d.get("data") or []
            trades += [(r["timestamp"], r["outcome_index"], r["side"][0], float(r["price"]), float(r["size"]))
                       for r in data if T_START <= r["timestamp"] < T_END]
            cur = (d.get("pagination") or {}).get("next_cursor")
            if not cur or not data or min(r["timestamp"] for r in data) < T_START:
                break
        mids = []
        for a in range(T_START, T_END, 7 * 86400):          # the endpoint refuses long intervals at fidelity 1
            try:
                h = get("https://clob.polymarket.com/prices-history?" + urllib.parse.urlencode(
                    {"market": p["token"], "startTs": a, "endTs": min(a + 7 * 86400, T_END), "fidelity": 1}))
            except RuntimeError:
                continue
            mids += [(x["t"], x["p"]) for x in (h.get("history") or []) if T_START <= x["t"] < T_END]
        g = get("https://gamma-api.polymarket.com/markets?" + urllib.parse.urlencode({"condition_ids": cond}))
        status = None
        if g:
            try:
                pr = [float(x) for x in json.loads(g[0].get("outcomePrices") or "[]")]
                ct = g[0].get("closedTime")
                if g[0].get("closed") and sorted(pr) == [0.0, 1.0] and ct:
                    status = {"yes_payoff": pr[0], "closed": calendar.timegm(time.strptime(ct[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"))}
            except (ValueError, TypeError):
                pass
        return cond, {"trades": trades, "mids": mids, "status": status}

    data = {}
    with ThreadPoolExecutor(6) as ex:
        for n, (c, v) in enumerate(ex.map(one, list(allm))):
            data[c] = v
            if n % 50 == 0:
                print(f"  {n}/{len(allm)}", flush=True)
    json.dump(data, open(f"{out}/history.json", "w"))


def simulate(p, h, recenter):
    """worst-case fills of a two-sided quote at mid(prev) -/+ s.
    returns per period [pnl, fills buy, fills sell, reward-weighted active days, peak inventory $]."""
    mids = sorted(h["mids"])
    if not mids:
        return None
    tick, s, N = p["tick"], p["s"] / 100, p["N"]
    st = h["status"]
    res_t = st["closed"] if st else None
    out = {}
    for part, (a, b) in (("sel", (T_START, T_SPLIT)), ("hold", (T_SPLIT, T_END))):
        ms = [x for x in mids if a <= x[0] < b and (res_t is None or x[0] < res_t)]
        if not ms:
            continue
        # mark at the end of the period: resolution payoff if resolved inside it, else the last mid
        mark = st["yes_payoff"] if (st and a <= res_t < b + 1) else ms[-1][1]
        if st and res_t < a:
            continue
        mid_at = {}
        for t, pr in ms:
            mid_at[(t // (60 * recenter))] = pr
        used = collections.Counter()
        pos = cash = 0.0
        nb = ns = 0
        peak = 0.0
        cap = CAP_MULT * N if CAP_MULT else float("inf")
        one_sided = 0.0                     # seconds with one side withdrawn at the position limit
        last_t = ms[0][0]
        for ts, oi, side, px, sz in sorted(x for x in h["trades"] if a <= x[0] < b):
            if abs(pos) >= cap - 1e-9 and ts > last_t:
                one_sided += min(ts, ms[-1][0]) - last_t
            last_t = max(last_t, ts)
            if res_t is not None and ts >= res_t:
                continue
            k = ts // (60 * recenter)
            m0 = mid_at.get(k - 1)
            if m0 is None:
                continue
            bid = math.floor((m0 - s) / tick + 1e-9) * tick
            ask = math.ceil((m0 + s) / tick - 1e-9) * tick
            # trade in YES terms: taker buys YES at y (lifts asks) or sells YES at y (hits bids)
            if oi == 0:
                buy_yes, y = side == "B", px
            else:
                buy_yes, y = side != "B", 1 - px
            if buy_yes and y >= ask - 1e-9 and used[(k, "a")] < N and pos > -cap + 1e-9:
                q = min(N - used[(k, "a")], sz, pos + cap); used[(k, "a")] += q
                pos -= q; cash += q * ask; ns += 1
            elif not buy_yes and y <= bid + 1e-9 and used[(k, "b")] < N and pos < cap - 1e-9:
                q = min(N - used[(k, "b")], sz, cap - pos); used[(k, "b")] += q
                pos += q; cash -= q * bid; nb += 1
            peak = max(peak, abs(pos) * (m0 if pos > 0 else 1 - m0))
        if abs(pos) >= cap - 1e-9 and ms[-1][0] > last_t:
            one_sided += ms[-1][0] - last_t
        span = ms[-1][0] - ms[0][0]
        active = (span - one_sided * (2 / 3)) / 86400   # one-sided time earns 1/3 of the two-sided reward
        out[part] = [cash + pos * mark, nb, ns, active, peak]
    return out


def event_map(out, conds):
    path = f"{out}/events.json"
    if os.path.exists(path):
        return json.load(open(path))
    em = {}
    conds = list(conds)
    for i in range(0, len(conds), 40):
        q = urllib.parse.urlencode([("condition_ids", c) for c in conds[i:i + 40]] + [("limit", 40)])
        for m in get("https://gamma-api.polymarket.com/markets?" + q):
            ev = (m.get("events") or [{}])[0]
            em[m["conditionId"]] = str(ev.get("id") or m["conditionId"])
    json.dump(em, open(path, "w"))
    return em


def tstat(groups):
    v = list(groups.values())
    mean = sum(v) / len(v); sd = math.sqrt(sum((x - mean) ** 2 for x in v) / len(v))
    return mean / sd * math.sqrt(len(v)) if sd else 0.0


def evaluate(out, snaps):
    port = json.load(open(f"{out}/portfolio.json"))
    hist = json.load(open(f"{out}/history.json"))
    snaps = [{m["cond"]: m for m in json.load(open(s))["markets"]} for s in snaps]
    em = event_map(out, {p["cond"] for cfg in CONFIGS for p in port[cfg]})
    for cfg in CONFIGS:
        P = port[cfg]
        for comp_mult in (1.0, 2.0):
            for recenter in (1, 5):
                rows = {"sel": [], "hold": []}
                for p in P:
                    h = hist.get(p["cond"])
                    if not h:
                        continue
                    r = simulate(p, h, recenter)
                    if not r:
                        continue
                    rw = [reward(sn[p["cond"]], cfg, comp_mult) if p["cond"] in sn else 0.0 for sn in snaps]
                    rday = sum(rw) / len(rw)
                    for part, (pnl, nb, ns, act, peak) in r.items():
                        rows[part].append((p, rday * act, pnl - 0.01 * act, nb + ns, act, peak))
                for part in ("sel", "hold"):
                    R = rows[part]
                    if not R:
                        continue
                    net = [x[1] + x[2] for x in R]
                    mean = sum(net) / len(net); sd = math.sqrt(sum((x - mean) ** 2 for x in net) / len(net))
                    t = mean / sd * math.sqrt(len(net)) if sd else 0.0
                    days = (T_SPLIT - T_START if part == "sel" else T_END - T_SPLIT) / 86400
                    rew = sum(x[1] for x in R); fills = sum(x[2] for x in R); nf = sum(x[3] for x in R)
                    cap = sum(x[0]["cap"] for x in R); inv = sum(x[5] for x in R)
                    byev = collections.defaultdict(float)
                    for x in R:
                        byev[em.get(x[0]["cond"], x[0]["cond"])] += x[1] + x[2]
                    top5 = sorted(byev.items(), key=lambda x: -x[1])[:5]
                    rest = {k: v for k, v in byev.items() if k not in dict(top5)}
                    conc = (f" | top-5 events ${sum(v for _, v in top5):6.0f}, without them ${sum(rest.values()):6.0f} "
                            f"t {tstat(rest):5.2f}") if comp_mult == 1.0 and recenter == 1 else ""
                    print(f"{cfg:>6} comp x{comp_mult:.0f} recenter {recenter} min {('selection' if part == 'sel' else 'hold-out'):>9}: "
                          f"markets {len(R):3d} events {len(byev):3d} cap ${cap:6.0f} + peak inventory ${inv:6.0f} | reward ${rew:8.0f} "
                          f"fills PnL ${fills:8.0f} ({nf} fills) net ${rew + fills:8.0f} = ${(rew + fills) / days:6.0f}/day, "
                          f"t(market) {t:5.2f} t(event) {tstat(byev):5.2f}" + conc)
        # concentration of fill losses
        worst = []
        for p in P:
            h = hist.get(p["cond"])
            r = simulate(p, h, 1) if h else None
            if r and "hold" in r:
                worst.append((r["hold"][0], p["q"][:60], r["hold"][1] + r["hold"][2]))
        worst.sort()
        print(f"   {cfg}: worst hold-out fill PnL markets:", [(round(w[0]), w[2], w[1]) for w in worst[:5]])


if __name__ == "__main__":
    if sys.argv[1] == "fetch":
        fetch(sys.argv[2], sys.argv[3])
    else:
        evaluate(sys.argv[2], sys.argv[3:])
