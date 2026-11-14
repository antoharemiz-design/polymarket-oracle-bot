"""Quoting logic, shared by the simulator, paper trading and live trading.

The market is described in Up-token cents. The bot only ever BUYS:
    "bid"  = buy Up   at `bid`  cents
    "ask"  = buy Down at `100 - ask` cents   (economically: sell Up at `ask`)
and holds everything to resolution. A matched Up+Down pair pays 100 cents.
"""
import math
from dataclasses import dataclass


@dataclass
class Params:
    clip: float = 10.0            # shares per order
    edge: float = 2.0             # required edge vs fair to place a quote, cents
    edge_cancel: float = 1.0      # cancel a resting quote when its edge falls below this
    skew: float = 0.5             # cents of fair-value shift per clip of net inventory
    max_net: float = 50.0         # max |Up - Down| shares
    max_gross: float = 400.0      # max total shares per market
    t_on: float = 5.0             # start quoting this many seconds after the window opens
    t_off: float = 235.0          # stop quoting (and pull quotes) this many seconds in
    p_min: float = 12.0           # only quote while the book mid is inside [p_min, 100 - p_min]
    improve: int = 0              # 0 = at most join the best price, 1 = may step one tick ahead
    gap_halflife: float = 20.0    # s; half-life of the strike-offset (basis) estimate
    max_gap: float = 25.0         # cents; stop quoting when raw model and book disagree more than this
    use_model: bool = True        # False = quote around the book mid only (baseline)


@dataclass
class Quote:
    bid: int | None = None        # buy Up at this price, or None for no quote
    ask: int | None = None        # sell-Up-equivalent price (buy Down at 100 - ask), or None


class Fair:
    """Fair value in cents from the spot model, anchored to the book.

    The model works from Binance while the market resolves on Chainlink, and near the strike a
    basis of a dollar or two moves the model probability by many cents. Measured on 1,268
    markets, the model's *level* deserves only ~0.1 weight against the traded price
    (research/03_backtest.md). What the model adds is speed: it moves with spot before the
    book does. So the basis is estimated in dollars, as the strike offset that makes the model
    agree with the book mid, averaged with half-life `halflife_s`:

        offset_t = inv_phi(mid) * sd - d          (d, sd from FairValue.components)
        fair     = phi((d + EWMA(offset)) / sd)

    A spot move changes d immediately, so fair moves with the right delta while the slow
    disagreement is absorbed by the offset.
    """

    _nd = None

    def __init__(self, halflife_s: float):
        from statistics import NormalDist
        Fair._nd = Fair._nd or NormalDist()
        self.halflife = halflife_s
        self.offset = None
        self.last_ms = None

    def update(self, now_ms: int, comp, bb: int, ba: int):
        """comp = FairValue.components() or None. Returns (fair_cents, raw_gap_cents)."""
        mid = (bb + ba) / 2.0
        if comp is None:
            return mid, 0.0
        d, sd = comp
        if sd <= 0:
            return mid, 0.0
        nd = Fair._nd
        raw = 100.0 * nd.cdf(d / sd) - mid
        o = nd.inv_cdf(min(max(mid / 100.0, 0.005), 0.995)) * sd - d
        if self.offset is None:
            self.offset = o
        else:
            dt = max(0.0, (now_ms - self.last_ms) / 1000.0)
            a = 1.0 - math.exp(-dt * math.log(2) / self.halflife)
            self.offset += a * (o - self.offset)
        self.last_ms = now_ms
        return 100.0 * nd.cdf((d + self.offset) / sd), raw


def decide(p: Params, elapsed: float, fair_c: float, raw_gap: float, bb: int, ba: int,
           q_up: float, q_dn: float) -> Quote:
    """Desired quotes given the current state. Pure function."""
    q = Quote()
    if not (p.t_on <= elapsed <= p.t_off) or not (0 < bb < ba < 100):
        return q
    mid = (bb + ba) / 2.0
    if mid < p.p_min or mid > 100 - p.p_min or abs(raw_gap) > p.max_gap:
        return q
    net = q_up - q_dn
    f = fair_c - p.skew * net / p.clip
    gross_ok = q_up + q_dn + p.clip <= p.max_gross
    if gross_ok and net + p.clip <= p.max_net:
        bid = min(math.floor(f - p.edge), ba - 1, bb + p.improve)
        if bid >= 1:
            q.bid = int(bid)
    if gross_ok and -net + p.clip <= p.max_net:
        ask = max(math.ceil(f + p.edge), bb + 1, ba - p.improve)
        if ask <= 99:
            q.ask = int(ask)
    return q


def keep(p: Params, side: str, price: int, fair_c: float, q_up: float, q_dn: float) -> bool:
    """Should a resting quote stay? Uses the looser `edge_cancel` threshold (hysteresis)."""
    f = fair_c - p.skew * (q_up - q_dn) / p.clip
    return (f - price) >= p.edge_cancel if side == "B" else (price - f) >= p.edge_cancel
