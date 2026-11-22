"""Phase 3 research: replay the bot's own quoting loop (polybot.lp.LPTest, paper rules) on recorded books and the
public trade tape, to compare exit rules on the same history (user's request, 22 Nov).

    python research/tools/lp_replay.py SCAN_DIR MARKETS.json OUT.json --from EPOCH --to EPOCH [--step 10]
                                       [--variants base,fifo,...]

MARKETS.json: [{"cond": ..., "k": 1..5}, ...] — each market is replayed alone at N = k x rewards_min_size.
Books: the latest lp_scan snapshot at or before the replay time (30 min apart), shifted by the change of the 1-minute
mid from the public price history since that snapshot, so the quoted prices follow the market between snapshots.
Fills: PaperExec's rule (a public taker trade through our price fills us first), with the replay clock.
Per variant and market: contracts filled, closed by taker (contracts, cost), share closed by taker, M1 earned,
rebates, mark-to-market at the end. Variants are LPConfig overrides on top of the phase 2 settings.
"""
import asyncio
import bisect
import gzip
import json
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polybot import lp  # noqa: E402

VARIANTS = {
    "base": {},                                                          # phase 2 as running on the VPS
    "fifo": {"inv_fifo": 1},
    "tick": {"exit_improve_ticks": 1},                                   # aged / exiting quotes one tick inside
    "tick_always": {"exit_improve_ticks": 1, "reduce_improve_always": 1},
    "tick_always_fifo": {"exit_improve_ticks": 1, "reduce_improve_always": 1, "inv_fifo": 1},
    "hold30": {"inv_reduce_h": 0.25, "inv_flatten_h": 0.5},
    "tick_always_fifo_hold30": {"exit_improve_ticks": 1, "reduce_improve_always": 1, "inv_fifo": 1,
                                "inv_reduce_h": 0.25, "inv_flatten_h": 0.5},
}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class ReplayExec(lp.PaperExec):
    """PaperExec on the replay clock: orders carry the replay time, the tape is cut at the replay time."""

    def __init__(self, tape, clock, every_s):
        super().__init__(trades_fn=lambda c: [r for r in tape.get(c, []) if r[0] <= clock()], every_s=every_s)
        self.clock = clock
        self.since = clock()

    async def place(self, m, side, price, size):
        self.n += 1
        oid = f"replay-{self.n}"
        self.orders[oid] = {"mkt": m["cond"], "side": side, "price": price, "rem": size, "t": self.clock()}
        return oid

    async def fills(self, markets):
        if self.clock() - self.last_poll < self.every_s:
            return []
        out = []
        self.last_poll = self.clock()
        for m in markets:
            mine = [(oid, o) for oid, o in self.orders.items() if o["mkt"] == m["cond"]]
            if not mine:
                continue
            for ts, oi, side, px, sz, key in sorted(self.trades_fn(m["cond"])):
                if key in self.seen or ts < self.since:
                    continue
                self.seen.add(key)
                live = [(oid, o) for oid, o in mine if o["t"] <= ts + 1 and o["rem"] > 1e-9]
                bid = next((o["price"] for _, o in live if o["side"] == "bid"), None)
                ask = next((o["price"] for _, o in live if o["side"] == "ask"), None)
                hit = lp.crosses((oi, side, px), bid, ask)
                if not hit:
                    continue
                oid, o = next((oid, o) for oid, o in live if o["side"] == hit)
                q = min(o["rem"], sz); o["rem"] -= q
                done = o["rem"] <= 1e-9
                if done:
                    self.orders.pop(oid, None)
                out.append({"order": oid, "mkt": m["cond"], "side": hit, "price": o["price"], "size": q, "ts": ts, "done": done})
        return out


def load_snaps(scan, t0, t1):
    files = sorted(f for f in os.listdir(scan) if f.startswith("snap_"))
    ts = lambda f: lp._ts(f[5:9] + "-" + f[9:11] + "-" + f[11:13] + "T" + f[14:16] + ":" + f[16:18] + ":" + f[18:20])
    keep = [f for f in files if t0 - 3600 <= ts(f) <= t1]
    out = []
    for f in keep:
        with gzip.open(os.path.join(scan, f), "rt") as fh:
            out.append(json.load(fh))
    return out


def fetch_tape(c, t0, t1):
    d = lp._req(f"{lp.DATA}/v2/trades?" + urllib.parse.urlencode({"condition": c, "taker_only": "true", "limit": 1000}))
    rows = []
    for r in d.get("data") or []:
        if t0 <= r["timestamp"] <= t1:
            rows.append((r["timestamp"], r["outcome_index"], r["side"][0], float(r["price"]), float(r["size"]),
                         f"{r.get('transaction_hash')}:{r['outcome_index']}:{r['price']}:{r['size']}"))
    return sorted(rows)


def fetch_mids(token, t0, t1):
    d = lp._req(f"{lp.CLOB}/prices-history?" + urllib.parse.urlencode({"market": token, "startTs": int(t0), "endTs": int(t1), "fidelity": 1}))
    h = sorted((x["t"], x["p"]) for x in d.get("history") or [])
    return [t for t, _ in h], [p for _, p in h]


