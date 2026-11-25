"""Phase 2 of the liquidity-rewards test (polybot/lp.py, research/PREREG_PHASE2.md): rolling selection, snapshot
store, rebalancing, exits before the end, paper rebates and closing costs, the phase 2 report."""
import asyncio
import gzip
import importlib.util
import json
import os
import tempfile
import time
import types
import unittest
from decimal import Decimal
from unittest import mock

from polybot import lp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
NOW = 1_800_000_000.0


def book(bid=0.48, ask=0.52, size=100.0, tick=0.01):
    return [tick, [[bid, size], [round(bid - tick, 4), size]], [[ask, size], [round(ask + tick, 4), size]]]


def meta(c, end_h=72.0, fee_type="weather_fees", q=None, event=None, created_h=-48.0, game=False, active=True):
    return {"question": q or f"Will the high in city {c} be 20C?", "event": event or f"E{c}", "token": f"Y{c}",
            "no": f"N{c}", "fee_type": fee_type, "fee_rate": 0.05, "rebate_rate": 0.25, "created": ISO(NOW + created_h * 3600),
            "end": ISO(NOW + end_h * 3600), "neg_risk": False, "game": game, "active": active}


def mk(cond="C1", N=20.0, k=1, end_h=72.0, rate=10.0, quote_cap=20.0, **kw):
    m = {"cond": cond, "yes": f"Y{cond}", "no": f"N{cond}", "N": N, "k": k, "v": 4.5, "min": N / k, "rate": rate,
         "tick": 0.01, "q": f"market {cond}", "end": ISO(NOW + end_h * 3600), "fee_rate": 0.05, "rebate_rate": 0.25,
         "quote_cap": quote_cap, "m1_usd_day": 1.0}
    m.update(kw)
    return m


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class SharedFunctionsTest(unittest.TestCase):
    def test_evaluate_joins_best_prices(self):
        rate, frac_book = 10.0, book()
        m1, comp, cap = lp.evaluate([rate, 4.5, 20.0], frac_book, 1)
        bids, asks = frac_book[1], frac_book[2]
        self.assertAlmostEqual(m1, rate * lp.m1_fraction(bids, asks, 0.48, 0.52, 20.0, 4.5, 20.0))
        self.assertEqual(comp, lp.comp_levels(bids, asks, 0.5, 4.5, 20.0))
        self.assertAlmostEqual(cap, 20.0 * (0.48 + 1 - 0.52))
        m1_3, _, cap3 = lp.evaluate([rate, 4.5, 20.0], frac_book, 3)
        self.assertAlmostEqual(cap3, 3 * cap)
        self.assertGreater(m1_3, m1)

    def test_evaluate_none_outside_zone_or_mid_range(self):
        self.assertIsNone(lp.evaluate([10.0, 4.5, 20.0], book(0.40, 0.60), 1))       # half spread 10 c > v
        self.assertIsNone(lp.evaluate([10.0, 4.5, 20.0], book(0.05, 0.07), 1))       # mid outside [0.15, 0.85]
        self.assertIsNone(lp.evaluate([0.0, 4.5, 20.0], book(), 1))                  # no reward
        self.assertIsNone(lp.evaluate([10.0, 4.5, 20.0], [0.01, [], [[0.5, 10]]], 1))

    def test_eligible_filters(self):
        ok = meta("A")
        self.assertTrue(lp.eligible(ok, NOW))
        self.assertFalse(lp.eligible(dict(ok, game=True), NOW))
        self.assertFalse(lp.eligible(dict(ok, active=False), NOW))
        self.assertFalse(lp.eligible(ok, NOW, exclude=("weather_fees",)))
        self.assertFalse(lp.eligible(dict(ok, question="Will Iran and Israel agree a ceasefire?"), NOW))
        self.assertTrue(lp.eligible(dict(ok, question="Will Iran and Israel agree a ceasefire?"), NOW, topic=False))
        self.assertFalse(lp.eligible(meta("B", end_h=0.5), NOW, min_hours_left=1.0))
        self.assertTrue(lp.eligible(meta("B", end_h=0.5), NOW, min_hours_left=0.0))
        self.assertFalse(lp.eligible(meta("C", created_h=-1), NOW, min_age_days=7))
        self.assertFalse(lp.eligible(dict(ok, end=None), NOW))
        self.assertFalse(lp.eligible(None, NOW))
        self.assertFalse(lp.eligible(ok, NOW, category="politics_fees"))

    def test_snap_rows(self):
        rw = {"A": [10.0, 4.5, 20.0], "B": [10.0, 4.5, 20.0], "C": [5.0, 4.5, 20.0]}
        bk = {"A": book(), "B": book(0.30, 0.50), "C": book()}
        rows = lp.snap_rows(rw, bk, ["A", "B", "D"], 3)
        self.assertEqual(set(rows), {"A"})                 # B outside the zone, C not asked, D has no book
        m1_k, cap_k, comp = rows["A"]
        self.assertEqual(len(m1_k), 3)
        self.assertEqual(len(cap_k), 3)
        self.assertEqual(comp, 4)
        self.assertTrue(cap_k[0] < cap_k[1] < cap_k[2])

    def test_grid_spacing(self):
        es = [{"ts": t} for t in (0, 600, 1795, 3600, 3700, 5400)]
        self.assertEqual([e["ts"] for e in lp.grid(es, 1800)], [0, 1795, 3600, 5400])


class AllocateTest(unittest.TestCase):
    def row(self, c, m1, cap, event=None, kmax=5, conc=0.6):
        # concave M1 in k: each extra unit adds conc of the previous gain
        m1_k, g, tot = [], m1, 0.0
        for _ in range(kmax):
            tot += g; m1_k.append(tot); g *= conc
        return {"cond": c, "event": event or c, "m1_k": m1_k, "cap_k": [cap * (i + 1) for i in range(kmax)]}

    def test_budget_and_kmax(self):
        rows = [self.row("A", 5.0, 10.0), self.row("B", 4.0, 10.0), self.row("C", 1.0, 10.0)]
        out = lp.allocate(rows, 100.0, 5, 40, 0.4)
        self.assertLessEqual(sum(x["quote_cap"] for x in out), 100.0 + 1e-9)
        self.assertTrue(all(1 <= x["k"] <= 5 for x in out))
        self.assertEqual(lp.allocate(rows, 100.0, 1, 40, 1.0)[0]["k"], 1)

    def test_one_market_per_event(self):
        rows = [self.row("A", 5.0, 10.0, event="E"), self.row("B", 4.0, 10.0, event="E"), self.row("C", 3.0, 10.0)]
        out = lp.allocate(rows, 1000.0, 1, 40, 1.0)
        self.assertEqual(sorted(x["cond"] for x in out), ["A", "C"])

    def test_capital_share_per_market(self):
        out = lp.allocate([self.row("A", 5.0, 30.0, conc=1.0)], 300.0, 5, 40, 0.4)
        self.assertEqual(out[0]["k"], 4)                   # a fifth unit would put $150 > 40% of $300 in one market
        self.assertAlmostEqual(out[0]["quote_cap"], 120.0)

    def test_m1_share_cut_by_one_unit(self):
        rows = [self.row("A", 10.0, 10.0, conc=0.9)] + [self.row(c, 3.0, 10.0, conc=0.5) for c in "BCD"]
        free = {x["cond"]: x for x in lp.allocate(rows, 150.0, 5, 40, 1.0)}
        capped = {x["cond"]: x for x in lp.allocate(rows, 150.0, 5, 40, 0.4)}
        self.assertIn("A", capped)
        self.assertLess(capped["A"]["k"], free["A"]["k"])
        tot = sum(x["m1_usd_day"] for x in capped.values())
        self.assertLessEqual(capped["A"]["m1_usd_day"] / tot, 0.4 + 1e-9)

    def test_markets_max(self):
        rows = [self.row(c, 5.0 - i * 0.1, 10.0) for i, c in enumerate("ABCDEFG")]
        self.assertLessEqual(len(lp.allocate(rows, 1000.0, 1, 3, 1.0)), 3)


