"""Audit of the live run of 25-26 Nov from public data only: the wallet's activity (data-api /v2/activity: trades,
merges, redeems, rewards), the markets' 1-minute price history and their public taker tapes.

    python research/tools/live_audit_wallet.py WALLET T_START T_STOP [--out OUT.json]

Prints:
  1. per market: our contracts bought, merges, the cash flow, what is left and its value now, PnL; split into the
     bot's run (until T_STOP) and afterwards (trades by hand);
  2. our maker fills during the run: markout at 1 / 5 / 30 / 60 minutes and at the outcome, against the average
     maker fill of the same markets in the same hours (public taker tape), by market and by local hour;
  3. rewards paid.
"""
import bisect
import collections
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polybot.lp import wx_local_hours  # noqa: E402


def get(url):
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research", "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except Exception:
            if i == 3:
                raise
            time.sleep(2)


def paged(path, q):
    out, cur = [], None
    while True:
        p = dict(q, limit=500 if "activity" in path else 1000)
        if cur:
            p["cursor"] = cur
        d = get(f"https://data-api.polymarket.com{path}?" + urllib.parse.urlencode(p))
        out += d["data"]
        cur = (d.get("pagination") or {}).get("next_cursor")
        if not cur:
            return out


def main():
    wallet, t0, t1 = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    out_path = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    act = sorted(paged("/v2/activity", {"user": wallet}), key=lambda x: x["timestamp"])
    act = [x for x in act if x["timestamp"] >= t0 - 600]
    conds = sorted({x["condition_id"] for x in act if x.get("condition_id")})
    meta, hist, tape = {}, {}, {}
    for c in conds:
        m = get(f"https://gamma-api.polymarket.com/markets?condition_ids={c}")
        m = m[0] if m else {}
        toks = json.loads(m.get("clobTokenIds") or "[]")
        meta[c] = {"q": m.get("question", c), "tokens": toks, "prices": [float(p) for p in json.loads(m.get("outcomePrices") or "[0,0]")],
                   "closed": m.get("closed")}
        h = get(f"https://clob.polymarket.com/prices-history?market={toks[0]}&interval=max&fidelity=1").get("history", []) if toks else []
        hist[c] = ([int(x["t"]) for x in h], [float(x["p"]) for x in h])
        tape[c] = [t for t in paged("/v2/trades", {"condition_id": c, "taker_only": "true"}) if t0 <= t["timestamp"] <= t1]

    def mid(c, t):
        ts, ps = hist[c]
        j = bisect.bisect_right(ts, t) - 1
        return ps[j] if j >= 0 and t <= ts[-1] + 120 else None

    # 1. PnL per market
    rows, tot = [], collections.Counter()
    for c in conds:
        pos = [0.0, 0.0]
        cash = {"run": 0.0, "after": 0.0}
        bought = merged = 0.0
        for x in act:
            if x.get("condition_id") != c:
                continue
            k = "run" if x["timestamp"] <= t1 else "after"
            usd, z = float(x.get("usdc_size") or 0), float(x.get("size") or 0)
            if x["type"] == "TRADE":
                i = int(x["outcome_index"])
                if x["side"] == "BUY":
                    pos[i] += z; cash[k] -= usd
                    bought += z if k == "run" else 0
                else:
                    pos[i] -= z; cash[k] += usd
            elif x["type"] == "MERGE":
                pos[0] -= z; pos[1] -= z; cash[k] += usd
                merged += z if k == "run" else 0
            elif x["type"] == "REDEEM":
                cash[k] += usd
                pos = [0.0, 0.0]
        value = pos[0] * meta[c]["prices"][0] + pos[1] * meta[c]["prices"][1]
        pnl = cash["run"] + cash["after"] + value
        rows.append({"market": meta[c]["q"][:62], "bought_run": round(bought, 1), "merged_run": round(merged, 1),
                     "cash_run": round(cash["run"], 2), "cash_after": round(cash["after"], 2),
                     "left_yes_no": [round(pos[0], 2), round(pos[1], 2)], "left_value": round(value, 2), "pnl": round(pnl, 2)})
        tot["pnl"] += pnl; tot["cash_run"] += cash["run"]; tot["cash_after"] += cash["after"]; tot["value"] += value
    rows.sort(key=lambda r: r["pnl"])
    rewards = [(time.strftime("%m-%d %H:%M", time.gmtime(x["timestamp"])), float(x.get("usdc_size") or 0))
               for x in act if x["type"] == "REWARD"]
    print(f"1. markets {len(conds)}; trading PnL ${tot['pnl']:.2f} (bot run cash {tot['cash_run']:.2f}, by hand after "
          f"{tot['cash_after']:.2f}, left now {tot['value']:.2f}); rewards {rewards}")
    for r in rows:
        print(f"   {r['market']:62s} bought {r['bought_run']:6.1f} merged {r['merged_run']:6.1f} cash {r['cash_run']:7.2f} "
              f"after {r['cash_after']:7.2f} left {r['left_yes_no']} ${r['left_value']:.2f}  PnL {r['pnl']:7.2f}")

    # 2. markouts of our fills against the public tape of the same markets and hours
    def mk_of(c, ts, sg, y, w, acc):
        for k, dt in (("m1", 60), ("m5", 300), ("m30", 1800), ("m60", 3600)):
            m = mid(c, ts + dt)
            if m is not None:
                acc[k] += sg * (m - y) * w
                acc["w" + k] += w
        win = meta[c]["prices"][0]
        if meta[c]["closed"] or win in (0.0, 1.0):
            acc["res"] += sg * (win - y) * w
            acc["wres"] += w
        acc["n"] += 1
        acc["size"] += w

    ours, theirs = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
    by_hour = collections.defaultdict(collections.Counter)
    fills = []
    for x in act:
        if x["type"] != "TRADE" or not t0 <= x["timestamp"] <= t1:
            continue
        c, i, z, p = x["condition_id"], int(x["outcome_index"]), float(x["size"]), float(x["price"])
        y = p if i == 0 else 1 - p
        long_yes = (x["side"] == "BUY") == (i == 0)
        sg = 1 if long_yes else -1
        mk_of(c, x["timestamp"], sg, y, z, ours[c])
        mk_of(c, x["timestamp"], sg, y, z, ours["ALL"])
        h = wx_local_hours(meta[c]["q"], x["timestamp"])
        hb = "day before (<0 h)" if h is not None and h < 0 else "measured day 0-6 h" if h is not None and h < 6 else \
            "measured day 6-12 h" if h is not None and h < 12 else "measured day >=12 h" if h is not None else "?"
        mk_of(c, x["timestamp"], sg, y, z, by_hour[hb])
        fills.append({"ts": x["timestamp"], "mkt": c, "y": y, "size": z, "sg": sg, "h": h})
    for c in conds:
        for t in tape[c]:
            if t.get("proxy_wallet", "").lower() == wallet.lower():
                continue
            oi, px, z = int(t["outcome_index"]), float(t["price"]), float(t["size"])
            buy_yes = (t["side"] == "BUY") if oi == 0 else (t["side"] != "BUY")
            y = px if oi == 0 else 1 - px
            m0 = mid(c, t["timestamp"] - 60)
            if m0 is None or abs(y - m0) > 0.045:
                continue
            mk_of(c, t["timestamp"], -1 if buy_yes else 1, y, min(z, 20.0), theirs[c])
            mk_of(c, t["timestamp"], -1 if buy_yes else 1, y, min(z, 20.0), theirs["ALL"])

    def fmt(a):
        f = lambda k: f"{100 * a[k] / a['w' + k]:6.2f}" if a["w" + k] else "   nan"
        return f"fills {a['n']:4d} contracts {a['size']:7.0f}  m1 {f('m1')}  m5 {f('m5')}  m30 {f('m30')}  m60 {f('m60')}  res {f('res')}"
    print("\n2. markout, cents per contract (maker's side); ours vs the average maker fill in the reward zone, same markets and hours")
    print(f"   {'ALL ours':40s} {fmt(ours['ALL'])}")
    print(f"   {'ALL tape':40s} {fmt(theirs['ALL'])}")
    for c in conds:
        if ours[c]["n"]:
            print(f"   ours {meta[c]['q'][:35]:35s} {fmt(ours[c])}")
            print(f"   tape {'':35s} {fmt(theirs[c])}")
    print("\n   ours by local time of the measured day")
    for k in sorted(by_hour):
        print(f"   {k:40s} {fmt(by_hour[k])}")
    if out_path:
        json.dump({"markets": rows, "rewards": rewards, "fills": fills,
                   "ours": {k: dict(v) for k, v in ours.items()}, "tape": {k: dict(v) for k, v in theirs.items()},
                   "by_hour": {k: dict(v) for k, v in by_hour.items()}, "meta": meta}, open(out_path, "w"), indent=1)


if __name__ == "__main__":
    main()
