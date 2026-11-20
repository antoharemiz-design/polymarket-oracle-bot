"""Server service (polybot/service.py): run lifecycle, resume after restart, pause, live refusal, status.md."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from polybot import lp, service

MARKETS = [{"cond": "C1", "yes": "Y1", "no": "N1", "N": 20.0, "v": 4.5, "min": 20.0, "rate": 10.0, "tick": 0.01,
            "q": "Will the test market resolve YES?"}] * 1
BOOK = {"Y1": ([(0.48, 100.0)], [(0.52, 100.0)], 0.01)}


class Env:
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def make(tmp, mode="paper", **kw):
    mfile = os.path.join(tmp, "markets_in.json")
    with open(mfile, "w") as f:
        json.dump({"markets": [dict(m) for m in MARKETS]}, f)
    base = {"POLYBOT_MARKETS": mfile, "LP_POLL_S": "0.01", "LP_MARKETS_MIN": "1", "LP_SAMPLE_S": "0.05",
            "LP_WINDOW_S": "1800", "LP_PHASE": "1"}
    base.update(kw)
    return Env(**base), dict(data=tmp, mode=mode, books_fn=lambda toks: BOOK,
                             rewards_fn=lambda: {"C1": {"v": 4.5, "min": 20.0, "rate": 10.0}}, trades_fn=lambda c: [],
                             geoblock_fn=lambda: {"blocked": False, "country": "FI"})


async def run_for(svc, seconds):
    stop = asyncio.Event()

    async def later():
        await asyncio.sleep(seconds)
        stop.set()
    t = asyncio.create_task(later())
    await svc.run(stop)
    t.cancel()


def rj(path):
    with open(path) as f:
        return json.load(f)


def events(tmp, mode="paper"):
    return lp.load_events(os.path.join(tmp, mode), mode)


class ServiceTest(unittest.TestCase):
    def test_run_completes_and_then_idles(self):
        tmp = tempfile.mkdtemp()
        e, kw = make(tmp, LP_TEST_DAYS=str(0.3 / 86400))
        with e:
            svc = service.Service(**kw)
            asyncio.run(run_for(svc, 1.0))
        run = rj((os.path.join(tmp, "paper", "run.json")))
        self.assertEqual(run["reason"], "time")
        st = rj((os.path.join(tmp, "service_status.json")))
        self.assertEqual(st["phase"], "complete")
        ev = events(tmp)
        self.assertEqual([x["reason"] for x in ev if x["event"] == "end"], ["time"])
        self.assertIn("place", [x["event"] for x in ev])
        # a restart after completion does not start a new run
        with e:
            asyncio.run(run_for(service.Service(**kw), 0.2))
        self.assertEqual(sum(1 for x in events(tmp) if x["event"] == "start"), 1)

    def test_stop_then_resume_for_the_time_left(self):
        tmp = tempfile.mkdtemp()
        e, kw = make(tmp, LP_TEST_DAYS=str(30 / 86400))
        with e:
            asyncio.run(run_for(service.Service(**kw), 0.3))
            run1 = rj((os.path.join(tmp, "paper", "run.json")))
            self.assertNotIn("completed", run1)
            asyncio.run(run_for(service.Service(**kw), 0.3))
        run2 = rj((os.path.join(tmp, "paper", "run.json")))
        self.assertEqual(run1["started"], run2["started"])            # same run, resumed
        ev = events(tmp)
        self.assertEqual([x["reason"] for x in ev if x["event"] == "end"], ["cancelled", "cancelled"])
        starts = [x for x in ev if x["event"] == "start"]
        self.assertEqual(len(starts), 2)
        self.assertLess(starts[1]["config"]["test_days"], 30 / 86400)  # only the time left

    def test_resume_restores_positions(self):
        tmp = tempfile.mkdtemp()
        e, kw = make(tmp, LP_TEST_DAYS=str(30 / 86400))
        with e:
            asyncio.run(run_for(service.Service(**kw), 0.3))
            with open(os.path.join(tmp, "paper", "paper.jsonl"), "a") as f:     # a fill recorded before the restart
                f.write(json.dumps({"event": "fill", "ts": time.time(), "mkt": "C1", "side": "bid", "price": 0.48,
                                    "size": 20.0, "fill_ts": time.time(), "order": "x"}) + "\n")
            svc = service.Service(**kw)
            asyncio.run(run_for(svc, 0.3))
        self.assertAlmostEqual(svc.test.st["C1"].yes, 20.0)
        self.assertIn("restored", [x["event"] for x in events(tmp)])

    def test_control_stop_pulls_quotes(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "control.json"), "w") as f:
            json.dump({"mode": "stop"}, f)
        e, kw = make(tmp, LP_TEST_DAYS=str(30 / 86400))
        with e:
            svc = service.Service(**kw)
            asyncio.run(run_for(svc, 0.3))
        ev = events(tmp)
        self.assertIn("pause", [x["event"] for x in ev])
        self.assertNotIn("place", [x["event"] for x in ev])

    def test_live_refused_without_permission(self):
        tmp = tempfile.mkdtemp()
        e, kw = make(tmp, mode="live", POLYBOT_ALLOW_LIVE="", POLYMARKET_PRIVATE_KEY="", POLYBOT_BRANCH="dev")
        with e:
            asyncio.run(run_for(service.Service(**kw), 0.2))
        st = rj((os.path.join(tmp, "service_status.json")))
        self.assertEqual(st["phase"], "refused")
        for word in ("POLYBOT_ALLOW_LIVE", "POLYMARKET_PRIVATE_KEY", "main branch"):
            self.assertIn(word, st["detail"])
        self.assertFalse(os.path.exists(os.path.join(tmp, "live", "run.json")))

    def test_live_refused_when_geoblocked(self):
        tmp = tempfile.mkdtemp()
        e, kw = make(tmp, mode="live", POLYBOT_ALLOW_LIVE="yes", POLYMARKET_PRIVATE_KEY="0xabc", POLYBOT_BRANCH="main")
        kw["geoblock_fn"] = lambda: {"blocked": True, "country": "US"}
        with e:
            asyncio.run(run_for(service.Service(**kw), 0.2))
        st = rj((os.path.join(tmp, "service_status.json")))
        self.assertEqual(st["phase"], "refused")
        self.assertIn("blocks this server", st["detail"])

    def test_status_md(self):
        tmp = tempfile.mkdtemp()
        e, kw = make(tmp, LP_TEST_DAYS=str(0.3 / 86400))
        with e:
            asyncio.run(run_for(service.Service(**kw), 0.8))
        with open(os.path.join(tmp, "health_state.json"), "w") as f:
            json.dump({"ts": time.time(), "count": 0, "alert": False}, f)
        md = service.render_status(tmp)
        for s in ("# polybot — статус", "| Бот |", "complete", "## Ход прогона", "## Рынки", "Will the test market", "## Лог"):
            self.assertIn(s, md)

    def test_day_selection_stops_and_resumes(self):
        tmp = tempfile.mkdtemp()
        rew = {"CB": {"rate": 10.0, "v": 4.5, "min": 20.0}}
        meta = {"CB": {"question": "Will the test market B resolve YES?", "active": True, "closed": False,
                       "acceptingOrders": True, "clobTokenIds": json.dumps(["YB", "NB"]), "createdAt": "2026-01-01T00:00:00Z",
                       "endDate": "2027-06-01T00:00:00Z", "events": [{"id": "EB"}]}}
        busy = {"YB": ([(0.48, 100.0), (0.47, 100.0)], [(0.52, 100.0), (0.53, 100.0)], 0.01)}
        base = {"POLYBOT_MARKETS": "", "LP_PHASE": "1", "LP_SELECT_SNAPSHOTS": "2", "LP_SELECT_GAP_S": "3600", "LP_MARKETS_MIN": "1",
                "LP_TEST_DAYS": str(30 / 86400), "LP_POLL_S": "0.01", "LP_SAMPLE_S": "0.05"}
        kw = dict(data=tmp, mode="paper", books_fn=lambda toks: busy, rewards_fn=lambda: rew, trades_fn=lambda c: [],
                  geoblock_fn=lambda: {"blocked": False, "country": "FI"})
        snaps = os.path.join(tmp, "paper", "selection", "snapshots.jsonl")
        with mock.patch.object(lp, "rewards_config", lambda: rew), mock.patch.object(lp, "gamma_markets", lambda rw: meta), \
                mock.patch.object(lp, "books", lambda toks: busy):
            with Env(**base):
                t0 = time.time()
                asyncio.run(run_for(service.Service(**kw), 0.5))   # stop while waiting 1 h for snapshot 2
                self.assertLess(time.time() - t0, 10)
                with open(snaps) as f:
                    self.assertEqual(len(f.readlines()), 1)
                self.assertFalse(os.path.exists(os.path.join(tmp, "paper", "run.json")))
                self.assertIn("снимок 1 из 2", service.render_status(tmp))
            with Env(**dict(base, LP_SELECT_GAP_S="0.01")):
                asyncio.run(run_for(service.Service(**kw), 1.0))   # continues: one more snapshot, then quoting
        with open(snaps) as f:
            self.assertEqual(len(f.readlines()), 2)
        self.assertTrue(os.path.exists(os.path.join(tmp, "paper", "run.json")))
        self.assertEqual([m["cond"] for m in rj(os.path.join(tmp, "paper", "markets.json"))["markets"]], ["CB"])
        ev = events(tmp)
        self.assertIn("place", [x["event"] for x in ev])
        self.assertGreater(next(x for x in ev if x["event"] == "start")["quote_cap"], 0)


class Phase2ServiceTest(unittest.TestCase):
    """Phase 2: warm-up (snapshots, no quotes), then quoting with rebalances; stop and resume keep everything."""

    def setup(self):
        tmp = tempfile.mkdtemp()
        end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5 * 86400))
        rew = {"CB": {"rate": 10.0, "v": 4.5, "min": 20.0}, "CX": {"rate": 10.0, "v": 4.5, "min": 20.0}}
        gm = {"CB": {"conditionId": "CB", "question": "Will the high in Paris be 15C?", "events": [{"id": "EB"}],
                     "clobTokenIds": json.dumps(["YB", "NB"]), "feesEnabled": True, "feeType": "weather_fees",
                     "feeSchedule": {"rate": 0.05, "rebateRate": 0.25}, "createdAt": "2026-01-01T00:00:00Z",
                     "endDate": end, "active": True, "closed": False, "acceptingOrders": True},
              "CX": {"conditionId": "CX", "question": "Will US GDP growth be above 2%?", "events": [{"id": "EX"}],
                     "clobTokenIds": json.dumps(["YX", "NX"]), "feesEnabled": True, "feeType": "economics_fees",
                     "feeSchedule": {"rate": 0.05, "rebateRate": 0.25}, "createdAt": "2026-01-01T00:00:00Z",
                     "endDate": end, "active": True, "closed": False, "acceptingOrders": True}}
        busy = {t: ([(0.48, 100.0), (0.47, 100.0)], [(0.52, 100.0), (0.53, 100.0)], 0.01) for t in ("YB", "YX")}
        base = {"POLYBOT_MARKETS": "", "LP_PHASE": "2", "LP_LOOKBACK_H": str(1800 * 2 / 3600), "LP_SNAPSHOT_GAP_S": "1800",
                "LP_TEST_DAYS": str(30 / 86400), "LP_POLL_S": "0.01", "LP_SAMPLE_S": "0.05"}
        kw = dict(data=tmp, mode="paper", books_fn=lambda toks: {t: busy[t] for t in toks if t in busy}, rewards_fn=lambda: rew,
                  trades_fn=lambda c: [], geoblock_fn=lambda: {"blocked": False, "country": "FI"}, gamma_fn=lambda conds: gm)
        return tmp, base, kw

    def test_warm_up_stops_resumes_then_quotes(self):
        tmp, base, kw = self.setup()
        rows = os.path.join(tmp, "paper", "snaps", "rows.jsonl")
        with Env(**base):
            t0 = time.time()
            asyncio.run(run_for(service.Service(**kw), 0.5))   # stop while waiting 30 min for snapshot 2
            self.assertLess(time.time() - t0, 10)
            with open(rows) as f:
                self.assertEqual(len(f.readlines()), 1)
            self.assertFalse(os.path.exists(os.path.join(tmp, "paper", "run.json")))
            st = service.render_status(tmp)
            self.assertIn("прогрев без котировок: снимок 1 из 2", st)
        with Env(**dict(base, LP_SNAPSHOT_GAP_S="0.2", LP_LOOKBACK_H=str(0.4 / 3600))):
            asyncio.run(run_for(service.Service(**kw), 2.0))   # continues: one more snapshot, then quoting
        run = rj(os.path.join(tmp, "paper", "run.json"))
        self.assertEqual(run["phase"], 2)
        ev = events(tmp)
        reb = [x for x in ev if x["event"] == "rebalance"]
        self.assertTrue(reb)
        self.assertEqual(reb[0]["target"], ["CB"])         # economics excluded
        self.assertEqual(reb[0]["markets"]["CB"]["no"], "NB")
        self.assertIn("place", [x["event"] for x in ev])
        self.assertIn("snapshot", [x["event"] for x in ev])
        with Env(**dict(base, LP_SNAPSHOT_GAP_S="0.2", LP_LOOKBACK_H=str(0.4 / 3600))):
            st = service.render_status(tmp)
        self.assertIn("Ход прогона (фаза 2)", st)
        self.assertIn("Will the high in Paris", st)
        self.assertIn("на $100 котировок в день", st)

    def test_resume_phase2_rebuilds_portfolio(self):
        tmp, base, kw = self.setup()
        fast = dict(base, LP_SNAPSHOT_GAP_S="0.2", LP_LOOKBACK_H=str(0.4 / 3600), LP_TEST_DAYS=str(60 / 86400))
        with Env(**fast):
            asyncio.run(run_for(service.Service(**kw), 1.5))
            asyncio.run(run_for(service.Service(**kw), 0.5))
        ev = events(tmp)
        self.assertEqual(sum(1 for x in ev if x["event"] == "start"), 2)
        self.assertIn("restored", [x["event"] for x in ev])
        self.assertEqual(rj(os.path.join(tmp, "paper", "run.json"))["phase"], 2)

    def test_phase1_run_keeps_phase1_settings(self):
        tmp = tempfile.mkdtemp()
        env, kw = make(tmp)
        with env:
            asyncio.run(run_for(service.Service(**kw), 0.3))  # a phase 1 run (LP_PHASE=1)
        run = rj(os.path.join(tmp, "paper", "run.json"))
        self.assertEqual(run["phase"], 1)
        env2, _ = make(tmp, LP_PHASE="2")
        with env2:
            asyncio.run(run_for(service.Service(**kw), 0.3))  # the server now defaults to phase 2: the run stays phase 1
        starts = [x for x in events(tmp) if x["event"] == "start"]
        self.assertEqual(starts[-1]["config"]["phase"], 1)
        self.assertEqual(starts[-1]["config"]["quote_budget_usd"], 100.0)


if __name__ == "__main__":
    unittest.main()
