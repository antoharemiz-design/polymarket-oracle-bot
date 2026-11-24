"""Live execution through the official `polymarket-client` SDK.

Rules this module enforces (they answer the ghost-fill and orphan-order bugs of the old bot):
  * Maker quotes are post-only limit BUYs. Nothing is ever replaced in place: an order is
    cancelled, the cancellation is confirmed, and only then may a new one be placed on that side.
  * Taker orders are fill-and-kill BUYs capped at the price seen when the decision was made.
  * A fill is counted only from the cumulative `size_matched` of an order reported on the
    authenticated user stream. A cancelled or vanished order is never counted as filled.
  * If the result of a request is unknown (timeout, transport error), every order of ours in
    that market is cancelled before quoting continues.
  * The on-chain token balance is the source of truth for inventory: it is read after quoting
    stops and any shortfall in tracked inventory is adopted and journaled.

NOT verified against the live exchange in the development environment (no trading keys there).
Run `python -m polybot check` first, then start with the minimum size.
"""
import asyncio
import time
from decimal import Decimal

from .engine import Order


def now_ms() -> int:
    return int(time.time() * 1000)


class LiveClient:
    """One authenticated SDK client, the user stream and the optional order heartbeat."""

    def __init__(self, private_key: str, wallet: str | None, relayer_key: str | None,
                 relayer_address: str | None, risk, log=print, heartbeat: bool = False):
        self.private_key, self.wallet = private_key, wallet or None
        self.relayer_key, self.relayer_address = relayer_key, relayer_address
        self.risk, self.log, self.heartbeat = risk, log, heartbeat
        self.client = None
        self.by_order = {}            # order_id -> (MarketExec, side)
        self.early = {}               # order_id -> [payload, ...] seen before the id was registered
        self.tasks = []

    async def start(self):
        from polymarket import AsyncSecureClient
        from polymarket.auth import RelayerApiKey
        api_key = None
        if self.relayer_key and self.relayer_address:
            api_key = RelayerApiKey(key=self.relayer_key, address=self.relayer_address)
        self.client = await AsyncSecureClient.create(private_key=self.private_key, wallet=self.wallet, api_key=api_key)
        self.tasks.append(asyncio.create_task(self._user_stream()))
        if self.heartbeat:
            self.tasks.append(asyncio.create_task(self._heartbeat()))
        return self

    async def _user_stream(self):
        from polymarket.streams import UserSpec
        while True:
            try:
                async with await self.client.subscribe([UserSpec()]) as stream:
                    self.log("user stream connected")
                    async for ev in stream:
                        if getattr(ev, "type", None) == "order":
                            self._route(ev.payload)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"user stream error: {e!r}"); await asyncio.sleep(1)

    def _route(self, payload):
        target = self.by_order.get(payload.id)
        if target is None:
            self.early.setdefault(payload.id, []).append(payload)
            return
        target[0].on_order_event(target[1], payload)

    def register(self, order_id, mexec, side):
        self.by_order[order_id] = (mexec, side)
        for payload in self.early.pop(order_id, []):
            mexec.on_order_event(side, payload)

    async def _heartbeat(self):
        from polymarket import OrderHeartbeatMismatchError
        hb = ""
        while True:
            try:
                try:
                    r = await self.client.send_order_heartbeat(heartbeat_id=hb)
                except OrderHeartbeatMismatchError as e:
                    r = await self.client.send_order_heartbeat(heartbeat_id=e.heartbeat_id)
                hb = r.heartbeat_id
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"heartbeat error: {e!r}"); self.risk.on_error()
            await asyncio.sleep(5)

    async def close(self):
        try:
            if self.client is not None:
                r = await self.client.cancel_all()
                self.log(f"cancel_all on shutdown: {len(r.canceled)} canceled, {len(r.not_canceled)} not canceled")
        except Exception as e:
            self.log(f"cancel_all failed: {e!r}")
        for t in self.tasks:
            t.cancel()
        if self.client is not None:
            await self.client.close()