class RankTest(unittest.TestCase):
    def test_median_with_zeros_and_filters(self):
        q = lambda m1, comp: [[m1, m1 * 1.5], [10.0, 20.0], comp]
        snaps = [{"A": q(2.0, 4), "B": q(9.0, 4), "C": q(3.0, 1)},
                 {"A": q(2.0, 4), "C": q(3.0, 1)},
                 {"A": q(2.0, 4), "C": q(3.0, 1)}]
        rows, chosen = lp.rank(snaps, {}, 2, 3.0, 100.0, 40, 1.0)
        self.assertEqual([r["cond"] for r in rows], ["A"])  # B: median M1 0 (quotable once); C: median comp 1 < 3
        self.assertEqual(chosen[0]["cond"], "A")
        self.assertAlmostEqual(rows[0]["quotable_share"], 1.0)

    def test_capital_median_over_quotable_snapshots(self):
        snaps = [{"A": [[2.0], [10.0], 4]}, {"A": [[2.0], [30.0], 4]}, {"A": [[2.0], [20.0], 4]}, {}]
        rows, _ = lp.rank(snaps, {}, 1, 3.0, 100.0, 40, 1.0)
        self.assertAlmostEqual(rows[0]["quote_cap"], 20.0)
        self.assertAlmostEqual(rows[0]["m1_usd_day"], 2.0)    # median of 2, 2, 2, 0

    def test_candidates_limit(self):
        snaps = [{"A": [[2.0], [10.0], 4], "B": [[3.0], [10.0], 4]}]
        rows, _ = lp.rank(snaps, {}, 1, 3.0, 100.0, 40, 1.0, cands=["A"])
        self.assertEqual([r["cond"] for r in rows], ["A"])

    def test_same_choice_as_the_simulator(self):
        spec = importlib.util.spec_from_file_location("lp_levers", os.path.join(ROOT, "research", "tools", "lp_levers.py"))
        L = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(L)
        import random
        rnd = random.Random(5)
        metas, snaps = {}, []
        for i in range(30):
            metas[f"M{i}"] = dict(meta(f"M{i}", event=f"E{i % 22}"), created=ISO(NOW - 30 * 86400), end=ISO(NOW + 30 * 86400))
        for j in range(8):
            rw, bk = {}, {}
            for i in range(30):
                c = f"M{i}"
                if rnd.random() < 0.15:
                    continue
                mn = rnd.choice([10.0, 20.0, 50.0])
                rw[c] = [rnd.uniform(1, 30), rnd.choice([3.5, 4.5]), mn]
                mid = rnd.uniform(0.2, 0.8)
                lv = lambda side: [[round(mid + side * (0.01 + 0.01 * n), 2), rnd.choice([5.0, 20.0, 60.0, 200.0])] for n in range(5)]
                bk[c] = [0.01, lv(-1), lv(+1)]
            snaps.append({"ts": NOW + j * 1800, "rw": rw, "books": bk})
        conf = L.Conf("t", budget=300, alloc="marginal", kmax=5, max_min=200, min_age_days=0, min_days_left=0,
                      exclude=L.TOXIC, markets_max=40, max_share=0.4)
        sim = L.run(conf, metas, snaps, [])
        cands = [c for c, r in snaps[0]["rw"].items() if r[2] <= 200 and L.eligible(metas.get(c), snaps[0]["ts"], conf)]
        rows_by_snap = [lp.snap_rows(s["rw"], s["books"], cands, 5) for s in snaps]
        _, chosen = lp.rank(rows_by_snap, {c: metas[c]["event"] for c in metas}, 5, 3.0, 300.0, 40, 0.4, cands=cands)
        self.assertEqual(sorted(zip(sim["selected"], sim["ks"])), sorted((x["cond"], x["k"]) for x in chosen))
        self.assertAlmostEqual(sim["pred"], sum(x["m1_usd_day"] for x in chosen), places=4)


class ConfigTest(unittest.TestCase):
    def test_phase2_defaults_and_overrides(self):
        old = {k: os.environ.pop(k, None) for k in ("LP_PHASE", "LP_QUOTE_BUDGET_USD", "LP_KMAX")}
        try:
            c = lp.from_env(phase=2)
            self.assertEqual(c.phase, 2)
            self.assertEqual(c.quote_budget_usd, 300.0)
            self.assertEqual(c.capital_usd, 500.0)
            self.assertEqual(c.kmax, 5)
            self.assertEqual((c.exit_improve_ticks, c.inv_fifo, c.paper_lifetime), (0, 0, 0))
            self.assertIn("economics_fees", c.exclude_categories)
            os.environ["LP_PHASE"] = "2"
            os.environ["LP_QUOTE_BUDGET_USD"] = "250"
            self.assertEqual(lp.from_env().quote_budget_usd, 250.0)
            os.environ["LP_PHASE"] = "1"
            c1 = lp.from_env()
            self.assertEqual((c1.phase, c1.max_min_size, c1.kmax), (1, 20.0, 1))
            self.assertEqual(lp.from_env(phase=2).phase, 2)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
            os.environ.pop("LP_QUOTE_BUDGET_USD", None)
            os.environ.pop("LP_PHASE", None)

    def test_phase2_config(self):
        c = lp.phase2_config(kmax=3)
        self.assertEqual((c.phase, c.kmax, c.max_min_size), (2, 3, 200.0))

    def test_phase3_is_the_default(self):
        old = {k: os.environ.pop(k, None) for k in ("LP_PHASE", "LP_CAPITAL_USD")}
        try:
            c = lp.from_env()
            self.assertEqual(c.phase, 3)
            # the phase 2 rules stay (rolling selection, exclusions, kmax, the 1 h position rule)
            self.assertEqual((c.kmax, c.max_min_size, c.inv_flatten_h, c.rebalance_h), (5, 200.0, 1.0, 3.0))
            self.assertIn("economics_fees", c.exclude_categories)
            # phase 3: tick-better reducing quotes, FIFO lots, lifetime paper fills, live capital $300
            self.assertEqual((c.exit_improve_ticks, c.reduce_improve_always, c.inv_fifo, c.paper_lifetime), (1, 1, 1, 1))
            self.assertEqual((c.capital_usd, c.quote_budget_usd, c.stop_loss_usd), (300.0, 200.0, 60.0))
            os.environ["LP_CAPITAL_USD"] = "320"
            self.assertEqual(lp.from_env().capital_usd, 320.0)
            self.assertEqual(lp.phase3_config(kmax=2).phase, 3)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


class FakeEx(lp.PaperExec):
    """PaperExec without the public tape: fills are injected by the test."""

    def __init__(self):
        super().__init__(trades_fn=lambda c: [])
        self.inject = []
        self.merged = []

    async def fills(self, markets):
        out, self.inject = self.inject, []
        return out

    async def merge(self, m, pairs):
        self.merged.append((m["cond"], pairs))
        return pairs


