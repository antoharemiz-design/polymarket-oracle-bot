"""Server service (polybot/service.py): run lifecycle, resume after restart, pause, live refusal, status.md."""
import asyncio
import json
import os
import tempfile
import time
import unittest

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
            "LP_WINDOW_S": "1800"}
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


if __name__ == "__main__":
    unittest.main()
