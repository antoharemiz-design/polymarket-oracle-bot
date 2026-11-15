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
    fast_ms: int = 250            # window of the spot move that the book is assumed not to have priced yet
    fast_k: float = 1.0           # weight of that move in fair value
    max_gap: float = 25.0         # cents; stop quoting when raw model and book disagree more than this
    use_model: bool = True        # False = quote around the book mid only (baseline)
    # directional lean on the book mid (research/06_maker_inventory.md, step 2); negative = fade
    mom_s: int = 60               # momentum window, seconds
    mom_w: float = 0.0            # cents of fair shift per cent of book-mid change over mom_s
    cci_w: float = 0.0            # cents of fair shift per 100 units of CCI(20) on 5 s bars of the mid
    # inventory policy: 0 = hold everything to settlement; >0 = at this second, stop quoting and
    # buy the short side as a taker until |Up - Down| < min_order (pairs pay 100c at settlement)
    t_flat: float = 0.0
    flat_slip: int = 3            # intraday close may walk the book this many cents beyond the touch
    join_min: float = 0.0         # place a quote only where at least this many shares already rest (0 = off)


@dataclass
class Quote:
    bid: int | None = None        # buy Up at this price, or None for no quote
    ask: int | None = None        # sell-Up-equivalent price (buy Down at 100 - ask), or None


class Fair:
    """Fair value in cents = book mid + the part of the latest spot move the book has not priced yet.

    Measured on recorded ticks (research/03_backtest.md): the Polymarket book follows spot
    with a lag of roughly one second. The spot move of the last 250-500 ms, converted to cents
    with the model delta, predicts the next move of the book mid (correlation 0.33-0.40, and
    the mid covers about that full distance within 1-3 s).

    Only the model's *sensitivity* is used, evaluated at the probability the book implies.
    The model's level is not: it is built on Binance while the market settles on Chainlink,
    and near the strike a one-dollar basis is worth several cents. On 2,749 markets the level
    deserved a weight of only 0.14 +- 0.07 against the traded price.
    """

    _nd = None

    def __init__(self, k: float = 1.0):
        from statistics import NormalDist
        Fair._nd = Fair._nd or NormalDist()
        self.k = k

    def update(self, now_ms: int, comp, bb: int, ba: int, spot_move: float = 0.0, sens: float = 1.0):
        """comp = FairValue.components() or None; spot_move in dollars over the fast window.
        Returns (fair_cents, raw_gap_cents) where raw gap = model level minus book mid."""
        mid = (bb + ba) / 2.0
        if comp is None:
            return mid, 0.0
        d, sd = comp
        if sd <= 0:
            return mid, 0.0
        nd = Fair._nd
        raw = 100.0 * nd.cdf(d / sd) - mid
        z = nd.inv_cdf(min(max(mid / 100.0, 0.005), 0.995))
        delta = 100.0 * nd.pdf(z) / sd * sens                # cents per dollar of spot
        fair = mid + self.k * delta * spot_move
        return min(max(fair, 0.5), 99.5), raw


class MidSignals:
    """Momentum and CCI of the book mid, sampled at whole seconds (5 s bars for CCI)."""

    def __init__(self, keep_s: int = 200):
        from collections import deque
        self.sec = deque(maxlen=keep_s)
        self.mid = deque(maxlen=keep_s)

    def update(self, now_ms: int, mid: float) -> None:
        s = now_ms // 1000
        if self.sec and self.sec[-1] == s:
            self.mid[-1] = mid
        else:
            self.sec.append(s); self.mid.append(mid)

    def _at(self, s: int):
        for i in range(len(self.sec) - 1, -1, -1):
            if self.sec[i] <= s:
                return self.mid[i]
        return None

    def momentum(self, n: int) -> float:
        if not self.sec:
            return 0.0
        old = self._at(self.sec[-1] - n)
        return 0.0 if old is None else self.mid[-1] - old

    def cci(self, n: int = 20, bar: int = 5) -> float:
        if not self.sec:
            return 0.0
        last = self.sec[-1]
        xs = []
        for b in range(n - 1, -1, -1):
            x = self._at(last - bar * b)
            if x is None:
                return 0.0
            xs.append(x)
        sma = sum(xs) / n
        mad = sum(abs(x - sma) for x in xs) / n
        return 0.0 if mad < 1e-9 else (xs[-1] - sma) / (0.015 * mad)


def directional(p: "Params", sig: MidSignals) -> float:
    """Fair-value shift in cents from the momentum / CCI lean (0 when both weights are 0)."""
    d = 0.0
    if p.mom_w:
        d += p.mom_w * sig.momentum(p.mom_s)
    if p.cci_w:
        d += p.cci_w * sig.cci() / 100.0
    return d


def decide(p: Params, elapsed: float, fair_c: float, raw_gap: float, bb: int, ba: int,
           q_up: float, q_dn: float, dir_c: float = 0.0) -> Quote:
    """Desired quotes given the current state. Pure function.

    Quotes are placed around fair + dir_c - skew * net / clip, so a long-Up inventory lowers
    the Up bid and raises the Down bid (Down bid = 100 - ask)."""
    q = Quote()
    if not (p.t_on <= elapsed <= p.t_off) or not (0 < bb < ba < 100):
        return q
    mid = (bb + ba) / 2.0
    if mid < p.p_min or mid > 100 - p.p_min or abs(raw_gap) > p.max_gap:
        return q
    net = q_up - q_dn
    f = fair_c + dir_c - p.skew * net / p.clip
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


def keep(p: Params, side: str, price: int, fair_c: float, q_up: float, q_dn: float, dir_c: float = 0.0) -> bool:
    """Should a resting quote stay? Uses the looser `edge_cancel` threshold (hysteresis)."""
    f = fair_c + dir_c - p.skew * (q_up - q_dn) / p.clip
    return (f - price) >= p.edge_cancel if side == "B" else (price - f) >= p.edge_cancel
