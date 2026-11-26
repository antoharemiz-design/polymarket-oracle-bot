"""The live path end to end on the simulated exchange (polybot/simexchange.py): the bot's own loop (LPTest, phase 4)
with LiveExec and the real polymarket-client SDK, without network and without money (26 Nov, after the live loss).

    python research/tools/live_path_sim.py            # needs polymarket-client (the server's venv has it)

Scenario, one simulated market (book 0.48 / 0.52):
  1. quotes both sides; another trader sells 20 YES into our bid (fill);
  2. the ask becomes a SELL of the 20 YES held (sell_to_reduce): no YES + NO pair is ever formed;
  3. nobody takes it for two hours (inv_flatten_h): the position is closed by a market SELL (taker close);
  4. a YES + NO pair made outside the bot (as by hand on the site) is merged through the relayer;
  5. a new position, then a loss beyond the stop: every quote is cancelled and the position is liquidated.
Prints the bot's log; the lines to look for: "taker close success", "merge success", "liquidation done".
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from polybot import lp  # noqa: E402
from polybot.simexchange import SimExchange  # noqa: E402


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def books(sim, ex):
    """The YES book as the exchange shows it: the other orders and ours (LPTest removes ours itself)."""
    bids = {p: z for p, z in sim.book[sim.yes]["BUY"]}
    asks = {p: z for p, z in sim.book[sim.yes]["SELL"]}
    for o in sim.orders.values():
        if o["token"] == sim.yes:
            d = bids if o["side"] == "BUY" else asks
            p = o["price"]
        else:                                           # NO orders show in the YES book at 1 - price
            d = asks if o["side"] == "BUY" else bids
            p = round(1 - o["price"], 6)
        d[p] = d.get(p, 0.0) + o["left"]
    return (sorted(bids.items(), key=lambda x: -x[0]), sorted(asks.items()), sim.tick)


async def scenario(log=print, relayer=True):
    clock = Clock()
    sim = SimExchange(usdc=300.0, relayer=relayer, clock=clock)
    from eth_account import Account
    with sim.patch():
        ex = await lp.LiveExec.create(Account.create().key.hex(), None, "relayer-key" if relayer else None,
                                      "0x" + "33" * 20 if relayer else None, log=log)
        m = {"cond": sim.cond, "yes": sim.yes, "no": sim.no, "N": 20.0, "k": 1, "v": 4.5, "min": 20.0, "rate": 10.0,
             "tick": sim.tick, "q": "simulated market", "end": None, "fee_rate": 0.0, "rebate_rate": 0.0,
             "quote_cap": 20.0, "m1_usd_day": 1.0}
        d = tempfile.mkdtemp()
        cfg = lp.phase4_config(lp_dir=d, poll_s=0.0, sample_s=0.0, fill_cooldown_s=0.0, market_loss_usd=0.0,
                               markout_kill_usd=0.0, equity_stop=0)
        t = lp.LPTest(cfg, ex, lp.Journal(os.path.join(d, "live.jsonl"), clock=clock), [m],
                      books_fn=lambda toks: {sim.yes: books(sim, ex)}, log=log, clock=clock)
        rw = {sim.cond: {"v": 4.5, "min": 20.0, "rate": 10.0}}

        async def step(dt=0.0):
            clock.t += dt
            await t.step(clock.t, rw)
        s = t.st[sim.cond]

        log("--- 1. quotes, and another trader sells 20 YES into our bid")
        await step()
        log(f"orders: bid {s.orders['bid'][1:4] if s.orders['bid'] else None}, ask {s.orders['ask'][1:4] if s.orders['ask'] else None}")
        sim.trade(sim.yes, "SELL", 0.48, 20.0)
        await step(5)
        log(f"position YES {s.yes:.2f} / NO {s.no:.2f}; wallet {await ex.balances(m)}")
        await step(5)
        o = sim.orders.get(s.orders["ask"][0]) if s.orders["ask"] else None
        log(f"--- 2. the ask is now a {o['side'] if o else '?'} of {o['token'][:1] == '1' and 'YES' or 'NO'} "
            f"{o['left'] if o else 0:.2f} at {o['price'] if o else 0}")

        log("--- 3. nobody takes it for two hours: taker close")
        await step(cfg.inv_flatten_h * 3600)
        log(f"position YES {s.yes:.2f} / NO {s.no:.2f}; wallet {await ex.balances(m)}; USDC {await ex.collateral():.2f}")

        log("--- 4. a YES + NO pair made outside the bot is merged through the relayer")
        sim.bal[sim.yes] += 10.0; sim.bal[sim.no] += 10.0; sim.bal["USDC"] -= 10.0
        await t.reconcile()
        await step(5)
        log(f"position YES {s.yes:.2f} / NO {s.no:.2f}; wallet {await ex.balances(m)}; USDC {await ex.collateral():.2f}")

        log("--- 5. a new position, then a loss beyond the stop: cancel everything and liquidate")
        await step(5)
        if s.orders["bid"]:
            sim.trade(sim.yes, "SELL", s.orders["bid"][1], 20.0)
        await step(5)
        log(f"position YES {s.yes:.2f} / NO {s.no:.2f}")
        s.cash -= 100.0                                 # stands for a crash of the market price
        await step(60)
        await step(5)
        log(f"stopped: {t.stopped}; liquidating: {t.liquidating}; position YES {s.yes:.2f} / NO {s.no:.2f}; "
            f"wallet {await ex.balances(m)}; resting orders {len(sim.orders)}")
        await ex.c.close()
        return sim, t


def main():
    asyncio.run(scenario(lambda msg: print(msg, flush=True)))


if __name__ == "__main__":
    main()
