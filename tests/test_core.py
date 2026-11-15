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

    def test_directional_shift_moves_both_quotes(self):
        p = Params(edge=2.0, max_gap=99)
        # bids never step above the best bid and asks never below the best ask (join, not improve),
        # so test each side where the fair value keeps it behind the touch
        self.assertEqual(decide(p, 60, 40.0, 0.0, 45, 55, 0, 0).bid, 38)
        self.assertEqual(decide(p, 60, 40.0, 0.0, 45, 55, 0, 0, dir_c=3.0).bid, 41)
        self.assertEqual(decide(p, 60, 60.0, 0.0, 45, 55, 0, 0).ask, 62)
        self.assertEqual(decide(p, 60, 60.0, 0.0, 45, 55, 0, 0, dir_c=3.0).ask, 65)

    def test_mid_signals(self):
        from polybot.strategy import MidSignals, directional
        sig = MidSignals()
        for sec in range(0, 120):
            sig.update(sec * 1000, 50.0 if sec < 100 else 60.0)
        self.assertAlmostEqual(sig.momentum(60), 10.0)          # the jump happened 20 s ago
        self.assertAlmostEqual(sig.momentum(10), 0.0)
        self.assertGreater(sig.cci(), 100)                      # a jump to the top of the range
        p = Params(mom_s=60, mom_w=-0.5)
        self.assertAlmostEqual(directional(p, sig), -5.0)       # fade: lean against the rise

    def test_share_filters(self):
        def session(bids, p):
            ex = PaperExecutor(None, 100)
            ses = MarketSession(START, p, SpotSeries(), ex, mode="mid"); ex.book = ses.book
            ses.on_snapshot(bids, [["0.52", "100"]])
            ses.on_timer(START * 1000 + 60000)
            return ex
        p = Params(clip=10, max_share=0.25, edge=1.0, use_model=False)
        # mid 50, wanted bid = best bid 48
        self.assertEqual(session([["0.47", "20"], ["0.48", "100"]], p).order("B").price, 48)   # share 10/110
        self.assertIsNone(session([["0.47", "100"], ["0.48", "20"]], p).order("B"))             # share 10/30
        self.assertEqual(session([["0.47", "100"], ["0.48", "20"]], Params(clip=10, edge=1.0, use_model=False))
                         .order("B").price, 48)                                                  # filter off

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

    def test_flatten_buys_the_short_side(self):
        ex, ses = self.make()
        ses.p.t_flat = 240.0
        ses.on_fill("B", 49, 20, 1000)                            # long 20 Up
        ses._flatten(START * 1000 + 241000, 49, 50)
        self.assertEqual(ex.taker[1:], ("S", 46, 20))             # buy 20 Down from 100-49, walking up to 3c

    def test_taker_walks_the_book(self):
        ex, ses = self.make()                                     # bids 49 x200, 48 x300; asks 50 x150, 51 x400
        ex.take("B", 51, 200, 1000)
        ex.on_time(1000 + 150 + 250)
        self.assertAlmostEqual(ses.res.q_up, 200)
        self.assertAlmostEqual(ses.res.cost, 150 * 0.50 + 50 * 0.51)
        self.assertAlmostEqual(ses.res.fees, (150 * fee_c(50) + 50 * fee_c(51)) / 100)

    def test_real_fill_driven_queue(self):
        ex, ses = self.make()                                     # bid level 49 shows 200 ahead of us
        ex.place("B", 49, 10, 1000); ex.on_time(1100)
        ex.on_real_fills([("B", 49, 195.0)], 1200)                # real makers ahead of us filled 195
        self.assertEqual(ses.res.fills, 0)
        ex.on_real_fills([("B", 49, 8.0)], 1300)                  # 5 left ahead, 3 for us
        self.assertAlmostEqual(ses.res.q_up, 3)
        ex.on_real_fills([("B", 49, 20.0), ("B", 48, 5.0)], 1400) # a sweep reached 48: our level is gone
        self.assertAlmostEqual(ses.res.q_up, 10)
        ex.on_real_fills([("S", 52, 50.0)], 1500)                 # other side: no effect
        self.assertAlmostEqual(ses.res.q_dn, 0)

    def test_order_log_records_resting_intervals(self):
        ex, ses = self.make()
        ex.place("B", 49, 10, 1000); ex.on_time(1100)
        ex.cancel("B", 1500); ex.on_time(1600)
        ex.place("S", 52, 10, 2000); ex.on_time(2100)
        ex.close_log(9999)
        self.assertEqual(ex.order_log, [("B", 49, 1100, 1600, 0.0, 200.0, 0), ("S", 52, 2100, 9999, 0.0, 0.0, 0)])

    def test_trade_before_order_rested_does_not_fill(self):
        ex, ses = self.make()
        ex.place("B", 49, 10, 1000); ex.on_time(1100)             # resting from exchange time 1100
        ses.on_level("B", 49, 0.0, 1150)
        ex.on_trade("S", 48, 50, 1090)                            # printed earlier, delivered late
        self.assertEqual(ses.res.fills, 0); self.assertEqual(ex.stale_trades, 1)
        ex.on_trade("S", 48, 50, 1200)
        self.assertAlmostEqual(ses.res.q_up, 10)

    def test_competition_haircuts(self):
        spot = SpotSeries()
        ex = PaperExecutor(None, 100, 1.0, fill_share=0.5)
        ses = MarketSession(START, Params(), spot, ex); ex.book = ses.book
        ses.on_snapshot([["0.49", "0"]], [["0.51", "100"]])
        ex.place("B", 49, 10, 1000); ex.on_time(1100)
        ex.on_trade("S", 49, 4, 1200)
        self.assertAlmostEqual(ses.res.q_up, 2)                   # half of the at-price fill
        ex2 = PaperExecutor(None, 100, 1.0, share_good=0.0)
        ses2 = MarketSession(START, Params(), spot, ex2); ex2.book = ses2.book
        ses2.on_snapshot([["0.49", "0"]], [["0.51", "100"]])
        ex2.place("B", 49, 10, 1000); ex2.on_time(1100)
        ses2.last = {"fair": 52.0}                                # bid 49 below fair 52: a good fill
        ex2.on_trade("S", 49, 4, 1200)
        self.assertEqual(ses2.res.fills, 0)                       # competitors take all good fills
        ses2.last = {"fair": 47.0}                                # bid 49 above fair: toxic, nobody competes
        ex2.on_trade("S", 49, 4, 1300)
        self.assertAlmostEqual(ses2.res.q_up, 4)

    def test_settlement(self):
        ex, ses = self.make()
        ses.on_fill("B", 40, 10, 1000); ses.on_fill("S", 45, 10, 2000)   # Up at 40c, Down at 55c
        r = ses.settle(0)
        rebate = 10 * 0.2 * (fee_c(40) + fee_c(55)) / 100          # 20% of our own maker fee-equivalent
        self.assertAlmostEqual(r.pnl_ex_rebate, 10 - (4.0 + 5.5))
        self.assertAlmostEqual(r.pnl, 10 - 9.5 + rebate)
        self.assertEqual(r.outcome_src, "actual")
        self.assertAlmostEqual(ses.settle(1).pnl_ex_rebate, 10 - 9.5)

    def test_ladder_places_once_and_holds(self):
        from polybot.engine import MultiPaperExecutor
        p = Params(clip=10, max_net=15, lad_place=5, lad_d0=2, lad_step=3, lad_levels=2, lad_stop=240)
        ex = MultiPaperExecutor(None, 100)
        ses = MarketSession(START, p, SpotSeries(), ex, mode="ladder"); ex.book = ses.book
        ses.on_snapshot([["0.49", "100"]], [["0.51", "100"]])           # mid 50
        t0 = START * 1000 + 6000
        ses.on_timer(t0); ex.on_time(t0 + 100)
        self.assertEqual(sorted(ex.multi), [("B", 45), ("B", 48), ("S", 52), ("S", 55)])
        ex.on_real_fills([("B", 48, 3.0), ("B", 47, 5.0)], t0 + 200)    # sweep through 48: level filled
        self.assertAlmostEqual(ses.res.q_up, 10)
        ses.on_timer(t0 + 300)
        self.assertNotIn(("B", 48), ex.multi)                              # no re-arm by default
        ex.on_real_fills([("B", 45, 50.0)], t0 + 400)                      # 0 resting ahead at 45 when placed
        ses.on_timer(t0 + 500); ex.on_time(t0 + 600)
        self.assertAlmostEqual(ses.res.q_up, 20)
        ses.on_timer(START * 1000 + 241000); ex.on_time(START * 1000 + 241200)
        self.assertEqual(ex.multi, {})                                     # everything pulled at lad_stop

    def test_reward_score(self):
        ex, ses = self.make()                                     # book: bids 49 x200, 48 x300; asks 50 x150, 51 x400
        ses.p = Params(reward_rate=1440.0, reward_min_size=50)    # $1 per minute-sample at 100% share
        ex.place("B", 49, 100, 0); ex.place("S", 50, 100, 0); ex.on_time(200)
        ses._reward_sample(49.5)
        v = 4.5; S = lambda d: ((v - d) / v) ** 2
        mine = S(0.5) * 100
        others_b = S(0.5) * 200 + S(1.5) * 300; others_s = S(0.5) * 150 + S(1.5) * 400
        q_all = max(min(others_b + mine, others_s + mine), max(others_b + mine, others_s + mine) / 3)
        self.assertAlmostEqual(ses.res.reward_share_min, mine / q_all)
        r = ses.settle(0)
        self.assertAlmostEqual(r.reward, mine / q_all)
        ex2, ses2 = self.make()                                   # orders below the minimum size do not score
        ex2.place("B", 49, 10, 0); ex2.on_time(200); ses2._reward_sample(49.5)
        self.assertEqual(ses2.res.reward_share_min, 0.0)

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