class MarketExec:
    """Executor for one market session. Same interface as engine.PaperExecutor."""

    def __init__(self, live: LiveClient, market: dict):
        self.live, self.m = live, market
        self.orders = {"B": None, "S": None}
        self.matched = {}             # order_id -> cumulative shares already counted
        self.session = None
        self.taker = None
        self.busy = {"B": False, "S": False}

    def _token(self, side):
        return self.m["token_up"] if side == "B" else self.m["token_down"]

    def order(self, side):
        return self.orders[side]

    def place(self, side, price_c, size, now):
        if self.orders[side] is not None or self.busy[side]:
            return
        o = Order(side, price_c, size, "PENDING")
        self.orders[side] = o
        asyncio.create_task(self._place(o))

    async def _place(self, o):
        side = o.side
        paid = o.price if side == "B" else 100 - o.price
        try:
            resp = await self.live.client.place_limit_order(
                token_id=self._token(side), price=str(Decimal(paid) / 100), size=str(o.rem), side="BUY", post_only=True)
        except Exception as e:
            self.live.log(f"place failed with unknown outcome ({e!r}); cancelling all orders in market")
            self.live.risk.on_error()
            await self._cancel_market()
            return
        self.live.risk.on_ok()
        if getattr(resp, "ok", False):
            o.order_id = resp.order_id
            self.matched.setdefault(o.order_id, Decimal(0))
            if self.orders[side] is o:
                o.state = "LIVE"
            self.live.register(o.order_id, self, side)
        else:
            if self.orders[side] is o:
                self.orders[side] = None
            self.session.on_reject(side, o.price, now_ms(), getattr(resp, "code", "rejected"))

    def cancel(self, side, now):
        o = self.orders[side]
        if o is None or o.state != "LIVE":
            return
        o.state = "CANCELING"
        self.busy[side] = True
        asyncio.create_task(self._cancel(o))

    async def _cancel(self, o):
        side = o.side
        try:
            for attempt in range(3):
                try:
                    resp = await self.live.client.cancel_order(order_id=o.order_id)
                    break
                except Exception as e:
                    self.live.log(f"cancel attempt {attempt + 1} failed: {e!r}")
                    self.live.risk.on_error()
                    await asyncio.sleep(0.05 * (attempt + 1))
            else:
                await self._cancel_market()
                return
            self.live.risk.on_ok()
            # canceled, or already gone (matched / expired): either way it no longer rests.
            # Any fill of it still arrives through the user stream and is counted there.
            if self.orders[side] is o:
                self.orders[side] = None
            if o.order_id in resp.canceled:
                self.session.on_canceled(side, o.price, now_ms())
            else:
                self.session._log(now_ms(), "cancel_not_applied", side=side, price=o.price,
                                  reason=resp.not_canceled.get(o.order_id, "unknown"))
        finally:
            self.busy[side] = False

    async def _cancel_market(self):
        """Last resort: remove every order of ours in this market and forget local order state."""
        for attempt in range(5):
            try:
                await self.live.client.cancel_market_orders(market=self.m["condition_id"])
                break
            except Exception as e:
                self.live.log(f"cancel_market_orders failed: {e!r}"); await asyncio.sleep(0.2 * (attempt + 1))
        self.orders = {"B": None, "S": None}
        self.busy = {"B": False, "S": False}
        if self.session is not None:
            self.session._log(now_ms(), "cancel_market", reason="unknown_order_state")

    def take(self, side, limit_c, size, now):
        """Marketable FAK buy capped at the price seen at decision time (Up-token cents)."""
        if self.taker is not None:
            return
        self.taker = (now, side, limit_c, size)
        asyncio.create_task(self._take(side, limit_c, size))

    async def _take(self, side, limit_c, size):
        paid_cap = limit_c if side == "B" else 100 - limit_c
        try:
            resp = await self.live.client.place_market_order(
                token_id=self._token(side), side="BUY", shares=str(size),
                max_price=str(Decimal(paid_cap) / 100), order_type="FAK")
        except Exception as e:
            # outcome unknown: the user stream or the balance reconciliation will reveal any fill
            self.live.log(f"take failed with unknown outcome: {e!r}")
            self.live.risk.on_error()
            self.taker = None
            return
        self.live.risk.on_ok()
        try:
            if getattr(resp, "ok", False):
                got = Decimal(resp.taking_amount)
                if got > 0:
                    paid_c = float(Decimal(resp.making_amount) / got * 100)
                    self.matched[resp.order_id] = got            # later stream events must not double count
                    self.session.on_fill(side, paid_c if side == "B" else 100 - paid_c, float(got), now_ms(), True)
                self.live.register(resp.order_id, self, side)
            else:
                self.session.on_reject(side, limit_c, now_ms(), getattr(resp, "code", "rejected"))
        finally:
            self.taker = None

    def on_order_event(self, side, payload):
        prev = self.matched.get(payload.id, Decimal(0))
        cur = Decimal(payload.size_matched)
        o = self.orders[side]
        if cur > prev:
            self.matched[payload.id] = cur
            price_c = int(round(float(payload.price) * 100))
            self.session.on_fill(side, price_c if side == "B" else 100 - price_c, float(cur - prev), now_ms(), False)
        done = payload.order_event_type == "CANCELLATION" or cur >= Decimal(payload.original_size)
        if done and o is not None and o.order_id == payload.id:
            self.orders[side] = None

    async def balances(self):
        """On-chain shares (Up, Down) held by the wallet."""
        out = []
        for tok in (self.m["token_up"], self.m["token_down"]):
            b = await self.live.client.get_balance_allowance(asset_type="CONDITIONAL", token_id=tok)
            out.append(b.balance / 1e6)
        return out

    async def reconcile(self):
        """Adopt the on-chain balance when it exceeds tracked inventory (a fill we never saw)."""
        try:
            up, dn = await self.balances()
        except Exception as e:
            self.live.log(f"balance read failed: {e!r}"); return
        r = self.session.res
        self.session._log(now_ms(), "reconcile", chain_up=round(up, 4), chain_dn=round(dn, 4))
        for side, chain, tracked in (("B", up, r.q_up), ("S", dn, r.q_dn)):
            if chain > tracked + 0.01:
                # price unknown: book it at 50c so the journal shows it, PnL is settled from balances anyway
                self.session.on_fill(side, 50, chain - tracked, now_ms(), False)

    async def close(self):
        await self._cancel_market()


