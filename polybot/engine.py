"""One market window: state, decisions and the paper execution model.

`MarketSession` is the single implementation of the strategy loop. It is driven by
  * the backtest  (polybot.sim: recorded events, simulated clock),
  * paper trading (polybot.run: live events, PaperExecutor),
  * live trading  (polybot.run: live events, polybot.live.LiveExecutor).
so the three cannot drift apart.

Sides are expressed in Up-token terms: "B" buys Up at `price`, "S" buys Down at `100 - price`.
"""
import dataclasses

from .book import UpBook
from .fairvalue import TW, FairValue, SpotSeries
from .strategy import Fair, MidSignals, Params, decide, directional, keep

TAKER_DELAY_MS = 250      # exchange hold on marketable orders in crypto up/down markets
FEE_RATE = 0.07           # taker fee = FEE_RATE * p * (1 - p) per share
# Maker rebate: 20% of the fee-equivalent of your own filled maker orders, paid daily.
# Verified against the public rebates endpoint for a real maker: actual / (0.20 * own fee-equivalent)
# = 1.000 on 795 market-days (research/06_maker_inventory.md). It does not depend on competitors.
REBATE_SHARE = 0.20


def fee_c(price_c: float) -> float:
    """Taker fee per share, in cents, at a price in cents."""
    p = price_c / 100.0
    return 100.0 * FEE_RATE * p * (1.0 - p)


@dataclasses.dataclass
class Order:
    side: str
    price: int
    rem: float
    state: str            # PENDING -> LIVE -> CANCELING
    t_eff: int = 1 << 62  # when the pending transition takes effect (paper)
    queue: float = 0.0    # shares ahead of us at our price (paper)
    live_at: int = 0      # exchange time at which the order started resting (paper)
    queue0: float = 0.0   # shares ahead of us when the order started resting (paper)
    filled: float = 0.0   # shares filled so far (paper)
    first_fill: int = 0   # exchange time of the first fill (paper)
    order_id: str = ""    # exchange id (live)


@dataclasses.dataclass
class Result:
    start: int
    outcome: int | None = None      # 0 = Up won, 1 = Down won
    outcome_src: str = ""
    q_up: float = 0.0
    q_dn: float = 0.0
    cost: float = 0.0               # dollars paid for shares
    fees: float = 0.0               # dollars of taker fees (taker research mode only)
    rebate: float = 0.0             # estimated maker rebate, dollars
    fills: int = 0
    placed: int = 0
    canceled: int = 0
    rejected: int = 0
    markout: float = 0.0            # dollars, vs book mid 60 s after each fill
    markout_shares: float = 0.0
    markout5: float = 0.0           # dollars, vs book mid 5 s after each fill (low-noise edge estimate)
    markout5_shares: float = 0.0
    orders_live: int = 0            # orders that rested on the book
    orders_filled: int = 0          # of those, orders with any fill
    filled_shares: float = 0.0      # maker shares filled
    ttf: list = dataclasses.field(default_factory=list)   # ms from resting to first fill
    reward_share_min: float = 0.0   # sum over 1-minute samples of our share of the reward score
    reward: float = 0.0             # liquidity reward estimate, dollars (0 while the market has no pool)
    pnl: float | None = None        # payoff - cost - taker fees + maker rebate
    pnl_ex_rebate: float | None = None


