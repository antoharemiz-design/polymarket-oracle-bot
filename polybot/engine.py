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
from .strategy import Fair, Params, decide, keep

TAKER_DELAY_MS = 250      # exchange hold on marketable orders in crypto up/down markets
FEE_RATE = 0.07           # taker fee = FEE_RATE * p * (1 - p) per share
REBATE_SHARE = 0.20       # share of taker fees returned to the makers of those fills


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
    pnl: float | None = None


class PaperExecutor:
    """Execution model shared by backtest and paper trading (see polybot.sim docstring)."""

    def __init__(self, book: UpBook, lat_ms: int = 150, queue_mult: float = 1.0):
        self.book, self.lat_ms, self.queue_mult = book, lat_ms, queue_mult
        self.orders = {"B": None, "S": None}
        self.taker = None             # (t_exec, side, limit, size)
        self.session = None

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
                    o.state = "LIVE"; o.queue = book.size(side, o.price) * self.queue_mult; o.t_eff = 1 << 62
            elif o.state == "CANCELING":
                self.orders[side] = None; s.on_canceled(side, o.price, t)
        if self.taker is not None and self.taker[0] <= t:
            _, side, limit, size = self.taker
            self.taker = None
            if book.ready:
                if side == "B":
                    px = book.best_ask()
                    if px <= limit and px < 100:
                        s.on_fill("B", px, min(size, book.size("S", px)), t, True)
                else:
                    px = book.best_bid()
                    if px >= limit and px > 0:
                        s.on_fill("S", px, min(size, book.size("B", px)), t, True)

    def on_level(self, side, price_c, size):
        o = self.orders[side]
        if o is not None and o.state != "PENDING" and o.price == price_c:
            o.queue = min(o.queue, size * self.queue_mult)   # cancels are assumed to be behind us

    def on_trade(self, taker_side, px, sz, t):
        # a taker selling Up hits bids ("B"); a taker buying Up lifts asks ("S")
        o = self.orders["B" if taker_side == "S" else "S"]
        if o is None or o.state == "PENDING" or sz <= 0:
            return
        through = px < o.price if o.side == "B" else px > o.price
        if through:
            got = min(o.rem, sz)
        elif px == o.price:
            got = max(0.0, min(o.rem, sz - o.queue)); o.queue = max(0.0, o.queue - sz)
        else:
            got = 0.0
        if got > 0:
            o.rem -= got
            if o.rem < 1e-9:
                self.orders[o.side] = None
            self.session.on_fill(o.side, o.price, got, t, False)


class MarketSession:
    def __init__(self, start: int, p: Params, spot: SpotSeries, executor, dur: int = 300,
                 mode: str = "maker", journal=None, taker_theta: float = 1.0, book: UpBook | None = None):
        self.start, self.dur, self.p, self.spot, self.ex = start, dur, p, spot, executor
        self.mode, self.journal, self.taker_theta = mode, journal, taker_theta
        self.start_ms, self.end_ms = start * 1000, (start + dur) * 1000
        self.book = book if book is not None else UpBook()
        self.fv = FairValue(spot, start, dur)
        self.fair = Fair(p.gap_halflife)
        self.res = Result(start=start)
        self.fill_log = []            # (t, sign, price_c, shares)
        self.grid_t, self.grid_mid = [], []
        self.halted = False           # set by risk control: pull quotes, place nothing
        self.last = {}                # last decision inputs, for the journal
        executor.session = self

    # ---- market data -------------------------------------------------------------------------
    def on_snapshot(self, bids, asks):
        self.book.snapshot(bids, asks)

    def on_level(self, side, price_c, size):
        if self.book.ready:
            self.book.change(side, price_c, size)
            if hasattr(self.ex, "on_level"):
                self.ex.on_level(side, price_c, size)

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
        model = None
        if p.use_model and self.mode != "mid":
            pu = self.fv.p_up(td) if self.spot.ready(self.start) else None
            if pu is None:
                self._pull(td, "model_not_ready"); return
            model = 100.0 * pu
        f, raw = self.fair.update(td, model, bb, ba)
        elapsed = (td - self.start_ms) / 1000.0
        self.last = {"bb": bb, "ba": ba, "fair": round(f, 2), "gap": round(raw, 2)}
        if self.halted:
            self._pull(td, "halted"); return
        if self.mode == "taker":
            self._taker(td, elapsed, f, raw, bb, ba); return
        want = decide(p, elapsed, f, raw, bb, ba, r.q_up, r.q_dn)
        for side, w in (("B", want.bid), ("S", want.ask)):
            o = ex.order(side)
            if o is None:
                if w is not None:
                    r.placed += 1
                    self._log(td, "place", side=side, price=w, size=p.clip)
                    ex.place(side, w, p.clip, td)
            elif o.state == "LIVE":
                more_aggressive = w is not None and (w > o.price if side == "B" else w < o.price)
                if w is None:
                    why = "gate"
                elif more_aggressive:
                    why = "requote"
                elif not keep(p, side, o.price, f, r.q_up, r.q_dn):
                    why = "edge_lost"
                else:
                    continue
                self._log(td, "cancel", side=side, price=o.price, reason=why)
                ex.cancel(side, td)

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
            ex.take("B", ba, p.clip, td)
        elif bb - f - fee_c(bb) >= self.taker_theta and -net + p.clip <= p.max_net:
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
        if outcome is not None:
            r.pnl = (r.q_up if outcome == 0 else r.q_dn) - r.cost - r.fees
        r.markout = r.markout_shares = 0.0
        for t, sign, price_c, sh in self.fill_log:
            j = bisect.bisect_right(self.grid_t, t + 60000) - 1
            if j >= 0 and self.grid_t[j] >= t + 60000 - 200:
                r.markout += sh * sign * (self.grid_mid[j] - price_c) / 100.0
                r.markout_shares += sh
        return r
