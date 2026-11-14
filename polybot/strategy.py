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
    gap_halflife: float = 20.0    # s; slow model-vs-book gap is treated as basis and removed
    max_gap: float = 8.0          # cents; stop quoting when model and book disagree more than this
    use_model: bool = True        # False = quote around the book mid only (baseline)


@dataclass
class Quote:
    bid: int | None = None        # buy Up at this price, or None for no quote
    ask: int | None = None        # sell-Up-equivalent price (buy Down at 100 - ask), or None


class Fair:
    """Fair value in cents = book mid + fast part of the model's disagreement with the book.

    The level of (model - mid) drifts with the Binance/Chainlink basis and volatility
    mis-estimation, so only its deviation from a slow average is used as signal.
    """

    def __init__(self, halflife_s: float):
        self.halflife = halflife_s
        self.gap = None
        self.last_ms = None

    def update(self, now_ms: int, model_c: float | None, bb: int, ba: int):
        mid = (bb + ba) / 2.0
        if model_c is None:
            return mid, 0.0
        raw = model_c - mid
        if self.gap is None:
            self.gap = raw
        else:
            dt = max(0.0, (now_ms - self.last_ms) / 1000.0)
            a = 1.0 - math.exp(-dt * math.log(2) / self.halflife)
            self.gap += a * (raw - self.gap)
        self.last_ms = now_ms
        return mid + (raw - self.gap), raw


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