class PaperExecutor:
    """Execution model shared by backtest and paper trading (see polybot.sim docstring).

    Calibration options (research/06_maker_inventory.md, step 3):
      queue_mult   multiplier on the displayed size ahead of us when the order starts resting
      fill_share   fraction of each at-price fill we keep: other makers at the same price with
                   equal priority (1.0 = we are alone behind the displayed queue)
      share_good   like fill_share, but applied only to fills that look favourable against the
                   fair value at that moment; models competitors who quote the same good prices
                   while nobody competes for toxic fills
    Taker orders walk the book from the best price up to their limit.
    """

    def __init__(self, book: UpBook, lat_ms: int = 150, queue_mult: float = 1.0,
                 fill_share: float = 1.0, share_good: float = 1.0):
        self.book, self.lat_ms, self.queue_mult = book, lat_ms, queue_mult
        self.fill_share, self.share_good = fill_share, share_good
        self.orders = {"B": None, "S": None}
        self.taker = None             # (t_exec, side, limit, size)
        self.session = None
        self.fill_meta = []           # (t, side, price, got, queue0, queue_before, trade_size, through, edge_vs_fair)
        self.stale_trades = 0         # trades ignored because they happened before our order rested
        self.order_log = []           # (side, price, live_at, end_at, filled, queue0, first_fill), exchange time

    def order(self, side):
        return self.orders[side]

    def place(self, side, price_c, size, now_ms):
        self.orders[side] = Order(side, price_c, size, "PENDING", now_ms + self.lat_ms)

    def cancel(self, side, now_ms):
        o = self.orders[side]
        if o is not None and o.state == "LIVE":
            o.state = "CANCELING"; o.t_eff = now_ms + self.lat_ms

    def take(self, side, limit_c, size, now_ms):
        self.taker = (now_ms + self.lat_ms + TAKER_DELAY_MS, side, limit_c, size)

    def on_time(self, t):
        """Apply transitions that are due at or before t. Call before applying the event at t."""
        s, book = self.session, self.book
        for side in ("B", "S"):
            o = self.orders[side]
            if o is None or o.t_eff > t:
                continue
            if o.state == "PENDING":
                crosses = o.price >= book.best_ask() if side == "B" else o.price <= book.best_bid()
                if crosses or not book.ready:
                    self.orders[side] = None; s.on_reject(side, o.price, t)
                else:
                    o.state = "LIVE"; o.queue = o.queue0 = book.size(side, o.price) * self.queue_mult
                    o.live_at = o.t_eff; o.t_eff = 1 << 62
            elif o.state == "CANCELING":
                self.order_log.append((side, o.price, o.live_at, t, o.filled, o.queue0, o.first_fill))
                self.orders[side] = None; s.on_canceled(side, o.price, t)
        if self.taker is not None and self.taker[0] <= t:
            _, side, limit, size = self.taker
            self.taker = None
            if book.ready:
                self._walk(side, limit, size, t)

    def close_log(self, t_end):
        """Record intervals of orders still resting when the session ends."""
        for side in ("B", "S"):
            o = self.orders[side]
            if o is not None and o.state != "PENDING":
                self.order_log.append((side, o.price, o.live_at, t_end, o.filled, o.queue0, o.first_fill))

    def _walk(self, side, limit, size, t):
        """Marketable buy: consume price levels from the best one up to the limit."""
        book, left = self.book, size
        if side == "B":                                   # buy Up: lift Up asks, cheapest first
            levels = sorted(p for p in book.asks if p <= limit and p < 100)
            depth = book.asks
        else:                                             # buy Down = hit Up bids, highest first
            levels = sorted((p for p in book.bids if p >= limit and p > 0), reverse=True)
            depth = book.bids
        for px in levels:
            if left <= 1e-9:
                break
            got = min(left, depth[px])
            if got > 0:
                self.session.on_fill(side, px, got, t, True)
                left -= got

    def on_real_fills(self, fills, t):
        """Fill model driven by the real maker fills of one transaction: [(side, price, size), ...]
        in Up terms. FIFO at our price: the real volume first consumes the queue ahead of us; any
        real fill beyond our price means the whole level was consumed, so we are filled in full."""
        for side in ("B", "S"):
            o = self.orders[side]
            if o is None or o.state == "PENDING" or t < o.live_at:
                continue
            at = sum(sz for s_, p_, sz in fills if s_ == side and p_ == o.price)
            deeper = any(s_ == side and (p_ < o.price if side == "B" else p_ > o.price) for s_, p_, _ in fills)
            if not at and not deeper:
                continue
            q_before = o.queue
            if deeper:
                got = o.rem
            else:
                got = max(0.0, min(o.rem, at - o.queue)); o.queue = max(0.0, o.queue - at)
            if got <= 0:
                continue
            fair = self.session.last.get("fair")
            edge = None if fair is None else ((fair - o.price) if side == "B" else (o.price - fair))
            share = self.fill_share
            if self.share_good < 1.0 and edge is not None and edge >= 0:
                share *= self.share_good
            got *= share
            if got < 1e-9:
                continue
            self.fill_meta.append((t, side, o.price, got, o.queue0, q_before, at, deeper, edge))
            o.rem -= got; o.filled += got; o.first_fill = o.first_fill or t
            if o.rem < 1e-9 or share < 1.0 and o.rem < 0.5:
                self.order_log.append((side, o.price, o.live_at, t, o.filled, o.queue0, o.first_fill))
                self.orders[side] = None
            self.session.on_fill(side, o.price, got, t, False)

    def on_level(self, side, price_c, size, t=None):
        o = self.orders[side]
        if o is not None and o.state != "PENDING" and o.price == price_c and (t is None or t >= o.live_at):
            o.queue = min(o.queue, size * self.queue_mult)   # cancels are assumed to be behind us

    def on_trade(self, taker_side, px, sz, t):
        # a taker selling Up hits bids ("B"); a taker buying Up lifts asks ("S")
        o = self.orders["B" if taker_side == "S" else "S"]
        if o is None or o.state == "PENDING" or sz <= 0:
            return
        if t < o.live_at:                  # exchange timestamps are not monotonic: this trade
            self.stale_trades += 1         # happened before our order was resting
            return
        through = px < o.price if o.side == "B" else px > o.price
        q_before = o.queue
        if through:
            got = min(o.rem, sz)
        elif px == o.price:
            got = max(0.0, min(o.rem, sz - o.queue)); o.queue = max(0.0, o.queue - sz)
        else:
            got = 0.0
        if got <= 0:
            return
        fair = self.session.last.get("fair")
        edge = None if fair is None else ((fair - o.price) if o.side == "B" else (o.price - fair))
        share = self.fill_share
        if self.share_good < 1.0 and edge is not None and edge >= 0:
            share *= self.share_good
        got *= share
        if got < 1e-9:
            return
        self.fill_meta.append((t, o.side, o.price, got, o.queue0, q_before, sz, through, edge))
        o.rem -= got; o.filled += got; o.first_fill = o.first_fill or t
        if o.rem < 1e-9 or share < 1.0 and o.rem < 0.5:
            self.order_log.append((o.side, o.price, o.live_at, t, o.filled, o.queue0, o.first_fill))
            self.orders[o.side] = None
        self.session.on_fill(o.side, o.price, got, t, False)


class MultiPaperExecutor(PaperExecutor):
    """PaperExecutor with any number of resting orders, keyed by (side, price). Used by ladder mode."""

    def __init__(self, book, lat_ms=150, queue_mult=1.0, fill_share=1.0, share_good=1.0):
        super().__init__(book, lat_ms, queue_mult, fill_share, share_good)
        self.multi = {}

    def all_orders(self):
        return list(self.multi.values())

    def order(self, side):
        return None

    def place_at(self, side, price_c, size, now_ms):
        if (side, price_c) not in self.multi:
            self.multi[(side, price_c)] = Order(side, price_c, size, "PENDING", now_ms + self.lat_ms)

    def cancel_at(self, key, now_ms):
        o = self.multi.get(key)
        if o is not None and o.state == "LIVE":
            o.state = "CANCELING"; o.t_eff = now_ms + self.lat_ms

    def _remove(self, o, t):
        self.order_log.append((o.side, o.price, o.live_at, t, o.filled, o.queue0, o.first_fill))
        self.multi.pop((o.side, o.price), None)

    def on_time(self, t):
        s, book = self.session, self.book
        for key, o in list(self.multi.items()):
            if o.t_eff > t:
                continue
            if o.state == "PENDING":
                crosses = o.price >= book.best_ask() if o.side == "B" else o.price <= book.best_bid()
                if crosses or not book.ready:
                    self.multi.pop(key, None); s.on_reject(o.side, o.price, t)
                else:
                    o.state = "LIVE"; o.queue = o.queue0 = book.size(o.side, o.price) * self.queue_mult
                    o.live_at = o.t_eff; o.t_eff = 1 << 62
            elif o.state == "CANCELING":
                self._remove(o, t); s.on_canceled(o.side, o.price, t)
        if self.taker is not None and self.taker[0] <= t:
            _, side, limit, size = self.taker
            self.taker = None
            if book.ready:
                self._walk(side, limit, size, t)

    def close_log(self, t_end):
        for o in list(self.multi.values()):
            if o.state != "PENDING":
                self.order_log.append((o.side, o.price, o.live_at, t_end, o.filled, o.queue0, o.first_fill))

    def on_level(self, side, price_c, size, t=None):
        o = self.multi.get((side, price_c))
        if o is not None and o.state != "PENDING" and (t is None or t >= o.live_at):
            o.queue = min(o.queue, size * self.queue_mult)

    def _fill(self, o, got, t, through, at, q_before):
        share = self.fill_share
        fair = self.session.last.get("fair")
        edge = None if fair is None else ((fair - o.price) if o.side == "B" else (o.price - fair))
        if self.share_good < 1.0 and edge is not None and edge >= 0:
            share *= self.share_good
        got *= share
        if got < 1e-9:
            return
        self.fill_meta.append((t, o.side, o.price, got, o.queue0, q_before, at, through, edge))
        o.rem -= got; o.filled += got; o.first_fill = o.first_fill or t
        if o.rem < 1e-9 or share < 1.0 and o.rem < 0.5:
            self._remove(o, t)
        self.session.on_fill(o.side, o.price, got, t, False)

    def on_real_fills(self, fills, t):
        for o in list(self.multi.values()):
            if o.state == "PENDING" or t < o.live_at:
                continue
            at = sum(sz for s_, p_, sz in fills if s_ == o.side and p_ == o.price)
            deeper = any(s_ == o.side and (p_ < o.price if o.side == "B" else p_ > o.price) for s_, p_, _ in fills)
            if not at and not deeper:
                continue
            q_before = o.queue
            if deeper:
                got = o.rem
            else:
                got = max(0.0, min(o.rem, at - o.queue)); o.queue = max(0.0, o.queue - at)
            if got > 0:
                self._fill(o, got, t, deeper, at, q_before)

    def on_trade(self, taker_side, px, sz, t):
        side = "B" if taker_side == "S" else "S"
        for o in sorted((o for o in self.multi.values() if o.side == side),
                        key=lambda o: -o.price if side == "B" else o.price):
            if o.state == "PENDING" or sz <= 0 or t < o.live_at:
                continue
            through = px < o.price if side == "B" else px > o.price
            q_before = o.queue
            if through:
                got = min(o.rem, sz)
            elif px == o.price:
                got = max(0.0, min(o.rem, sz - o.queue)); o.queue = max(0.0, o.queue - sz)
            else:
                continue
            if got > 0:
                self._fill(o, got, t, through, sz, q_before)


