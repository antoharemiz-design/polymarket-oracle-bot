"""Unit tests for the parts whose mistakes cost money: pricing, quoting rules, fill model, risk.

run: python -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest

from polybot.book import UpBook
from polybot.engine import MarketSession, PaperExecutor, fee_c
from polybot.fairvalue import FairValue, SpotSeries
from polybot.journal import Journal
from polybot.risk import Risk, RiskLimits
from polybot.strategy import Fair, Params, decide, keep

START = 1_000_000_200          # a window start on a 300 s boundary


def spot_with(prices_by_second, first_sec):
    s = SpotSeries()
    for i, px in enumerate(prices_by_second):
        s.update((first_sec + i) * 1000 + 500, px)
    return s


def noisy_flat(n, base=70000.0, amp=1.0):
    return [base + (amp if i % 2 else -amp) for i in range(n)]


class FairValueTest(unittest.TestCase):
    def setUp(self):
        self.first = START - 1000
        self.prices = noisy_flat(1000)

    def test_twap_and_sigma(self):
        s = spot_with([100.0, 102.0, 104.0, 106.0], 10)
        s.advance(14_000)
        self.assertAlmostEqual(s.mean(10, 13), (100 + 102 + 104) / 3)   # boundaries 11, 12, 13
        self.assertIsNone(s.mean(5, 13))                                # not covered
        self.assertEqual(s.sigma(), 0.0)                                # too few returns yet

    def test_half_at_strike_and_monotonic(self):
        s = spot_with(self.prices, self.first)
        fv = FairValue(s, START)
        s.update((START + 30) * 1000, 70000.0)
        p0 = fv.p_up((START + 30) * 1000)
        self.assertAlmostEqual(p0, 0.5, delta=0.02)
        s.update((START + 31) * 1000, 70010.0)
        self.assertGreater(fv.p_up((START + 31) * 1000), p0)
        s.update((START + 32) * 1000, 69980.0)
        self.assertLess(fv.p_up((START + 32) * 1000), p0)

    def test_strike_is_twap_before_start_not_spot_at_start(self):
        prices = noisy_flat(940) + [70100.0] * 60          # last 60 s before the start are 100 higher
        s = spot_with(prices, self.first)
        fv = FairValue(s, START)
        s.update((START + 5) * 1000, 70100.0)
        fv.p_up((START + 5) * 1000)
        self.assertAlmostEqual(fv.strike, 70100.0, delta=0.5)

    def test_certainty_grows_into_the_final_minute(self):
        s = spot_with(self.prices, self.first)
        fv = FairValue(s, START)
        s.update((START + 100) * 1000, 70003.0)
        early = fv.p_up((START + 100) * 1000)
        for sec in range(101, 296):
            s.update((START + sec) * 1000, 70003.0)
        late = fv.p_up((START + 295) * 1000)
        self.assertGreater(late, early)
        self.assertGreater(late, 0.99)

    def test_resolved_after_end(self):
        s = spot_with(self.prices, self.first)
        fv = FairValue(s, START)
        for sec in range(0, 302):
            s.update((START + sec) * 1000, 69990.0)
        self.assertEqual(fv.p_up((START + 301) * 1000), 0.0)


class StrategyTest(unittest.TestCase):
    def test_quotes_never_cross_and_respect_edge(self):
        p = Params(edge=2.0, improve=0)
        q = decide(p, 60, 50.0, 0.0, 49, 50, 0, 0)
        self.assertEqual(q.bid, 48)            # floor(50 - 2), not above best bid
        self.assertEqual(q.ask, 52)
        q = decide(p, 60, 60.0, 0.0, 49, 50, 0, 0)   # fair far above the book: bid is capped at the best bid
        self.assertEqual(q.bid, 49)
        self.assertGreaterEqual(q.ask, 50)

    def test_gates(self):
        p = Params()
        self.assertEqual(decide(p, 1, 50, 0, 49, 50, 0, 0).bid, None)        # before t_on
        self.assertEqual(decide(p, 290, 50, 0, 49, 50, 0, 0).bid, None)      # after t_off
        self.assertEqual(decide(p, 60, 5, 0, 4, 5, 0, 0).bid, None)          # outside the price band
        self.assertEqual(decide(p, 60, 50, 99, 49, 50, 0, 0).bid, None)      # model and book disagree wildly
        self.assertEqual(decide(p, 60, 50, 0, 0, 100, 0, 0).bid, None)       # empty book

    def test_inventory_limits_and_skew(self):
        p = Params(clip=10, max_net=20, skew=1.0, edge=1.0)
        q = decide(p, 60, 50.0, 0.0, 49, 50, 20, 0)      # already long 20 Up: no more Up
        self.assertIsNone(q.bid); self.assertIsNotNone(q.ask)
        q = decide(p, 60, 50.0, 0.0, 49, 50, 0, 20)
        self.assertIsNone(q.ask); self.assertIsNotNone(q.bid)
        flat = decide(p, 60, 50.0, 0.0, 45, 55, 0, 0)
        long_up = decide(p, 60, 50.0, 0.0, 45, 55, 10, 0)
        self.assertLess(long_up.bid, flat.bid + 1)       # long Up lowers both quotes
        self.assertLessEqual(long_up.ask, flat.ask)
        p2 = Params(clip=10, max_gross=30)
        self.assertIsNone(decide(p2, 60, 50.0, 0.0, 49, 50, 15, 10).bid)

    def test_keep_hysteresis(self):
        p = Params(edge=2.0, edge_cancel=1.0)
        self.assertTrue(keep(p, "B", 48, 49.5, 0, 0))
        self.assertFalse(keep(p, "B", 48, 48.5, 0, 0))
        self.assertTrue(keep(p, "S", 52, 50.5, 0, 0))
        self.assertFalse(keep(p, "S", 52, 51.5, 0, 0))

    def test_fair_is_mid_plus_unpriced_spot_move(self):
        f = Fair(k=1.0)
        fair, raw = f.update(0, (10.0, 20.0), 49, 50)                  # model level says 69c, book 49.5c
        self.assertAlmostEqual(fair, 49.5, delta=0.01)                 # the level disagreement is ignored
        self.assertGreater(raw, 15)
        up, _ = f.update(100, (10.0, 20.0), 49, 50, spot_move=4.0)     # spot just rose $4
        self.assertAlmostEqual(up, 49.5 + 100 * 0.3989 / 20 * 4, delta=0.1)
        late, _ = f.update(100, (10.0, 20.0), 49, 50, spot_move=4.0, sens=0.25)   # 15 s of the TWAP left
        self.assertAlmostEqual(late - 49.5, (up - 49.5) / 4, delta=0.05)
        down, _ = f.update(100, (10.0, 20.0), 49, 50, spot_move=-4.0)
        self.assertAlmostEqual(down, 49.5 - (up - 49.5), delta=0.01)

    def test_spot_move_window(self):
        s = SpotSeries()
        for t, px in ((1000, 100.0), (1300, 101.0), (1700, 103.0), (2000, 106.0)):
            s.update(t, px)
        self.assertAlmostEqual(s.move(2000, 500), 106.0 - 101.0)       # latest tick at or before 1500
        self.assertAlmostEqual(s.move(2000, 250), 106.0 - 103.0)
        self.assertAlmostEqual(s.move(2000, 5000), 0.0)                # no tick that old


class PaperExecutorTest(unittest.TestCase):
    def make(self, lat=100, queue_mult=1.0):
        spot = SpotSeries()
        ex = PaperExecutor(None, lat, queue_mult)
        ses = MarketSession(START, Params(), spot, ex)
        ex.book = ses.book
        ses.on_snapshot([["0.48", "300"], ["0.49", "200"]], [["0.50", "150"], ["0.51", "400"]])
        return ex, ses

    def test_latency_and_queue(self):
        ex, ses = self.make()
        ex.place("B", 49, 10, 1000)
        ex.on_time(1050)
        self.assertEqual(ex.order("B").state, "PENDING")
        ex.on_trade("S", 49, 500, 1060)                  # not live yet: no fill
        self.assertEqual(ses.res.fills, 0)
        ex.on_time(1100)
        self.assertEqual(ex.order("B").state, "LIVE")
        self.assertEqual(ex.order("B").queue, 200)
        ex.on_trade("S", 49, 150, 1200)                  # eats queue only
        self.assertEqual(ses.res.fills, 0)
        self.assertEqual(ex.order("B").queue, 50)
        ex.on_trade("S", 49, 54, 1300)                   # 50 ahead, 4 for us
        self.assertAlmostEqual(ses.res.q_up, 4)
        ex.on_trade("S", 49, 100, 1400)                  # rest of our 10
        self.assertAlmostEqual(ses.res.q_up, 10)
        self.assertIsNone(ex.order("B"))
        self.assertAlmostEqual(ses.res.cost, 4.9)

    def test_trade_through_fills_fully_and_down_side_accounting(self):
        ex, ses = self.make()
        ex.place("S", 50, 10, 1000); ex.on_time(1100)
        ex.on_trade("B", 51, 7, 1200)                    # someone paid 51 while we offered 50
        self.assertAlmostEqual(ses.res.q_dn, 7)
        self.assertAlmostEqual(ses.res.cost, 7 * 0.50)   # bought Down at 100 - 50
        self.assertAlmostEqual(ses.res.rebate, 7 * 0.2 * fee_c(50) / 100)

    def test_post_only_rejection(self):
        ex, ses = self.make()
        ex.place("B", 50, 10, 1000)                      # would cross the 50 ask
        ex.on_time(1100)
        self.assertIsNone(ex.order("B")); self.assertEqual(ses.res.rejected, 1)

    def test_cancel_takes_effect_after_latency(self):
        ex, ses = self.make()
        ex.place("B", 49, 10, 1000); ex.on_time(1100)
        ses.on_level("B", 49, 0.0)                       # everyone ahead cancelled
        ex.cancel("B", 2000)
        ex.on_trade("S", 49, 3, 2050)                    # still exposed while the cancel is in flight
        self.assertAlmostEqual(ses.res.q_up, 3)
        ex.on_time(2100)
        self.assertIsNone(ex.order("B"))
        ex.on_trade("S", 49, 5, 2200)
        self.assertAlmostEqual(ses.res.q_up, 3)

    def test_queue_multiplier_and_pessimistic_cancels(self):
        ex, ses = self.make(queue_mult=2.0)
        ex.place("B", 49, 10, 1000); ex.on_time(1100)
        self.assertEqual(ex.order("B").queue, 400)
        ses.on_level("B", 49, 120.0)                     # level shrank to 120: at most 240 (x2) ahead
        self.assertEqual(ex.order("B").queue, 240)

    def test_taker_delay_and_fee(self):
        ex, ses = self.make()
        ex.take("B", 50, 10, 1000)
        ex.on_time(1300)                                 # 100 latency + 250 exchange delay not elapsed
        self.assertEqual(ses.res.fills, 0)
        ses.on_level("S", 50, 0.0)                       # the quote we wanted is pulled meanwhile
        ex.on_time(1350)
        self.assertEqual(ses.res.fills, 0)               # best ask is now 51 > limit: nothing bought

    def test_fills_use_exchange_time_not_receive_time(self):
        ex, ses = self.make(lat=100)
        ses.feed(("P", 10_050, "S", 60, 5.0, 10_000))            # establishes a 50 ms minimum feed lag
        ex.take("B", 50, 10, 10_100)                             # matched at exchange time 10_450
        # the 50c offer was pulled on the exchange at 10_300 but the message reached us at 10_900
        ses.feed(("P", 10_900, "S", 50, 0.0, 10_250))
        ses.feed(("P", 10_950, "S", 52, 9.0, 10_900))            # first event after the match time
        self.assertEqual(ses.res.fills, 0)                       # nothing left at or below the limit

    def test_settlement(self):
        ex, ses = self.make()
        ses.on_fill("B", 40, 10, 1000); ses.on_fill("S", 45, 10, 2000)   # Up at 40c, Down at 55c
        r = ses.settle(0)
        rebate = 10 * 0.2 * (fee_c(40) + fee_c(55)) / 100          # 20% of our own maker fee-equivalent
        self.assertAlmostEqual(r.pnl_ex_rebate, 10 - (4.0 + 5.5))
        self.assertAlmostEqual(r.pnl, 10 - 9.5 + rebate)
        self.assertEqual(r.outcome_src, "actual")
        self.assertAlmostEqual(ses.settle(1).pnl_ex_rebate, 10 - 9.5)

    def test_taker_fills_pay_fee_and_earn_no_rebate(self):
        ex, ses = self.make()
        ses.on_fill("B", 50, 10, 1000, True)
        r = ses.settle(0)
        self.assertAlmostEqual(r.fees, 10 * fee_c(50) / 100)
        self.assertEqual(r.rebate, 0.0)
        self.assertAlmostEqual(r.pnl, 10 - 5.0 - 0.175)


class BookRiskJournalTest(unittest.TestCase):
    def test_book(self):
        b = UpBook()
        b.snapshot([["0.48", "10"], ["0.49", "5"]], [["0.51", "7"]])
        self.assertEqual((b.best_bid(), b.best_ask()), (49, 51))
        b.change("B", 49, 0.0); b.change("S", 50, 3.0)
        self.assertEqual((b.best_bid(), b.best_ask()), (48, 50))

    def test_daily_stop_and_reset(self):
        r = Risk(RiskLimits(daily_stop_usd=10))
        day1 = 1_700_000_000_000
        r.on_settled(day1, 1, -6.0)
        self.assertTrue(r.may_open(day1)[0])
        r.on_settled(day1, 2, -5.0)
        self.assertEqual(r.may_open(day1), (False, "daily_stop"))
        self.assertTrue(r.may_open(day1 + 86_400_000)[0])        # next UTC day

    def test_stale_feeds_and_exposure(self):
        r = Risk(RiskLimits(max_open_usd=50, spot_stale_ms=3000, book_stale_ms=5000))
        t = 1_700_000_000_000
        self.assertEqual(r.halt_reason(t, 1, 10.0, t - 100, t - 100), "")
        self.assertEqual(r.halt_reason(t, 1, 10.0, t - 4000, t - 100), "spot_stale")
        self.assertEqual(r.halt_reason(t, 1, 10.0, t - 100, t - 6000), "book_stale")
        self.assertEqual(r.halt_reason(t, 2, 45.0, t, t), "max_open_usd")

    def test_journal_lines_are_json_with_timestamps(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d, "paper")
            j.write(1_700_000_000_123, START, "place", side="B", price=49)
            j.close()
            with open(os.path.join(d, os.listdir(d)[0])) as f:
                line = f.readline()
            rec = json.loads(line)
            self.assertEqual(rec["event"], "place"); self.assertEqual(rec["ts"], 1_700_000_000_123)
            self.assertTrue(rec["iso"].endswith(".123Z"))


if __name__ == "__main__":
    unittest.main()
