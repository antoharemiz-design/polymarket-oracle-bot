"""H11 fair value of daily temperature markets (polybot/wxfair.py): market parsing, the resolution rules of the METAR,
rounding, bucket probabilities, the model on given sources."""
import math
import time
import unittest

from polybot import wxfair as W


def ts(s):
    return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M")) - time.timezone


class ParseTest(unittest.TestCase):
    def test_buckets(self):
        p = W.parse_market("Will the highest temperature in Seoul (Incheon) be 15°C on November 26?", ts("2026-11-25 00:00"))
        self.assertEqual((p["kind"], p["icao"], p["unit"], p["lo"], p["hi"], p["date"]), ("highest", "RKSI", "C", 15, 15, "2026-11-26"))
        p = W.parse_market("Will the highest temperature in Dallas be between 54-55°F on November 26?", ts("2026-11-25 00:00"))
        self.assertEqual((p["icao"], p["unit"], p["lo"], p["hi"]), ("KDAL", "F", 54, 55))
        p = W.parse_market("Will the highest temperature in Seoul (Incheon) be 9°C or below on November 26?", ts("2026-11-25 00:00"))
        self.assertEqual((p["lo"], p["hi"]), (None, 9))
        p = W.parse_market("Will the highest temperature in Helsinki be 1°C or higher on November 28?", ts("2026-11-25 00:00"))
        self.assertEqual((p["lo"], p["hi"]), (1, None))
        p = W.parse_market("Will the lowest temperature in Paris be -1°C on November 28?", ts("2026-11-25 00:00"))
        self.assertEqual((p["kind"], p["icao"], p["lo"], p["hi"]), ("lowest", "LFPB", -1, -1))
        p = W.parse_market("Will the highest temperature in New York City be between 50-51°F on November 28?", ts("2026-11-25 00:00"))
        self.assertEqual(p["icao"], "KLGA")
        self.assertIsNone(W.parse_market("Will the lowest temperature in Hong Kong be 20°C on November 28?"))   # not a METAR
        self.assertIsNone(W.parse_market("Will Nate Sheppard be the 2026 D1 Rushing Yards Leader?"))
        self.assertIsNone(W.parse_market("Will the highest temperature in Dallas be 15°C on November 26?"))     # wrong unit

    def test_year_rollover(self):
        p = W.parse_market("Will the highest temperature in Seoul (Incheon) be 1°C on January 2?", ts("2026-12-30 00:00"))
        self.assertEqual(p["date"], "2027-01-02")

    def test_local_day(self):
        a, b = W.day_bounds("2026-11-26", "Asia/Seoul")
        self.assertEqual((a, b), (ts("2026-11-25 15:00"), ts("2026-11-26 15:00")))


class ResolutionRulesTest(unittest.TestCase):
    def test_metar_temperature(self):
        self.assertEqual(W.metar_temp("KDAL 260053Z 34005KT 10SM CLR 08/M05 A3029 RMK AO2 SLP256 T00831050"), 8.3)
        self.assertEqual(W.metar_temp("METAR KDAL 260053Z 34005KT 10SM CLR M02/M05 A3029 RMK AO2 T10171022"), -1.7)
        self.assertEqual(W.metar_temp("RKSI 260000Z 16010KT 9999 BKN040 10/07 Q1019 NOSIG"), 10)      # no remark: body
        self.assertEqual(W.metar_temp("RKSI 260000Z 16010KT 9999 BKN040 M11/M17 Q1019 NOSIG"), -11)
        self.assertIsNone(W.metar_temp("SPECI KDAL 260110Z 34005KT 10SM CLR 08/M05 A3029 RMK AO2 T00901050"))
        self.assertIsNone(W.metar_temp("RKSI 260000Z 16010KT 9999 BKN040 Q1019"))

    def test_rounding(self):
        # the examples of the market descriptions
        self.assertEqual([W.to_unit(c, "C") for c in (13.9, 17.4, 16.5, -1.7, -8.5)], [14, 17, 17, -2, -8])
        self.assertEqual([W.to_unit(c, "F") for c in (18.3, 17.8, 19.0, -22.5)], [65, 64, 66, -8])

    def test_bucket_prob_observed(self):
        # day over (no hours left): the observation alone decides
        self.assertEqual(W.bucket_prob("highest", "C", 15, 15, 15.2, None, 1.0), 1.0)
        self.assertEqual(W.bucket_prob("highest", "C", 15, 15, 15.6, None, 1.0), 0.0)
        self.assertEqual(W.bucket_prob("highest", "F", 54, 55, 12.6, None, 1.0), 1.0)    # 54.68 F -> 55
        # a bucket below the running maximum is dead whatever the forecast
        self.assertEqual(W.bucket_prob("highest", "C", 12, 12, 13.0, 10.0, 2.0), 0.0)
        self.assertEqual(W.bucket_prob("lowest", "C", 5, 5, 3.0, 8.0, 2.0), 0.0)

    def test_bucket_prob_sums_to_one(self):
        edges = [(None, 9)] + [(k, k) for k in range(10, 19)] + [(19, None)]
        for m_obs in (None, 11.0, 14.4):
            tot = sum(W.bucket_prob("highest", "C", lo, hi, m_obs, 14.0, 1.5) for lo, hi in edges)
            self.assertAlmostEqual(tot, 1.0, places=9)
        edges = [(None, 45)] + [(k, k + 1) for k in range(46, 70, 2)] + [(70, None)]
        tot = sum(W.bucket_prob("lowest", "F", lo, hi, None, 12.0, 2.0) for lo, hi in edges)
        self.assertAlmostEqual(tot, 1.0, places=9)

    def test_bucket_prob_forecast(self):
        p = W.bucket_prob("highest", "C", 15, 15, None, 15.0, 1.0)      # 14.5 <= v < 15.5
        self.assertAlmostEqual(p, W._phi(0.5) - W._phi(-0.5), places=9)


