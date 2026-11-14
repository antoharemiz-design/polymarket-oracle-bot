"""Account-level risk control. Per-market limits (max net / gross shares) live in strategy.Params."""
import time
from dataclasses import dataclass


@dataclass
class RiskLimits:
    daily_stop_usd: float = 25.0        # stop for the rest of the UTC day at this realized loss
    max_open_usd: float = 300.0         # cap on dollars committed to unresolved markets
    spot_stale_ms: int = 3000           # pull quotes when the spot feed is older than this
    book_stale_ms: int = 5000           # pull quotes when the market feed is older than this
    max_consecutive_errors: int = 5     # executor errors in a row before the bot stops quoting


class Risk:
    def __init__(self, limits: RiskLimits):
        self.l = limits
        self.day = None
        self.day_pnl = 0.0
        self.open_cost = {}              # market start -> dollars committed, until settled
        self.errors = 0
        self.stopped_reason = None

    def _roll(self, now_ms: int) -> None:
        day = time.strftime("%Y%m%d", time.gmtime(now_ms / 1000))
        if day != self.day:
            self.day, self.day_pnl = day, 0.0
            if self.stopped_reason == "daily_stop":
                self.stopped_reason = None

    def on_settled(self, now_ms: int, market: int, pnl: float) -> None:
        self._roll(now_ms)
        self.day_pnl += pnl
        self.open_cost.pop(market, None)
        if self.day_pnl <= -self.l.daily_stop_usd:
            self.stopped_reason = "daily_stop"

    def on_error(self) -> None:
        self.errors += 1
        if self.errors >= self.l.max_consecutive_errors:
            self.stopped_reason = "executor_errors"

    def on_ok(self) -> None:
        self.errors = 0

    def may_open(self, now_ms: int) -> tuple[bool, str]:
        """May a new market session start quoting?"""
        self._roll(now_ms)
        if self.stopped_reason:
            return False, self.stopped_reason
        if sum(self.open_cost.values()) >= self.l.max_open_usd:
            return False, "max_open_usd"
        return True, ""

    def halt_reason(self, now_ms: int, market: int, cost: float, spot_ms: int, book_ms: int) -> str:
        """Reason to pull quotes right now in one market, or '' to keep quoting."""
        self._roll(now_ms)
        self.open_cost[market] = cost
        if self.stopped_reason:
            return self.stopped_reason
        if now_ms - spot_ms > self.l.spot_stale_ms:
            return "spot_stale"
        if now_ms - book_ms > self.l.book_stale_ms:
            return "book_stale"
        if sum(self.open_cost.values()) > self.l.max_open_usd:
            return "max_open_usd"
        return ""
