"""The live path against the real polymarket-client SDK on the simulated exchange (polybot/simexchange.py).

On 25 Nov every live taker close failed inside the SDK ("amount is required for BUY market orders") and no pair
was merged without a relayer key, while the old tests passed with a fake client that accepted any arguments. These
tests run the SDK's own validation, signing and request building; they need polymarket-client (requirements.txt;
the server installs it and runs them before every deploy) and are skipped without it.
"""
import asyncio
import importlib.util
import os
import sys
import unittest

from polybot import lp

HAVE_SDK = importlib.util.find_spec("polymarket") is not None and importlib.util.find_spec("eth_account") is not None
if HAVE_SDK:
    from eth_account import Account
    from polybot.simexchange import SimExchange


def market(sim):
    return {"cond": sim.cond, "yes": sim.yes, "no": sim.no, "fee_rate": 0.0, "tick": sim.tick}


@unittest.skipUnless(HAVE_SDK, "polymarket-client not installed")
class SimExchangeTest(unittest.TestCase):
    def run_with(self, body, relayer=True, **kw):
        sim = SimExchange(relayer=relayer, **kw)
        log = []

        async def go():
            with sim.patch():
                ex = await lp.LiveExec.create(Account.create().key.hex(), None, "rk" if relayer else None,
                                              "0x" + "33" * 20 if relayer else None, log=log.append)
                try:
                    return await body(sim, ex, market(sim))
                finally:
                    await ex.c.close()
        return sim, asyncio.run(go()), log

    def test_the_25_nov_call_fails_in_the_sdk(self):
        async def body(sim, ex, m):
            with self.assertRaisesRegex(Exception, "amount is required for BUY market orders"):
                await ex.c.place_market_order(token_id=sim.no, side="BUY", shares="20", max_price="0.55",
                                              order_type="FAK")
        self.run_with(body)

    def test_quote_fill_and_sell_to_reduce(self):
        async def body(sim, ex, m):
            bid = await ex.place(m, "bid", 0.48, 20.0)
            self.assertIn(bid, sim.orders)
            sim.trade(sim.yes, "SELL", 0.48, 20.0)
            f = await ex.fills([m])
            self.assertEqual([(x["side"], x["size"], x["sell"]) for x in f], [("bid", 20.0, False)])
            ask = await ex.place(m, "ask", 0.51, 20.0, sell=True)
            o = sim.orders[ask]
            self.assertEqual((o["token"], o["side"], o["price"]), (sim.yes, "SELL", 0.51))
            sim.trade(sim.yes, "BUY", 0.51, 20.0)
            f = await ex.fills([m])
            self.assertEqual([(x["side"], x["size"], x["sell"]) for x in f], [("ask", 20.0, True)])
            return await ex.balances(m), await ex.collateral()
        sim, (bal, usdc), _ = self.run_with(body)
        self.assertEqual(bal, (0.0, 0.0))                   # no YES + NO pair: nothing left to merge
        self.assertAlmostEqual(usdc, 300.0 + 20 * 0.03)

    def test_taker_close_sells_exact_shares(self):
        async def body(sim, ex, m):
            await ex.place(m, "bid", 0.48, 20.0)
            sim.trade(sim.yes, "SELL", 0.48, 20.0)
            await ex.fills([m])
            r = await ex.take(m, "ask", 20.0, [(0.48, 500.0)], [(0.52, 500.0)], wait_s=1.5, sell=True)
            return r, await ex.balances(m)
        sim, (r, bal), log = self.run_with(body)
        self.assertEqual((r["size"], r["sell"]), (20.0, True))
        self.assertAlmostEqual(r["price"], 0.48)
        self.assertEqual(bal, (0.0, 0.0))
        self.assertTrue(any(x.startswith("taker close success") for x in log))

    def test_taker_close_respects_the_floor(self):
        async def body(sim, ex, m):
            await ex.place(m, "bid", 0.48, 20.0)
            sim.trade(sim.yes, "SELL", 0.48, 20.0)
            await ex.fills([m])
            held = await ex.balances(m)
            above = await ex.take(m, "ask", 20.0, [(0.48, 500.0)], [(0.52, 500.0)], wait_s=1.5, sell=True, limit=0.50)
            kept = await ex.balances(m)
            within = await ex.take(m, "ask", 20.0, [(0.48, 500.0)], [(0.52, 500.0)], wait_s=1.5, sell=True, limit=0.40)
            return held, above, kept, within, await ex.balances(m)
        sim, (held, above, kept, within, bal), log = self.run_with(body)
        self.assertEqual(above["unfilled"], "no orders within the limit")   # best bid 0.48 under the floor 0.50
        self.assertEqual(kept, held)                         # nothing was sent
        self.assertEqual((within["size"], bal), (20.0, (0.0, 0.0)))
        self.assertTrue(any(x.startswith("taker close success") and "floor 0.46" in x for x in log))

    def test_taker_close_of_a_long_no(self):
        async def body(sim, ex, m):
            await ex.place(m, "ask", 0.52, 20.0)            # BUY NO at 0.48
            sim.trade(sim.no, "SELL", 0.48, 20.0)
            await ex.fills([m])
            r = await ex.take(m, "bid", 20.0, [(0.48, 500.0)], [(0.52, 500.0)], wait_s=1.5, sell=True)
            return r, await ex.balances(m)
        sim, (r, bal), log = self.run_with(body)
        self.assertEqual(bal, (0.0, 0.0))
        self.assertAlmostEqual(r["price"], 0.52)            # NO sold at 0.48 = YES at 0.52

    def test_buy_close_path_is_valid_too(self):
        async def body(sim, ex, m):
            sim.bal[sim.yes] = 20.0
            return await ex.take(m, "ask", 20.0, [(0.48, 500.0)], [(0.52, 500.0)], wait_s=1.5)
        sim, r, log = self.run_with(body)
        self.assertAlmostEqual(r["size"], 20.0)
        self.assertNotIn("closing order failed", " ".join(log))

    def test_merge_needs_a_relayer_key(self):
        async def body(sim, ex, m):
            sim.bal[sim.yes] = sim.bal[sim.no] = 10.0
            return await ex.merge(m, 10.0), await ex.balances(m)
        _, (done, bal), log = self.run_with(body, relayer=False)
        self.assertEqual((done, bal), (0.0, (10.0, 10.0)))
        self.assertIn("Builder API Key or Relayer API Key", log[-1])
        _, (done, bal), log = self.run_with(body, relayer=True)
        self.assertEqual((done, bal), (10.0, (0.0, 0.0)))
        self.assertTrue(log[-1].startswith("merge success"))

    def test_refused_order_keeps_the_market(self):
        async def body(sim, ex, m):
            keep = await ex.place(m, "bid", 0.47, 20.0)
            sim.bal["USDC"] = 1.0                           # no money left for the next order
            r = await ex.place(m, "ask", 0.53, 20.0)
            return keep, r
        sim, (keep, r), log = self.run_with(body)
        self.assertIsNone(r)
        self.assertIn(keep, sim.orders)                     # the other order was not cancelled
        self.assertIn("place rejected", log[-1])

    def test_whole_loop_scenario(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "research", "tools"))
        import live_path_sim
        log = []
        sim, t = asyncio.run(live_path_sim.scenario(log.append))
        text = "\n".join(log)
        self.assertGreaterEqual(text.count("taker close success"), 2)
        self.assertIn("merge success: 10.00 pairs", text)
        self.assertIn("liquidation done: 0 positions left", text)
        self.assertEqual(sim.orders, {})
        self.assertEqual((sim.bal[sim.yes], sim.bal[sim.no]), (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