class WxFairTest(unittest.TestCase):
    def test_value_from_sources(self):
        q = "Will the highest temperature in Seoul (Incheon) be 15°C on November 26?"
        now = ts("2026-11-26 03:00")                       # 12:00 in Seoul

        def metar_fn(icaos, t):
            return {"RKSI": [(ts("2026-11-26 00:00"), "RKSI 260000Z 16010KT 9999 BKN040 10/07 Q1019"),
                             (ts("2026-11-26 02:30"), "RKSI 260230Z 17013KT 9999 BKN025 13/07 Q1019"),
                             (ts("2026-11-26 02:40"), "SPECI RKSI 260240Z 17013KT 9999 BKN025 16/07 Q1019"),
                             (ts("2026-11-25 14:00"), "RKSI 251400Z 17013KT 9999 BKN025 17/07 Q1019")]}   # day before

        def runs_fn(model, pts, t):
            hours = [ts("2026-11-26 00:00") + 3600 * i for i in range(15)]
            return t - 6 * 3600, {"RKSI": [(h, 14.0 + (0.5 if model == "gfs_seamless" else 0.0)) for h in hours]}
        f = W.WxFair(metar_fn, runs_fn, clock=lambda: now)
        f.refresh([q], now)
        v = f.value(q, now)
        self.assertEqual((v["m_obs"], v["obs"], v["models"]), (13, 2, 2))   # the SPECI and the day before do not count
        self.assertAlmostEqual(v["mu"], 14.25)
        self.assertAlmostEqual(v["h_end"], 12.0)
        self.assertGreater(v["p"], 0.2)
        self.assertIsNone(f.value("Will Nate Sheppard be the 2026 D1 Rushing Yards Leader?", now))

    def test_day_over(self):
        q = "Will the highest temperature in Seoul (Incheon) be 13°C on November 26?"
        now = ts("2026-11-26 16:00")

        def metar_fn(icaos, t):
            return {"RKSI": [(ts("2026-11-26 05:00"), "RKSI 260500Z 16010KT 9999 BKN040 13/07 Q1019")]}

        def runs_fn(model, pts, t):
            return t, {"RKSI": []}
        f = W.WxFair(metar_fn, runs_fn, clock=lambda: now)
        f.refresh([q], now)
        self.assertEqual(f.value(q, now)["p"], 1.0)


if __name__ == "__main__":
    unittest.main()


class ObsTimeTest(unittest.TestCase):
    def test_observation_time_from_the_report(self):
        # aviationweather's reportTime of 232050Z is 2100Z: the report is at 23:50 local in Ankara (UTC+3), on 23 Nov
        raw = "METAR LTAC 232050Z 22022KT 9999 SCT040 BKN080 13/04 Q1009 TEMPO -TSRA"
        self.assertEqual(W.obs_time(raw, ts("2026-11-23 21:00")), ts("2026-11-23 20:50"))
        a, b = W.day_bounds("2026-11-24", "Europe/Istanbul")
        self.assertLess(W.obs_time(raw, ts("2026-11-23 21:00")), a)       # not in the 24 Nov local day
        # across a month end
        self.assertEqual(W.obs_time("KDAL 302353Z 34005KT 08/M05", ts("2026-12-01 00:00")), ts("2026-11-30 23:53"))
        self.assertEqual(W.obs_time("no time group", 123.0), 123.0)
