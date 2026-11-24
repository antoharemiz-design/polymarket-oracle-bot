#!/usr/bin/env python3
"""
Polymarket V12 TURBO — Oracle-Enhanced Redeem Arbitrage Market Maker
(Binance Oracle + Directional Trading + Gasless Redeem + CLOB V2)
"""

import os
import json
import time
import asyncio
import logging
import warnings
import signal
import aiosqlite
import numpy as np
import math
import websockets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
# ------------------- V2 Migration: New imports -------------------
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import OrderArgs, PostOrdersArgs
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
# -------------------------------------------------------------------

warnings.simplefilter("ignore")
load_dotenv(dotenv_path=Path('../polymarket-bot') / '.env')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("REDEEM_TURBO")
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
FUNDER_ADDR = os.getenv("FUNDER_ADDRESS", "").strip()
API_CREDS_FILE = "../polymarket-bot/.api_creds_cache.json"

# ------------------- V2 Migration: New endpoints -------------------
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com/markets"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
# -------------------------------------------------------------------

# ── Binance Oracle ──
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@kline_1s"
ORACLE_WINDOW_SEC = 300
IMPULSE_THRESHOLD = 2.0
MEAN_REVERSION_THRESHOLD = 0.3
MIN_MISPRICING = 0.03
MAX_MISPRICING = 0.40
MIN_CONFIDENCE = 0.35
KELLY_FRACTION = 0.3
MAX_IMBALANCE_USDC = 25.0

# ── Safety ──
DRY_RUN = True
MAX_DRAWDOWN_USDC = 50.0

# ── Strategy Parameters ──
BASE_ORDER_SIZE = 10.0
MAX_TOTAL_EXPOSURE = 80.0
REDEEM_THRESHOLD = 1.0
NUM_LEVELS = 3
LEVEL_SPACING = 0.02
LEVEL_SIZE_DECAY = 0.7

# ── Sell Strategy ──
ENABLE_SELLING = True
SELL_MISPRICING_THRESHOLD = 0.03
MAX_SELL_SIZE = 10.0

# ── Quant ──
BASE_GAMMA = 0.015
SPREAD_MULTIPLIER = 0.4
MIN_PRICE = 0.01
MAX_PRICE = 0.99

# ── Timing ──
REQUOTE_INTERVAL = 0.5
FILL_POLL_INTERVAL = 1.0
STATE_SAVE_INTERVAL = 10.0
PRICE_CHANGE_THRESHOLD = 0.01

DB_FILE = "../polymarket-bot/bot_state_v12_turbo.sqlite"
STOP_EVENT = asyncio.Event()
ws_task = None