def journal_events(j):
    j.f.flush()
    with open(j.f.name) as f:
        return [json.loads(x) for x in f]


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = Clock()
        self.metas = {"A": meta("A"), "B": meta("B", fee_type="economics_fees"), "C": meta("C", end_h=0.5),
                      "D": meta("D", event="EA")}
        self.rw = {c: {"rate": 10.0, "v": 4.5, "min": 20.0} for c in self.metas}
        self.rw["E"] = {"rate": 10.0, "v": 4.5, "min": 500.0}           # rewards_min_size above 200
        self.books = {f"Y{c}": ([(0.48, 100.0), (0.47, 100.0)], [(0.52, 100.0), (0.53, 100.0)], 0.01) for c in self.metas}
        self.books["YD"] = ([(0.47, 100.0), (0.46, 100.0)], [(0.53, 100.0), (0.54, 100.0)], 0.01)
        self.gamma_calls = 0

    def gamma(self, conds):
        self.gamma_calls += 1
        return {c: {"conditionId": c, "question": self.metas[c]["question"], "events": [{"id": self.metas[c]["event"]}],
                    "clobTokenIds": json.dumps([f"Y{c}", f"N{c}"]), "feesEnabled": True, "feeType": self.metas[c]["fee_type"],
                    "feeSchedule": {"rate": 0.05, "rebateRate": 0.25}, "createdAt": self.metas[c]["created"],
                    "endDate": self.metas[c]["end"], "active": True, "closed": False, "acceptingOrders": True}
                for c in conds if c in self.metas}

    def store(self, **kw):
        cfg = lp.phase2_config(lp_dir=self.tmp, lookback_h=2.0, **kw)
        return lp.SnapStore(cfg, log=lambda *a: None, clock=self.clock, rewards_fn=lambda: self.rw,
                            books_fn=lambda toks: {t: self.books[t] for t in toks if t in self.books}, gamma_fn=self.gamma)

    def test_take_filters_and_persists(self):
        st = self.store()
        sm = st.take(NOW)
        self.assertEqual(sm["n_rewarded"], 4)              # E: min size above 200
        self.assertEqual(sm["n_eligible"], 3)              # B: economics excluded
        self.assertEqual(set(st.entries[-1]["rows"]), {"A", "C", "D"})
        self.assertEqual(len(st.entries[-1]["rows"]["A"][0]), 5)
        files = os.listdir(os.path.join(self.tmp, "snaps"))
        self.assertTrue(any(f.startswith("books_") for f in files))
        with gzip.open(os.path.join(self.tmp, "snaps", [f for f in files if f.startswith("books_")][0]), "rt") as f:
            self.assertIn("A", json.load(f)["books"])
        again = self.store()                               # a restart reads the same snapshots from disk
        self.assertEqual(len(again.entries), 1)
        self.assertEqual(again.meta["A"]["no"], "NA")
        st2 = lp.SnapStore(st.cfg, log=lambda *a: None, clock=self.clock, rewards_fn=lambda: self.rw,
                           books_fn=lambda toks: {}, gamma_fn=self.gamma)
        self.assertEqual(st2.meta_ts, NOW)

    def test_meta_refresh(self):
        st = self.store()
        st.take(NOW)
        st.take(NOW + 1800)
        self.assertEqual(self.gamma_calls, 1)
        st.take(NOW + 6 * 3600 + 1)
        self.assertEqual(self.gamma_calls, 2)

    def test_warm_and_due_and_prune(self):
        st = self.store()
        self.assertTrue(st.due(NOW))
        self.assertFalse(st.warm(NOW))
        for i in range(3):
            st.take(NOW + i * 1800)
        self.assertFalse(st.due(NOW + 2 * 1800 + 60))
        self.assertTrue(st.due(NOW + 3 * 1800))
        self.assertFalse(st.warm(NOW + 2 * 1800))          # 1 h of a 2 h lookback
        st.take(NOW + 3 * 1800)
        self.assertTrue(st.warm(NOW + 3 * 1800))           # 4 snapshots over 1.5 h: the lookback less one gap
        self.assertEqual(json.load(open(os.path.join(self.tmp, "snaps", "state.json")))["warm"], True)
        st.prune(NOW + 32 * 3600)
        self.assertEqual(st.entries, [])
        self.assertFalse(any(f.startswith("books_") for f in os.listdir(os.path.join(self.tmp, "snaps"))))

    def test_warm_needs_most_snapshots(self):
        st = self.store()
        st.take(NOW)
        st.take(NOW + 3 * 1800)                            # an outage in between: 2 of 4 snapshots
        self.assertFalse(st.warm(NOW + 3 * 1800))

    def test_rank_market_dicts(self):
        st = self.store()
        for i in range(5):
            st.take(NOW + i * 1800)
        target, summary = st.rank(NOW + 4 * 1800)
        conds = [m["cond"] for m in target]
        self.assertNotIn("C", conds)                       # less than min_hours_left (1 h) to its end
        self.assertNotIn("B", conds)
        self.assertEqual(len([c for c in conds if c in ("A", "D")]), 1)   # A and D share event EA
        m = target[0]
        self.assertEqual(m["N"], m["k"] * m["min"])
        self.assertEqual((m["yes"], m["no"]), (f"Y{m['cond']}", f"N{m['cond']}"))
        for key in ("end", "fee_rate", "rebate_rate", "quote_cap", "m1_usd_day", "tick", "v", "rate"):
            self.assertIn(key, m)
        self.assertEqual(summary["markets"], len(target))
        self.assertEqual(summary["snapshots"], 5)


