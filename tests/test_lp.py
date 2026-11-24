"""Liquidity-rewards test module (polybot/lp.py): scoring, quoting rules, fills, stop, report."""
import asyncio
import importlib.util
import json
import os
import time
import tempfile
import unittest

from polybot import lp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M = {"cond": "C1", "yes": "Y1", "no": "N1", "N": 20.0, "v": 4.5, "min": 20.0, "rate": 10.0, "tick": 0.01}


class ScoringTest(unittest.TestCase):
    def test_order_score(self):
        self.assertAlmostEqual(lp.order_score(4.5, 0.0), 1.0)
        self.assertAlmostEqual(lp.order_score(4.5, 1.5), (3.0 / 4.5) ** 2)
        self.assertEqual(lp.order_score(4.5, 4.5), 0.0)
        self.assertEqual(lp.order_score(4.5, -0.1), 0.0)

    def test_side_score_respects_min_size(self):
        levels = [(0.49, 30.0), (0.48, 10.0)]          # the 10-share level is below min size 20
        self.assertAlmostEqual(lp.side_score(levels, 0.50, 4.5, 20.0, "bid"), lp.order_score(4.5, 1.0) * 30)

    def test_m1_alone_takes_the_pool(self):
        self.assertAlmostEqual(lp.m1_fraction([], [], 0.49, 0.51, 20, 4.5, 20), 1.0)

    def test_m1_equal_two_sided_competitor(self):
        # one other maker with the same size at the same prices: competition bound (Qb + Qa) / 2 = our Q
        frac = lp.m1_fraction([(0.49, 20.0)], [(0.51, 20.0)], 0.49, 0.51, 20, 4.5, 20)
        self.assertAlmostEqual(frac, 0.5)

    def test_m1_one_sided_rules(self):
        one = lp.m1_fraction([(0.49, 20.0)], [(0.51, 20.0)], 0.49, None, 20, 4.5, 20)
        two = lp.m1_fraction([(0.49, 20.0)], [(0.51, 20.0)], 0.49, 0.51, 20, 4.5, 20)
        self.assertLess(one, two)
        q = lp.order_score(4.5, 1.0) * 20
        self.assertAlmostEqual(one, (q / 3) / (q + q / 3))
        # midpoint outside [0.10, 0.90]: one side scores nothing
        self.assertEqual(lp.m1_fraction([(0.04, 20.0)], [(0.06, 20.0)], 0.04, None, 20, 4.5, 20), 0.0)

    def test_m1_below_min_size_scores_nothing(self):
        self.assertEqual(lp.m1_fraction([], [], 0.49, 0.51, 10, 4.5, 20), 0.0)


class QuotingTest(unittest.TestCase):
    def test_join_best_inside_zone(self):
        self.assertEqual(lp.targets([(0.48, 50)], [(0.52, 50)], 0.01, 4.5, 0.0, 60), (0.48, 0.52))

    def test_outside_zone_no_quotes(self):
        self.assertEqual(lp.targets([(0.40, 50)], [(0.60, 50)], 0.01, 4.5, 0.0, 60), (None, None))

    def test_position_cap_withdraws_one_side(self):
        self.assertEqual(lp.targets([(0.48, 50)], [(0.52, 50)], 0.01, 4.5, 60.0, 60), (None, 0.52))
        self.assertEqual(lp.targets([(0.48, 50)], [(0.52, 50)], 0.01, 4.5, -60.0, 60), (0.48, None))

    def test_without_mine(self):
        self.assertEqual(lp.without_mine([(0.48, 50.0), (0.47, 20.0)], {0.48: 20.0}), [(0.48, 30.0), (0.47, 20.0)])
        self.assertEqual(lp.without_mine([(0.48, 20.0)], {0.48: 20.0}), [])

    def test_crosses(self):
        self.assertEqual(lp.crosses((0, "B", 0.52), 0.48, 0.52), "ask")      # taker buys YES at our ask
        self.assertEqual(lp.crosses((1, "S", 0.48), 0.48, 0.52), "ask")      # taker sells NO = buys YES at 0.52
        self.assertEqual(lp.crosses((0, "S", 0.48), 0.48, 0.52), "bid")
        self.assertEqual(lp.crosses((1, "B", 0.52), 0.48, 0.52), "bid")      # taker buys NO = sells YES at 0.48
        self.assertIsNone(lp.crosses((0, "B", 0.51), 0.48, 0.52))