# ==============================================================================
# 2. DATA STRUCTURES
# ==============================================================================
def norm_cdf(x):
    """Аппроксимация кумулятивной функции нормального распределения."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class BinanceOracle:
    """
    Поток цен BTC с Binance по WebSocket (kline 1s).
    Вычисляет волатильность, z-score и справедливую цену бинарного опциона.
    """

    def __init__(self):
        self.spot_price: float = 0.0
        self.price_history = []
        self.volatility: float = 0.02
        self.last_update: float = 0.0
        self.z_score: float = 0.0
        self.lag_seconds: float = 0.0
        self._lock = asyncio.Lock()

    async def run(self):
        """Подключение к Binance WS, бесконечный приём данных."""
        while True:
            try:
                async with websockets.connect(BINANCE_WS_URL, ping_interval=20) as ws:
                    logger.info("🔗 Binance oracle connected")
                    while True:
                        raw = await ws.recv()
                        data = json.loads(raw)
                        k = data.get('k', {})
                        if k.get('x'):
                            price = float(k['c'])
                            async with self._lock:
                                self.spot_price = price
                                self.price_history.append(price)
                                if len(self.price_history) > ORACLE_WINDOW_SEC:
                                    self.price_history.pop(0)
                                self._update_metrics()
                                self.last_update = time.monotonic()
            except Exception as e:
                logger.error(f"Binance oracle error: {e}, reconnect in 5s")
                await asyncio.sleep(5)

    async def get_state(self) -> dict:
        async with self._lock:
            return {
                'spot': self.spot_price,
                'vol': self.volatility,
                'z_score': self.z_score,
                'lag': self.lag_seconds,
            }

    def _update_metrics(self):
        prices = np.array(self.price_history, dtype=float)
        if len(prices) < 30:
            return
        log_returns = np.diff(np.log(prices + 1e-12))
        self.volatility = np.std(log_returns) * np.sqrt(365 * 24 * 3600)
        mean_p = np.mean(prices)
        std_p = np.std(prices) + 1e-9
        self.z_score = (self.spot_price - mean_p) / std_p
        self.lag_seconds = 0
        for i in range(1, len(prices)):
            past_mean = np.mean(prices[:-i])
            past_std = np.std(prices[:-i]) + 1e-9
            past_z = (prices[-(i + 1)] - past_mean) / past_std
            if abs(past_z) >= IMPULSE_THRESHOLD:
                self.lag_seconds = i
                break

    def fair_value(self, strike: float, T_sec: float) -> dict | None:
        """Справедливая цена бинарного опциона по Black-Scholes."""
        if self.spot_price <= 0 or self.volatility < 0.001 or T_sec < 2:
            return None
        T = T_sec / (365.0 * 24 * 3600)
        S = self.spot_price
        K = strike
        sigma = max(self.volatility, 0.01)
        d1 = (np.log(S / K) + (sigma ** 2 / 2) * T) / (sigma * np.sqrt(T) + 1e-12)
        d2 = d1 - sigma * np.sqrt(T)
        prob = norm_cdf(d2)
        confidence = 1.0 / (1.0 + sigma * np.sqrt(T) * 5)
        return {
            'price_yes': prob,
            'price_no': 1.0 - prob,
            'confidence': max(0.0, min(1.0, confidence)),
            'd1': d1, 'd2': d2,
        }

    @staticmethod
    def get_strike_from_slug(slug: str) -> float:
        """Извлекает страйк из slug вида btc-updown-15m-67000."""
        parts = slug.split('-')
        if len(parts) >= 4:
            try:
                return float(parts[-1])
            except ValueError:
                pass
        return 0.0


class MarketInfo:
    """Immutable market metadata for one round."""
    __slots__ = ('slug', 'end', 'token_yes', 'token_no', 'condition_id')

    def __init__(self, slug: str, end: datetime,
                 token_yes: str, token_no: str, condition_id: str = ""):
        self.slug = slug
        self.end = end
        self.token_yes = token_yes
        self.token_no = token_no
        self.condition_id = condition_id


class OrderBook:
    """Real-time orderbook state from WebSocket + REST fallback."""

    def __init__(self):
        self.best_bid_yes: float = 0.0
        self.best_ask_yes: float = 1.0
        self.best_bid_no: float = 0.0
        self.best_ask_no: float = 1.0
        self.last_update: float = 0.0

    @property
    def mid_yes(self) -> float:
        return (self.best_bid_yes + self.best_ask_yes) / 2.0

    @property
    def mid_no(self) -> float:
        return (self.best_bid_no + self.best_ask_no) / 2.0

    @property
    def spread_yes(self) -> float:
        return max(0.01, self.best_ask_yes - self.best_bid_yes)

    @property
    def spread_no(self) -> float:
        return max(0.01, self.best_ask_no - self.best_bid_no)

    @property
    def combined_ask(self) -> float:
        return self.best_ask_yes + self.best_ask_no

    @property
    def theoretical_edge(self) -> float:
        return 1.0 - self.combined_ask

    def update(self, side: str, bids: list, asks: list):
        if side == 'yes':
            if bids:
                self.best_bid_yes = max(float(b['price']) for b in bids)
            if asks:
                self.best_ask_yes = min(float(a['price']) for a in asks)
        else:
            if bids:
                self.best_bid_no = max(float(b['price']) for b in bids)
            if asks:
                self.best_ask_no = min(float(a['price']) for a in asks)
        self.last_update = time.monotonic()


class Inventory:
    """Position tracking for redeem-only strategy."""

    def __init__(self):
        self.qty_yes: float = 0.0
        self.qty_no: float = 0.0
        self.cost_yes: float = 0.0
        self.cost_no: float = 0.0
        self.realized_pnl: float = 0.0
        self.total_redeemed: float = 0.0

    @property
    def avg_price_yes(self) -> float:
        return self.cost_yes / self.qty_yes if self.qty_yes > 0.001 else 0.0

    @property
    def avg_price_no(self) -> float:
        return self.cost_no / self.qty_no if self.qty_no > 0.001 else 0.0

    @property
    def redeemable_pairs(self) -> float:
        return min(self.qty_yes, self.qty_no)

    @property
    def imbalance(self) -> float:
        return self.qty_yes - self.qty_no

    @property
    def total_exposure(self) -> float:
        return self.cost_yes + self.cost_no

    def unrealized_pnl(self, book: OrderBook) -> float:
        mark_yes = self.qty_yes * book.mid_yes
        mark_no = self.qty_no * book.mid_no
        return (mark_yes + mark_no) - (self.cost_yes + self.cost_no)

    def total_pnl(self, book: OrderBook) -> float:
        return self.realized_pnl + self.unrealized_pnl(book)

    def record_buy(self, token: str, qty: float, price: float):
        if token == 'yes':
            self.cost_yes += qty * price
            self.qty_yes += qty
        else:
            self.cost_no += qty * price
            self.qty_no += qty
        logger.info(
            f"📥 Bought {qty:.1f} {token.upper()} @ ${price:.2f} | "
            f"Position: YES={self.qty_yes:.1f} NO={self.qty_no:.1f} | "
            f"Pairs ready: {self.redeemable_pairs:.1f}"
        )

    def record_sell(self, token: str, qty: float, price: float):
        if token == 'yes':
            self.cost_yes = max(0.0, self.cost_yes - qty * price)
            self.qty_yes = max(0.0, self.qty_yes - qty)
        else:
            self.cost_no = max(0.0, self.cost_no - qty * price)
            self.qty_no = max(0.0, self.qty_no - qty)
        logger.info(
            f"📤 Sold {qty:.1f} {token.upper()} @ ${price:.2f} | "
            f"Position: YES={self.qty_yes:.1f} NO={self.qty_no:.1f}"
        )

    def record_redeem(self, qty: float) -> float:
        if qty < 0.01:
            return 0.0
        cost_per_pair = self.avg_price_yes + self.avg_price_no
        profit = qty * (1.0 - cost_per_pair)
        self.cost_yes -= qty * self.avg_price_yes
        self.cost_no -= qty * self.avg_price_no
        self.qty_yes -= qty
        self.qty_no -= qty
        if self.qty_yes < 0.001:
            self.qty_yes = 0.0
            self.cost_yes = 0.0
        if self.qty_no < 0.001:
            self.qty_no = 0.0
            self.cost_no = 0.0
        self.realized_pnl += profit
        self.total_redeemed += qty
        return profit

    def to_dict(self) -> dict:
        return {
            'qty_yes': self.qty_yes, 'qty_no': self.qty_no,
            'cost_yes': self.cost_yes, 'cost_no': self.cost_no,
            'realized_pnl': self.realized_pnl,
            'total_redeemed': self.total_redeemed,
        }

    def from_dict(self, d: dict):
        self.qty_yes = d.get('qty_yes', 0.0)
        self.qty_no = d.get('qty_no', 0.0)
        self.cost_yes = d.get('cost_yes', 0.0)
        self.cost_no = d.get('cost_no', 0.0)
        self.realized_pnl = d.get('realized_pnl', 0.0)
        self.total_redeemed = d.get('total_redeemed', 0.0)


class ActiveOrders:
    """Thread-safe registry of our live orders on the exchange."""

    def __init__(self):
        self._orders: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def add(self, order_id: str, info: dict):
        async with self._lock:
            self._orders[order_id] = info

    async def add_many(self, placed: list[dict]):
        async with self._lock:
            for p in placed:
                oid = p.pop('id')
                self._orders[oid] = p

    async def remove(self, order_id: str) -> Optional[dict]:
        async with self._lock:
            return self._orders.pop(order_id, None)

    async def get_all(self) -> list[dict]:
        async with self._lock:
            return [{'id': k, **v} for k, v in self._orders.items()]

    async def get_all_ids(self) -> list[str]:
        async with self._lock:
            return list(self._orders.keys())

    async def clear(self):
        async with self._lock:
            self._orders.clear()

    async def snapshot_for_save(self) -> list[dict]:
        async with self._lock:
            return [{'id': k, **v} for k, v in self._orders.items()]

    async def load_from_save(self, saved: list[dict]):
        async with self._lock:
            self._orders.clear()
            for o in saved:
                oid = o.get('id')
                if oid:
                    self._orders[oid] = {
                        k: v for k, v in o.items() if k != 'id'
                    }

    @property
    def count(self) -> int:
        return len(self._orders)

    @property
    def is_empty(self) -> bool:
        return len(self._orders) == 0


# ==============================================================================
# 3. EXCHANGE LAYER
# ==============================================================================
class Exchange:
    """All Polymarket API interactions."""

    def __init__(self):
        self.client: Optional[ClobClient] = None
        self._lock = asyncio.Lock()

    async def init_client(self):
        if not PRIVATE_KEY or not FUNDER_ADDR:
            raise RuntimeError(
                "POLYMARKET_PRIVATE_KEY or FUNDER_ADDRESS missing in .env"
            )

        # ------------------- V2 Migration: Constructor -------------------
        self.client = ClobClient(
            host=CLOB_API,
            chain=137,
            signer=PRIVATE_KEY,
            creds=None
        )
        # -------------------------------------------------------------------

        creds = self._load_cached_creds()
        if creds:
            try:
                self.client.set_api_creds(creds)
                await asyncio.to_thread(self.client.get_ok)
                logger.info("🔑 Cached API credentials valid")
                return
            except Exception:
                logger.info("🔑 Cached credentials expired")

        creds = await asyncio.to_thread(
            self.client.create_or_derive_api_creds
        )
        self.client.set_api_creds(creds)
        self._save_cached_creds(creds)
        logger.info("🔑 New API credentials derived and cached")

    def _load_cached_creds(self):
        try:
            if not os.path.exists(API_CREDS_FILE):
                return None
            with open(API_CREDS_FILE) as f:
                d = json.load(f)
            from py_clob_client_v2.clob_types import ApiCreds
            return ApiCreds(
                api_key=d['api_key'],
                api_secret=d['api_secret'],
                api_passphrase=d['api_passphrase'],
            )
        except Exception:
            return None

    def _save_cached_creds(self, creds):
        try:
            with open(API_CREDS_FILE, 'w') as f:
                json.dump({
                    'api_key': creds.api_key,
                    'api_secret': creds.api_secret,
                    'api_passphrase': creds.api_passphrase,
                }, f)
        except Exception as e:
            logger.warning(f"Could not cache creds: {e}")

    async def get_balance(self) -> float:
        if not self.client:
            return 0.0
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            resp = await asyncio.to_thread(
                self.client.get_balance_allowance, params
            )
            return float(resp.get('balance', 0)) / 1_000_000
        except Exception as e:
            logger.error(f"Balance check error: {e}")
            return 0.0

    async def fetch_orderbook(self, token_id: str) -> dict:
        if not self.client:
            return {'bids': [], 'asks': []}
        try:
            book = await asyncio.to_thread(
                self.client.get_order_book, token_id
            )
            return {
                'bids': [
                    {'price': str(b.price), 'size': str(b.size)}
                    for b in (book.bids or [])
                ],
                'asks': [
                    {'price': str(a.price), 'size': str(a.size)}
                    for a in (book.asks or [])
                ],
            }
        except Exception as e:
            logger.error(f"Orderbook fetch error: {e}")
            return {'bids': [], 'asks': []}

    async def get_open_order_ids(self) -> set[str]:
        if not self.client:
            return set()
        try:
            resp = await asyncio.to_thread(self.client.get_orders)
            if isinstance(resp, list):
                return {
                    o.get('id') or o.get('orderID')
                    for o in resp
                    if isinstance(o, dict) and (o.get('id') or o.get('orderID'))
                }
            return set()
        except Exception as e:
            logger.debug(f"get_open_order_ids error: {e}")
            return set()

    async def get_open_orders_with_sizes(self) -> dict[str, float]:
        """Return dict {order_id: remaining_size} for our open orders."""
        if not self.client:
            return {}
        try:
            resp = await asyncio.to_thread(self.client.get_orders)
            result = {}
            for o in (resp if isinstance(resp, list) else []):
                oid = o.get('id') or o.get('orderID')
                if oid:
                    size = float(o.get('size', o.get('original_size', 0)))
                    result[oid] = size
            return result
        except Exception as e:
            logger.error(f"get_open_orders_with_sizes: {e}")
            return {}

    async def cancel_orders(self, order_ids: list[str]):
        if not order_ids or not self.client or DRY_RUN:
            return
        async with self._lock:
            try:
                for i in range(0, len(order_ids), 50):
                    chunk = order_ids[i:i + 50]
                    await asyncio.to_thread(
                        self.client.cancel_orders, chunk
                    )
                logger.info(f"❌ Cancelled {len(order_ids)} orders")
            except Exception as e:
                logger.error(f"Cancel error: {e}")

    async def cancel_and_place(
            self,
            cancel_ids: list[str],
            new_orders: list[dict],
            market: MarketInfo,
    ) -> list[dict]:
        if DRY_RUN:
            for o in new_orders:
                logger.info(
                    f"🏜️ [DRY] {o['side']} {o['token'].upper()} "
                    f"{o['size']:.1f} @ ${o['price']:.2f}"
                )
            return []

        if not self.client:
            return []

        async with self._lock:
            if cancel_ids:
                try:
                    for i in range(0, len(cancel_ids), 50):
                        await asyncio.to_thread(
                            self.client.cancel_orders,
                            cancel_ids[i:i + 50],
                        )
                except Exception as e:
                    logger.error(f"Cancel in atomic op: {e}")

            if not new_orders:
                return []

            total_cost = sum(o['size'] * o['price'] for o in new_orders if o['side'] == 'BUY')
            balance = await self.get_balance()
            if balance < total_cost * 1.05:
                logger.warning(
                    f"⚠️ Insufficient balance: need ${total_cost:.2f}, "
                    f"have ${balance:.2f}"
                )
                if balance > 1.0:
                    scale = (balance * 0.9) / total_cost
                    for o in new_orders:
                        o['size'] = round(o['size'] * scale, 1)
                    new_orders = [o for o in new_orders if o['size'] >= 1.0]
                    if not new_orders:
                        return []
                    logger.info(
                        f"📉 Scaled orders down by {scale:.1%}"
                    )
                else:
                    return []

            post_args = []
            records = []
            for o in new_orders:
                token_id = (
                    market.token_yes if o['token'] == 'yes'
                    else market.token_no
                )
                try:
                    signed = self.client.create_order(OrderArgs(
                        price=o['price'],
                        size=o['size'],
                        side=o['side'],
                        token_id=token_id,
                    ))
                    post_args.append(PostOrdersArgs(order=signed))
                    records.append(o)
                except Exception as e:
                    logger.error(f"Sign error for {o}: {e}")

            if not post_args:
                return []

            placed = []
            for i in range(0, len(post_args), 14):
                chunk = post_args[i:i + 14]
                chunk_recs = records[i:i + 14]
                try:
                    resp = await asyncio.to_thread(
                        self.client.post_orders, chunk
                    )

                    resp_list = []
                    if isinstance(resp, list):
                        resp_list = resp
                    elif isinstance(resp, dict):
                        if resp.get("error"):
                            logger.error(
                                f"🚫 Exchange error: {resp['error']}"
                            )
                        resp_list = resp.get("responses", [resp])

                    for j, r in enumerate(resp_list):
                        rd = (
                            r if isinstance(r, dict)
                            else vars(r) if hasattr(r, '__dict__')
                            else {}
                        )
                        rec = chunk_recs[j] if j < len(chunk_recs) else {}

                        success = (
                                rd.get("success")
                                or rd.get("status") in ("live", "matched")
                        )
                        oid = rd.get("orderID") or rd.get("id")

                        if success and oid:
                            placed.append({
                                'id': oid,
                                'side': rec.get('side'),
                                'token': rec.get('token'),
                                'size': rec.get('size'),
                                'price': rec.get('price'),
                            })
                            logger.info(
                                f"✅ {rec['side']} {rec['token'].upper()} "
                                f"{rec['size']:.1f} @ ${rec['price']:.2f}"
                            )
                        elif rec:
                            logger.error(f"🚫 Rejected: {rec} → {rd}")

                except Exception as e:
                    logger.error(f"Post orders chunk error: {e}")

            return placed

    async def redeem_pairs(
            self, qty: float, market: MarketInfo
    ) -> bool:
        if qty * 1_000_000 < 100000 or not self.client or DRY_RUN:
            if DRY_RUN:
                logger.info(f"🏜️ [DRY] Would redeem {qty:.1f} pairs")
            return False

        async with self._lock:
            try:
                headers = self.client._get_auth_headers()
                relayer_key = os.getenv("POLYMARKET_RELAYER_KEY", "").strip()
                relayer_address = os.getenv("POLYMARKET_RELAYER_ADDRESS", "").strip()

                if relayer_key and relayer_address:
                    headers["RELAYER_API_KEY"] = relayer_key
                    headers["RELAYER_API_KEY_ADDRESS"] = relayer_address

                resp = await asyncio.to_thread(
                    self.client.session.post,
                    f"{CLOB_API}/redeem",
                    json={
                        "conditionId": market.condition_id,
                        "amount": str(int(qty * 1_000_000)),
                    },
                    headers=headers,
                )

                if resp.status_code == 200:
                    logger.info(f"💸 Redeemed {qty:.1f} pairs via Gasless Relayer")
                    return True
                else:
                    logger.error(f"Redeem API error {resp.status_code}: {resp.text}")
                    return False

            except Exception as e:
                logger.error(f"❌ Redeem failed: {e}")
                return False


# ==============================================================================
# 4. QUOTING ENGINE
# ==============================================================================
class OracleQuoter:
    def __init__(self, book: OrderBook, inventory: Inventory, oracle: BinanceOracle):
        self.book = book
        self.inv = inventory
        self.oracle = oracle
        self._prev_quotes: list[dict] = []

    def should_requote(self, new_quotes: list[dict]) -> bool:
        if len(new_quotes) != len(self._prev_quotes):
            return True
        old_sorted = sorted(self._prev_quotes, key=lambda x: (x['token'], x['price']))
        new_sorted = sorted(new_quotes, key=lambda x: (x['token'], x['price']))
        for old, new in zip(old_sorted, new_sorted):
            if abs(old['price'] - new['price']) >= PRICE_CHANGE_THRESHOLD or abs(old['size'] - new['size']) >= 0.5:
                return True
        return False

    def compute_quotes(self, market: MarketInfo, available_balance: float = 0.0) -> list[dict]:
        """Основная логика: сравниваем справедливую цену с рыночной."""
        strike = BinanceOracle.get_strike_from_slug(market.slug)
        if strike == 0.0:
            logger.warning("❌ Cannot parse strike from slug, fallback to neutral")
            return self._fallback_neutral_quotes()

        time_left = (market.end - datetime.now(timezone.utc)).total_seconds()
        if time_left < 15:
            self._prev_quotes = []
            return []

        fv = self.oracle.fair_value(strike, time_left)
        if fv is None or fv['confidence'] < MIN_CONFIDENCE:
            logger.debug("Model confidence low, fallback to neutral quoting")
            return self._fallback_neutral_quotes()

        total_buy_budget = max(0.0, available_balance * 0.9)

        orders = []
        z = self.oracle.z_score
        imbalance = self.inv.qty_yes - self.inv.qty_no
        max_imbalance_usdc = MAX_IMBALANCE_USDC / ((self.book.mid_yes + self.book.mid_no) / 2 + 0.001)

        # ── YES undervalued (BTC растёт) ──
        mispricing_yes = fv['price_yes'] - self.book.mid_yes
        if (MIN_MISPRICING < mispricing_yes < MAX_MISPRICING
                and z > 0
                and imbalance < max_imbalance_usdc
                and time_left > 30):
            size = self._kelly_size(fv['price_yes'], self.book.best_ask_yes)
            size *= min(1.0 + abs(z) * 0.2, 2.5)
            size = min(size, (MAX_TOTAL_EXPOSURE - self.inv.total_exposure) / self.book.best_ask_yes * 0.8)
            if size >= 1.0:
                price = round(self.book.best_bid_yes + 0.01, 2)
                price = min(price, round(self.book.best_ask_yes - 0.01, 2), MAX_PRICE)
                if price >= MIN_PRICE:
                    cost = size * price
                    if cost <= total_buy_budget:
                        orders.append({'side': 'BUY', 'token': 'yes', 'size': round(size, 1), 'price': price})
                        total_buy_budget -= cost

        # ── NO undervalued (BTC падает) ──
        mispricing_no = fv['price_no'] - self.book.mid_no
        if (MIN_MISPRICING < mispricing_no < MAX_MISPRICING
                and z < 0
                and imbalance > -max_imbalance_usdc
                and time_left > 30):
            size = self._kelly_size(fv['price_no'], self.book.best_ask_no)
            size *= min(1.0 + abs(z) * 0.2, 2.5)
            size = min(size, (MAX_TOTAL_EXPOSURE - self.inv.total_exposure) / self.book.best_ask_no * 0.8)
            if size >= 1.0:
                price = round(self.book.best_bid_no + 0.01, 2)
                price = min(price, round(self.book.best_ask_no - 0.01, 2), MAX_PRICE)
                if price >= MIN_PRICE:
                    cost = size * price
                    if cost <= total_buy_budget:
                        orders.append({'side': 'BUY', 'token': 'no', 'size': round(size, 1), 'price': price})
                        total_buy_budget -= cost

        # ── SELL overvalued tokens ──
        if ENABLE_SELLING:
            sell_mispricing_yes = fv['price_yes'] - self.book.mid_yes
            if (self.inv.qty_yes > 1.0 and
                    sell_mispricing_yes < -SELL_MISPRICING_THRESHOLD and
                    time_left > 60):
                sell_size = min(self.inv.qty_yes * 0.5, MAX_SELL_SIZE)
                sell_price = round(self.book.best_bid_yes - 0.01, 2)
                if sell_price >= MIN_PRICE and sell_size >= 1.0:
                    orders.append({
                        'side': 'SELL', 'token': 'yes',
                        'size': round(sell_size, 1),
                        'price': sell_price,
                        'reason': 'YES_OVERVALUED'
                    })

            sell_mispricing_no = fv['price_no'] - self.book.mid_no
            if (self.inv.qty_no > 1.0 and
                    sell_mispricing_no < -SELL_MISPRICING_THRESHOLD and
                    time_left > 60):
                sell_size = min(self.inv.qty_no * 0.5, MAX_SELL_SIZE)
                sell_price = round(self.book.best_bid_no - 0.01, 2)
                if sell_price >= MIN_PRICE and sell_size >= 1.0:
                    orders.append({
                        'side': 'SELL', 'token': 'no',
                        'size': round(sell_size, 1),
                        'price': sell_price,
                        'reason': 'NO_OVERVALUED'
                    })

        # ── Mean-reversion forced pair closing ──
        if abs(z) < MEAN_REVERSION_THRESHOLD and abs(imbalance) > 1.0:
            token_to_buy = 'no' if imbalance > 0 else 'yes'
            need_qty = min(abs(imbalance), 10.0)
            best_ask = self.book.best_ask_no if token_to_buy == 'no' else self.book.best_ask_yes
            if best_ask < 0.98:
                orders.append({
                    'side': 'BUY', 'token': token_to_buy,
                    'size': round(need_qty, 1),
                    'price': round(best_ask - 0.01, 2),
                    'reason': 'MEAN_REVERSION_PAIR'
                })

        if not orders:
            orders = self._fallback_neutral_quotes()

        self._prev_quotes = orders
        return orders    def _fallback_neutral_quotes(self) -> list[dict]:
        """Симметричный redeem-маркетмейкинг."""
        orders = []
        remaining_budget = max(0, MAX_TOTAL_EXPOSURE - self.inv.total_exposure)
        if remaining_budget < 2.0:
            return []

        for token in ('yes', 'no'):
            if token == 'yes':
                mid = self.book.mid_yes
                my_qty = self.inv.qty_yes
                other_qty = self.inv.qty_no
                best_ask = self.book.best_ask_yes
                other_best_ask = self.book.best_ask_no
                current_market_bid = self.book.best_bid_yes
            else:
                mid = self.book.mid_no
                my_qty = self.inv.qty_no
                other_qty = self.inv.qty_yes
                best_ask = self.book.best_ask_no
                other_best_ask = self.book.best_ask_yes
                current_market_bid = self.book.best_bid_no

            max_bid = min(MAX_PRICE, 1.0 - other_best_ask - 0.01)
            if max_bid < MIN_PRICE:
                continue
            aggressive_bid = current_market_bid + 0.01
            base_bid = min(aggressive_bid, max_bid, mid)
            base_bid = round(base_bid, 2)
            base_bid = min(base_bid, round(best_ask - 0.01, 2))
            base_bid = max(MIN_PRICE, base_bid)

            base_size = BASE_ORDER_SIZE * min(1.0, remaining_budget / MAX_TOTAL_EXPOSURE)
            budget_per_token = remaining_budget / 2.0
            for level in range(NUM_LEVELS):
                offset = level * LEVEL_SPACING
                size = base_size * (LEVEL_SIZE_DECAY ** level)
                price = round(base_bid - offset, 2)
                price = max(MIN_PRICE, min(price, max_bid))
                cost = size * price
                if cost > budget_per_token:
                    size = round(budget_per_token / price, 1)
                if size >= 1.0 and price >= MIN_PRICE:
                    estimated_pair_cost = price + other_best_ask
                    if estimated_pair_cost < 0.995:
                        orders.append({
                            'side': 'BUY', 'token': token,
                            'size': round(size, 1), 'price': price,
                        })
                        budget_per_token -= size * price
        return orders

    def _kelly_size(self, p_true: float, p_market: float) -> float:
        edge = p_true - p_market
        if edge <= 0:
            return 0.0
        f_star = edge / (1.0 - p_market + 1e-9)
        return MAX_TOTAL_EXPOSURE * f_star * KELLY_FRACTION


# ==============================================================================
# 5. FILL DETECTION
# ==============================================================================
class FillDetector:
    def __init__(self, exchange: Exchange, orders: ActiveOrders,
                 inventory: Inventory):
        self.exchange = exchange
        self.orders = orders
        self.inv = inventory

    async def detect_fills(self) -> bool:
        our_orders = await self.orders.get_all()
        if not our_orders:
            return False

        try:
            open_sizes = await self.exchange.get_open_orders_with_sizes()
        except Exception as e:
            logger.error(f"Fill detection error: {e}")
            return False

        had_fills = False

        for order in our_orders:
            oid = order['id']
            recorded_size = float(order['size'])
            side = order.get('side', 'BUY')
            live_size = open_sizes.get(oid, 0.0)

            if oid not in open_sizes:
                if side == 'SELL':
                    self.inv.record_sell(order['token'], recorded_size, order['price'])
                else:
                    self.inv.record_buy(order['token'], recorded_size, order['price'])
                await self.orders.remove(oid)
                had_fills = True
            elif live_size < recorded_size - 0.001:
                filled = recorded_size - live_size
                if side == 'SELL':
                    self.inv.record_sell(order['token'], filled, order['price'])
                else:
                    self.inv.record_buy(order['token'], filled, order['price'])
                order['size'] = live_size
                await self.orders.add(oid, order)
                had_fills = True

        if had_fills:
            pairs = self.inv.redeemable_pairs
            logger.info(
                f"📊 After fills: YES={self.inv.qty_yes:.1f} "
                f"(avg ${self.inv.avg_price_yes:.3f}) | "
                f"NO={self.inv.qty_no:.1f} "
                f"(avg ${self.inv.avg_price_no:.3f}) | "
                f"Pairs ready: {pairs:.1f}"
            )

        return had_fills


# ==============================================================================
# 6. PERSISTENCE
# ==============================================================================
class StateDB:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY,
                    slug TEXT NOT NULL,
                    inventory_json TEXT,
                    orders_json TEXT,
                    updated_at TEXT
                )
            """)
            await db.commit()

    async def save(self, slug: str, inv: Inventory, orders: ActiveOrders):
        async with aiosqlite.connect(self.path) as db:
            order_snap = await orders.snapshot_for_save()
            await db.execute("""
                INSERT OR REPLACE INTO state
                (id, slug, inventory_json, orders_json, updated_at)
                VALUES (1, ?, ?, ?, ?)
            """, (
                slug,
                json.dumps(inv.to_dict()),
                json.dumps(order_snap),
                datetime.now(timezone.utc).isoformat(),
            ))
            await db.commit()

    async def load(
            self, slug: str, inv: Inventory, orders: ActiveOrders
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                    "SELECT * FROM state WHERE id = 1"
            ) as cur:
                row = await cur.fetchone()
                if not row or row[1] != slug:
                    return False
                inv.from_dict(json.loads(row[2]))
                await orders.load_from_save(json.loads(row[3]))
                logger.info(
                    f"💾 State loaded | PnL=${inv.realized_pnl:.2f} "
                    f"Redeemed={inv.total_redeemed:.0f} pairs"
                )
                return True

    async def clear(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM state")
            await db.commit()


# ==============================================================================
# 7. WEBSOCKET (Book feed only)
# ==============================================================================
async def ws_book_feed(book: OrderBook, market_holder: list):
    attempt = 0
    while not STOP_EVENT.is_set():
        mkt = market_holder[0]
        if mkt is None:
            await asyncio.sleep(2)
            attempt = 0
            continue

        try:
            async with websockets.connect(
                    WS_URL, ping_interval=20, ping_timeout=10
            ) as ws:
                attempt = 0
                await ws.send(json.dumps({
                    "assets": [mkt.token_yes, mkt.token_no],
                    "type": "market",
                }))
                logger.info("🔗 WS book feed connected")

                while not STOP_EVENT.is_set():
                    if market_holder[0] is not mkt:
                        logger.info("🔄 Market changed, reconnecting WS...")
                        break

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        await ws.ping()
                        continue

                    data = json.loads(raw)
                    if data.get('event_type') != 'book':
                        continue

                    asset_id = data.get('asset_id')
                    if asset_id == mkt.token_yes:
                        book.update(
                            'yes',
                            data.get('bids', []),
                            data.get('asks', []),
                        )
                    elif asset_id == mkt.token_no:
                        book.update(
                            'no',
                            data.get('bids', []),
                            data.get('asks', []),
                        )

        except asyncio.CancelledError:
            return
        except Exception as e:
            attempt += 1
            delay = min(60, 2 ** attempt)
            logger.warning(
                f"WS error: {e}. Reconnecting in {delay}s "
                f"(attempt {attempt})"
            )
            await asyncio.sleep(delay)


# ==============================================================================
# 8. MAIN ORCHESTRATOR
# ==============================================================================
async def main():
    global ws_task
    exchange = Exchange()
    book = OrderBook()
    inventory = Inventory()
    active_orders = ActiveOrders()
    db = StateDB(DB_FILE)
    http = requests.Session()
    oracle = BinanceOracle()

    await db.init()

    market_holder: list[Optional[MarketInfo]] = [None]

    if DRY_RUN:
        logger.warning(
            "⚠️ DRY RUN MODE — no real orders, no real redeem"
        )

    ws_task = asyncio.create_task(ws_book_feed(book, market_holder))
    oracle_task = asyncio.create_task(oracle.run())

    quoter: Optional[OracleQuoter] = None
    fill_detector: Optional[FillDetector] = None

    last_save_t = 0.0
    last_requote_t = 0.0
    last_poll_t = 0.0
    round_start_balance = 0.0
    last_status_ts = 0.0

    try:
        while not STOP_EVENT.is_set():
            now = datetime.now(timezone.utc)
            mono = time.monotonic()

            if mono - last_status_ts >= 30.0:
                last_status_ts = mono
                balance = await exchange.get_balance()
                total_pnl_temp = inventory.total_pnl(book)
                logger.info(
                    f"💹 STATUS | Balance: ${balance:.2f} | "
                    f"PnL: ${total_pnl_temp:.2f} "
                    f"(R:${inventory.realized_pnl:.2f} "
                    f"U:${inventory.unrealized_pnl(book):.2f}) | "
                    f"Exposure: ${inventory.total_exposure:.2f} | "
                    f"Pairs: {inventory.redeemable_pairs:.1f} | "
                    f"Orders: {active_orders.count} | "
                    f"Edge: {book.theoretical_edge:.3f}"
                )

            if market_holder[0] is None:
                ts_base = (int(now.timestamp()) // 900) * 900
                slug = f"btc-updown-15m-{ts_base}"
                try:
                    resp = await asyncio.to_thread(
                        http.get, GAMMA_API,
                        params={"slug": slug}, timeout=5,
                    )
                    if resp.status_code == 200 and resp.json():
                        m = resp.json()[0]
                        end = datetime.fromisoformat(
                            m["endDate"].replace("Z", "+00:00")
                        )

                        if (end - now).total_seconds() > 60:
                            await exchange.init_client()

                            clob_ids = json.loads(m["clobTokenIds"])
                            mkt = MarketInfo(
                                slug=m["slug"],
                                end=end,
                                token_yes=str(clob_ids[0]),
                                token_no=str(clob_ids[1]),
                                condition_id=m.get("conditionId", ""),
                            )

                            inventory = Inventory()
                            active_orders = ActiveOrders()

                            await db.load(mkt.slug, inventory, active_orders)

                            for token_id, side in [
                                (mkt.token_yes, 'yes'),
                                (mkt.token_no, 'no'),
                            ]:
                                ob = await exchange.fetch_orderbook(token_id)
                                book.update(side, ob['bids'], ob['asks'])

                            quoter = OracleQuoter(book, inventory, oracle)
                            fill_detector = FillDetector(
                                exchange, active_orders, inventory
                            )

                            round_start_balance = await exchange.get_balance()
                            market_holder[0] = mkt

                            logger.info(
                                f"{'=' * 60}\n"
                                f"🎯 ROUND: {mkt.slug}\n"
                                f"   Balance: ${round_start_balance:.2f}\n"
                                f"   FV YES={book.mid_yes:.3f} "
                                f"NO={book.mid_no:.3f}\n"
                                f"   Combined ask: ${book.combined_ask:.3f} "
                                f"(edge: {book.theoretical_edge:.3f})\n"
                                f"   Condition: {mkt.condition_id}\n"
                                f"{'=' * 60}"
                            )

                except Exception as e:
                    logger.debug(f"Discovery: {e}")

                await asyncio.sleep(3)
                continue

            mkt = market_holder[0]
            time_left = (mkt.end - now).total_seconds()

            if time_left <= 15:
                logger.info(
                    f"⏳ Round ending | "
                    f"Realized: ${inventory.realized_pnl:.2f} | "
                    f"Unrealized: ${inventory.unrealized_pnl(book):.2f} | "
                    f"Total redeemed: {inventory.total_redeemed:.0f} pairs"
                )

                all_ids = await active_orders.get_all_ids()
                if all_ids:
                    await exchange.cancel_orders(all_ids)
                    await active_orders.clear()

                if inventory.redeemable_pairs >= 0.5:
                    pairs = inventory.redeemable_pairs
                    success = await exchange.redeem_pairs(pairs, mkt)
                    if success:
                        profit = inventory.record_redeem(pairs)
                        logger.info(f"💸 Final redeem: +${profit:.2f}")

                await db.clear()

                market_holder[0] = None
                quoter = None
                fill_detector = None
                await asyncio.sleep(20)
                continue

            total_pnl = inventory.total_pnl(book)
            if total_pnl <= -MAX_DRAWDOWN_USDC:
                logger.critical(
                    f"🛑 CIRCUIT BREAKER! PnL=${total_pnl:.2f}. "
                    f"Cancelling all orders and stopping!"
                )
                all_ids = await active_orders.get_all_ids()
                await exchange.cancel_orders(all_ids)
                await active_orders.clear()
                STOP_EVENT.set()
                break

            had_fills = False
            if fill_detector and mono - last_poll_t >= FILL_POLL_INTERVAL:
                had_fills = await fill_detector.detect_fills()
                last_poll_t = mono

            if inventory.redeemable_pairs >= REDEEM_THRESHOLD:
                pairs = inventory.redeemable_pairs
                avg_cost = inventory.avg_price_yes + inventory.avg_price_no
                expected_profit = pairs * (1.0 - avg_cost)

                logger.info(
                    f"💰 REDEEM {pairs:.1f} pairs | "
                    f"Avg cost/pair: ${avg_cost:.3f} | "
                    f"Expected profit: ${expected_profit:.2f}"
                )

                all_ids = await active_orders.get_all_ids()
                if all_ids:
                    await exchange.cancel_orders(all_ids)
                    await active_orders.clear()

                success = await exchange.redeem_pairs(pairs, mkt)
                if success:
                    profit = inventory.record_redeem(pairs)
                    logger.info(
                        f"💸 REDEEMED! +${profit:.2f} | "
                        f"Session PnL: ${inventory.realized_pnl:.2f} | "
                        f"Total pairs redeemed: "
                        f"{inventory.total_redeemed:.0f}"
                    )
                    had_fills = True
                else:
                    logger.error("❌ Redeem failed — will retry next cycle")

            if quoter and mono - last_requote_t >= REQUOTE_INTERVAL:
                balance = await exchange.get_balance()
                new_quotes = quoter.compute_quotes(mkt, available_balance=balance)

                needs_update = (
                        had_fills
                        or active_orders.is_empty
                        or quoter.should_requote(new_quotes)
                )

                if needs_update and new_quotes:
                    old_ids = await active_orders.get_all_ids()
                    placed = await exchange.cancel_and_place(
                        old_ids, new_quotes, mkt
                    )
                    await active_orders.clear()
                    await active_orders.add_many(placed)

                    if placed:
                        total_bid = sum(
                            o['size'] * o['price'] for o in placed
                        )
                        logger.info(
                            f"📋 {len(placed)} orders placed "
                            f"(${total_bid:.2f} total) | "
                            f"Edge: {book.theoretical_edge:.3f}"
                        )
                elif not new_quotes and not active_orders.is_empty:
                    old_ids = await active_orders.get_all_ids()
                    if old_ids:
                        await exchange.cancel_orders(old_ids)
                        await active_orders.clear()
                        logger.info("📋 All orders pulled (no edge)")

                last_requote_t = mono

            if mono - last_save_t >= STATE_SAVE_INTERVAL:
                await db.save(mkt.slug, inventory, active_orders)
                last_save_t = mono

            await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        pass
    finally:
        logger.info("🛑 Graceful shutdown...")
        mkt = market_holder[0]
        if oracle_task and not oracle_task.done():
            oracle_task.cancel()
            try:
                await oracle_task
            except asyncio.CancelledError:
                pass
        if mkt:
            all_ids = await active_orders.get_all_ids()
            if all_ids:
                await exchange.cancel_orders(all_ids)
            await db.save(mkt.slug, inventory, active_orders)

        if ws_task and not ws_task.done():
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass

        logger.info(
            f"📊 FINAL: Realized=${inventory.realized_pnl:.2f} | "
            f"Redeemed={inventory.total_redeemed:.0f} pairs"
        )


def handle_sigint(signum, frame):
    logger.info("🛑 SIGINT received")
    global ws_task
    STOP_EVENT.set()
    if ws_task and not ws_task.done():
        ws_task.cancel()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_sigint)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass