"""Orchestrator: one MarketSession per market window, in paper or live mode."""
import asyncio
import json
import time
import urllib.request

from .config import Config
from .engine import MarketSession, PaperExecutor
from .fairvalue import SpotSeries
from .feeds import market_feed, now_ms, spot_feed
from .journal import Journal
from .markets import DUR, lookup, resolve
from .risk import Risk

GRID_S = 0.05
KLINES = "https://data-api.binance.vision/api/v3/klines?symbol={sym}&interval=1s&limit=1000"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def backfill_spot(spot: SpotSeries, first_live_ms: int, first_live_mid: float, symbol="BTCUSDT"):
    """Seed ~16 minutes of 1s history so volatility is known right after start.

    The history comes from Binance and is shifted to the level of the live source, so the splice
    does not show up as a price jump. Only volatility is taken from it: fair value never uses the
    model's level (see strategy.Fair).
    """
    req = urllib.request.Request(KLINES.format(sym=symbol), headers={"User-Agent": "polybot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        rows = [k for k in json.load(r) if int(k[6]) < first_live_ms]
    if not rows:
        return None
    shift = first_live_mid - float(rows[-1][4])
    hist = SpotSeries(spot.vol_lookback, spot.keep)
    for k in rows:
        hist.update(int(k[6]), float(k[4]) + shift)     # close time, shifted close
    return hist, len(rows)


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.spot = SpotSeries()
        self.spot_ms = 0
        self.journal = Journal(cfg.journal_dir, cfg.mode)
        self.risk = Risk(cfg.limits)
        self.live = None
        self.started = set()
        self.totals = {"markets": 0, "pnl": 0.0, "rebate": 0.0, "shares": 0.0, "fills": 0}

    def on_mid(self, t, mid):
        self.spot.update(t, mid)

    def on_spot_alive(self, t):
        self.spot_ms = t

    async def run(self, max_markets: int | None = None):
        cfg = self.cfg
        dur = DUR[cfg.tf]
        first = asyncio.get_running_loop().create_future()

        def first_tick(t, mid):
            if not first.done():
                first.set_result((t, mid))
            else:
                self.on_mid(t, mid)

        tasks = [asyncio.create_task(spot_feed(first_tick, cfg.asset, log, self.on_spot_alive, cfg.spot_source))]
        t0, mid0 = await first
        try:
            got = await asyncio.to_thread(backfill_spot, self.spot, t0, mid0, cfg.asset.upper() + "USDT")
            if got:
                self.spot, n = got
                log(f"volatility history seeded with {n} seconds")
        except Exception as e:
            log(f"history seed failed ({e!r}); quoting starts after ~6 minutes of live data")
        self.on_mid(t0, mid0)
        if cfg.mode == "live":
            from .live import LiveClient
            self.live = await LiveClient(cfg.private_key, cfg.wallet, cfg.relayer_key, cfg.relayer_address,
                                         self.risk, log, cfg.heartbeat).start()
        self.journal.write(now_ms(), 0, "start", strategy=cfg.strategy, taker_theta=cfg.taker_theta,
                           params=vars(cfg.params), limits=vars(cfg.limits), lat_ms=cfg.lat_ms,
                           queue_mult=cfg.queue_mult, spot_source=cfg.spot_source)
        sessions = []
        try:
            while max_markets is None or len(self.started) < max_markets or any(not s.done() for s in sessions):
                now = time.time()
                nxt = (int(now) // dur + 1) * dur
                if nxt not in self.started and now >= nxt - 40 and (max_markets is None or len(self.started) < max_markets):
                    self.started.add(nxt)
                    sessions.append(asyncio.create_task(self.session(nxt, dur)))
                await asyncio.sleep(0.5)
        finally:
            for t in tasks:
                t.cancel()
            if self.live is not None:
                await self.live.close()
            self.journal.write(now_ms(), 0, "stop", **self.totals)
            self.journal.close()
        return self.totals

    async def session(self, start: int, dur: int):
        cfg = self.cfg
        market = None
        for _ in range(10):
            try:
                market = await asyncio.to_thread(lookup, cfg.asset, cfg.tf, start)
                if market:
                    break
            except Exception as e:
                log(f"lookup {start}: {e!r}")
            await asyncio.sleep(2)
        if not market:
            log(f"{start}: market not found, skipped"); return
        ok, why = self.risk.may_open(now_ms())
        if cfg.mode == "live":
            ex = self.live_exec(market)
        else:
            ex = PaperExecutor(None, cfg.lat_ms, cfg.queue_mult)
        ses = MarketSession(start, cfg.params, self.spot, ex, dur, cfg.strategy, self.journal, cfg.taker_theta)
        if cfg.mode != "live":
            ex.book = ses.book
        feed = _Feed(ses, ex, cfg.mode)
        ftask = asyncio.create_task(market_feed(market["token_up"], market["token_down"], feed, start + dur + 10, log))
        self.journal.write(now_ms(), start, "session_open", slug=market["slug"], allowed=ok, reason=why)
        try:
            await asyncio.sleep(max(0.0, start - time.time()))
            while time.time() < start + dur:
                t = now_ms()
                halt = "" if ok else why
                halt = halt or self.risk.halt_reason(t, start, ses.res.cost, self.spot_ms, feed.last_ms)
                if halt and not ses.halted:
                    self.journal.write(t, start, "halt", reason=halt)
                ses.halted = bool(halt)
                ses.on_timer(t)
                await asyncio.sleep(GRID_S)
            if cfg.mode == "live":
                await ex.close()
                await asyncio.sleep(20)          # let the last matches settle on chain
                await ex.reconcile()
        finally:
            ftask.cancel()
        # resolution
        outcome = None
        for _ in range(180):
            await asyncio.sleep(10)
            try:
                outcome = await asyncio.to_thread(resolve, cfg.asset, cfg.tf, start, market["condition_id"])
            except Exception:
                outcome = None
            if outcome is not None:
                break
        r = ses.settle(outcome) if outcome is not None else ses.settle()
        t = now_ms()
        if r.pnl is not None:
            self.risk.on_settled(t, start, r.pnl)
            tt = self.totals
            tt["markets"] += 1; tt["pnl"] += r.pnl; tt["rebate"] += r.rebate
            tt["shares"] += r.q_up + r.q_dn; tt["fills"] += r.fills
        self.journal.write(t, start, "settle", outcome=r.outcome, outcome_src=r.outcome_src, up=round(r.q_up, 4),
                           dn=round(r.q_dn, 4), cost=round(r.cost, 4), pnl=None if r.pnl is None else round(r.pnl, 4),
                           rebate_est=round(r.rebate, 4), fills=r.fills, placed=r.placed, canceled=r.canceled,
                           rejected=r.rejected, markout60=round(r.markout, 4), day_pnl=round(self.risk.day_pnl, 4))
        log(f"{market['slug']} outcome={r.outcome} fills={r.fills} up={r.q_up:.1f} dn={r.q_dn:.1f} cost=${r.cost:.2f} "
            f"pnl={'n/a' if r.pnl is None else f'${r.pnl:.2f}'} | total ${self.totals['pnl']:.2f} over {self.totals['markets']} markets")
        if cfg.mode == "live" and outcome is not None and (r.q_up if outcome == 0 else r.q_dn) > 0:
            try:
                h = await self.live.client.redeem_positions(condition_id=market["condition_id"])
                await h.wait()
                self.journal.write(now_ms(), start, "redeem", ok=True)
            except Exception as e:           # often already redeemed automatically
                self.journal.write(now_ms(), start, "redeem", ok=False, error=repr(e)[:200])

    def live_exec(self, market):
        from .live import MarketExec
        return MarketExec(self.live, market)


class _Feed:
    """Adapts market_feed to a session. Paper mode: the session's fill model consumes every event.
    Live mode: only the book is maintained; fills come from the exchange."""

    def __init__(self, ses, ex, mode):
        self.ses, self.paper, self.last_ms = ses, mode != "live", 0

    def on_feed(self, t):
        self.last_ms = t

    def feed(self, e):
        if self.paper:
            self.ses.feed(e)
        elif e[0] == "P":
            self.ses.on_level(e[2], e[3], e[4])
        elif e[0] == "B":
            self.ses.on_snapshot(e[2], e[3])