class LiveMultiExec(MarketExec):
    """Live executor for ladder mode: any number of post-only BUY orders keyed by (side, price).
    Same rules as MarketExec: fills only from cumulative size_matched on the user stream,
    cancellation confirmed before a level can be re-placed, unknown outcomes cancel the market."""

    def __init__(self, live: LiveClient, market: dict):
        super().__init__(live, market)
        self.multi = {}

    def all_orders(self):
        return list(self.multi.values())

    def order(self, side):
        return None

    def place_at(self, side, price_c, size, now):
        key = (side, price_c)
        if key in self.multi:
            return
        o = Order(side, price_c, size, "PENDING")
        self.multi[key] = o
        asyncio.create_task(self._place_key(key, o))

    async def _place_key(self, key, o):
        side = o.side
        paid = o.price if side == "B" else 100 - o.price
        try:
            resp = await self.live.client.place_limit_order(
                token_id=self._token(side), price=str(Decimal(paid) / 100), size=str(o.rem), side="BUY", post_only=True)
        except Exception as e:
            self.live.log(f"ladder place failed with unknown outcome ({e!r}); cancelling all orders in market")
            self.live.risk.on_error()
            await self._cancel_market()
            return
        self.live.risk.on_ok()
        if getattr(resp, "ok", False):
            o.order_id = resp.order_id
            self.matched.setdefault(o.order_id, Decimal(0))
            if self.multi.get(key) is o:
                o.state = "LIVE"
            self.live.register(o.order_id, self, key)
        else:
            if self.multi.get(key) is o:
                del self.multi[key]
            self.session.on_reject(side, o.price, now_ms(), getattr(resp, "code", "rejected"))

    def cancel_at(self, key, now):
        o = self.multi.get(key)
        if o is None or o.state != "LIVE":
            return
        o.state = "CANCELING"
        asyncio.create_task(self._cancel_key(key, o))

    async def _cancel_key(self, key, o):
        for attempt in range(3):
            try:
                resp = await self.live.client.cancel_order(order_id=o.order_id)
                break
            except Exception as e:
                self.live.log(f"ladder cancel attempt {attempt + 1} failed: {e!r}")
                self.live.risk.on_error()
                await asyncio.sleep(0.05 * (attempt + 1))
        else:
            await self._cancel_market()
            return
        self.live.risk.on_ok()
        if self.multi.get(key) is o:
            del self.multi[key]
        if o.order_id in resp.canceled:
            self.session.on_canceled(o.side, o.price, now_ms())
        else:
            self.session._log(now_ms(), "cancel_not_applied", side=o.side, price=o.price,
                              reason=resp.not_canceled.get(o.order_id, "unknown"))

    async def _cancel_market(self):
        await super()._cancel_market()
        self.multi = {}

    def on_order_event(self, key, payload):
        side = key[0]
        prev = self.matched.get(payload.id, Decimal(0))
        cur = Decimal(payload.size_matched)
        if cur > prev:
            self.matched[payload.id] = cur
            price_c = int(round(float(payload.price) * 100))
            self.session.on_fill(side, price_c if side == "B" else 100 - price_c, float(cur - prev), now_ms(), False)
        done = payload.order_event_type == "CANCELLATION" or cur >= Decimal(payload.original_size)
        o = self.multi.get(key)
        if done and o is not None and o.order_id == payload.id:
            del self.multi[key]
