"""Live executors against a fake SDK client: order placement, fills from size_matched, cancellation."""
import asyncio
import types
import unittest
from decimal import Decimal

from polybot.engine import MarketSession
from polybot.fairvalue import SpotSeries
from polybot.live import LiveMultiExec
from polybot.risk import Risk, RiskLimits
from polybot.strategy import Params

START = 1_000_000_200


class FakeClient:
    def __init__(self):
        self.placed, self.canceled, self.n = [], [], 0

    async def place_limit_order(self, **kw):
        self.n += 1; self.placed.append(kw)
        return types.SimpleNamespace(ok=True, order_id=f"o{self.n}")

    async def cancel_order(self, order_id):
        self.canceled.append(order_id)
        return types.SimpleNamespace(canceled=(order_id,), not_canceled={})

    async def cancel_market_orders(self, market):
        return types.SimpleNamespace(canceled=(), not_canceled={})


class FakeLive:
    def __init__(self):
        self.client, self.risk, self.by_order = FakeClient(), Risk(RiskLimits()), {}
        self.log = lambda *a: None

    def register(self, order_id, mexec, key):
        self.by_order[order_id] = (mexec, key)


def event(oid, matched, original, kind="UPDATE", price="0.48"):
    return types.SimpleNamespace(id=oid, size_matched=Decimal(matched), original_size=Decimal(original),
                                 order_event_type=kind, price=Decimal(price))


class LiveMultiExecTest(unittest.TestCase):
    def test_place_fill_cancel(self):
        async def run():
            live = FakeLive()
            ex = LiveMultiExec(live, {"token_up": "UP", "token_down": "DN", "condition_id": "C"})
            ses = MarketSession(START, Params(clip=10), SpotSeries(), ex, mode="ladder")
            ex.place_at("B", 48, 10, 0)
            ex.place_at("S", 53, 10, 0)                       # buy Down at 47c
            await asyncio.sleep(0)
            self.assertEqual([(p["token_id"], p["price"], p["post_only"]) for p in live.client.placed],
                             [("UP", "0.48", True), ("DN", "0.47", True)])
            self.assertEqual(ex.multi[("B", 48)].state, "LIVE")
            mexec, key = live.by_order["o1"]
            mexec.on_order_event(key, event("o1", "4", "10"))
            mexec.on_order_event(key, event("o1", "4", "10"))  # repeated update: no double count
            self.assertAlmostEqual(ses.res.q_up, 4)
            mexec.on_order_event(key, event("o1", "10", "10"))
            self.assertAlmostEqual(ses.res.q_up, 10)
            self.assertNotIn(("B", 48), ex.multi)               # fully filled: removed
            ex.cancel_at(("S", 53), 0)
            await asyncio.sleep(0)
            self.assertEqual(live.client.canceled, ["o2"])
            self.assertEqual(ex.multi, {})
            self.assertEqual(ses.res.canceled, 1)
            mexec2, key2 = live.by_order["o2"]                  # a fill reported after the cancel still counts
            mexec2.on_order_event(key2, event("o2", "3", "10", "CANCELLATION", price="0.47"))
            self.assertAlmostEqual(ses.res.q_dn, 3)
            self.assertAlmostEqual(ses.res.cost, 4 * 0.48 + 6 * 0.48 + 3 * 0.47)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