class Phase2LoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = Clock()
        self.cfg = lp.phase2_config(lp_dir=self.tmp, poll_s=0.0, sample_s=0.0)
        self.books = {"YC1": ([(0.48, 100.0)], [(0.52, 100.0)], 0.01), "YC2": ([(0.38, 100.0)], [(0.42, 100.0)], 0.01),
                      "YC3": ([(0.58, 100.0)], [(0.62, 100.0)], 0.01)}
        self.ex = FakeEx()
        self.j = lp.Journal(os.path.join(self.tmp, "paper.jsonl"))

    def make(self, markets=()):
        return lp.LPTest(self.cfg, self.ex, self.j, list(markets), books_fn=lambda toks: {t: self.books[t] for t in toks},
                         log=lambda *a: None, clock=self.clock)

    def cfgs(self, t, rate=10.0):
        return {c: {"v": 4.5, "min": s.m["min"], "rate": rate} for c, s in t.st.items()}

    def step(self, t, dt=0.0, cfgs=None):
        self.clock.t += dt
        asyncio.run(t.step(self.clock.t, cfgs if cfgs is not None else self.cfgs(t)))

    def test_rebalance_adds_resizes_retires(self):
        t = self.make([mk("C1"), mk("C2")])
        self.step(t)
        self.assertEqual(t.st["C1"].orders["bid"][3], 20.0)
        t.apply_rebalance([mk("C1", N=60.0, k=3), mk("C3")], {"m1_pred": 3.0}, self.clock.t)
        self.step(t, 5)
        self.assertEqual(t.st["C1"].orders["bid"][3], 60.0)          # re-quoted at the new size
        self.assertIn("C3", t.st)
        self.assertIsNotNone(t.st["C3"].orders["ask"])
        ev = [e for e in journal_events(self.j) if e["event"] == "rebalance"][-1]
        self.assertEqual((ev["added"], ev["resized"], ev["retired"]), (["C3"], ["C1"], ["C2"]))
        self.assertNotIn("C2", t.st)                       # C2 had no position: dropped at once
        self.assertIn("retired", [e["event"] for e in journal_events(self.j)])
        self.assertEqual(t.target, {"C1", "C3"})

    def test_exit_quotes_reduce_only(self):
        t = self.make([mk("C1")])
        self.step(t)
        oid = t.st["C1"].orders["bid"][0]
        self.ex.inject = [{"order": oid, "mkt": "C1", "side": "bid", "price": 0.48, "size": 20.0, "ts": self.clock.t, "done": True}]
        self.step(t, 1)
        self.assertEqual(t.st["C1"].pos(), 20.0)
        t.apply_rebalance([], {}, self.clock.t)
        self.step(t, 1)
        s = t.st["C1"]
        self.assertIsNone(s.orders["bid"])                 # long YES: only the ask (BUY NO) side, for the position
        self.assertEqual(s.orders["ask"][1], 0.52)
        self.assertEqual(s.orders["ask"][3], 20.0)
        self.ex.inject = [{"order": s.orders["ask"][0], "mkt": "C1", "side": "ask", "price": 0.52, "size": 15.0,
                           "ts": self.clock.t, "done": False}]
        self.step(t, 1)
        self.assertEqual(s.pos(), 5.0)
        self.step(t, 1)
        self.assertEqual(s.orders["ask"][3], 5.0)          # re-quoted for what is left
        self.assertEqual(self.ex.merged[-1], ("C1", 15.0))  # pairs merged while exiting, below N

    def test_flatten_after_exit_window(self):
        t = self.make([mk("C1")])
        self.step(t)
        oid = t.st["C1"].orders["bid"][0]
        self.ex.inject = [{"order": oid, "mkt": "C1", "side": "bid", "price": 0.48, "size": 20.0, "ts": self.clock.t, "done": True}]
        self.step(t, 1)
        t.apply_rebalance([], {}, self.clock.t)
        self.step(t, 1800)
        self.assertEqual(t.st["C1"].pos(), 20.0)           # within the hour: still quoting
        self.step(t, 1801 + 600)                           # past exit_window_h and the 600 s markout
        fl = [e for e in journal_events(self.j) if e["event"] == "flatten"]
        self.assertEqual(len(fl), 1)
        self.assertEqual((fl[0]["side"], fl[0]["price"], fl[0]["size"]), ("ask", 0.48, 20.0))
        fee = 20.0 * 0.05 * 0.52 * 0.48
        self.assertAlmostEqual(fl[0]["fee"], fee)
        self.assertAlmostEqual(fl[0]["cost"], -(20.0 * 0.02 + fee))   # sold 2 c under the mid, plus the taker fee
        self.assertNotIn("C1", t.st)                       # flat and merged: dropped
        self.assertAlmostEqual(t.pnl(), 20.0 * (0.48 - 0.48) - fee, places=9)

    def test_end_near_reduces_then_flattens(self):
        self.cfg.exit_at_end, self.cfg.inv_reduce_h, self.cfg.inv_flatten_h = 1, 0.0, 0.0
        t = self.make([mk("C1", end_h=2.0)])
        self.step(t)
        oid = t.st["C1"].orders["ask"][0]
        self.ex.inject = [{"order": oid, "mkt": "C1", "side": "ask", "price": 0.52, "size": 20.0, "ts": self.clock.t, "done": True}]
        self.step(t, 1)
        self.assertIsNone(t.st["C1"].exit_since)
        self.step(t, 1.5 * 3600)                           # 0.5 h left: exit, reduce only (long NO: bid side)
        s = t.st["C1"]
        self.assertIsNotNone(s.exit_since)
        self.assertIsNone(s.orders["ask"])
        self.assertEqual(s.orders["bid"][3], 20.0)
        self.assertEqual(next(e for e in journal_events(self.j) if e["event"] == "exit")["reason"], "end near")
        self.step(t, 0.3 * 3600)                           # 0.2 h left < exit_flatten_h: taker
        self.assertEqual([e["side"] for e in journal_events(self.j) if e["event"] == "flatten"], ["bid"])

    def test_no_end_exit_by_default(self):
        t = self.make([mk("C1", end_h=0.5)])
        self.step(t)
        self.assertIsNone(t.st["C1"].exit_since)           # weather trades its measured day after endDate
        self.assertIsNotNone(t.st["C1"].orders["bid"])

    def test_position_held_reduce_then_flatten(self):
        t = self.make([mk("C1")])
        self.step(t)
        oid = t.st["C1"].orders["bid"][0]
        self.ex.inject = [{"order": oid, "mkt": "C1", "side": "bid", "price": 0.48, "size": 20.0, "ts": self.clock.t, "done": True}]
        self.step(t, 1)
        s = t.st["C1"]
        self.step(t, 1)                                    # the next pass sees the position
        self.assertIsNotNone(s.pos_since)
        self.step(t, 600)
        self.assertIsNotNone(s.orders["bid"])              # 10 min: still two-sided
        self.step(t, 1200)
        self.assertIsNone(s.orders["bid"])                 # 30 min: only the reducing side
        self.assertEqual(s.orders["ask"][3], 20.0)
        self.assertIsNone(s.exit_since)                    # still in the portfolio
        self.step(t, 1800)
        fl = [e for e in journal_events(self.j) if e["event"] == "flatten"]
        self.assertEqual([(e["side"], e["reason"]) for e in fl], [("ask", "position held 1 h")])
        self.assertAlmostEqual(s.pos(), 0.0)
        self.assertIsNone(s.pos_since)
        self.step(t, 5)
        self.assertIsNotNone(s.orders["bid"])              # flat again: two-sided quotes come back
        self.assertIn("C1", t.st)

    def test_failed_taker_close_waits_and_is_reported(self):
        """A taker close that does not fill is an error in the journal and is not retried for flatten_retry_s, so the
        reducing quote can rest in between (live, 25 Nov: every close was refused and the quotes were cancelled every
        step)."""
        t = self.make([mk("C1")])
        self.step(t)
        oid = t.st["C1"].orders["bid"][0]
        self.ex.inject = [{"order": oid, "mkt": "C1", "side": "bid", "price": 0.48, "size": 20.0, "ts": self.clock.t, "done": True}]
        self.step(t, 1)
        s = t.st["C1"]
        self.step(t, 1)
        calls = []

        async def refuse(m, side, size, bids, asks, **kw):
            calls.append((side, size))
            return None
        self.ex.take = refuse
        self.step(t, 3600)
        self.assertEqual(calls, [("ask", 20.0)])
        self.assertEqual(t.errors, 1)
        err = [e for e in journal_events(self.j) if e["event"] == "error"]
        self.assertIn("taker close did not fill (position held 1 h)", err[0]["error"])
        self.step(t, 5)
        self.assertEqual(len(calls), 1)                    # paused
        self.assertIsNotNone(s.orders["ask"])              # the reducing quote rests meanwhile
        self.step(t, 60)
        self.assertEqual(len(calls), 2)                    # tried again after the pause

    def test_mid_out_of_range_starts_exit(self):
        t = self.make([mk("C1")])
        self.step(t)
        self.books["YC1"] = ([(0.93, 100.0)], [(0.95, 100.0)], 0.01)
        self.step(t, 5)
        e = next(e for e in journal_events(self.j) if e["event"] == "exit")
        self.assertEqual(e["reason"], "mid out of range")

    def test_reward_removed_starts_exit(self):
        t = self.make([mk("C1")])
        self.step(t)
        self.step(t, 1, cfgs={"C1": None})
        self.assertEqual(next(e for e in journal_events(self.j) if e["event"] == "exit")["reason"], "reward removed")
        self.assertNotIn("C1", t.st)                       # nothing held: dropped

    def test_back_in_portfolio(self):
        t = self.make([mk("C1")])
        self.step(t)
        oid = t.st["C1"].orders["bid"][0]
        self.ex.inject = [{"order": oid, "mkt": "C1", "side": "bid", "price": 0.48, "size": 20.0, "ts": self.clock.t, "done": True}]
        self.step(t, 1)
        t.apply_rebalance([], {}, self.clock.t)
        self.step(t, 1)
        t.apply_rebalance([mk("C1")], {}, self.clock.t)
        self.step(t, 1)
        self.assertIsNone(t.st["C1"].exit_since)
        self.assertIsNotNone(t.st["C1"].orders["ask"])
        self.assertEqual(next(e for e in journal_events(self.j)[::-1] if e["event"] == "rebalance")["back"], ["C1"])

    def test_paper_rebate_on_fill(self):
        t = self.make([mk("C1")])
        self.step(t)
        oid = t.st["C1"].orders["ask"][0]
        self.ex.inject = [{"order": oid, "mkt": "C1", "side": "ask", "price": 0.52, "size": 10.0, "ts": self.clock.t, "done": False}]
        self.step(t, 1)
        f = next(e for e in journal_events(self.j) if e["event"] == "fill")
        self.assertAlmostEqual(f["rebate"], round(10.0 * 0.05 * 0.48 * 0.52 * 0.25, 6))

    def test_position_limit_in_dollars(self):
        self.cfg.pos_limit_usd = 15.0
        t = self.make([mk("C1")])
        self.step(t)
        oid = t.st["C1"].orders["bid"][0]
        self.ex.inject = [{"order": oid, "mkt": "C1", "side": "bid", "price": 0.48, "size": 20.0, "ts": self.clock.t, "done": True}]
        self.step(t, 1)
        self.assertIsNone(t.st["C1"].orders["bid"])        # 20 YES at 0.5 = $10 ... cap 15 / 0.52 = 28.8 shares: one more
        self.assertIsNotNone(t.st["C1"].orders["ask"])     # bid of 20 would reach 40 > 28.8: withdrawn by targets()

    def test_window_fields(self):
        t = self.make([mk("C1", quote_cap=21.0)])
        t.target = {"C1"}
        self.step(t)
        self.step(t, 1800)
        w = next(e for e in journal_events(self.j) if e["event"] == "window")
        self.assertAlmostEqual(w["cap_target"], 21.0)
        self.assertEqual((w["target"], w["exiting"]), (1, 0))
        self.assertIn("rebate", w["markets"]["C1"])

    def test_restore_phase2(self):
        t = self.make([])
        t.apply_rebalance([mk("C1"), mk("C2")], {"m1_pred": 2.0}, self.clock.t)
        self.step(t)
        oid = t.st["C1"].orders["bid"][0]
        self.ex.inject = [{"order": oid, "mkt": "C1", "side": "bid", "price": 0.48, "size": 20.0, "ts": self.clock.t, "done": True}]
        self.step(t, 1)
        t.apply_rebalance([mk("C2")], {"m1_pred": 1.0}, self.clock.t)
        self.step(t, 1)
        pnl = t.pnl()
        t2 = lp.LPTest(self.cfg, FakeEx(), lp.Journal(os.path.join(self.tmp, "x.jsonl")), [], books_fn=lambda toks: {},
                       log=lambda *a: None, clock=self.clock)
        self.assertEqual(t2.restore(journal_events(self.j)), 1)
        self.assertEqual(set(t2.st), {"C1", "C2"})
        self.assertEqual(t2.target, {"C2"})
        self.assertIsNotNone(t2.st["C1"].exit_since)
        self.assertEqual(t2.st["C1"].pos(), 20.0)
        self.assertAlmostEqual(t2.st["C1"].cash, t.st["C1"].cash)
        self.assertEqual(t2.last_rebalance, t.last_rebalance)
        t2.st["C1"].mid = t.st["C1"].mid
        t2.st["C2"].mid = t.st["C2"].mid
        self.assertAlmostEqual(t2.pnl(), pnl)

    def test_restore_retired_keeps_pnl(self):
        evs = [{"event": "rebalance", "ts": NOW, "target": ["C1"], "markets": {"C1": mk("C1")}, "exiting": {}},
               {"event": "fill", "ts": NOW, "mkt": "C1", "side": "bid", "price": 0.4, "size": 10.0},
               {"event": "retired", "ts": NOW + 9, "mkt": "C1", "pnl": 1.5, "yes": 0.0, "no": 0.0}]
        t = self.make([])
        t.restore(evs)
        self.assertNotIn("C1", t.st)
        self.assertAlmostEqual(t.pnl(), 1.5)

    def test_paper_take(self):
        ex = lp.PaperExec(trades_fn=lambda c: [])
        m = mk("C1")
        r = asyncio.run(ex.take(m, "ask", 30.0, [(0.48, 20.0), (0.47, 50.0)], [(0.52, 10.0)]))
        self.assertAlmostEqual(r["size"], 30.0)
        self.assertAlmostEqual(r["price"], (20 * 0.48 + 10 * 0.47) / 30)
        p = 1 - r["price"]
        self.assertAlmostEqual(r["fee"], 30.0 * 0.05 * p * (1 - p))
        r = asyncio.run(ex.take(m, "bid", 30.0, [(0.48, 20.0)], [(0.52, 10.0)]))
        self.assertAlmostEqual(r["size"], 10.0)            # book thinner than the order: partial
        self.assertIsNone(asyncio.run(ex.take(m, "bid", 5.0, [(0.48, 20.0)], [])))

    def test_paused_does_not_flatten(self):
        t = self.make([mk("C1")])
        self.step(t)
        oid = t.st["C1"].orders["bid"][0]
        self.ex.inject = [{"order": oid, "mkt": "C1", "side": "bid", "price": 0.48, "size": 20.0, "ts": self.clock.t, "done": True}]
        self.step(t, 1)
        t.apply_rebalance([], {}, self.clock.t)
        t.paused = True
        self.step(t, 2 * 3600)
        self.assertNotIn("flatten", [e["event"] for e in journal_events(self.j)])
        self.assertIsNone(t.st["C1"].orders["ask"])


class SnapshotTickTest(unittest.TestCase):
    def test_snapshot_then_rebalance_schedule(self):
        tmp = tempfile.mkdtemp()
        clock = Clock()
        cfg = lp.phase2_config(lp_dir=tmp, lookback_h=1.0)
        calls = []

        class Store:
            def __init__(self):
                self.last = None
            def due(self, now):
                return self.last is None or now - self.last >= 1795
            def take(self, now):
                self.last = now
                calls.append(("take", now))
                return {"ts": now, "n_rewarded": 1, "n_eligible": 1, "n_quotable": 1}
            def warm(self, now):
                return now >= NOW + 1800
            def rank(self, now):
                calls.append(("rank", now))
                return [mk("C1")], {"m1_pred": 1.0}

        j = lp.Journal(os.path.join(tmp, "paper.jsonl"))
        t = lp.LPTest(cfg, FakeEx(), j, [], books_fn=lambda toks: {}, log=lambda *a: None, clock=clock, store=Store())
        asyncio.run(t.snapshot_tick(NOW))
        self.assertEqual(calls, [("take", NOW)])           # not warm yet: no rank
        clock.t = NOW + 1800
        asyncio.run(t.snapshot_tick(clock.t))
        self.assertEqual(calls[-1], ("rank", NOW + 1800))
        self.assertIsNotNone(t.pending_target)
        asyncio.run(t.step(clock.t, {}))                   # the target is applied by the loop
        self.assertIn("C1", t.st)
        self.assertIsNone(t.pending_target)
        clock.t = NOW + 3600
        asyncio.run(t.snapshot_tick(clock.t))
        self.assertEqual(calls[-1], ("take", NOW + 3600))  # 30 min later: a snapshot, no new rank
        clock.t = NOW + 1800 + 3 * 3600
        asyncio.run(t.snapshot_tick(clock.t))
        self.assertEqual(calls[-1], ("rank", clock.t))     # 3 h after the last rebalance
        self.assertIn("snapshot", [e["event"] for e in journal_events(j)])


class Phase2ReportTest(unittest.TestCase):
    def events(self, m1=10.0, cap=100.0, hours=48, rebate=0.5, pnl_end=-20.0, pred=10.0):
        ev = [{"event": "start", "ts": NOW},
              {"event": "rebalance", "ts": NOW, "target": ["A", "B", "C"], "added": ["A", "B", "C"], "retired": [],
               "summary": {"m1_pred": pred}, "markets": {}, "quote_cap": cap},
              {"event": "pnl", "ts": NOW, "pnl": 0.0}]
        for i in range(hours * 2):
            s = NOW + i * 1800
            ms = {c: {"m1_usd_day": m1 / 3, "fills": 1, "contracts": 10.0, "markout60": [[-0.5, 10.0]], "rebate": rebate / 3,
                      "samples": 30} for c in "ABC"}
            ev.append({"event": "window", "ts": s + 1800, "start": s, "end": s + 1800, "m1_usd_day": m1, "markets": ms,
                       "cap_target": cap})
        ev.append({"event": "pnl", "ts": NOW + hours * 3600, "pnl": pnl_end})
        return ev

    def test_net_per_100(self):
        r = lp.phase2(self.events())
        self.assertAlmostEqual(r["m1_per100"], 10.0)
        self.assertAlmostEqual(r["rebates_usd"], 0.5 * 96)
        self.assertAlmostEqual(r["rebates_per100"], 0.5 * 96 / 2)
        self.assertAlmostEqual(r["mtm_per100"], -10.0)
        self.assertAlmostEqual(r["net_per100"], 10.0 + 24.0 - 10.0)
        self.assertAlmostEqual(r["fact_over_pred"], 1.0)
        self.assertAlmostEqual(r["cv"], 0.0)
        self.assertAlmostEqual(r["max_share_last24"], 1 / 3)
        self.assertAlmostEqual(r["markout60_c"], -0.5)
        self.assertEqual(r["verdict"], "PASS")

    def test_fail_reasons(self):
        r = lp.phase2(self.events(m1=5.0, rebate=0.0, pnl_end=0.0, pred=20.0))
        self.assertIn("net >= $20", r["verdict"])
        self.assertIn("fact / prediction", r["verdict"])
        r = lp.phase2(self.events(hours=20))
        self.assertIn("net >= $20", r["verdict"])          # less than 48 h: not complete

    def test_no_data(self):
        self.assertEqual(lp.phase2([])["verdict"], "no data")
        self.assertEqual(lp.phase2([{"event": "start", "ts": NOW}])["verdict"], "no data")


