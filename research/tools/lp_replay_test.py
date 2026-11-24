"""Phase 3 research: replay of the phase 2 test itself, to calibrate the replay against the VPS journal and to
forecast phase 3 on the same history (23 Nov).

    python research/tools/lp_replay_test.py JOURNAL.jsonl[.gz] SCAN_DIR TAPE_DIR OUT.json --from EPOCH --to EPOCH
                                            [--step 15] [--variants base,life,p3]

The bot's own loop (polybot.lp.LPTest, phase 2 rules) on a replay clock:
  - portfolio: the journal's rebalance events at their times (the same markets, sizes and exits as on the VPS);
    the replay starts flat at --from with the portfolio of the last rebalance before it;
  - books: the lp_scan snapshot at or before the replay time, shifted by the change of the 1-minute price history
    since that snapshot (research/tools/lp_replay.py);
  - reward parameters: from the same snapshot;
  - fills: PaperExec on the full public taker tape collected during the test (research/tools/lp_tape.py).
The same window metrics are computed for the journal and for each variant over [--from, --to]:
contracts filled, closed by taker (contracts, cost), share closed by taker, M1 per $100 per day, rebates, the
change of mark-to-market, net per $100 per day.
Variants: base = phase 2 as on the VPS; life = phase 2 with the lifetime paper fill model; p3 = phase 3 rules
(tick-better reducing quotes, FIFO lots) with the lifetime model.
"""
import asyncio
import bisect
import gzip
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import lp_replay as R  # noqa: E402
from polybot import lp  # noqa: E402

VARIANTS = {
    "base": {},
    "life": {"paper_lifetime": 1},
    "p3": {"paper_lifetime": 1, "exit_improve_ticks": 1, "reduce_improve_always": 1, "inv_fifo": 1},
}


def window_metrics(ev, t0, t1):
    """Metrics of events in [t0, t1]: fills by trade time, closing orders, M1 windows, mark-to-market change."""
    fills = [e for e in ev if e["event"] == "fill" and t0 <= e.get("fill_ts", e["ts"]) <= t1]
    flat = [e for e in ev if e["event"] == "flatten" and t0 <= e["ts"] <= t1]
    wins = [e for e in ev if e["event"] == "window" and t0 <= e["start"] and e["end"] <= t1 + 60]
    pnls = [e for e in ev if e["event"] in ("pnl", "end") and t0 <= e["ts"] <= t1]
    filled = sum(e["size"] for e in fills)
    taker = sum(e["size"] for e in flat)
    caps = [w["cap_target"] for w in wins if w.get("cap_target")]
    cap = sum(caps) / len(caps) if caps else None
    m1 = sum(w["m1_usd_day"] for w in wins) / len(wins) if wins else None
    rebates = sum(m.get("rebate", 0.0) or 0.0 for w in wins for m in w["markets"].values())
    mtm = (pnls[-1]["pnl"] - pnls[0]["pnl"]) if len(pnls) > 1 else None
    days = (t1 - t0) / 86400
    per100 = lambda x: x * 100 / cap / days if cap and x is not None else None
    return {"filled": filled, "fills": len(fills), "taker_contracts": taker, "taker_n": len(flat),
            "taker_cost": -sum(e.get("cost", 0.0) for e in flat), "taker_share": taker / filled if filled else None,
            "windows": len(wins), "cap": cap, "m1_per100": m1 * 100 / cap if m1 is not None and cap else None,
            "rebates_per100": per100(rebates), "mtm": mtm, "mtm_per100": per100(mtm),
            "net_per100": (m1 * 100 / cap + per100(rebates) + per100(mtm))
            if m1 is not None and cap and mtm is not None else None}


def load_tape(tape_dir, c, t0, t1):
    path = os.path.join(tape_dir, f"{c}.jsonl")
    if not os.path.exists(path):
        return []
    rows = set()
    for line in open(path):
        r = json.loads(line)
        if t0 <= r["timestamp"] <= t1:
            rows.add((r["timestamp"], r["outcome_index"], r["side"][0], float(r["price"]), float(r["size"]),
                      f"{r.get('transaction_hash')}:{r['outcome_index']}:{r['price']}:{r['size']}"))
    return sorted(rows)