class MarketSession:
    def __init__(self, start: int, p: Params, spot: SpotSeries, executor, dur: int = 300,
                 mode: str = "maker", journal=None, taker_theta: float = 1.0, book: UpBook | None = None):
        self.start, self.dur, self.p, self.spot, self.ex = start, dur, p, spot, executor
        self.mode, self.journal, self.taker_theta = mode, journal, taker_theta
        self.start_ms, self.end_ms = start * 1000, (start + dur) * 1000
        self.book = book if book is not None else UpBook()
        self.fv = FairValue(spot, start, dur)
        self.fair = Fair(p.fast_k)
        self.res = Result(start=start)
        self.fill_log = []            # (t, sign, price_c, shares)
        self.grid_t, self.grid_mid = [], []
        self.min_lag = None           # smallest (receive - exchange) timestamp gap seen, ms
        self.sig = MidSignals()       # book-mid momentum / CCI for the directional lean
        self.min_order = 5.0          # exchange minimum order size, shares
        self.halted = False           # set by risk control: pull quotes, place nothing
        self.last = {}                # last decision inputs, for the journal
        self.next_reward_sample = self.start_ms + 30000   # reward program samples the book once a minute
        self.ladder_keys, self.ladder_done = [], set()
        executor.session = self

    # ---- market data -------------------------------------------------------------------------
    def exch_time(self, recv_ms: int, srv_ms: int) -> int:
        """Exchange time of an event on the local clock: srv + (smallest feed delay seen so far).

        Our view of the book is `recv`; what the exchange had already done is `srv`. In bursts the
        feed lags by hundreds of ms, so a quote that still looks available locally may be long
        gone. Order transitions and fills are therefore timed on this clock, decisions on `recv`.
        """
        if not srv_ms:
            return recv_ms
        lag = recv_ms - srv_ms
        if self.min_lag is None or lag < self.min_lag:
            self.min_lag = lag
        return srv_ms + self.min_lag

    def feed(self, e):
        """Apply one normalized market event (see polybot.replay) with paper-execution timing."""
        k = e[0]
        if k == "P":
            tau = self.exch_time(e[1], e[5])
            self._tick(tau); self.on_level(e[2], e[3], e[4], tau)
        elif k == "T":
            tau = self.exch_time(e[1], e[6])
            self._tick(tau); self.on_trade(e[2], e[3], e[4], tau)
        elif k == "B":
            tau = self.exch_time(e[1], e[4])
            self._tick(tau); self.on_snapshot(e[2], e[3])
        elif k == "R":                                    # ("R", recv, [(side, price, size)], srv)
            tau = self.exch_time(e[1], e[3])
            self._tick(tau)
            if hasattr(self.ex, "on_real_fills") and tau < self.end_ms + 5000:
                self.ex.on_real_fills(e[2], tau)

    def _tick(self, tau):
        if hasattr(self.ex, "on_time"):
            self.ex.on_time(tau)

    def on_snapshot(self, bids, asks):
        self.book.snapshot(bids, asks)

    def on_level(self, side, price_c, size, t=None):
        if self.book.ready:
            self.book.change(side, price_c, size)
            if hasattr(self.ex, "on_level"):
                self.ex.on_level(side, price_c, size, t)

    def on_trade(self, taker_side, px, sz, t):
        if hasattr(self.ex, "on_trade") and t < self.end_ms + 5000:
            self.ex.on_trade(taker_side, px, sz, t)

    # ---- executor callbacks ------------------------------------------------------------------
    def on_fill(self, side, price_c, shares, t, is_taker=False):
        if shares <= 0:
            return
        r = self.res
        paid = price_c if side == "B" else 100 - price_c
        if side == "B":
            r.q_up += shares
        else:
            r.q_dn += shares
        r.cost += shares * paid / 100.0
        if is_taker:
            r.fees += shares * fee_c(paid) / 100.0
        else:
            r.rebate += shares * REBATE_SHARE * fee_c(paid) / 100.0
        r.fills += 1
        self.fill_log.append((t, 1 if side == "B" else -1, price_c, shares))
        self._log(t, "fill", side=side, price=price_c, shares=round(shares, 4), paid_c=paid, taker=is_taker)

    def on_reject(self, side, price_c, t, reason="post_only_would_cross"):
        self.res.rejected += 1
        self._log(t, "reject", side=side, price=price_c, reason=reason)

    def on_canceled(self, side, price_c, t):
        self.res.canceled += 1
        self._log(t, "canceled", side=side, price=price_c)

    def _log(self, t, event, **kw):
        if self.journal is not None:
            self.journal.write(t, self.start, event, q_up=round(self.res.q_up, 4), q_dn=round(self.res.q_dn, 4),
                               **self.last, **kw)

    # ---- decision ----------------------------------------------------------------------------
    def on_timer(self, td: int):
        """Evaluate the strategy at local time td (ms) and send the resulting order actions."""
        book, p, ex, r = self.book, self.p, self.ex, self.res
        if not book.ready:
            return
        bb, ba = book.best_bid(), book.best_ask()
        if not (0 < bb < ba < 100):
            self._pull(td, "book_empty"); return
        self.grid_t.append(td); self.grid_mid.append((bb + ba) / 2.0)
        self.sig.update(td, (bb + ba) / 2.0)
        if td >= self.next_reward_sample:
            self._reward_sample((bb + ba) / 2.0)
            self.next_reward_sample += 60000
        if self.mode == "ladder":
            self._ladder(td, bb, ba); return
        comp = None
        if p.use_model and self.mode != "mid":
            comp = self.fv.components(td) if self.spot.ready(self.start) else None
            if comp is None:
                self._pull(td, "model_not_ready"); return
        f, raw = self.fair.update(td, comp, bb, ba, self.spot.move(td, p.fast_ms) if comp is not None else 0.0, self.fv.sens(td))
        elapsed = (td - self.start_ms) / 1000.0
        self.last = {"bb": bb, "ba": ba, "fair": round(f, 2), "gap": round(raw, 2)}
        if self.halted:
            self._pull(td, "halted"); return
        if self.mode == "taker":
            self._taker(td, elapsed, f, raw, bb, ba); return
        if p.t_flat > 0 and elapsed >= p.t_flat:
            self._flatten(td, bb, ba); return
        dc = directional(p, self.sig)
        if dc:
            self.last["dir"] = round(dc, 2)
        want = decide(p, elapsed, f, raw, bb, ba, r.q_up, r.q_dn, dc)
        for side, w in (("B", want.bid), ("S", want.ask)):
            o = ex.order(side)
            if o is None:
                if w is not None and p.join_min > 0 and book.size(side, w) < p.join_min:
                    w = None                    # nobody else rests there: such fills were the most toxic (step 3)
                if w is not None and p.max_share < 1.0 and p.clip / (p.clip + book.size(side, w)) >= p.max_share:
                    w = None                    # we would be a large part of the level: the shield for others (step B)
                if w is not None:
                    r.placed += 1
                    self._log(td, "place", side=side, price=w, size=p.clip)
                    ex.place(side, w, p.clip, td)
            elif o.state == "LIVE":
                more_aggressive = w is not None and (w > o.price if side == "B" else w < o.price)
                if p.keep_share < 1.0 and o.rem / (o.rem + book.size(side, o.price)) > p.keep_share:
                    why = "shield"              # the level thinned out around us
                elif w is None:
                    why = "gate"
                elif more_aggressive:
                    why = "requote"
                elif not keep(p, side, o.price, f, r.q_up, r.q_dn, dc):
                    why = "edge_lost"
                else:
                    continue
                self._log(td, "cancel", side=side, price=o.price, reason=why)
                ex.cancel(side, td)

    def _ladder(self, td, bb, ba):
        """Place a ladder of bids on both sides once, hold it; positions are held to settlement."""
        p, r, ex = self.p, self.res, self.ex
        el = (td - self.start_ms) / 1000.0
        if self.halted or el >= p.lad_stop:
            for key, o in list(ex.multi.items()):
                if o.state == "LIVE":
                    ex.cancel_at(key, td)
            return
        if el < p.lad_place:
            return
        net = r.q_up - r.q_dn
        if not self.ladder_keys:
            m = int(round((bb + ba) / 2.0))
            for k in range(p.lad_levels):
                d = p.lad_d0 + k * p.lad_step
                for side, price in (("B", m - d), ("S", m + d)):           # S at P = buy Down at 100 - P
                    paid = price if side == "B" else 100 - price
                    if paid < p.lad_lo or paid > 100 - p.lad_lo:
                        continue
                    if p.max_share < 1.0 and p.clip / (p.clip + self.book.size(side, price)) >= p.max_share:
                        continue
                    self.ladder_keys.append((side, price))
            self._log(td, "ladder", levels=self.ladder_keys, mid=m)
        for side, price in self.ladder_keys:
            o = ex.multi.get((side, price))
            grows = (side == "B" and net + p.clip > p.max_net) or (side == "S" and -net + p.clip > p.max_net)
            full = r.q_up + r.q_dn + p.clip > p.max_gross
            if o is not None:
                if (grows or full) and o.state == "LIVE":
                    ex.cancel_at((side, price), td)
            elif not grows and not full and (p.lad_rearm or (side, price) not in self.ladder_done):
                crosses = price >= ba if side == "B" else price <= bb
                if not crosses:
                    r.placed += 1
                    ex.place_at(side, price, p.clip, td)
                    self.ladder_done.add((side, price))

    def _reward_sample(self, mid):
        """Our share of the liquidity reward score at one sample (programs/liquidity-rewards).
        Side one = Up bids (+ Down asks), side two = Up asks (= Down bids); others are the book."""
        p, book, ex = self.p, self.book, self.ex
        v = p.reward_max_spread
        S = lambda price: max(0.0, (v - abs(price - mid)) / v) ** 2
        mine = {"B": 0.0, "S": 0.0}
        live = ex.all_orders() if hasattr(ex, "all_orders") else [ex.order(sd) for sd in ("B", "S")]
        for o in live:
            if o is not None and o.state != "PENDING" and o.rem >= p.reward_min_size:
                mine[o.side] += S(o.price) * o.rem
        others = {"B": sum(S(px) * sz for px, sz in book.bids.items()),
                  "S": sum(S(px) * sz for px, sz in book.asks.items())}

        def qmin(a, b):
            return max(min(a, b), max(a, b) / 3.0) if 10 <= mid <= 90 else min(a, b)
        q_me, q_all = qmin(mine["B"], mine["S"]), qmin(others["B"] + mine["B"], others["S"] + mine["S"])
        if q_all > 0:
            self.res.reward_share_min += q_me / q_all

    def _flatten(self, td, bb, ba):
        """Intraday close: stop quoting, then buy the short side at the touch until the
        position is paired (a pair pays 100c at settlement whatever the outcome)."""
        self._pull(td, "flatten")
        r, ex = self.res, self.ex
        net = r.q_up - r.q_dn
        if ex.taker is not None or abs(net) < self.min_order:
            return
        slip = self.p.flat_slip
        side, limit = ("S", bb - slip) if net > 0 else ("B", ba + slip)   # long Up -> buy Down at 100 - bb
        self._log(td, "flatten", side=side, limit=limit, size=round(abs(net), 4))
        ex.take(side, limit, abs(net), td)

    def _pull(self, td, why):
        for side in ("B", "S"):
            o = self.ex.order(side)
            if o is not None and o.state == "LIVE":
                self._log(td, "cancel", side=side, price=o.price, reason=why)
                self.ex.cancel(side, td)

    def _taker(self, td, elapsed, f, raw, bb, ba):
        p, r, ex = self.p, self.res, self.ex
        if ex.taker is not None or not (p.t_on <= elapsed <= p.t_off):
            return
        if not (p.p_min <= (bb + ba) / 2 <= 100 - p.p_min) or abs(raw) > p.max_gap:
            return
        if r.q_up + r.q_dn + p.clip > p.max_gross:
            return
        net = r.q_up - r.q_dn
        if f - ba - fee_c(ba) >= self.taker_theta and net + p.clip <= p.max_net:
            self._log(td, "take", side="B", limit=ba, size=p.clip, edge=round(f - ba - fee_c(ba), 2))
            ex.take("B", ba, p.clip, td)
        elif bb - f - fee_c(bb) >= self.taker_theta and -net + p.clip <= p.max_net:
            self._log(td, "take", side="S", limit=bb, size=p.clip, edge=round(bb - f - fee_c(bb), 2))
            ex.take("S", bb, p.clip, td)

    # ---- settlement --------------------------------------------------------------------------
    def spot_proxy_outcome(self):
        self.spot.advance(self.end_ms + 1000)
        k0 = self.spot.mean(self.start - TW, self.start)
        k1 = self.spot.mean(self.start + self.dur - TW, self.start + self.dur)
        return None if k0 is None or k1 is None else (0 if k1 >= k0 else 1)

    def settle(self, outcome=None, src="actual") -> Result:
        import bisect
        r = self.res
        if outcome is None:
            outcome, src = self.spot_proxy_outcome(), "spot-proxy"
        r.outcome, r.outcome_src = outcome, src if outcome is not None else ""
        r.reward = self.p.reward_rate / 1440.0 * r.reward_share_min
        if outcome is not None:
            r.pnl_ex_rebate = (r.q_up if outcome == 0 else r.q_dn) - r.cost - r.fees
            r.pnl = r.pnl_ex_rebate + r.rebate + r.reward
        r.markout = r.markout_shares = r.markout5 = r.markout5_shares = 0.0
        for t, sign, price_c, sh in self.fill_log:
            j = bisect.bisect_right(self.grid_t, t + 60000) - 1
            if j >= 0 and self.grid_t[j] >= t + 60000 - 200:
                r.markout += sh * sign * (self.grid_mid[j] - price_c) / 100.0
                r.markout_shares += sh
            j = bisect.bisect_right(self.grid_t, t + 5000) - 1
            if j >= 0 and self.grid_t[j] >= t + 5000 - 200:
                r.markout5 += sh * sign * (self.grid_mid[j] - price_c) / 100.0
                r.markout5_shares += sh
        return r