class Phase3ExitTest(unittest.TestCase):
    """Phase 3 exit logic (research/PHASE3_PLAN.md): tick-better reducing quotes, FIFO position age."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = Clock()
        self.cfg = lp.phase2_config(lp_dir=self.tmp, poll_s=0.0, sample_s=0.0)
        self.books = {"YC1": ([(0.47, 100.0)], [(0.52, 100.0)], 0.01)}
        self.ex = FakeEx()
        self.j = lp.Journal(os.path.join(self.tmp, "paper.jsonl"))

    def make(self, markets):
        return lp.LPTest(self.cfg, self.ex, self.j, list(markets), books_fn=lambda toks: {t: self.books[t] for t in toks},
                         log=lambda *a: None, clock=self.clock)

    def step(self, t, dt=0.0):
        self.clock.t += dt
        asyncio.run(t.step(self.clock.t, {c: {"v": 4.5, "min": s.m["min"], "rate": 10.0} for c, s in t.st.items()}))

    def fill(self, t, side, size, price=None):
        oid = t.st["C1"].orders[side][0]
        price = price if price is not None else t.st["C1"].orders[side][1]
        self.ex.inject = [{"order": oid, "mkt": "C1", "side": side, "price": price, "size": size, "ts": self.clock.t, "done": True}]
        self.step(t, 1)

    def test_lot_update_fifo(self):
        s = lp.MarketState(mk("C1"))
        s.lot_update(0, 20); s.lot_update(10, 10); s.lot_update(20, -25)
        self.assertEqual(s.lots, [[10, 5]])                 # the oldest lot is consumed first
        s.lot_update(30, -15)
        self.assertEqual(s.lots, [[30, -10]])               # over-reduction opens a lot the other way
        self.assertEqual(s.aged_qty(40, 5), 10)
        self.assertEqual(s.aged_qty(32, 5), 0)

    def test_reduce_price_one_tick_inside(self):
        self.cfg.exit_improve_ticks = 1
        t = self.make([mk("C1")])
        self.assertEqual(t._reduce_price("ask", [(0.47, 1)], [(0.52, 1)], 0.01), 0.51)
        self.assertEqual(t._reduce_price("bid", [(0.47, 1)], [(0.52, 1)], 0.01), 0.48)
        self.assertEqual(t._reduce_price("ask", [(0.50, 1)], [(0.51, 1)], 0.01), 0.51)   # one-tick spread: join
        self.cfg.exit_improve_ticks = 3
        self.assertEqual(t._reduce_price("ask", [(0.49, 1)], [(0.52, 1)], 0.01), 0.50)   # never at the bid
        self.cfg.exit_improve_ticks = 0
        self.assertEqual(t._reduce_price("ask", [(0.47, 1)], [(0.52, 1)], 0.01), 0.52)

    def test_aged_exit_quote_improved(self):
        self.cfg.exit_improve_ticks = 1
        t = self.make([mk("C1")])
        self.step(t)
        self.fill(t, "bid", 20.0)
        self.step(t, 1)
        self.step(t, 1800)                                 # 30 min: reduce only, one tick inside
        s = t.st["C1"]
        self.assertIsNone(s.orders["bid"])
        self.assertEqual(s.orders["ask"][1], 0.51)

    def test_reduce_improve_always(self):
        self.cfg.exit_improve_ticks, self.cfg.reduce_improve_always = 1, 1
        t = self.make([mk("C1")])
        self.step(t)
        self.fill(t, "bid", 20.0)
        self.step(t, 1)
        s = t.st["C1"]
        self.assertEqual(s.orders["ask"][1], 0.51)          # reducing side improved at once
        self.assertEqual(s.orders["bid"][1], 0.47)          # the other side still joins
        self.books["YC1"] = ([(0.40, 100.0)], [(0.52, 100.0)], 0.01)   # outside the zone: only the reducing side
        self.step(t, 1)
        self.assertIsNone(s.orders["bid"])
        self.assertEqual(s.orders["ask"][1], 0.51)

    def test_fifo_flattens_only_old_lots(self):
        self.cfg.inv_fifo = 1
        t = self.make([mk("C1")])
        self.step(t)
        self.fill(t, "bid", 20.0)                          # lot 1 at t0
        self.step(t, 1)
        self.clock.t += 1800
        self.books["YC1"] = ([(0.47, 100.0)], [(0.52, 100.0)], 0.01)
        s = t.st["C1"]
        s.lot_update(self.clock.t, 30); s.yes += 30          # a younger lot of 30 at t0 + 30 min
        self.step(t, 1801)                                 # lot 1 is 1 h old, the young lot 30 min
        fl = [e for e in journal_events(self.j) if e["event"] == "flatten"]
        self.assertEqual(len(fl), 1)
        self.assertAlmostEqual(fl[0]["size"], 20.0)        # only the old lot
        self.assertAlmostEqual(s.pos(), 30.0)

    def churn(self, fifo):
        self.cfg.inv_fifo = fifo
        self.j = lp.Journal(os.path.join(tempfile.mkdtemp(), "paper.jsonl"))
        self.clock = Clock()
        t = self.make([mk("C1")])
        self.step(t)
        for i in range(6):                                 # every lot is consumed within 15 min; |pos| stays >= dust
            side = "bid" if i % 2 == 0 else "ask"
            if t.st["C1"].orders[side]:                    # (the phase 2 rule withdraws the growing side after 30 min)
                self.fill(t, side, 20.0 if i == 0 else 25.0)
            self.step(t, 900)
        return [e for e in journal_events(self.j) if e["event"] == "flatten"]

    def test_fifo_churning_position_not_flattened(self):
        self.assertEqual(self.churn(1), [])
        self.assertTrue(self.churn(0))                     # the phase 2 rule closes the same churning position


if __name__ == "__main__":
    unittest.main()


def _filler_titles(n=600):
    """Distinct weather-like event titles: the common words (temperature, november) are not rare, the town is."""
    out = {}
    for i in range(n):
        town = "".join(chr(97 + (i // 26 ** j) % 26) for j in range(3)) + "ville"
        out[f"EF{i}"] = f"Highest temperature in {town} on November 22?"
    return out


class Phase3SelectionTest(unittest.TestCase):
    """Phase 3 selection filters (research/PHASE3_PLAN.md): related events, one-sided flow, flow-sized k."""

    def test_related_groups(self):
        titles = dict(_filler_titles(), R="Which math problems will AI solve in 2026?",
                      M="How many more Millennium Prize Problems will AI solve in 2026?",
                      B1="Highest temperature in Busan on November 22?", B2="Highest temperature in Busan on November 23?")
        g = lp.related_groups(titles)
        self.assertEqual(g["R"], g["M"])                   # problems + solve
        self.assertNotEqual(g["B1"], g["B2"])              # one rare word (busan) only
        self.assertEqual(len(set(g.values())), len(titles) - 1)

    def test_flow_stats(self):
        tr = [(100, 0, "B", 0.5, 30.0, "a"),               # buys YES (capped at n = 20)
              (400, 0, "S", 0.5, 10.0, "b"),               # sells YES
              (5000, 1, "B", 0.5, 5.0, "c"),               # buys NO = sells YES; less than 60 min before t1
              (7000, 0, "B", 0.5, 5.0, "d"),
              (9000, 0, "B", 0.5, 5.0, "e")]               # after t1
        f = lp.flow_stats(tr, 0, 7200, 20)
        self.assertEqual(f["trades"], 4)
        self.assertAlmostEqual(f["buy_yes_share"], 25 / 40)
        self.assertAlmostEqual(f["opp60"], 20 / 30)        # the buy at 100 s meets the sell at 400 s; the sell at 400 s
        self.assertAlmostEqual(f["side_per_30m"], 15 / 4)  # waits 6600 s; trades of the last hour do not count
        self.assertEqual(lp.flow_stats([], 0, 7200, 20)["trades"], 0)

    def test_flow_keep(self):
        cfg = lp.phase2_config()
        f = {"trades": 50, "buy_yes_share": 0.5, "opp60": 0.9, "side_per_30m": 45.0}
        self.assertEqual(lp.flow_keep(f, 20, cfg), 2)      # N = k x 20 <= 45 contracts per 30 min
        self.assertEqual(lp.flow_keep(dict(f, side_per_30m=1000.0), 20, cfg), cfg.kmax)
        self.assertEqual(lp.flow_keep(dict(f, side_per_30m=10.0), 20, cfg), 0)
        self.assertEqual(lp.flow_keep(dict(f, buy_yes_share=0.85), 20, cfg), 0)
        self.assertEqual(lp.flow_keep(dict(f, buy_yes_share=0.15), 20, cfg), 0)
        self.assertEqual(lp.flow_keep(dict(f, opp60=0.7), 20, cfg), 0)
        self.assertEqual(lp.flow_keep({"trades": 0}, 20, cfg), 0)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = Clock()
        self.metas = {c: meta(c) for c in ("X", "Y", "Z")}
        self.rw = {c: {"rate": 10.0, "v": 4.5, "min": 20.0} for c in self.metas}
        self.books = {f"Y{c}": ([(0.48, 100.0), (0.47, 100.0)], [(0.52, 100.0), (0.53, 100.0)], 0.01) for c in self.metas}
        self.fetched = []

    def gamma(self, conds):
        return {c: {"conditionId": c, "question": self.metas[c]["question"],
                    "events": [{"id": self.metas[c]["event"], "title": self.metas[c].get("title")}],
                    "clobTokenIds": json.dumps([f"Y{c}", f"N{c}"]), "feesEnabled": True, "feeType": "weather_fees",
                    "feeSchedule": {"rate": 0.05, "rebateRate": 0.25}, "createdAt": self.metas[c]["created"],
                    "endDate": self.metas[c]["end"], "active": True, "closed": False, "acceptingOrders": True}
                for c in conds if c in self.metas}

    def trades(self, c):
        self.fetched.append(c)
        now = NOW + 4 * 1800
        out = []
        for i in range(120):                               # one trade a minute over the 2 h lookback
            t = now - 7200 + 30 + i * 60
            buy = True if c == "Y" else i % 2 == 0         # Y: every taker buys YES
            out.append((t, 0, "B" if buy else "S", 0.5, 20.0 if c == "X" else 2.7, f"{c}{i}"))
        return out

    def store(self, **kw):
        cfg = lp.phase2_config(lp_dir=self.tmp, lookback_h=2.0, max_share=1.0, **kw)
        return lp.SnapStore(cfg, log=lambda *a: None, clock=self.clock, rewards_fn=lambda: self.rw,
                            books_fn=lambda toks: {t: self.books[t] for t in toks if t in self.books}, gamma_fn=self.gamma,
                            trades_fn=self.trades)

    def ranked(self, **kw):
        st = self.store(**kw)
        for i in range(5):
            st.take(NOW + i * 1800)
        return st, st.rank(NOW + 4 * 1800)

    def test_flow_filter_in_rank(self):
        st, (target, summary) = self.ranked(flow_filter=1)
        k = {m["cond"]: m["k"] for m in target}
        self.assertNotIn("Y", k)                           # one-sided
        self.assertEqual(k["Z"], 2)                        # 60 sells x 2.7 over 2 h = 40.5 a side per 30 min
        self.assertEqual(k["X"], 5)                        # dense flow: kmax
        self.assertEqual((summary["flow_checked"], summary["flow_dropped"]), (3, 1))
        self.assertEqual(st.flow["Y"]["k_max"], 0)

    def test_flow_filter_off_by_default(self):
        st, (target, summary) = self.ranked()
        self.assertEqual(self.fetched, [])
        self.assertIn("Y", {m["cond"] for m in target})
        self.assertNotIn("flow_checked", summary)

    def test_related_filter_in_rank(self):
        self.metas["X"]["title"] = "Which math problems will AI solve in 2026?"
        self.metas["Y"]["title"] = "How many more Millennium Prize Problems will AI solve in 2026?"
        st = self.store(related_filter=1)
        st.take(NOW)
        for e, t in _filler_titles().items():              # the rest of the rewarded events (word frequencies)
            st.meta[e] = {"event": e, "event_title": t}
        for i in range(1, 5):
            st.take(NOW + i * 1800)
        target, summary = st.rank(NOW + 4 * 1800)
        conds = {m["cond"] for m in target}
        self.assertEqual(len(conds & {"X", "Y"}), 1)
        self.assertIn("Z", conds)
        self.assertEqual(summary["groups_merged"], 1)
        self.assertEqual(st.meta["X"]["event_title"], "Which math problems will AI solve in 2026?")


class PaperLifetimeTest(unittest.TestCase):
    """Phase 3 paper fill model: an order cancelled before the tape is read still gets the trades of its lifetime."""

    def run_case(self, lifetime, poll_at=30.0):
        clock = Clock()
        tape = [(NOW + 5, 0, "S", 0.47, 10.0, "k1"),        # sells YES through our 0.48 bid while it stands
                (NOW + 15, 0, "S", 0.45, 10.0, "k2")]       # after the re-price, through neither bid
        ex = lp.PaperExec(trades_fn=lambda c: [r for r in tape if r[0] <= clock()], lifetime=lifetime, clock=clock)
        m = {"cond": "C1"}

        async def go():
            oid = await ex.place(m, "bid", 0.48, 20.0)
            clock.t = NOW + 10
            await ex.cancel(m, oid)                         # re-priced before the poll
            await ex.place(m, "bid", 0.44, 20.0)
            clock.t = NOW + poll_at
            return oid, await ex.fills([m])
        return asyncio.run(go())

    def test_phase2_misses_the_cancelled_order(self):
        _, f = self.run_case(0)
        self.assertEqual(f, [])

    def test_lifetime_fills_the_cancelled_order(self):
        oid, f = self.run_case(1)
        self.assertEqual([(x["order"], x["side"], x["price"], x["size"], x["ts"]) for x in f], [(oid, "bid", 0.48, 10.0, NOW + 5)])

    def test_cancelled_orders_expire(self):
        _, f = self.run_case(1, poll_at=10 + lp.PaperExec.GONE_KEEP_S + 1)
        self.assertEqual(f, [])

    def test_late_fill_of_a_cancelled_order_keeps_the_new_one(self):
        clock = Clock()
        cfg = lp.phase2_config(lp_dir=tempfile.mkdtemp())
        j = lp.Journal(os.path.join(cfg.lp_dir, "paper.jsonl"))
        t = lp.LPTest(cfg, lp.PaperExec(trades_fn=lambda c: []), j, [mk("C1")], books_fn=lambda toks: {}, log=lambda *a: None,
                      clock=clock)
        s = t.st["C1"]
        s.orders["bid"] = ("paper-2", 0.44, NOW, 20.0)
        t.apply_fill({"order": "paper-1", "mkt": "C1", "side": "bid", "price": 0.48, "size": 10.0, "ts": NOW, "done": True})
        self.assertEqual(s.yes, 10.0)
        self.assertEqual(s.orders["bid"][0], "paper-2")
        j.close()


class LiveAccountingTest(unittest.TestCase):
    """Live: wallet equity every 10 minutes, rewards of every market of the day, both in the phase 2/3 report."""

    def test_equity_and_earnings(self):
        clock = Clock()
        cfg = lp.phase3_config(lp_dir=tempfile.mkdtemp())
        j = lp.Journal(os.path.join(cfg.lp_dir, "live.jsonl"), clock=clock)

        class Ex(FakeEx):
            cash = 300.0

            async def collateral(self):
                return self.cash

            async def earnings(self, day):
                return {"C1": 1.5, "GONE": 2.0}            # GONE left the test during the day

        ex = Ex()
        t = lp.LPTest(cfg, ex, j, [mk("C1")], books_fn=lambda toks: {"YC1": ([(0.48, 100.0)], [(0.52, 100.0)], 0.01)},
                      log=lambda *a: None, clock=clock)
        clock.t = NOW - 300                                # just before midnight UTC of the next day? (any day change)
        day0 = time.strftime("%Y-%m-%d", time.gmtime(clock.t))

        async def go():
            await t.step(clock.t, {"C1": {"rate": 10.0, "v": 4.5, "min": 20.0}})
            ex.cash = 310.0
            clock.t += 86400                                 # the next day, 24 h later
            await t.step(clock.t, {"C1": {"rate": 10.0, "v": 4.5, "min": 20.0}})
        asyncio.run(go())
        ev = journal_events(j)
        eq = [e for e in ev if e["event"] == "equity"]
        self.assertEqual(len(eq), 2)
        self.assertAlmostEqual(eq[0]["collateral"], 300.0)
        self.assertAlmostEqual(eq[1]["equity"] - eq[0]["equity"], 10.0)
        earn = [e for e in ev if e["event"] == "earnings"]
        self.assertEqual(earn[0]["day"], day0)
        self.assertEqual(earn[0]["by_market"], {"C1": 1.5, "GONE": 2.0})
        j.close()


class LiveTakeTest(unittest.TestCase):
    """LiveExec.take: the closing order is a FAK market BUY sized in USDC (polymarket-client refuses `shares` for a
    BUY: every live close failed on 25 Nov), and the shares bought come from the order's taking amount."""

    def client(self, resp=None, fill=None, exc=None):
        class C:
            def __init__(self):
                self.bal = {"YC1": 0.0, "NC1": 0.0}
                self.sent = []

            async def get_balance_allowance(self, asset_type, token_id):
                return types.SimpleNamespace(balance=int(self.bal[token_id] * 1e6))

            async def place_market_order(self, **kw):
                self.sent.append(kw)
                if exc:
                    raise exc
                if fill:
                    self.bal[kw["token_id"]] += fill
                return resp
        return C()

    def take(self, c, side, size, bids, asks):
        log = []
        ex = lp.LiveExec(c, log=log.append)
        return asyncio.run(ex.take(mk("C1"), side, size, bids, asks, wait_s=1.5)), log

    def test_long_yes_buys_no_with_usdc(self):
        resp = types.SimpleNamespace(ok=True, taking_amount=Decimal("20"), making_amount=Decimal("10.6"), status="matched")
        c = self.client(resp, fill=20.0)
        r, log = self.take(c, "ask", 20.0, [(0.47, 50.0)], [(0.49, 50.0)])
        self.assertEqual(c.sent, [{"token_id": "NC1", "side": "BUY", "amount": "10.60", "max_price": "0.55",
                                   "order_type": "FAK"}])
        self.assertNotIn("shares", c.sent[0])
        self.assertAlmostEqual(r["size"], 20.0)
        self.assertAlmostEqual(r["price"], 0.47)           # YES terms: 1 - 10.60 / 20
        self.assertAlmostEqual(r["fee"], 20.0 * 0.05 * 0.53 * 0.47)
        self.assertIn("closing order filled", log[-1])

    def test_long_no_buys_yes_partial_fill(self):
        resp = types.SimpleNamespace(ok=True, taking_amount=Decimal("12.5"), making_amount=Decimal("6.25"), status="matched")
        r, _ = self.take(self.client(resp, fill=12.5), "bid", 30.0, [(0.47, 50.0)], [(0.49, 12.0), (0.51, 50.0)])
        self.assertAlmostEqual(r["size"], 12.5)            # deeper fills buy fewer shares; the rest closes later
        self.assertAlmostEqual(r["price"], 0.5)

    def test_small_lot_spends_the_minimum(self):
        resp = types.SimpleNamespace(ok=True, taking_amount=Decimal("6"), making_amount=Decimal("0.9"), status="matched")
        c = self.client(resp, fill=6.0)
        self.take(c, "bid", 5.0, [(0.12, 50.0)], [(0.15, 50.0)])
        self.assertEqual((c.sent[0]["token_id"], c.sent[0]["amount"], c.sent[0]["max_price"]), ("YC1", "1.00", "0.17"))

    def test_wallet_fallback_when_amounts_missing(self):
        resp = types.SimpleNamespace(ok=True, taking_amount=Decimal("0"), making_amount=Decimal("0"), status="delayed")
        r, _ = self.take(self.client(resp, fill=19.0), "ask", 20.0, [(0.47, 50.0)], [(0.49, 50.0)])
        self.assertAlmostEqual(r["size"], 19.0)
        self.assertAlmostEqual(r["price"], 0.47)           # no amounts: the best price

    def test_rejected_and_failed(self):
        rej = types.SimpleNamespace(ok=False, code="fak_not_filled", message="no orders found to match")
        r, log = self.take(self.client(rej), "ask", 20.0, [(0.47, 50.0)], [(0.49, 50.0)])
        self.assertIsNone(r)
        self.assertIn("closing order rejected: fak_not_filled", log[-1])
        r, log = self.take(self.client(exc=RuntimeError("down")), "ask", 20.0, [(0.47, 50.0)], [(0.49, 50.0)])
        self.assertIsNone(r)
        self.assertIn("closing order failed", log[-1])
        r, _ = self.take(self.client(), "ask", 20.0, [], [(0.49, 50.0)])
        self.assertIsNone(r)                               # no bids to sell into


