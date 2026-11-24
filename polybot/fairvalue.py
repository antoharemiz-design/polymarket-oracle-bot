"""Online fair value for a BTC up/down market.

Resolution rule (confirmed empirically, research/01_trader_investigation.md):
    Up wins iff TWAP60(end of window) >= TWAP60(just before window start).

The spot is modelled as driftless Brownian motion. Prices are sampled once per second
(last mid known at each whole-second boundary), which is how the TWAPs are approximated.
"""
import math
from collections import deque

TW = 60  # TWAP length in seconds


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class SpotSeries:
    """Per-second spot samples with rolling realized volatility."""

    def __init__(self, vol_lookback: int = 900, keep: int = 2400):
        self.vol_lookback = vol_lookback
        self.keep = keep
        self.sec0 = None              # epoch second of samples[0]
        self.samples = deque()        # samples[i] = last mid known at boundary sec0 + i
        self.last_mid = None
        self.last_ms = 0
        self._r2 = deque()            # squared 1s log returns, aligned with samples[1:]
        self._r2_sum = 0.0
        self.ticks = deque()          # (ts_ms, mid) of the last few seconds, for short-horizon moves

    def update(self, ts_ms: int, mid: float) -> None:
        sec = ts_ms // 1000
        if self.sec0 is None:
            self.sec0 = sec + 1
        else:
            self._fill_to(sec)        # boundaries passed before this tick carry the previous mid
        self.last_mid = mid
        self.last_ms = ts_ms
        tk = self.ticks
        tk.append((ts_ms, mid))
        while len(tk) > 2 and tk[1][0] <= ts_ms - 10000:
            tk.popleft()

    def move(self, now_ms: int, window_ms: int) -> float:
        """Spot change in dollars over the last `window_ms` (0 if there is no tick that old)."""
        cut = now_ms - window_ms
        ref = None
        for ts, mid in reversed(self.ticks):
            if ts <= cut:
                ref = mid
                break
        return 0.0 if ref is None or self.last_mid is None else self.last_mid - ref

    def _fill_to(self, sec: int) -> None:
        nxt = self.sec0 + len(self.samples)
        while nxt <= sec:
            if self.samples:
                r = math.log(self.last_mid / self.samples[-1])
                self._r2.append(r * r); self._r2_sum += r * r
                if len(self._r2) > self.vol_lookback:
                    self._r2_sum -= self._r2.popleft()
            self.samples.append(self.last_mid)
            if len(self.samples) > self.keep:
                self.samples.popleft(); self.sec0 += 1
            nxt += 1

    def advance(self, now_ms: int) -> None:
        """Roll second boundaries forward even when no tick arrived."""
        if self.sec0 is not None and self.last_mid is not None:
            self._fill_to(now_ms // 1000)

    def sample(self, sec: int):
        i = sec - self.sec0 if self.sec0 is not None else -1
        return self.samples[i] if 0 <= i < len(self.samples) else None

    def mean(self, a: int, b: int):
        """Mean of boundary samples a+1 .. b (the TWAP over (a, b]); None if not fully covered."""
        if self.sec0 is None:
            return None
        i, j = a + 1 - self.sec0, b - self.sec0
        if i < 0 or j >= len(self.samples):
            return None
        tot = 0.0
        for k in range(i, j + 1):
            tot += self.samples[k]
        return tot / (j - i + 1)

    def sigma(self) -> float:
        """Per-second volatility of log returns."""
        n = len(self._r2)
        return math.sqrt(self._r2_sum / n) if n >= 60 else 0.0

    def ready(self, start: int) -> bool:
        return self.sec0 is not None and self.sec0 <= start - TW and len(self._r2) >= 300


class FairValue:
    """P(Up) for one market window [start, start+dur)."""

    def __init__(self, spot: SpotSeries, start: int, dur: int = 300, vol_mult: float = 1.25):
        self.spot, self.start, self.end, self.vol_mult = spot, start, start + dur, vol_mult
        self.strike = None

    def components(self, now_ms: int):
        """(d, sd): expected final TWAP minus strike, and its standard deviation, in dollars.
        P(Up) = phi(d / sd). Returns None while data is missing; sd == 0 after the window ends."""
        sp = self.spot
        sp.advance(now_ms)
        if self.strike is None:
            self.strike = sp.mean(self.start - TW, self.start)
            if self.strike is None:
                return None
        s = sp.last_mid
        if s is None:
            return None
        sig = sp.sigma() * s * self.vol_mult
        if sig <= 0:
            return None
        now = now_ms / 1000.0
        if now >= self.end:
            fin = sp.mean(self.end - TW, self.end)
            return None if fin is None else (fin - self.strike, 0.0)
        first = self.end - TW + 1                      # first boundary of the final TWAP
        nsec = int(now)                                # last boundary already sampled
        if nsec < first:
            tau = self.end - now
            mean, var = s, sig * sig * (tau - TW + TW / 3.0)
        else:
            m = nsec - first + 1                       # realized boundaries
            realized = sp.mean(first - 1, nsec)
            if realized is None:
                return None
            n = TW - m
            mean = (m * realized + n * s) / TW
            acc = 0.0                                  # sum_ij min(d_i, d_j) over remaining boundaries
            for k in range(1, n + 1):
                d = (nsec + k) - now
                acc += d * (2 * (n - k) + 1)
            var = sig * sig * acc / (TW * TW)
        return mean - self.strike, math.sqrt(var + 1e-12)

    def sens(self, now_ms: int) -> float:
        """d(expected final TWAP)/d(spot): 1 before the final minute, then the share still open."""
        left = self.end - now_ms / 1000.0
        return 1.0 if left >= TW else max(0.0, left / TW)

    def p_up(self, now_ms: int):
        c = self.components(now_ms)
        if c is None:
            return None
        d, sd = c
        if sd == 0.0:
            return 1.0 if d >= 0 else 0.0
        return phi(d / sd)
