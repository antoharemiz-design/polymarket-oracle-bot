"""Fair value of a BTC up/down 5m market under the empirically confirmed rule:
Up wins iff mean(price over last 60s of window) >= mean(price over 60s before window start).

Price is modelled as driftless Brownian motion with per-second volatility sigma (in price units).
"""
import math

import numpy as np

TW = 60  # averaging length, seconds


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class Spot:
    def __init__(self, npz_path):
        z = np.load(npz_path)
        self.t0 = int(z["t0"]); self.S = z["close"]
        self.cs = np.concatenate([[0.0], np.cumsum(self.S)])
        r = np.diff(np.log(self.S)); self.cr2 = np.concatenate([[0.0, 0.0], np.cumsum(r * r)])

    def covers(self, a, b):
        return a - self.t0 >= 1000 and b - self.t0 < len(self.S)

    def px(self, t):                      # price known at the end of second t
        return self.S[t - self.t0]

    def mean(self, a, b):                 # mean of closes of seconds [a, b)
        return (self.cs[b - self.t0] - self.cs[a - self.t0]) / (b - a)

    def sigma(self, t, look=900):         # realized per-second vol of log returns over trailing window
        i = t - self.t0
        return math.sqrt(max((self.cr2[i + 1] - self.cr2[i + 1 - look]) / look, 1e-12))


def p_up(spot, start, t, dur=300, look=900, vol_mult=1.0):
    """Probability that Up wins, using information up to and including second t (start <= t < start+dur)."""
    end = start + dur
    K = spot.mean(start - TW, start)
    s = spot.px(t)
    sig = spot.sigma(t, look) * s * vol_mult
    tau = end - (t + 1)                   # seconds still unknown
    if tau <= 0:
        return 1.0 if spot.mean(end - TW, end) >= K else 0.0
    if tau >= TW:
        mean, var = s, sig * sig * (tau - TW + TW / 3.0)
    else:
        m = TW - tau                      # already realized seconds of the averaging window
        a = spot.mean(end - TW, t + 1)
        mean = (m * a + tau * s) / TW
        var = (tau / TW) ** 2 * sig * sig * tau / 3.0
    return phi((mean - K) / math.sqrt(var + 1e-12))