def replay_market(m, snaps, tape, mids, t0, t1, overrides, step_s):
    cond = m["cond"]
    tmp = tempfile.mkdtemp()
    cfg = lp.phase2_config(lp_dir=tmp, poll_s=step_s, sample_s=60.0, **overrides)
    clock = Clock(t0)
    snap_ts = [s["ts"] for s in snaps]
    mt, mp = mids

    def snap_at(t):
        i = bisect.bisect_right(snap_ts, t) - 1
        return snaps[i] if i >= 0 else None

    def mid_at(t):
        j = bisect.bisect_right(mt, t) - 1
        return mp[j] if j >= 0 else None

    def books_fn(tokens):
        s = snap_at(clock())
        bk = s["books"].get(cond) if s else None
        if not bk or not bk[1] or not bk[2]:
            return {}
        tick, bids, asks = bk
        m0 = (bids[0][0] + asks[0][0]) / 2
        a, b = mid_at(s["ts"]), mid_at(clock())
        shift = round(((b - a) if a is not None and b is not None else 0.0) / tick) * tick
        sh = lambda lv: [(round(min(1 - tick, max(tick, p + shift)), 6), z) for p, z in lv]
        return {m["yes"]: (sh(bids), sh(asks), tick)}

    def cfg_at(t):
        s = snap_at(t)
        r = s["rw"].get(cond) if s else None
        return {cond: {"rate": r[0], "v": r[1], "min": r[2]} if r else None}

    ex = ReplayExec({cond: tape}, clock, every_s=cfg.fills_every_s)
    j = lp.Journal(os.path.join(tmp, "paper.jsonl"))
    test = lp.LPTest(cfg, ex, j, [m], books_fn=books_fn, log=lambda *a: None, clock=clock)

    async def go():
        t = t0
        while t < t1:
            clock.t = t
            await test.step(t, cfg_at(t))
            t += step_s
    asyncio.run(go())
    j.close()
    ev = lp.load_events(tmp, "paper")
    fills = [e for e in ev if e["event"] == "fill"]
    flat = [e for e in ev if e["event"] == "flatten"]
    filled = sum(e["size"] for e in fills)
    taker = sum(e["size"] for e in flat)
    s = test.st.get(cond)
    return {"filled": filled, "fills": len(fills), "taker_contracts": taker, "taker_n": len(flat),
            "taker_cost": -sum(e.get("cost", 0.0) for e in flat), "m1_usd": sum(e.get("usd", 0.0) for e in ev if e["event"] == "m1"),
            "rebate": sum(e.get("rebate", 0.0) for e in fills), "pnl": test.pnl(),
            "open": abs(s.pos()) if s else 0.0, "taker_share": taker / filled if filled else None}


def main():
    scan, mfile, out = sys.argv[1], sys.argv[2], sys.argv[3]
    arg = lambda k, d: type(d)(sys.argv[sys.argv.index(k) + 1]) if k in sys.argv else d
    t0, t1, step = arg("--from", 0.0), arg("--to", 0.0), arg("--step", 10.0)
    names = arg("--variants", ",".join(VARIANTS)).split(",")
    snaps = load_snaps(scan, t0, t1)
    meta = {}
    for f in sorted(x for x in os.listdir(scan) if x.startswith("meta_")):
        with gzip.open(os.path.join(scan, f), "rt") as fh:
            meta.update(json.load(fh))
    res = {v: {} for v in names}
    for spec in json.load(open(mfile)):
        c, k = spec["cond"], int(spec.get("k", 1))
        mm = meta.get(c) or {}
        last = next((s for s in reversed(snaps) if c in s["rw"] and c in s["books"]), None)
        if not last or not mm.get("token"):
            print(f"{c[:10]}: no snapshot or metadata in the window", flush=True)
            continue
        rate, v, mn = last["rw"][c]
        m = {"cond": c, "q": mm.get("question"), "yes": mm["token"], "no": None, "k": k, "N": k * mn, "v": v, "min": mn,
             "rate": rate, "tick": last["books"][c][0], "end": mm.get("end"), "fee_rate": mm.get("fee_rate") or 0.0,
             "rebate_rate": mm.get("rebate_rate") or 0.0, "quote_cap": k * mn}
        tape, mids = fetch_tape(c, t0, t1), fetch_mids(mm["token"], t0 - 3600, t1)
        for vname in names:
            r = replay_market(m, snaps, tape, mids, t0, t1, VARIANTS[vname], step)
            r.update(q=(mm.get("question") or "")[:60], k=k, N=k * mn, trades=len(tape))
            res[vname][c] = r
            print(f"{vname:24s} {r['q'][:44]:44s} k={k} trades {len(tape):4d} filled {r['filled']:6.0f} taker {r['taker_contracts']:5.0f} "
                  f"({(r['taker_share'] or 0):4.0%}) cost ${r['taker_cost']:5.2f} M1 ${r['m1_usd']:5.2f} pnl ${r['pnl']:6.2f}", flush=True)
    json.dump(res, open(out, "w"), indent=1)
    print()
    for vname in names:
        rs = res[vname].values()
        f = sum(r["filled"] for r in rs)
        tk = sum(r["taker_contracts"] for r in rs)
        print(f"{vname:24s} filled {f:7.0f} taker {tk:6.0f} share {tk / f if f else 0:5.1%} cost ${sum(r['taker_cost'] for r in rs):6.2f} "
              f"M1 ${sum(r['m1_usd'] for r in rs):6.2f} rebates ${sum(r['rebate'] for r in rs):5.2f} pnl ${sum(r['pnl'] for r in rs):7.2f}")


if __name__ == "__main__":
    main()
