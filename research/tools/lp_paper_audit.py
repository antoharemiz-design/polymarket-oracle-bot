"""Audit of the paper fill model on a phase 2 journal (22 Nov): which public trades crossed our orders while they
stood, and which of them paper counted.

    python3 research/tools/lp_paper_audit.py JOURNAL.jsonl [--hours 48]

Standard library only, so it runs on the VPS as is (read-only: the journal and public market data).

Why. Paper (PaperExec) reads the public tape every 30 s and fills only the orders that still stand at that moment.
An order cancelled or re-priced in between never gets the trades that crossed it while it stood. On a live exchange
those trades fill us, and they come exactly when the price runs through our quote (adverse fills). If many crossing
trades are missed, and they mark out worse than the counted ones, paper overstates the result.

How. Order lifetimes come from the journal: a `place` (market, side, price, size) stands until the next `cancel` of
that side (or of the market), until fills reach its size, or until the process stops (`start` / `stop` / `pause`).
For each lifetime, the public taker trades (data-api, newest 1000 per market) that cross its price, from its
placement to its end, fill it first in the queue, up to its size (the paper rule, without the 30-second poll).
Per market and in total: crossing contracts, those paper counted (journal `fill`), those it missed, and the markout
of both groups against the 1-minute price history (clob prices-history) after 60 s and 600 s, in cents per contract
(positive = in our favour). The journal's own markout of the counted fills is shown as a check of the price source.

Share closed by taker (second table). Paper: closing-order contracts / filled contracts, as in the journal. Estimate
with the missed fills: each missed fill is matched against the paper position (YES - NO) at its time:
  reducing   = the part that would have cut the position (a bid while short, an ask while long, up to |position|):
               an opposite fill that closes for free, so one closing-order contract less;
  increasing = the rest: a new position, closed by taker at the market's paper rate (closing contracts / filled).
  estimate   = (max(0, taker - reducing) + increasing x paper rate) / (filled + missed)
This is a first-order estimate on the paper path (the bot would have acted differently afterwards); the full answer
is a replay of the test with the corrected fill model (research/tools/lp_replay.py, paper_lifetime=1).
"""
import bisect
import collections
import itertools
import json
import sys
import time
import urllib.parse
import urllib.request

DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def req(url):
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "lp-paper-audit"}), timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            if i == 2:
                raise
            time.sleep(2 * (i + 1))


def crosses(oi, side, px, bid, ask):
    """Same rule as polybot.lp.crosses: does a taker trade go through our YES bid / ask?"""
    buy_yes, y = (side == "B", px) if oi == 0 else (side != "B", 1 - px)
    if buy_yes and ask is not None and y >= ask - 1e-9:
        return "ask"
    if not buy_yes and bid is not None and y <= bid + 1e-9:
        return "bid"
    return None


def lifetimes(ev):
    """[(mkt, side, price, size, t_place, t_end)] from place / cancel / fill / start / stop / pause events."""
    open_, out = {}, []

    def end(key, t):
        o = open_.pop(key, None)
        if o:
            out.append((key[0], key[1], o["price"], o["size"], o["t"], t))
    for e in sorted(ev, key=lambda e: e["ts"]):
        k = e["event"]
        if k == "place":
            end((e["mkt"], e["side"]), e["ts"])
            open_[(e["mkt"], e["side"])] = {"price": e["price"], "size": e["size"], "t": e["ts"], "filled": 0.0}
        elif k == "cancel":
            if e.get("scope") == "market":
                for key in [x for x in open_ if x[0] == e["mkt"]]:
                    end(key, e["ts"])
            else:
                end((e["mkt"], e["side"]), e["ts"])
        elif k == "fill":
            o = open_.get((e["mkt"], e["side"]))
            if o:
                o["filled"] += e["size"]
                if o["filled"] >= o["size"] - 1e-9:
                    end((e["mkt"], e["side"]), e.get("fill_ts", e["ts"]))
        elif k in ("start", "stop", "pause"):
            for key in list(open_):
                end(key, e["ts"])
    last = max(e["ts"] for e in ev)
    for key in list(open_):
        end(key, last)
    return out