class ClassifierTest(unittest.TestCase):
    def test_same_as_research_classifier(self):
        spec = importlib.util.spec_from_file_location("h10_groups_src", os.path.join(ROOT, "research/tools/h10_groups.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(lp.NEWS.pattern, mod.NEWS.pattern)
        self.assertEqual(lp.STABLE_FIRST.pattern, mod.STABLE_FIRST.pattern)

    def test_examples(self):
        self.assertTrue(lp.topic_news("Will Rene Haas be the next CEO of NVIDIA?"))
        self.assertTrue(lp.topic_news("US x Iran Effective Ceasefire by December 31?"))
        self.assertFalse(lp.topic_news("Will Josh Allen have 3,849.5+ passing yards in the 2026-27 NFL regular season?"))
        self.assertFalse(lp.topic_news("Will Zelenskyy post 40-59 posts from November 13 to November 20, 2026?"))
        self.assertFalse(lp.topic_news("Will West Bromwich Albion be promoted to Premier League?"))


class FakeExec(lp.PaperExec):
    def __init__(self):
        super().__init__(trades_fn=lambda cond: [])
        self.queue, self.merged, self.cancel_all_calls = [], [], 0

    async def fills(self, markets):
        out, self.queue = self.queue, []
        return out

    async def merge(self, m, pairs):
        self.merged.append((m["cond"], pairs))
        return pairs

    async def cancel_all(self):
        self.cancel_all_calls += 1
        await super().cancel_all()


def make_test(book, cfg=None, ex=None):
    tmp = tempfile.mkdtemp()
    cfg = cfg or lp.LPConfig(lp_dir=tmp)
    ex = ex or FakeExec()
    j = lp.Journal(os.path.join(tmp, "t.jsonl"))
    t = lp.LPTest(cfg, ex, j, [dict(M)], books_fn=lambda toks: {"Y1": book[0]}, rewards_fn=lambda: {})
    _open.append(j)
    return t, ex, os.path.join(tmp, "t.jsonl")


_open = []


def tearDownModule():
    for j in _open:
        j.close()


CFGS = {"C1": {"v": 4.5, "min": 20.0, "rate": 10.0}}


class LoopTest(unittest.TestCase):
    def test_places_both_sides_and_samples_m1(self):
        t, ex, path = make_test([([(0.48, 100.0)], [(0.52, 100.0)], 0.01)])
        asyncio.run(t.step(1000.0, CFGS))
        s = t.st["C1"]
        self.assertEqual((s.orders["bid"][1], s.orders["ask"][1]), (0.48, 0.52))
        ev = lp.load_events(os.path.dirname(path), "t")
        m1 = [e for e in ev if e["event"] == "m1"]
        self.assertEqual(len(m1), 1)
        q = lp.order_score(4.5, 2.0) * 20
        comp = lp.order_score(4.5, 2.0) * 100          # (Qb + Qa) / 2 of the other orders
        self.assertAlmostEqual(m1[0]["frac"], q / (comp + q))

    def test_paper_quotes_do_not_churn(self):
        # a paper order is not in the public book: the book must be read as is, so the quote stays put
        t, ex, path = make_test([([(0.48, 20.0), (0.47, 50.0)], [(0.52, 20.0), (0.53, 50.0)], 0.01)])
        for k in range(10):
            asyncio.run(t.step(1000.0 + 5 * k, CFGS))
        ev = lp.load_events(os.path.dirname(path), "t")
        self.assertEqual(sum(1 for e in ev if e["event"] == "place"), 2)
        self.assertEqual(sum(1 for e in ev if e["event"] == "cancel"), 0)
        self.assertEqual((t.st["C1"].orders["bid"][1], t.st["C1"].orders["ask"][1]), (0.48, 0.52))

    def test_live_book_excludes_our_own_order(self):
        # on the exchange our 20 at 0.48 is part of the 0.48 level; the others' best bid is still 0.48 (30 left)
        class InBook(FakeExec):
            in_book = True
        t, ex, path = make_test([([(0.48, 50.0)], [(0.52, 50.0)], 0.01)], ex=InBook())
        for k in range(6):
            asyncio.run(t.step(1000.0 + 5 * k, CFGS))
        ev = lp.load_events(os.path.dirname(path), "t")
        self.assertEqual(sum(1 for e in ev if e["event"] == "place"), 2)
        m1 = [e for e in ev if e["event"] == "m1"]
        q = lp.order_score(4.5, 2.0) * 20
        self.assertAlmostEqual(m1[-1]["frac"], q / (lp.order_score(4.5, 2.0) * 50 + q))       # first sample: not yet removed

    def test_fill_markout_and_merge(self):
        book = [([(0.48, 100.0)], [(0.52, 100.0)], 0.01)]
        t, ex, path = make_test(book)
        asyncio.run(t.step(1000.0, CFGS))
        bid_id, ask_id = t.st["C1"].orders["bid"][0], t.st["C1"].orders["ask"][0]
        ex.queue = [{"order": bid_id, "mkt": "C1", "side": "bid", "price": 0.48, "size": 20.0, "ts": 1001.0, "done": True},
                    {"order": ask_id, "mkt": "C1", "side": "ask", "price": 0.52, "size": 20.0, "ts": 1002.0, "done": True}]
        asyncio.run(t.step(1005.0, CFGS))
        s = t.st["C1"]
        self.assertEqual(ex.merged, [("C1", 20.0)])     # 20 YES + 20 NO merged back into $20
        self.assertAlmostEqual(s.yes, 0.0); self.assertAlmostEqual(s.no, 0.0)
        self.assertAlmostEqual(s.cash, -20 * 0.48 - 20 * 0.48 + 20)     # earned the 4c spread on 20 pairs
        book[0] = ([(0.45, 100.0)], [(0.49, 100.0)], 0.01)              # mid falls to 0.47
        asyncio.run(t.step(1070.0, CFGS))
        mk = [e for e in lp.load_events(os.path.dirname(path), "t") if e["event"] == "markout" and e["horizon"] == 60]
        self.assertEqual(len(mk), 2)
        by_side = {e["side"]: e["markout"] for e in mk}
        self.assertAlmostEqual(by_side["bid"], 0.47 - 0.48)
        self.assertAlmostEqual(by_side["ask"], 0.52 - 0.47)

    def test_stop_loss(self):
        book = [([(0.48, 100.0)], [(0.52, 100.0)], 0.01)]
        t, ex, path = make_test(book)
        asyncio.run(t.step(1000.0, CFGS))
        bid_id = t.st["C1"].orders["bid"][0]
        ex.queue = [{"order": bid_id, "mkt": "C1", "side": "bid", "price": 0.48, "size": 200.0, "ts": 1001.0, "done": True}]
        book[0] = ([(0.18, 100.0)], [(0.22, 100.0)], 0.01)              # 200 YES bought at 0.48 now marked at 0.20
        asyncio.run(t.step(1100.0, CFGS))
        self.assertTrue(t.stopped and t.stopped.startswith("stop_loss"))
        self.assertEqual(ex.cancel_all_calls, 1)
        self.assertEqual(t.st["C1"].orders, {"bid": None, "ask": None})
        asyncio.run(t.step(1200.0, CFGS))
        self.assertEqual(t.st["C1"].orders, {"bid": None, "ask": None})   # stays out after the stop

    def test_position_cap_in_loop(self):
        book = [([(0.48, 100.0)], [(0.52, 100.0)], 0.01)]
        t, ex, path = make_test(book)
        asyncio.run(t.step(1000.0, CFGS))
        bid_id = t.st["C1"].orders["bid"][0]
        ex.queue = [{"order": bid_id, "mkt": "C1", "side": "bid", "price": 0.48, "size": 60.0, "ts": 1001.0, "done": True}]
        asyncio.run(t.step(1005.0, CFGS))
        asyncio.run(t.step(1010.0, CFGS))
        s = t.st["C1"]
        self.assertIsNone(s.orders["bid"])              # +60 YES = 3N: no more bids
        self.assertIsNotNone(s.orders["ask"])

    def test_capital_guard(self):
        book = [([(0.48, 100.0)], [(0.52, 100.0)], 0.01)]
        cfg = lp.LPConfig(lp_dir=tempfile.mkdtemp(), capital_usd=30.0, quote_budget_usd=20.0)
        t, ex, path = make_test(book, cfg)
        asyncio.run(t.step(1000.0, CFGS))
        bid_id = t.st["C1"].orders["bid"][0]
        ex.queue = [{"order": bid_id, "mkt": "C1", "side": "bid", "price": 0.48, "size": 25.0, "ts": 1001.0, "done": True}]
        asyncio.run(t.step(1005.0, CFGS))               # exposure 25 * 0.5 = 12.5 >= reserve 10
        asyncio.run(t.step(1010.0, CFGS))
        self.assertIsNone(t.st["C1"].orders["bid"])
        self.assertIsNotNone(t.st["C1"].orders["ask"])

    def test_reward_removed_pulls_quotes(self):
        t, ex, path = make_test([([(0.48, 100.0)], [(0.52, 100.0)], 0.01)])
        asyncio.run(t.step(1000.0, CFGS))
        asyncio.run(t.step(1005.0, {"C1": {"v": 4.5, "min": 20.0, "rate": 0.0}}))
        self.assertEqual(t.st["C1"].orders, {"bid": None, "ask": None})


class RestoreTest(unittest.TestCase):
    def test_restore_positions_from_journal(self):
        t, ex, path = make_test([([(0.48, 100.0)], [(0.52, 100.0)], 0.01)])
        ev = [{"event": "fill", "mkt": "C1", "side": "bid", "price": 0.48, "size": 30.0},
              {"event": "fill", "mkt": "C1", "side": "ask", "price": 0.52, "size": 20.0},
              {"event": "merge", "mkt": "C1", "pairs": 20.0},
              {"event": "fill", "mkt": "OTHER", "side": "bid", "price": 0.5, "size": 5.0}]
        self.assertEqual(t.restore(ev), 2)
        s = t.st["C1"]
        self.assertAlmostEqual(s.yes, 10.0); self.assertAlmostEqual(s.no, 0.0)
        self.assertAlmostEqual(s.cash, -30 * 0.48 - 20 * 0.48 + 20)
        # the restored position counts against the 3N limit: 50 more YES would breach 60
        self.assertEqual(lp.targets([(0.48, 100.0)], [(0.52, 100.0)], 0.01, 4.5, s.pos() + 50, 60), (None, 0.52))


class PaperFillTest(unittest.TestCase):
    def test_public_trade_fills_first_in_queue(self):
        trades = [(2e9, 0, "S", 0.48, 15.0, "k1"), (2e9, 0, "S", 0.48, 15.0, "k2"), (2e9, 0, "B", 0.51, 5.0, "k3")]
        ex = lp.PaperExec(trades_fn=lambda cond: trades)
        ex.since = 0

        async def go():
            oid = await ex.place(M, "bid", 0.48, 20.0)
            await ex.place(M, "ask", 0.52, 20.0)
            for o in ex.orders.values():
                o["t"] = 0
            return oid, await ex.fills([M])
        oid, f = asyncio.run(go())
        self.assertEqual([(x["side"], x["size"], x["done"]) for x in f], [("bid", 15.0, False), ("bid", 5.0, True)])
        self.assertNotIn(oid, ex.orders)


class ReportTest(unittest.TestCase):
    def events(self, earned, markout, contracts):
        ev = [{"event": "start", "ts": 0.0, "markets": ["C1"]}]
        for k in range(7 * 24 * 60):                    # one m1 sample a minute for 7 days, $1/day in total
            ev.append({"event": "m1", "ts": 60.0 * k, "mkt": "C1", "usd": 1.0 / 1440})
        for d in range(7):
            ev.append({"event": "earnings", "ts": 0.0, "day": f"1970-01-0{d + 1}", "by_market": {"C1": earned / 7}})
        ev.append({"event": "fill", "ts": 1.0, "mkt": "C1", "side": "bid", "price": 0.5, "size": contracts})
        ev.append({"event": "markout", "ts": 2.0, "mkt": "C1", "horizon": 60, "side": "bid", "price": 0.5,
                   "size": contracts, "markout": markout})
        ev.append({"event": "end", "ts": 7 * 86400.0, "pnl": -1.0})
        return ev

    def test_pass(self):
        s = lp.summarize(self.events(earned=5.0, markout=-0.015, contracts=100.0), lp.LPConfig())
        self.assertAlmostEqual(s["reward_ratio"], 5.0 / 7.0, places=3)
        self.assertEqual(s["verdict"], "PASS")

    def test_fail_reasons(self):
        s = lp.summarize(self.events(earned=3.0, markout=-0.03, contracts=100.0), lp.LPConfig())
        self.assertIn("reward >= 0.6 x M1", s["verdict"])
        self.assertIn("markout60 >= -2.1 c", s["verdict"])
        s = lp.summarize(self.events(earned=7.0, markout=-0.01, contracts=500.0), lp.LPConfig())
        self.assertIn("fill rate", s["verdict"])


class MedianTest(unittest.TestCase):
    def test_median(self):
        self.assertAlmostEqual(lp.median([0, 9.26, 11.73, 9.81, 0, 0]), 4.63)
        self.assertAlmostEqual(lp.median([1, 3, 2]), 2)
        self.assertEqual(lp.median([]), 0.0)


class SelectionTest(unittest.TestCase):
    def row(self, c, m1, cap=20.0, ev=None):
        return {"cond": c, "event": ev or c, "m1_usd_day": m1, "quote_cap": cap}

    def test_concentration_cap_drops_dominant_market(self):
        cfg = lp.LPConfig(markets_max=3, quote_budget_usd=100, max_share=0.4)
        rows = [self.row("big", 64.0), self.row("a", 5.0), self.row("b", 4.0), self.row("c", 4.0), self.row("d", 3.0)]
        out = [x["cond"] for x in lp.choose(rows, cfg)]
        self.assertNotIn("big", out)
        self.assertEqual(out, ["a", "b", "c"])

    def test_one_market_per_event_and_budget(self):
        cfg = lp.LPConfig(markets_max=5, quote_budget_usd=45, max_share=1.0)
        rows = [self.row("a", 5.0, ev="E"), self.row("b", 4.0, ev="E"), self.row("c", 3.0), self.row("d", 2.0)]
        self.assertEqual([x["cond"] for x in lp.choose(rows, cfg)], ["a", "c"])


class DaySelectionTest(unittest.TestCase):
    """One-day selection (research/PREREG_LP_PHASE1B.md): snapshots on disk, median M1 with 0 where we cannot
    quote, hard filter on the median number of competitor levels."""
    REW = {c: {"rate": 10.0, "v": 4.5, "min": 20.0} for c in "ABC"}

    def meta(self, c):
        return {"question": f"Will test market {c} resolve YES?", "active": True, "closed": False, "acceptingOrders": True,
                "clobTokenIds": json.dumps([f"Y{c}", f"N{c}"]), "createdAt": "2026-01-01T00:00:00Z",
                "endDate": "2027-06-01T00:00:00Z", "events": [{"id": "E" + c}]}

    def books(self, toks):
        k = self.calls = self.calls + 1
        lone = ([(0.48, 100.0)], [(0.52, 100.0)], 0.01)                                 # 2 levels in the zone
        busy = ([(0.48, 100.0), (0.47, 100.0)], [(0.52, 100.0), (0.53, 100.0)], 0.01)   # 4 levels
        wide = ([(0.40, 100.0), (0.39, 100.0)], [(0.60, 100.0), (0.61, 100.0)], 0.01)   # best prices outside the zone
        bk = {"YA": lone, "YB": busy, "YC": busy if k == 1 else wide}
        return {t: bk[t] for t in toks}

    def setUp(self):
        self.tmp, self.calls, self.t = tempfile.mkdtemp(), 0, [time.time()]
        self.cfg = lp.LPConfig(lp_dir=self.tmp, select_snapshots=3, select_gap_s=1800, markets_min=1, max_share=1.0)

    def run_select(self, sleep=None, gamma_fn=None):
        def adv(s):
            self.t[0] += s
        return lp.select(self.cfg, log=lambda *_: None, sleep=sleep or adv, clock=lambda: self.t[0],
                         rewards_fn=lambda: self.REW, gamma_fn=gamma_fn or (lambda rw: {c: self.meta(c) for c in rw}),
                         books_fn=self.books)

    def test_comp_levels(self):
        bids, asks = [(0.48, 100.0), (0.47, 5.0), (0.45, 100.0)], [(0.52, 100.0), (0.56, 100.0)]
        self.assertEqual(lp.comp_levels(bids, asks, 0.50, 4.5, 20.0), 2)   # 0.47 too small, 0.45 / 0.56 outside

    def test_filter_median_and_resume_after_stop(self):
        def stop_now(_):
            raise lp.SelectionStopped()
        with self.assertRaises(lp.SelectionStopped):
            self.run_select(sleep=stop_now)                 # stopped after the first snapshot
        with open(os.path.join(self.tmp, "selection", "snapshots.jsonl")) as f:
            self.assertEqual(len(f.readlines()), 1)

        def no_gamma(rw):
            raise AssertionError("candidates must come from disk on resume")
        out = self.run_select(gamma_fn=no_gamma)
        self.assertEqual(self.calls, 3)                     # 1 before the stop + 2 after, not 3 new ones
        self.assertEqual([x["cond"] for x in out], ["B"])   # A: 2 competitor levels; C: in the zone 1 of 3 times
        b = out[0]
        self.assertEqual(b["comp_levels_median"], 4)
        self.assertEqual(len(b["m1_snapshots"]), 3)
        self.assertGreater(b["m1_usd_day"], 0)
        with open(os.path.join(self.tmp, "selection", "summary.json")) as f:
            sm = json.load(f)
        # C is outside the zone in 2 of 3 snapshots: 0 levels there, median 0, so the filter drops it too
        self.assertEqual((sm["snapshots"], sm["candidates_quotable"], sm["passed_filter"]), (3, 3, 1))
        self.assertEqual(sm["stats"]["C"][1], 0.0)          # median M1 of C is 0: outside the zone 2 of 3 times
        self.assertAlmostEqual(sm["span_h"], 1.0)
        with open(os.path.join(self.tmp, "markets.json")) as f:
            self.assertEqual([m["cond"] for m in json.load(f)["markets"]], ["B"])


class WindowTest(unittest.TestCase):
    def test_window_summary_and_resilience(self):
        calls = {"n": 0}
        book = ([(0.48, 100.0)], [(0.52, 100.0)], 0.01)

        def books_fn(toks):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("network down")
            return {"Y1": book}
        tmp = tempfile.mkdtemp()
        cfg = lp.LPConfig(lp_dir=tmp, poll_s=0.0, window_s=1800.0, test_days=0.0)
        j = lp.Journal(os.path.join(tmp, "w.jsonl"))
        _open.append(j)
        clock = {"t": 1800.0 * 10}
        t = lp.LPTest(cfg, FakeExec(), j, [dict(M)], books_fn=books_fn,
                      rewards_fn=lambda: {"C1": {"v": 4.5, "min": 20.0, "rate": 10.0}}, clock=lambda: clock["t"])

        async def go():
            cfgs = {"C1": {"v": 4.5, "min": 20.0, "rate": 10.0}}
            for k in range(4):
                try:
                    await t.step(clock["t"], cfgs)
                except OSError:
                    t.errors += 1
                clock["t"] += 600.0                      # 4 steps over 40 minutes: crosses one window boundary
        asyncio.run(go())
        ev = lp.load_events(tmp, "w")
        w = [e for e in ev if e["event"] == "window"]
        self.assertEqual(len(w), 1)
        mk = w[0]["markets"]["C1"]
        self.assertEqual(mk["samples"], 2)               # the failed step took no sample
        self.assertEqual(mk["places"], 2)
        self.assertGreater(mk["m1_usd_day"], 0)
        self.assertEqual(mk["comp_levels"], 2.0)         # one level on each side inside the zone
        self.assertTrue(os.path.exists(os.path.join(tmp, "status.json")))

    def test_run_survives_errors(self):
        tmp = tempfile.mkdtemp()
        n = {"k": 0}

        def books_fn(toks):
            n["k"] += 1
            raise OSError("down")
        cfg = lp.LPConfig(lp_dir=tmp, poll_s=0.0, test_days=0.0)
        j = lp.Journal(os.path.join(tmp, "r.jsonl"))
        _open.append(j)
        clock = {"t": 0.0}

        def tick():
            clock["t"] += 10.0
            return clock["t"]
        cfg.test_days = 50.0 / 86400                     # five iterations of the clock
        t = lp.LPTest(cfg, FakeExec(), j, [dict(M)], books_fn=books_fn, rewards_fn=lambda: {}, clock=tick)
        asyncio.run(t.run())
        self.assertGreaterEqual(t.errors, 1)
        self.assertIn("end", [e["event"] for e in lp.load_events(tmp, "r")])


class Phase1Test(unittest.TestCase):
    def wins(self, values, shares=(0.4, 0.3, 0.3), jumpy_c=None, markouts=None):
        """values: portfolio M1 per 30-minute window; jumpy_c: market C alternates 0 / 2x its share"""
        ev = [{"event": "start", "ts": 0.0}]
        for k, v in enumerate(values):
            mk = {}
            for name, sh in zip("ABC", shares):
                m1 = v * sh
                if name == "C" and jumpy_c:
                    m1 = 0.0 if k % 2 else 2 * v * sh * jumpy_c
                mk[name] = {"m1_usd_day": m1, "fills": 0, "contracts": 0.0, "comp_levels": 2.0, "markout60": []}
            if markouts and k == 0:
                mk["A"]["markout60"] = markouts; mk["A"]["fills"] = len(markouts)
                mk["A"]["contracts"] = sum(w for _, w in markouts)
            ev.append({"event": "window", "start": 1800.0 * k, "end": 1800.0 * (k + 1),
                       "m1_usd_day": sum(x["m1_usd_day"] for x in mk.values()), "markets": mk})
        return ev

    def test_pass_and_markout_is_data_only(self):
        r = lp.phase1(self.wins([10.0, 11.0, 9.0, 10.0] * 24, markouts=[[0.0, 90.0], [-12.0, 10.0]]))
        self.assertEqual(r["verdict"], "PASS")              # a -12 c tail does not gate phase 1
        self.assertAlmostEqual(r["max_share_last24"], 0.4)
        self.assertAlmostEqual(r["cv"], (0.5 ** 0.5) / 10)
        self.assertAlmostEqual(r["markout60"]["p5_c"], -12.0)
        self.assertAlmostEqual(r["markout60"]["share_worse_than_-2.1c"], 0.1)

    def test_cv_and_concentration_fail(self):
        r = lp.phase1(self.wins([1.0, 30.0] * 48))
        self.assertIn("M1 CV", r["verdict"])
        r = lp.phase1(self.wins([10.0] * 96, shares=(0.6, 0.2, 0.2)))
        self.assertIn("no market > 40%", r["verdict"])

    def test_exclude_a_jumpy_market(self):
        ev = self.wins([10.0] * 96, shares=(0.2, 0.2, 0.6), jumpy_c=2.0)
        self.assertIn("M1 CV", lp.phase1(ev)["verdict"])
        r = lp.phase1(ev, exclude={"C"})
        self.assertEqual(r["excluded"], ["C"])
        self.assertAlmostEqual(r["cv"], 0.0)
        self.assertAlmostEqual(r["max_share_last24"], 0.5)   # two equal markets left: 50% each
        self.assertIn("no market > 40%", r["verdict"])

    def test_h10_closure_rule(self):
        ev = self.wins([10.0, 11.0, 9.0, 10.0] * 24)       # portfolio M1 of hours 24-48: $10/day
        r = lp.phase1(ev, m1_per_100_min=10.0, quote_cap_usd=100.0)
        self.assertEqual((r["verdict"], r["h10_closed"]), ("PASS", False))
        r = lp.phase1(ev, m1_per_100_min=10.0, quote_cap_usd=200.0)      # $5 per $100
        self.assertTrue(r["h10_closed"])
        self.assertIn("H10 closes", r["verdict"])
        ev[0]["quote_cap"] = 50.0                                          # from the start event: $20 per $100
        r = lp.phase1(ev, m1_per_100_min=10.0)
        self.assertAlmostEqual(r["m1_per_100usd_2nd_half"], 20.0)
        self.assertFalse(r["h10_closed"])
        self.assertIsNone(lp.phase1(ev)["h10_closed"])                    # rule off: phase 1 as before
        self.assertFalse(lp.phase1(self.wins([1.0] * 20), m1_per_100_min=10.0, quote_cap_usd=100.0)["h10_closed"])  # < 48 h


if __name__ == "__main__":
    unittest.main()