class LiveCheckTest(unittest.TestCase):
    """lp-live-check: keys, wallet, allowance and a far-from-market post-only order placed and cancelled."""

    def run_check(self, cash, allow):
        log = []

        class C:
            async def get_balance_allowance(self, asset_type):
                class B:
                    balance = int(cash * 1e6)
                    allowances = {"exchange": allow}
                return B()

            def list_account_trades(self, after):
                class P:
                    async def iter_items(self):
                        for i in range(3):
                            yield i
                return P()

            async def close(self):
                pass

        class Ex:
            c = C()
            placed, cancelled = [], []

            async def place(self, m, side, price, size):
                self.placed.append((m["cond"], side, price, size))
                return "oid-1"

            async def scoring(self, oids):
                return {o: True for o in oids}

            async def cancel(self, m, oid):
                self.cancelled.append(oid)
                return "one"

            async def _cancel_market(self, cond):
                pass

            async def earnings(self, day):
                return {"X": 1.0}

            async def reward_pct(self):
                return {}
        ex = Ex()

        async def create(*a, **k):
            return ex
        gm = {"C1": {"conditionId": "C1", "question": "Will it rain?", "events": [{"id": "E1"}],
                     "clobTokenIds": json.dumps(["Y1", "N1"]), "active": True, "closed": False, "acceptingOrders": True,
                     "orderMinSize": 5}}
        with mock.patch.object(lp.LiveExec, "create", create), \
                mock.patch.object(lp, "rewards_config", lambda: {"C1": {"rate": 10.0, "v": 4.5, "min": 20.0}}), \
                mock.patch.object(lp, "gamma_markets", lambda conds: gm), \
                mock.patch.object(lp, "books", lambda toks: {"Y1": ([(0.45, 100.0)], [(0.47, 100.0)], 0.01)}):
            rc = asyncio.run(lp.live_check(lp.phase3_config(), out=log.append))
        return rc, log, ex

    def test_ok(self):
        rc, log, ex = self.run_check(300.0, 10 ** 12)
        self.assertEqual(rc, 0)
        self.assertEqual(ex.placed, [("C1", "bid", 0.35, 5.0)])      # 10 ticks below the best bid, exchange minimum
        self.assertEqual(ex.cancelled, ["oid-1"])
        self.assertIn("RESULT: OK", log[-1])

    def test_short_wallet_and_no_allowance(self):
        rc, log, _ = self.run_check(100.0, 0)
        self.assertEqual(rc, 1)
        self.assertTrue(any("below 90%" in x for x in log))
        self.assertTrue(any("no USDC allowance" in x for x in log))


