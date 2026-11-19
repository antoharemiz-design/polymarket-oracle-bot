"""Phase 2 research: snapshots of every rewarded market's book, for out-of-sample tests of scaling levers
(capital, number of markets, category, reward size, quote placement). Read-only public data.

    python research/tools/lp_scan.py OUT_DIR --until EPOCH [--every 1800]

Every `every` seconds (aligned to the last snapshot on disk, so a restart continues the schedule):
  snap_<UTC>.json.gz  {"ts", "rw": {cond: [rate, v, min]}, "books": {cond: [tick, bids, asks]}}
                      YES book only, levels within 2v (and at most 40) of the midpoint
Every 6 hours:
  meta_<UTC>.json.gz  {cond: {question, event, event_title, fee_type, fee_rate, rebate_rate, created, end,
                              neg_risk, vol24, liquidity, holding_rewards, active}}
"""
import gzip
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polybot import lp  # noqa: E402


def trim(levels, mid, v_c, side):
    out = []
    for p, s in levels:
        d = (mid - p) if side == "bid" else (p - mid)
        if d * 100 > 2 * v_c:
            break
        out.append([p, round(s, 2)])
        if len(out) >= 40:
            break
    return out


def meta_snapshot(conds):
    gm = lp.gamma_markets(conds)
    out = {}
    for c, m in gm.items():
        ev = (m.get("events") or [{}])[0]
        fs = m.get("feeSchedule") or {}
        out[c] = {"question": m.get("question"), "event": str(ev.get("id") or c), "event_title": ev.get("title"),
                  "token": json.loads(m["clobTokenIds"])[0] if m.get("clobTokenIds") else None,
                  "fee_type": m.get("feeType") if m.get("feesEnabled") else None,
                  "fee_rate": fs.get("rate") if m.get("feesEnabled") else 0.0, "rebate_rate": fs.get("rebateRate"),
                  "created": m.get("createdAt") or m.get("startDate"), "end": m.get("endDate"),
                  "neg_risk": bool(m.get("negRisk")), "vol24": m.get("volume24hr"), "liquidity": m.get("liquidityNum"),
                  "holding_rewards": bool(m.get("holdingRewardsEnabled")), "game": bool(m.get("gameStartTime")),
                  "active": bool(m.get("active")) and not m.get("closed") and bool(m.get("acceptingOrders"))}
    return out


def snapshot(rw, meta):
    toks = {c: meta[c]["token"] for c in rw if c in meta and meta[c].get("token")}
    bk = lp.books(list(toks.values()))
    books = {}
    for c, t in toks.items():
        if t not in bk:
            continue
        bids, asks, tick = bk[t]
        if not bids or not asks:
            books[c] = [tick, [], []]
            continue
        mid = (bids[0][0] + asks[0][0]) / 2
        v = rw[c]["v"]
        books[c] = [tick, trim(bids, mid, v, "bid"), trim(asks, mid, v, "ask")]
    return books


def write_gz(path, obj):
    with gzip.open(path + ".tmp", "wt") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def main():
    out, until = sys.argv[1], float(sys.argv[sys.argv.index("--until") + 1])
    every = float(sys.argv[sys.argv.index("--every") + 1]) if "--every" in sys.argv else 1800.0
    os.makedirs(out, exist_ok=True)
    stamp = lambda t: time.strftime("%Y%m%dT%H%M%S", time.gmtime(t))
    while time.time() < until:
        snaps = sorted(f for f in os.listdir(out) if f.startswith("snap_"))
        metas = sorted(f for f in os.listdir(out) if f.startswith("meta_"))
        last = time.mktime(time.strptime(snaps[-1][5:20], "%Y%m%dT%H%M%S")) - time.timezone if snaps else 0
        wait = last + every - time.time()
        if wait > 0:
            time.sleep(min(wait, 60))
            continue
        t0 = time.time()
        try:
            rw = {c: r for c, r in lp.rewards_config().items() if r["rate"] > 0 and r["v"] > 0}
            meta = None
            if metas:
                with gzip.open(os.path.join(out, metas[-1]), "rt") as f:
                    meta = json.load(f)
            mt = time.mktime(time.strptime(metas[-1][5:20], "%Y%m%dT%H%M%S")) - time.timezone if metas else 0
            missing = [c for c in rw if not meta or c not in meta]
            if meta is None or t0 - mt > 6 * 3600 or len(missing) > 50:
                meta = meta_snapshot(list(rw))
                write_gz(os.path.join(out, f"meta_{stamp(t0)}.json.gz"), meta)
            books = snapshot(rw, meta)
            write_gz(os.path.join(out, f"snap_{stamp(t0)}.json.gz"),
                     {"ts": t0, "rw": {c: [r["rate"], r["v"], r["min"]] for c, r in rw.items()}, "books": books})
            print(f"{stamp(t0)} snapshot: {len(rw)} rewarded, {len(books)} books, {time.time() - t0:.0f} s", flush=True)
        except Exception as e:
            print(f"{stamp(t0)} snapshot failed: {e!r:.300}", flush=True)
            time.sleep(120)


if __name__ == "__main__":
    main()