def replay(ev, snaps, tapes, mids, t0, t1, overrides, step_s):
    rebs = sorted((e for e in ev if e["event"] == "rebalance"), key=lambda e: e["ts"])
    first = [e for e in rebs if e["ts"] <= t0][-1]
    todo = [e for e in rebs if t0 < e["ts"] <= t1]
    tmp = tempfile.mkdtemp()
    clock = R.Clock(t0)
    cfg = lp.phase2_config(lp_dir=tmp, poll_s=step_s, **overrides)
    snap_ts = [s["ts"] for s in snaps]
    tok = {}
    for e in rebs:
        for c, m in (e.get("markets") or {}).items():
            tok[m["yes"]] = c

    def snap_at(t):
        i = bisect.bisect_right(snap_ts, t) - 1
        return snaps[i] if i >= 0 else None

    def mid_at(c, t):
        mt, mp = mids.get(c, ([], []))
        j = bisect.bisect_right(mt, t) - 1
        return mp[j] if j >= 0 else None

    def books_fn(tokens):
        s = snap_at(clock())
        out = {}
        if not s:
            return out
        for t in tokens:
            c = tok.get(t)
            bk = s["books"].get(c) if c else None
            if not bk or not bk[1] or not bk[2]:
                continue
            tick, bids, asks = bk
            a, b = mid_at(c, s["ts"]), mid_at(c, clock())
            shift = round(((b - a) if a is not None and b is not None else 0.0) / tick) * tick
            sh = lambda lv: [(round(min(1 - tick, max(tick, p + shift)), 6), z) for p, z in lv]
            out[t] = (sh(bids), sh(asks), tick)
        return out

    def cfgs_at(t, conds):
        s = snap_at(t)
        rw = s["rw"] if s else {}
        return {c: ({"rate": rw[c][0], "v": rw[c][1], "min": rw[c][2]} if c in rw else None) for c in conds}

    ex = lp.PaperExec(trades_fn=lambda c: [r for r in tapes.get(c, []) if r[0] <= clock()], every_s=cfg.fills_every_s,
                      lifetime=cfg.paper_lifetime, clock=clock)
    j = lp.Journal(os.path.join(tmp, "paper.jsonl"), clock=clock)
    start = [first["markets"][c] for c in first["target"]]
    test = lp.LPTest(cfg, ex, j, start, books_fn=books_fn, log=lambda *a: None, clock=clock)
    j.write("start", markets=[m["cond"] for m in start], config={}, quote_cap=sum(float(m.get("quote_cap") or 0) for m in start))

    async def go():
        t = t0
        k = 0
        while t < t1:
            clock.t = t
            while k < len(todo) and todo[k]["ts"] <= t:
                e = todo[k]
                test.pending_target = ([e["markets"][c] for c in e["target"]], e.get("summary") or {}, e["ts"])
                k += 1
            await test.step(t, cfgs_at(t, list(test.st)))
            t += step_s
        clock.t = t1
        test._close_window(t1)
        j.write("end", reason="time", pnl=test.pnl())
    asyncio.run(go())
    j.close()
    return lp.load_events(tmp, "paper")


def main():
    jpath, scan, tape_dir, out = sys.argv[1:5]
    arg = lambda k, d: type(d)(sys.argv[sys.argv.index(k) + 1]) if k in sys.argv else d
    t0, t1, step = arg("--from", 0.0), arg("--to", 0.0), arg("--step", 15.0)
    names = arg("--variants", ",".join(VARIANTS)).split(",")
    op = gzip.open if jpath.endswith(".gz") else open
    ev = [json.loads(line) for line in op(jpath, "rt")]
    snaps = R.load_snaps(scan, t0, t1)
    conds = {}
    for e in ev:
        if e["event"] == "rebalance" and e["ts"] <= t1:
            for c, m in (e.get("markets") or {}).items():
                conds[c] = m["yes"]
    tapes = {c: load_tape(tape_dir, c, t0 - 3600, t1) for c in conds}
    mids = {}
    for c, y in conds.items():
        try:
            mids[c] = R.fetch_mids(y, t0 - 7200, t1)
        except Exception as e:
            print(f"{c[:10]} mids failed: {e!r:.80}", flush=True)
    print(f"{len(snaps)} snapshots, {len(conds)} markets, {sum(len(v) for v in tapes.values())} tape trades", flush=True)
    res = {"journal": window_metrics(ev, t0, t1)}
    print("journal ", json.dumps(res["journal"]), flush=True)
    for v in names:
        rev = replay(ev, snaps, tapes, mids, t0, t1, VARIANTS[v], step)
        res[v] = window_metrics(rev, t0, t1)
        print(f"{v:8s}", json.dumps(res[v]), flush=True)
    json.dump(res, open(out, "w"), indent=1)


if __name__ == "__main__":
    main()