class EarningsCommandTest(unittest.TestCase):
    def test_every_complete_day_and_every_market(self):
        data = tempfile.mkdtemp()
        os.makedirs(os.path.join(data, "live"))
        t0 = time.time() - 2.5 * 86400
        with open(os.path.join(data, "live", "live.jsonl"), "w") as f:
            f.write(json.dumps({"ts": t0, "event": "start", "markets": ["A"]}) + "\n")

        class C:
            async def close(self):
                pass

        class Ex:
            c = C()

            async def earnings(self, day):
                return {"A": 1.0, "B": 2.5}              # B joined the rolling portfolio later

        async def fake():
            return Ex()
        with mock.patch.object(lp, "_live_exec", fake), mock.patch.dict(os.environ, {"POLYBOT_DATA": data}), \
                mock.patch("builtins.print"):
            lp.cli("lp-earnings", [])
        ev = lp.load_events(os.path.join(data, "live"), "live")
        earn = [e for e in ev if e["event"] == "earnings"]
        days = [e["day"] for e in earn]
        self.assertEqual(days[0], time.strftime("%Y-%m-%d", time.gmtime(t0)))
        self.assertEqual(days[-1], time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400)))
        self.assertEqual(len(days), len(set(days)))
        self.assertEqual(earn[0]["by_market"], {"A": 1.0, "B": 2.5})