def main():
    path = sys.argv[1]
    hours = float(sys.argv[sys.argv.index("--hours") + 1]) if "--hours" in sys.argv else 48.0
    ev = [json.loads(line) for line in open(path)]
    t0 = min(e["ts"] for e in ev if e["event"] == "start")
    ev = [e for e in ev if t0 <= e["ts"] <= t0 + hours * 3600]
    t1 = max(e["ts"] for e in ev)
    names, yes = {}, {}
    for e in ev:
        if e["event"] == "rebalance":
            for c, m in (e.get("markets") or {}).items():
                names[c] = (m.get("q") or c)[:56]
                yes[c] = m.get("yes")
    lives = lifetimes(ev)
    paper = collections.defaultdict(list)                   # (mkt, side) -> [(trade ts, price, size)]
    for e in ev:
        if e["event"] == "fill":
            paper[(e["mkt"], e["side"])].append((e.get("fill_ts", e["ts"]), e["price"], e["size"]))
    jmk = collections.defaultdict(lambda: [0.0, 0.0])        # mkt -> [markout x size at 600 s, size]
    for e in ev:
        if e["event"] == "markout" and e.get("horizon") == 600:
            jmk[e["mkt"]][0] += e["markout"] * e["size"]
            jmk[e["mkt"]][1] += e["size"]
    by_mkt = collections.defaultdict(list)
    for lt in lives:
        by_mkt[lt[0]].append(lt)
    tot = collections.Counter()
    share_rows = []
    print(f"journal {path}: {(t1 - t0) / 3600:.1f} h, {len(lives)} order lifetimes, {len(by_mkt)} markets\n")
    print(f"{'market':56s} {'cross':>7s} {'paper':>7s} {'missed':>7s} {'miss %':>6s} | markout c/contract 60 s / 600 s: "
          f"{'paper':>13s} {'missed':>13s} | journal 600 s")
    for c, lts in sorted(by_mkt.items(), key=lambda kv: -len(kv[1])):
        try:
            d = req(f"{DATA}/v2/trades?" + urllib.parse.urlencode({"condition": c, "taker_only": "true", "limit": 1000}))
            tape = sorted((r["timestamp"], r["outcome_index"], r["side"][0], float(r["price"]), float(r["size"]))
                          for r in d.get("data") or [])
        except Exception as e:
            print(f"{names.get(c, c)[:56]:56s} tape failed: {e!r:.80}")
            continue
        cut = len(tape) >= 1000 and tape[0][0] > min(lt[4] for lt in lts)
        hist_t, hist_p = [], []
        if yes.get(c):
            try:
                h = req(f"{CLOB}/prices-history?" + urllib.parse.urlencode(
                    {"market": yes[c], "startTs": int(t0 - 3600), "endTs": int(t1 + 3600), "fidelity": 1}))
                hh = sorted((x["t"], x["p"]) for x in h.get("history") or [])
                hist_t, hist_p = [t for t, _ in hh], [p for _, p in hh]
            except Exception:
                pass

        def price_at(t):
            j = bisect.bisect_right(hist_t, t) - 1
            return hist_p[j] if j >= 0 else None
        cross, used = [], set()
        for mkt, side, price, size, ta, tb in lts:
            left = size
            for ts, oi, sd, px, sz in tape:
                if ts + 1 < ta or ts > tb or left <= 1e-9:
                    continue
                if crosses(oi, sd, px, price if side == "bid" else None, price if side == "ask" else None) == side:
                    q = min(left, sz)
                    left -= q
                    cross.append((ts, side, price, q))
        got = paper
        counted, missed = [], []
        for ts, side, price, q in cross:
            hit = next((i for i, (t, p, z) in enumerate(got.get((c, side), []))
                        if (c, side, i) not in used and abs(t - ts) <= 1 and abs(p - price) < 1e-9), None)
            if hit is not None:
                used.add((c, side, hit))
                counted.append((ts, side, price, q))
            else:
                missed.append((ts, side, price, q))

        def mk(group, h):
            num = den = 0.0
            for ts, side, price, q in group:
                later = price_at(ts + h)
                if later is None:
                    continue
                num += ((later - price) if side == "bid" else (price - later)) * q
                den += q
            return 100 * num / den if den else None
        # the paper position path of this market (YES - NO): fills and closing orders, against which missed fills count
        path = sorted((e.get("fill_ts", e["ts"]), e["size"] if e["side"] == "bid" else -e["size"])
                      for e in ev if e.get("mkt") == c and e["event"] in ("fill", "flatten"))
        pt, cum = [t for t, _ in path], list(itertools.accumulate(d for _, d in path))
        red = inc = 0.0
        for ts, side, price, q in missed:
            j = bisect.bisect_left(pt, ts) - 1
            net = cum[j] if j >= 0 else 0.0
            d = q if side == "bid" else -q
            r_ = min(q, abs(net)) if net * d < 0 else 0.0
            red += r_
            inc += q - r_
        filled = sum(e["size"] for e in ev if e.get("mkt") == c and e["event"] == "fill")
        taker = sum(e["size"] for e in ev if e.get("mkt") == c and e["event"] == "flatten")
        fmt = lambda x: "   -" if x is None else f"{x:+5.2f}"
        cq, pq, mq = (sum(x[3] for x in g) for g in (cross, counted, missed))
        rate = taker / filled if filled else 0.0
        est = max(0.0, taker - red) + inc * rate
        share_rows.append((names.get(c, c)[:56], filled, taker, mq, red, inc, est, cut))
        tot["filled"] += filled; tot["taker"] += taker; tot["red"] += red; tot["inc"] += inc; tot["est"] += est
        jm = 100 * jmk[c][0] / jmk[c][1] if jmk[c][1] else None
        print(f"{names.get(c, c)[:56]:56s} {cq:7.0f} {pq:7.0f} {mq:7.0f} {mq / cq if cq else 0:6.0%} | "
              f"{fmt(mk(counted, 60))} / {fmt(mk(counted, 600))}  {fmt(mk(missed, 60))} / {fmt(mk(missed, 600))} | "
              f"{fmt(jm)}{'  (tape cut: older trades not fetched)' if cut else ''}", flush=True)
        for g, name in ((counted, "paper"), (missed, "missed")):
            for h in (60, 600):
                for ts, side, price, q in g:
                    later = price_at(ts + h)
                    if later is not None:
                        tot[f"{name}_{h}_num"] += ((later - price) if side == "bid" else (price - later)) * q
                        tot[f"{name}_{h}_den"] += q
        tot["cross"] += cq; tot["paper"] += pq; tot["missed"] += mq
    r = lambda n, h: 100 * tot[f"{n}_{h}_num"] / tot[f"{n}_{h}_den"] if tot[f"{n}_{h}_den"] else None
    fmt = lambda x: "   -" if x is None else f"{x:+5.2f}"
    print(f"\n{'TOTAL':56s} {tot['cross']:7.0f} {tot['paper']:7.0f} {tot['missed']:7.0f} "
          f"{tot['missed'] / tot['cross'] if tot['cross'] else 0:6.0%} | {fmt(r('paper', 60))} / {fmt(r('paper', 600))}  "
          f"{fmt(r('missed', 60))} / {fmt(r('missed', 600))}")
    print("cross = contracts of public taker trades through our standing orders (first in the queue, up to the order "
          "size); paper = those the paper run counted; missed = the rest (the order was gone by the next 30 s poll)")

    print(f"\nShare of filled contracts closed by taker: paper (journal) and estimated with the missed fills")
    print(f"{'market':56s} {'filled':>7s} {'taker':>7s} {'paper %':>7s} {'missed':>7s} {'reduce':>7s} {'increase':>8s} "
          f"{'estimate %':>10s}")
    for name, filled, taker, mq, red, inc, est, cut in sorted(share_rows, key=lambda r: -r[1]):
        if not filled and not mq:
            continue
        print(f"{name:56s} {filled:7.0f} {taker:7.0f} {taker / filled if filled else 0:7.0%} {mq:7.0f} {red:7.0f} {inc:8.0f} "
              f"{est / (filled + mq) if filled + mq else 0:10.0%}{'  (tape cut)' if cut else ''}")
    f, m = tot["filled"], tot["missed"]
    print(f"{'TOTAL':56s} {f:7.0f} {tot['taker']:7.0f} {tot['taker'] / f if f else 0:7.0%} {m:7.0f} {tot['red']:7.0f} "
          f"{tot['inc']:8.0f} {tot['est'] / (f + m) if f + m else 0:10.0%}")
    print("reduce / increase = missed contracts that would have cut / grown the paper position at their time; "
          "estimate = (max(0, taker - reduce) + increase x paper rate) / (filled + missed)")


if __name__ == "__main__":
    main()
