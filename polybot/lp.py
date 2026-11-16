"""Liquidity-rewards test (research/07_new_niches.md, H10).

Quotes the best bid and the best ask of a few "stable" rewarded markets at the reward minimum size and
measures the three numbers historical data cannot give:
  (a) the REWARD actually paid, against the M1 prediction for the very quotes that rested;
  (b) the markout of our fills 60 s (and 600 s) later;
  (c) our fill rate, contracts per market-day.

Both sides are post-only limit BUYs (YES at the best bid, NO at 1 - best ask), so no inventory is
needed to quote. Net position YES - NO is capped at 3N; balanced YES+NO pairs are merged back to USDC.

  python -m polybot lp-select                      choose markets (public data), writes LP_DIR/markets.json
  python -m polybot lp-paper                       same loop on live public data, fills simulated
                                                   from the public tape (we are first in the queue)
  python -m polybot lp-live --i-understand-live    real orders; needs keys in .env
  python -m polybot lp-phase1 [paper]              phase 1 criteria (48 h paper run)
  python -m polybot lp-earnings                    fetch the actual REWARD per market for each test day
  python -m polybot lp-report [live|paper]         criteria verdict from the journal

NOT run against the exchange in the development environment (no trading keys there).
"""
import asyncio
import dataclasses
import gzip
import json
import os
import re
import time
import urllib.parse
import urllib.request

CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"


@dataclasses.dataclass
class LPConfig:
    lp_dir: str = "journal/lp"
    markets_min: int = 5
    markets_max: int = 8
    capital_usd: float = 200.0         # wallet balance the test may use
    quote_budget_usd: float = 100.0    # resting quotes; the rest is reserve for inventory and unmerged pairs
    max_min_size: float = 20.0         # only markets whose rewards_min_size (our quote size N) is at most this
    pos_limit_mult: float = 3.0        # |YES - NO| <= 3N
    stop_loss_usd: float = 50.0        # mark-to-market loss (rewards excluded) that stops the test
    poll_s: float = 5.0
    sample_s: float = 60.0
    window_s: float = 1800.0           # phase 1: summary written to the journal every 30 minutes
    fills_every_s: float = 15.0        # paper: how often the public tape is polled for fills
    select_snapshots: int = 6          # selection: book snapshots, median M1 per market
    select_gap_s: float = 300.0
    max_share: float = 0.4             # selection: no market may carry more than this share of predicted M1
    test_days: float = 7.0
    min_age_days: float = 7.0          # H10N: markets younger than a week carried the November loss
    min_days_left: float = 14.0
    mid_lo: float = 0.15
    mid_hi: float = 0.85
    # success criteria (research/07_new_niches.md, section 8)
    reward_ratio_min: float = 0.6      # actual REWARD / M1 prediction for the same quotes
    markout60_min_c: float = -2.1      # cents per contract; break-even at 0.6 x M1 in the worst observed week
    fill_rate_max: float = 30.0        # contracts per market-day; the backtest's worst case (first in queue)


def from_env() -> LPConfig:
    c = LPConfig()
    for f in dataclasses.fields(c):
        raw = os.environ.get("LP_" + f.name.upper())
        if raw is not None:
            setattr(c, f.name, type(getattr(c, f.name))(raw))
    return c


# --------------------------------------------------------------------------------------------------
# classification A (research/PREREG_H10N.md); must stay identical to research/tools/h10_groups.py

NEWS = re.compile(
    r"nvidia|jensen|ian buck|rene haas|kimberly powell|anthropic|openai|\bgpt\b|chatgpt|gemini|claude|fable|muse|"
    r"databricks|ai lab|ai model|arena|livebench|humanity.s last exam|iran|israel|netanyahu|russia|ukraine|"
    r"zelenskyy|hormuz|bab el-mandeb|ukmto|ceasefire|military|\bxi\b|takaichi|syria|out as|leave the trump|"
    r"impeach|arrested|nomination odds|chief of staff|next premier|minister|next israeli government|witkoff|"
    r"trump speak|searched person|mecca agreement", re.I)
STABLE_FIRST = re.compile(r"\bposts? from|\bposts \d|tweets|interest rate|repo rate|monetary policy rate|free app", re.I)


def topic_news(q: str) -> bool:
    return bool(NEWS.search(q or "")) and not STABLE_FIRST.search(q or "")


# --------------------------------------------------------------------------------------------------
# official reward scoring (Polymarket liquidity rewards) and the M1 prediction

def order_score(v_c: float, dist_c: float) -> float:
    """S(v, s) = ((v - s) / v)^2 inside the max spread v (cents), 0 outside."""
    return ((v_c - dist_c) / v_c) ** 2 if 0 <= dist_c < v_c else 0.0


def side_score(levels, mid, v_c, min_size, side) -> float:
    tot = 0.0
    for p, s in levels:
        if s + 1e-9 < min_size:
            continue
        tot += order_score(v_c, ((mid - p) if side == "bid" else (p - mid)) * 100) * s
    return tot


def without_mine(levels, mine: dict):
    """Book levels with our own resting size removed (mine: {price: size})."""
    out = []
    for p, s in levels:
        left = s - mine.get(round(p, 4), 0.0)
        if left > 1e-9:
            out.append((p, left))
    return out


def m1_fraction(bids, asks, my_bid, my_ask, size, v_c, min_size) -> float:
    """Predicted share of the market's pool for our quotes at this instant.

    bids / asks: YES book of the OTHER orders (ours removed), best first. my_bid / my_ask: YES prices
    of our orders or None. Competition = (Q_bid + Q_ask) / 2 of the other orders, an upper bound on
    their summed Q_min (research/07, H10). Two-sided rule: with the midpoint in [0.10, 0.90] a single
    side scores one third; outside that range both sides are required.
    """
    best_b = max([b[0] for b in bids[:1]] + ([my_bid] if my_bid is not None else []), default=None)
    best_a = min([a[0] for a in asks[:1]] + ([my_ask] if my_ask is not None else []), default=None)
    if best_b is None or best_a is None or size + 1e-9 < min_size:
        return 0.0
    mid = (best_b + best_a) / 2
    qb = order_score(v_c, (mid - my_bid) * 100) * size if my_bid is not None else 0.0
    qa = order_score(v_c, (my_ask - mid) * 100) * size if my_ask is not None else 0.0
    mine = max(min(qb, qa), max(qb, qa) / 3) if 0.10 <= mid <= 0.90 else min(qb, qa)
    if mine <= 0:
        return 0.0
    comp = (side_score(bids, mid, v_c, min_size, "bid") + side_score(asks, mid, v_c, min_size, "ask")) / 2
    return mine / (comp + mine)


def targets(bids, asks, tick, v_c, pos, cap):
    """YES prices of our bid and ask (None = no order): join the best prices of the other orders, only
    inside the reward zone, and withdraw the side that would push |YES - NO| beyond the cap."""
    if not bids or not asks:
        return None, None
    b, a = bids[0][0], asks[0][0]
    if a - b < tick - 1e-9:
        return None, None
    if ((a - b) / 2) * 100 >= v_c:
        return None, None
    return (b if pos < cap - 1e-9 else None), (a if pos > -cap + 1e-9 else None)


def crosses(trade, bid, ask):
    """Does a public taker trade (outcome_index, side, price) trade through our YES bid / ask?
    Returns "bid", "ask" or None. Same convention as research/tools/h10_backtest.py."""
    oi, side, px = trade
    buy_yes, y = (side == "B", px) if oi == 0 else (side != "B", 1 - px)
    if buy_yes and ask is not None and y >= ask - 1e-9:
        return "ask"
    if not buy_yes and bid is not None and y <= bid + 1e-9:
        return "bid"
    return None


# --------------------------------------------------------------------------------------------------
# public data

def _req(url, body=None, timeout=30):
    for i in range(5):
        try:
            hdr = {"User-Agent": "polybot-lp/1.0", "Accept-Encoding": "gzip"}
            data = None
            if body is not None:
                hdr["Content-Type"] = "application/json"; data = json.dumps(body).encode()
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=hdr), timeout=timeout) as r:
                raw = r.read()
                return json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
        except Exception:
            if i == 4:
                raise
            time.sleep(2 ** i)


def rewards_config():
    out, cur = {}, None
    while True:
        d = _req(f"{CLOB}/rewards/markets/current?" + urllib.parse.urlencode({"next_cursor": cur} if cur else {}))
        for r in d.get("data") or []:
            out[r["condition_id"]] = {"rate": float(r.get("total_daily_rate") or 0), "v": float(r["rewards_max_spread"]),
                                      "min": float(r["rewards_min_size"])}
        cur = d.get("next_cursor")
        if not cur or cur == "LTE=" or not d.get("data"):
            return out


def books(tokens):
    """{token: (bids best-first, asks best-first, tick)}"""
    out = {}
    tokens = list(tokens)
    for i in range(0, len(tokens), 100):
        for b in _req(f"{CLOB}/books", [{"token_id": t} for t in tokens[i:i + 100]]):
            out[b["asset_id"]] = (sorted(((float(x["price"]), float(x["size"])) for x in b["bids"]), reverse=True),
                                  sorted((float(x["price"]), float(x["size"])) for x in b["asks"]),
                                  float(b.get("tick_size") or 0.01))
    return out


def gamma_markets(conds):
    out = {}
    conds = list(conds)
    for i in range(0, len(conds), 40):
        q = urllib.parse.urlencode([("condition_ids", c) for c in conds[i:i + 40]] + [("limit", 40)])
        for m in _req(f"{GAMMA}/markets?" + q):
            out[m["conditionId"]] = m
    return out


def taker_trades(cond, limit=200):
    d = _req(f"{DATA}/v2/trades?" + urllib.parse.urlencode({"condition": cond, "taker_only": "true", "limit": limit}))
    return [(r["timestamp"], r["outcome_index"], r["side"][0], float(r["price"]), float(r["size"]),
             f"{r.get('transaction_hash')}:{r['outcome_index']}:{r['price']}:{r['size']}") for r in d.get("data") or []]


def _ts(iso):
    return time.mktime(time.strptime(iso[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")) - time.timezone


# --------------------------------------------------------------------------------------------------
# market selection

def _rows(cand, rw, bk, cfg):
    """Quote, M1 prediction and capital per candidate market for one book snapshot."""
    rows = {}
    for c, m in cand:
        tok = json.loads(m["clobTokenIds"])[0]
        if tok not in bk:
            continue
        bids, asks, tick = bk[tok]
        r = rw[c]
        bid, ask = targets(bids, asks, tick, r["v"], 0.0, 1.0)
        if bid is None or ask is None:
            continue
        mid = (bid + ask) / 2
        if not cfg.mid_lo <= mid <= cfg.mid_hi:
            continue
        n = r["min"]
        frac = m1_fraction(bids, asks, bid, ask, n, r["v"], r["min"])
        rows[c] = {"cond": c, "q": m.get("question"), "yes": tok, "no": json.loads(m["clobTokenIds"])[1],
                   "event": str((m.get("events") or [{}])[0].get("id") or c), "N": n, "v": r["v"], "min": r["min"],
                   "rate": r["rate"], "tick": tick, "mid": mid, "m1_usd_day": r["rate"] * frac,
                   "quote_cap": n * (bid + 1 - ask), "neg_risk": bool(m.get("negRisk")), "end": m["endDate"]}
    return rows


def choose(rows, cfg):
    """Greedy by M1 per dollar of quote capital, one market per event, within markets_max and the quote
    budget; then, while one market carries more than max_share of the predicted M1, drop it and refill."""
    banned = set()
    while True:
        out, events, cap = [], set(), 0.0
        for x in sorted(rows, key=lambda x: -x["m1_usd_day"] / x["quote_cap"]):
            if x["cond"] in banned or x["event"] in events or x["m1_usd_day"] <= 0:
                continue
            if len(out) >= cfg.markets_max or cap + x["quote_cap"] > cfg.quote_budget_usd:
                continue
            out.append(x); events.add(x["event"]); cap += x["quote_cap"]
        tot = sum(x["m1_usd_day"] for x in out)
        top = max(out, key=lambda x: x["m1_usd_day"], default=None)
        if top is None or tot <= 0 or top["m1_usd_day"] / tot <= cfg.max_share or len(out) < 2:
            return out
        banned.add(top["cond"])


def select(cfg: LPConfig, now=None, log=print, sleep=time.sleep):
    """Stable (classification A), at least a week old, at least two weeks left, mid in [0.15, 0.85],
    reward size <= max_min_size, best prices inside the reward zone. M1 is the median over
    select_snapshots book snapshots (a market outside the zone in a snapshot counts as 0 there)."""
    now = now or time.time()
    rw = {c: r for c, r in rewards_config().items() if r["rate"] > 0 and r["min"] <= cfg.max_min_size}
    meta = gamma_markets(rw)
    cand = []
    for c, m in meta.items():
        q = m.get("question") or ""
        if topic_news(q) or not m.get("active") or m.get("closed") or not m.get("acceptingOrders") or not m.get("clobTokenIds"):
            continue
        born = m.get("createdAt") or m.get("startDate")
        if not born or not m.get("endDate") or now - _ts(born) < cfg.min_age_days * 86400 or _ts(m["endDate"]) - now < cfg.min_days_left * 86400:
            continue
        if m.get("gameStartTime"):
            continue
        cand.append((c, m))
    toks = [json.loads(m["clobTokenIds"])[0] for _, m in cand]
    snaps = []
    for k in range(max(1, cfg.select_snapshots)):
        if k:
            sleep(cfg.select_gap_s)
        snaps.append(_rows(cand, rw, books(toks), cfg))
        log(f"selection snapshot {k + 1}/{cfg.select_snapshots}: {len(snaps[-1])} markets quotable")
    rows = []
    for c in set().union(*snaps):
        vals = sorted(sn[c]["m1_usd_day"] if c in sn else 0.0 for sn in snaps)
        x = dict(next(sn[c] for sn in snaps if c in sn))
        x["m1_snapshots"] = [round(sn[c]["m1_usd_day"], 3) if c in sn else 0.0 for sn in snaps]
        x["m1_usd_day"] = vals[len(vals) // 2]
        rows.append(x)
    out = choose(rows, cfg)
    cap = sum(x["quote_cap"] for x in out)
    tot = sum(x["m1_usd_day"] for x in out)
    log(f"candidates {len(rows)}; selected {len(out)} markets, quote capital ${cap:.0f}, "
        f"median M1 prediction ${tot:.2f}/day, largest share {max((x['m1_usd_day'] / tot for x in out), default=0):.0%}")
    if len(out) < cfg.markets_min:
        log(f"fewer than {cfg.markets_min} markets pass the filters; the test should not start")
    os.makedirs(cfg.lp_dir, exist_ok=True)
    with open(os.path.join(cfg.lp_dir, "markets.json"), "w") as f:
        json.dump({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "markets": out}, f, indent=1)
    return out


# --------------------------------------------------------------------------------------------------
# executors: paper (simulated fills from the public tape) and live (SDK)

class PaperExec:
    """Orders are virtual. A public taker trade through our price fills us first (worst case,
    the same model as the backtest). Interface shared with LiveExec:
      place(m, side, price, size) -> order id or None      side "bid" = BUY YES, "ask" = BUY NO at 1 - price
      cancel(m, oid) -> "one" | "market"                   "market": every order of ours in m is gone
      fills(markets) -> [{order, mkt, side, price, size, ts, done}]
    """

    in_book = False                       # virtual orders never appear in the public book

    def __init__(self, trades_fn=taker_trades, every_s=0.0):
        self.n, self.orders, self.seen, self.since = 0, {}, set(), time.time()
        self.trades_fn, self.every_s, self.last_poll = trades_fn, every_s, 0.0

    async def place(self, m, side, price, size):
        self.n += 1
        oid = f"paper-{self.n}"
        self.orders[oid] = {"mkt": m["cond"], "side": side, "price": price, "rem": size, "t": time.time()}
        return oid

    async def cancel(self, m, oid):
        self.orders.pop(oid, None)
        return "one"

    async def cancel_all(self):
        self.orders.clear()

    async def fills(self, markets):
        out = []
        if time.time() - self.last_poll < self.every_s:
            return out
        self.last_poll = time.time()
        for m in markets:
            mine = [(oid, o) for oid, o in self.orders.items() if o["mkt"] == m["cond"]]
            if not mine:
                continue
            for ts, oi, side, px, sz, key in sorted(await asyncio.to_thread(self.trades_fn, m["cond"])):
                if key in self.seen or ts < self.since:
                    continue
                self.seen.add(key)
                live = [(oid, o) for oid, o in mine if o["t"] <= ts + 1 and o["rem"] > 1e-9]
                bid = next((o["price"] for _, o in live if o["side"] == "bid"), None)
                ask = next((o["price"] for _, o in live if o["side"] == "ask"), None)
                hit = crosses((oi, side, px), bid, ask)
                if not hit:
                    continue
                oid, o = next((oid, o) for oid, o in live if o["side"] == hit)
                q = min(o["rem"], sz); o["rem"] -= q
                done = o["rem"] <= 1e-9
                if done:
                    self.orders.pop(oid, None)
                out.append({"order": oid, "mkt": m["cond"], "side": hit, "price": o["price"], "size": q, "ts": ts, "done": done})
        return out

    async def merge(self, m, pairs):
        return pairs

    async def reward_pct(self):
        return None

    async def scoring(self, oids):
        return None

    async def earnings(self, day):
        return None

    async def close(self):
        await self.cancel_all()


class LiveExec:
    """Real post-only BUY orders through the official SDK (polymarket-client)."""

    in_book = True

    def __init__(self, client, log=print):
        self.c, self.log = client, log
        self.ours = {}                    # order id -> [cond, side, price, size, matched]
        self.seen = set()
        self.since = str(int(time.time()))

    @classmethod
    async def create(cls, private_key, wallet, relayer_key, relayer_address, log=print):
        from polymarket import AsyncSecureClient
        from polymarket.auth import RelayerApiKey
        api_key = RelayerApiKey(key=relayer_key, address=relayer_address) if relayer_key and relayer_address else None
        return cls(await AsyncSecureClient.create(private_key=private_key, wallet=wallet or None, api_key=api_key), log)

    async def place(self, m, side, price, size):
        token, paid = (m["yes"], price) if side == "bid" else (m["no"], round(1 - price, 4))
        try:
            r = await self.c.place_limit_order(token_id=token, price=str(paid), size=str(size), side="BUY", post_only=True)
        except Exception as e:
            self.log(f"place failed with unknown outcome: {e!r}; cancelling every order in the market")
            await self._cancel_market(m["cond"])
            return None
        if getattr(r, "ok", False):
            self.ours[r.order_id] = [m["cond"], side, price, float(size), 0.0]
            return r.order_id
        self.log(f"place rejected: {getattr(r, 'code', r)}")
        return None

    async def _cancel_market(self, cond):
        for attempt in range(5):
            try:
                await self.c.cancel_market_orders(market=cond)
                return
            except Exception as e:
                self.log(f"cancel_market_orders failed: {e!r}")
                await asyncio.sleep(0.5 * (attempt + 1))

    async def cancel(self, m, oid):
        try:
            await self.c.cancel_order(order_id=oid)
            return "one"                  # canceled, or already filled / expired: either way it no longer rests
        except Exception as e:
            self.log(f"cancel failed with unknown outcome: {e!r}; cancelling every order in the market")
            await self._cancel_market(m["cond"])
            return "market"

    async def cancel_all(self):
        try:
            r = await self.c.cancel_all()
            self.log(f"cancel_all: {len(r.canceled)} canceled, {len(r.not_canceled)} not canceled")
        except Exception as e:
            self.log(f"cancel_all failed: {e!r}")

    async def fills(self, markets):
        """Our maker fills from the account trade list; each (trade, our order) pair is counted once."""
        out = []
        try:
            async for t in self.c.list_account_trades(after=self.since).iter_items():
                if str(getattr(t, "status", "")).upper().endswith("FAILED"):
                    continue
                for mo in t.maker_orders:
                    key = (t.id, mo.order_id)
                    if mo.order_id not in self.ours or key in self.seen:
                        continue
                    self.seen.add(key)
                    rec = self.ours[mo.order_id]
                    rec[4] += float(mo.matched_amount)
                    out.append({"order": mo.order_id, "mkt": rec[0], "side": rec[1], "price": rec[2],
                                "size": float(mo.matched_amount), "ts": t.matched_at.timestamp(), "done": rec[4] >= rec[3] - 1e-9})
        except Exception as e:
            self.log(f"trade list failed: {e!r}")
        return out

    async def merge(self, m, pairs):
        try:
            h = await self.c.merge_positions(condition_id=m["cond"], amount="max")
            await h.wait()
            return pairs
        except Exception as e:
            self.log(f"merge failed: {e!r}")
            return 0.0

    async def reward_pct(self):
        try:
            return dict(await self.c.get_reward_percentages())
        except Exception as e:
            self.log(f"reward percentages failed: {e!r}")
            return None

    async def scoring(self, oids):
        try:
            return await self.c.get_orders_scoring(order_ids=list(oids)) if oids else {}
        except Exception as e:
            self.log(f"orders scoring failed: {e!r}")
            return None

    async def earnings(self, day):
        try:
            out = {}
            async for e in self.c.list_user_earnings_for_day(date=day).iter_items():
                out[e.condition_id] = out.get(e.condition_id, 0.0) + float(e.earnings)
            return out
        except Exception as e:
            self.log(f"earnings for {day} failed: {e!r}")
            return None

    async def close(self):
        await self.cancel_all()
        await self.c.close()


# --------------------------------------------------------------------------------------------------
# the test loop

class Journal:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.f = open(path, "a", buffering=1)

    def write(self, event, ts=None, **kw):
        kw.update(ts=ts if ts is not None else time.time(), event=event)
        self.f.write(json.dumps(kw, separators=(",", ":")) + "\n")

    def close(self):
        self.f.close()


class MarketState:
    def __init__(self, m):
        self.m = m
        self.orders = {"bid": None, "ask": None}       # side -> (order id, YES price, placed at)
        self.yes = self.no = self.cash = 0.0
        self.mid = None
        self.mids = []                                  # (ts, mid of the other orders) for markouts

    def pos(self):
        return self.yes - self.no

    def mtm(self):
        return self.cash + (self.yes * self.mid + self.no * (1 - self.mid) if self.mid is not None else 0.0)

    def exposure(self):
        if self.mid is None:
            return 0.0
        p = self.pos()
        return p * self.mid if p > 0 else -p * (1 - self.mid)


class LPTest:
    def __init__(self, cfg: LPConfig, ex, journal: Journal, markets, books_fn=books, rewards_fn=rewards_config,
                 log=print, clock=time.time):
        self.cfg, self.ex, self.j, self.log, self.clock = cfg, ex, journal, log, clock
        self.books_fn, self.rewards_fn = books_fn, rewards_fn
        self.st = {m["cond"]: MarketState(m) for m in markets}
        self.pending = []                               # (due ts, horizon, cond, side, price, size)
        self.stopped = None
        self.last_sample = 0.0
        self.day = None
        self.win = None                                 # current 30-minute window index
        self.acc = self._new_acc()
        self.errors = 0
        self.status_path = os.path.join(os.path.dirname(getattr(journal.f, "name", "") or "."), "status.json")

    def _new_acc(self):
        return {c: {"m1": [], "frac": [], "comp_levels": [], "comp_size": [], "places": 0, "cancels": 0,
                    "fills": 0, "contracts": 0.0, "markout60": []} for c in self.st}

    def _close_window(self, now):
        w = self.win
        per = {}
        for c, a in self.acc.items():
            mean = lambda xs: sum(xs) / len(xs) if xs else None
            per[c] = {"m1_usd_day": mean(a["m1"]), "frac": mean(a["frac"]), "comp_levels": mean(a["comp_levels"]),
                      "comp_size": mean(a["comp_size"]), "samples": len(a["m1"]), "places": a["places"],
                      "cancels": a["cancels"], "fills": a["fills"], "contracts": a["contracts"], "markout60": a["markout60"]}
        tot = sum(v["m1_usd_day"] or 0.0 for v in per.values())
        self.j.write("window", ts=now, start=w * self.cfg.window_s, end=(w + 1) * self.cfg.window_s,
                     m1_usd_day=tot, markets=per, pnl=self.pnl(), errors=self.errors)
        self.acc = self._new_acc()

    def pnl(self):
        return sum(s.mtm() for s in self.st.values())

    def apply_fill(self, f):
        s = self.st[f["mkt"]]
        self.acc[f["mkt"]]["fills"] += 1
        self.acc[f["mkt"]]["contracts"] += f["size"]
        if f["side"] == "bid":
            s.yes += f["size"]; s.cash -= f["size"] * f["price"]
        else:
            s.no += f["size"]; s.cash -= f["size"] * (1 - f["price"])
        o = s.orders[f["side"]]
        if f.get("done") and o and o[0] == f["order"]:
            s.orders[f["side"]] = None
        for h in (60, 600):
            self.pending.append((f["ts"] + h, h, f["mkt"], f["side"], f["price"], f["size"]))
        self.j.write("fill", mkt=f["mkt"], side=f["side"], price=f["price"], size=f["size"], fill_ts=f["ts"], order=f["order"])

    def due_markouts(self, now):
        keep = []
        for due, h, cond, side, price, size in self.pending:
            s = self.st[cond]
            later = next((m for t, m in s.mids if t >= due), None)
            if later is None:
                if now - due <= 600:
                    keep.append((due, h, cond, side, price, size))
                continue                                # otherwise no book sample after the horizon: dropped
            mk = (later - price) if side == "bid" else (price - later)
            self.j.write("markout", mkt=cond, horizon=h, side=side, price=price, size=size, mid=later, markout=mk)
            if h == 60 and cond in self.acc:
                self.acc[cond]["markout60"].append([round(100 * mk, 3), size])
        self.pending = keep

    async def step(self, now, cfgs):
        w = int(now // self.cfg.window_s)               # close the window before anything of this step is counted
        if self.win is None:
            self.win = w
        elif w != self.win:
            self._close_window(now)
            self.win = w
        bk = await asyncio.to_thread(self.books_fn, [s.m["yes"] for s in self.st.values()])
        reserve = self.cfg.capital_usd - self.cfg.quote_budget_usd
        over = sum(s.exposure() for s in self.st.values()) >= reserve
        sample = now - self.last_sample >= self.cfg.sample_s
        for cond, s in self.st.items():
            b = bk.get(s.m["yes"])
            if not b:
                continue
            bids, asks, tick = b
            # our own resting orders are removed from the book before it is read, but only on the exchange
            # (paper orders are not in it) and only once they have had a poll interval to show up
            def mine(side):
                o = s.orders[side]
                if not o or not getattr(self.ex, "in_book", False) or now - o[2] < self.cfg.poll_s:
                    return {}
                return {round(o[1], 4): s.m["N"]}
            ob, oa = without_mine(bids, mine("bid")), without_mine(asks, mine("ask"))
            if ob and oa:
                s.mid = (ob[0][0] + oa[0][0]) / 2
                s.mids = (s.mids + [(now, s.mid)])[-400:]
            c = cfgs.get(cond) or {"v": s.m["v"], "min": s.m["min"], "rate": 0.0}
            cap = self.cfg.pos_limit_mult * s.m["N"]
            if over:
                cap = min(cap, abs(s.pos()))           # capital guard: no side may grow the position
            want = dict(zip(("bid", "ask"), targets(ob, oa, tick, c["v"], s.pos(), cap)))
            if self.stopped or c["rate"] <= 0:
                want = {"bid": None, "ask": None}
            for side in ("bid", "ask"):
                cur = s.orders[side]
                if cur and (want[side] is None or abs(cur[1] - want[side]) > 1e-9):
                    res = await self.ex.cancel(s.m, cur[0])
                    if res == "market":
                        s.orders = {"bid": None, "ask": None}
                    else:
                        s.orders[side] = None
                    self.j.write("cancel", mkt=cond, side=side, price=cur[1], scope=res)
                    self.acc[cond]["cancels"] += 1
            for side in ("bid", "ask"):
                if s.orders[side] is None and want[side] is not None:
                    oid = await self.ex.place(s.m, side, want[side], s.m["N"])
                    if oid:
                        s.orders[side] = (oid, want[side], now)
                        self.j.write("place", mkt=cond, side=side, price=want[side], size=s.m["N"])
                        self.acc[cond]["places"] += 1
            if sample:
                my_b = s.orders["bid"][1] if s.orders["bid"] else None
                my_a = s.orders["ask"][1] if s.orders["ask"] else None
                frac = m1_fraction(ob, oa, my_b, my_a, s.m["N"], c["v"], c["min"])
                # competitors seen in the book: price levels and resting size of other orders inside the zone
                lv = [(p, z) for p, z in ob if s.mid is not None and 0 <= (s.mid - p) * 100 < c["v"] and z + 1e-9 >= c["min"]]
                lv += [(p, z) for p, z in oa if s.mid is not None and 0 <= (p - s.mid) * 100 < c["v"] and z + 1e-9 >= c["min"]]
                self.j.write("m1", ts=now, mkt=cond, frac=frac, usd=c["rate"] * frac * self.cfg.sample_s / 86400,
                             bid=my_b, ask=my_a, mid=s.mid, comp_levels=len(lv), comp_size=sum(z for _, z in lv))
                a = self.acc[cond]
                a["m1"].append(c["rate"] * frac); a["frac"].append(frac)
                a["comp_levels"].append(len(lv)); a["comp_size"].append(sum(z for _, z in lv))
        for f in await self.ex.fills([s.m for s in self.st.values()]):
            if f["mkt"] in self.st:
                self.apply_fill(f)
        self.due_markouts(now)
        for cond, s in self.st.items():
            pairs = min(s.yes, s.no)
            if pairs >= s.m["N"]:
                done = await self.ex.merge(s.m, pairs)
                if done:
                    s.yes -= done; s.no -= done; s.cash += done
                    self.j.write("merge", mkt=cond, pairs=done)
        if sample:
            self.last_sample = now
            pct = await self.ex.reward_pct()
            if pct is not None:
                self.j.write("reward_pct", pct={c: pct.get(c) for c in self.st})
            oids = [o[0] for s in self.st.values() for o in s.orders.values() if o]
            sc = await self.ex.scoring(oids)
            if sc is not None:
                self.j.write("scoring", scoring=sum(1 for v in sc.values() if v), orders=len(oids))
            pnl = self.pnl()
            self.j.write("pnl", ts=now, pnl=pnl, exposure=sum(x.exposure() for x in self.st.values()),
                         positions={c: [x.yes, x.no] for c, x in self.st.items()})
            try:
                with open(self.status_path + ".tmp", "w") as f:
                    json.dump({"ts": now, "pnl": pnl, "errors": self.errors, "stopped": self.stopped,
                               "orders": sum(1 for x in self.st.values() for o in x.orders.values() if o),
                               "m1_usd_day_now": sum(a["m1"][-1] for a in self.acc.values() if a["m1"])}, f)
                os.replace(self.status_path + ".tmp", self.status_path)
            except OSError:
                pass
            if pnl <= -self.cfg.stop_loss_usd and not self.stopped:
                self.stopped = f"stop_loss {pnl:.2f}"
                self.log(f"STOP: mark-to-market {pnl:.2f} <= -{self.cfg.stop_loss_usd:.0f}")
                await self.ex.cancel_all()
                for x in self.st.values():
                    x.orders = {"bid": None, "ask": None}
                self.j.write("stop", reason=self.stopped, pnl=pnl)
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if self.day and day != self.day:
            e = await self.ex.earnings(self.day)
            if e is not None:
                self.j.write("earnings", day=self.day, by_market={c: e.get(c, 0.0) for c in self.st})
        self.day = day

    async def run(self):
        t_end = self.clock() + self.cfg.test_days * 86400
        cfgs, cfg_t = {}, -1e18
        self.j.write("start", markets=[s.m["cond"] for s in self.st.values()], config=dataclasses.asdict(self.cfg))
        try:
            while self.clock() < t_end and not self.stopped:
                now = self.clock()
                try:
                    if now - cfg_t > 3600:
                        allr = await asyncio.to_thread(self.rewards_fn)
                        cfgs, cfg_t = {c: allr.get(c) for c in self.st}, now
                        gone = [c for c, v in cfgs.items() if not v or v["rate"] <= 0]
                        if gone:
                            self.j.write("reward_removed", markets=gone)
                    await self.step(now, cfgs)
                except Exception as e:                  # a network error must not end a 48-hour run
                    self.errors += 1
                    self.j.write("error", error=repr(e)[:300])
                    if getattr(self.ex, "in_book", False):  # live: order state unknown, start clean
                        await self.ex.cancel_all()
                        for x in self.st.values():
                            x.orders = {"bid": None, "ask": None}
                await asyncio.sleep(max(0.0, self.cfg.poll_s - (self.clock() - now)))
        finally:
            await self.ex.cancel_all()
            if self.win is not None:
                self._close_window(self.clock())
            self.j.write("end", reason=self.stopped or "time", pnl=self.pnl())


# --------------------------------------------------------------------------------------------------
# report and success criteria

def summarize(events, cfg: LPConfig):
    """Criteria of research/07_new_niches.md, section 8. The reward ratio uses only complete UTC days
    for which actual earnings were fetched, and the M1 prediction of the same days."""
    earned = {}
    for e in events:
        if e["event"] == "earnings":
            for c, v in e["by_market"].items():
                earned[(e["day"], c)] = v                # a later fetch of the same day replaces an earlier one
    days_with = {d for d, _ in earned}
    day_of = lambda ts: time.strftime("%Y-%m-%d", time.gmtime(ts))
    m1_all = sum(e["usd"] for e in events if e["event"] == "m1")
    m1_days = sum(e["usd"] for e in events if e["event"] == "m1" and day_of(e["ts"]) in days_with)
    actual = sum(earned.values()) if earned else None
    mk = {h: [e for e in events if e["event"] == "markout" and e["horizon"] == h] for h in (60, 600)}
    mko = {h: (100 * sum(e["markout"] * e["size"] for e in v) / sum(e["size"] for e in v)) if v else None for h, v in mk.items()}
    fills = [e for e in events if e["event"] == "fill"]
    t0 = min((e["ts"] for e in events if e["event"] == "start"), default=None)
    t1 = max((e["ts"] for e in events if e["event"] in ("pnl", "end", "m1")), default=None)
    n_mk = len({e["mkt"] for e in events if e["event"] == "m1"})
    days = (t1 - t0) / 86400 if t0 is not None and t1 is not None else 0.0
    contracts = sum(f["size"] for f in fills)
    rate = contracts / (n_mk * days) if n_mk and days > 0 else None
    pnls = [e["pnl"] for e in events if e["event"] in ("pnl", "end")]
    out = {"days": round(days, 2), "markets": n_mk, "m1_pred_usd_all": round(m1_all, 2),
           "m1_pred_usd_days_with_earnings": round(m1_days, 2), "reward_actual_usd": actual,
           "reward_ratio": (actual / m1_days) if actual is not None and m1_days > 0 else None,
           "markout60_c": mko[60], "markout600_c": mko[600], "fills": len(fills), "contracts": contracts,
           "fill_rate": rate, "mtm_pnl_usd": pnls[-1] if pnls else 0.0,
           "stopped": any(e["event"] == "stop" for e in events)}
    out["net_usd"] = out["mtm_pnl_usd"] + (actual or 0.0)
    checks = {
        f"reward >= {cfg.reward_ratio_min:.1f} x M1": out["reward_ratio"] is not None and out["reward_ratio"] >= cfg.reward_ratio_min,
        f"markout60 >= {cfg.markout60_min_c:.1f} c": mko[60] is not None and mko[60] >= cfg.markout60_min_c,
        f"fill rate <= {cfg.fill_rate_max:.0f} contracts/market-day": rate is not None and rate <= cfg.fill_rate_max,
    }
    out["checks"] = checks
    out["verdict"] = "PASS" if all(checks.values()) else "FAIL: " + ", ".join(k for k, v in checks.items() if not v)
    return out


def _wq(pairs, q):
    """size-weighted quantile of [value, weight] pairs"""
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * tot - 1e-12:
            return v
    return None


def phase1(events, cv_max=0.5, share_max=0.4, exclude=()):
    """Phase 1 gate (research/PREREG_LP_PHASE1.md, amended 2026-11-16): stability of the portfolio M1 over
    the 30-minute windows of hours 24-48, and no market above share_max of M1 over the last 24 hours.
    Markouts are reported as data, not as a criterion. `exclude`: condition ids left out of everything."""
    starts = [e["ts"] for e in events if e["event"] == "start"]
    wins = sorted((e for e in events if e["event"] == "window"), key=lambda e: e["start"])
    if not starts or not wins:
        return {"verdict": "no data"}
    ex = set(exclude)
    mk_of = lambda w: {c: v for c, v in w["markets"].items() if c not in ex}
    m1_of = lambda w: sum(v["m1_usd_day"] or 0.0 for v in mk_of(w).values())
    t0, t_last = min(starts), wins[-1]["end"]
    half = [w for w in wins if w["start"] >= t0 + 86400]
    last24 = [w for w in wins if w["start"] >= t_last - 86400]
    port = [m1_of(w) for w in half]
    mean = sum(port) / len(port) if port else 0.0
    cv = (sum((x - mean) ** 2 for x in port) / len(port)) ** 0.5 / mean if port and mean > 0 else None
    conds = sorted({c for w in wins for c in mk_of(w)})
    tot24 = sum(m1_of(w) for w in last24)
    avg = lambda v: sum(v) / len(v) if v else None
    per = {}
    for c in conds:
        xs = [w["markets"][c]["m1_usd_day"] or 0.0 for w in half if c in w["markets"]]
        mu = avg(xs) or 0.0
        sd = (sum((x - mu) ** 2 for x in xs) / len(xs)) ** 0.5 if xs else 0.0
        mk = [m for w in wins for m in (w["markets"].get(c) or {}).get("markout60", [])]
        per[c] = {"m1_usd_day_2nd_half": mu, "cv": sd / mu if mu > 0 else None,
                  "share_last24": (sum(w["markets"][c]["m1_usd_day"] or 0.0 for w in last24 if c in w["markets"]) / tot24) if tot24 else None,
                  "comp_levels": avg([w["markets"][c]["comp_levels"] for w in half if c in w["markets"] and w["markets"][c].get("comp_levels") is not None]),
                  "places": sum(w["markets"][c].get("places", 0) for w in wins if c in w["markets"]),
                  "cancels": sum(w["markets"][c].get("cancels", 0) for w in wins if c in w["markets"]),
                  "fills": sum(w["markets"][c]["fills"] for w in wins if c in w["markets"]),
                  "contracts": sum(w["markets"][c]["contracts"] for w in wins if c in w["markets"]),
                  "markout60_mean_c": (sum(v * q for v, q in mk) / sum(q for _, q in mk)) if mk else None,
                  "markout60_p5_c": _wq(mk, 0.05) if mk else None}
    mk_all = [m for w in wins for v in mk_of(w).values() for m in v.get("markout60", [])]
    q = sum(w for _, w in mk_all)
    hours = (t_last - t0) / 3600
    out = {"hours": round(hours, 1), "windows_2nd_half": len(half), "excluded": sorted(ex),
           "portfolio_m1_usd_day_2nd_half": mean, "cv": cv,
           "max_share_last24": max((v["share_last24"] or 0.0 for v in per.values()), default=None),
           "fills": sum(v["fills"] for v in per.values()), "contracts": sum(v["contracts"] for v in per.values()),
           "contracts_per_market_day": sum(v["contracts"] for v in per.values()) / (len(conds) * hours / 24) if conds and hours else None,
           "markout60": {"n_fills": len(mk_all), "mean_c": sum(v * w for v, w in mk_all) / q if q else None,
                         "p5_c": _wq(mk_all, 0.05) if mk_all else None, "p25_c": _wq(mk_all, 0.25) if mk_all else None,
                         "p50_c": _wq(mk_all, 0.5) if mk_all else None,
                         "share_worse_than_-2.1c": sum(w for v, w in mk_all if v < -2.1) / q if q else None},
           "expected_week_usd": 7 * mean, "markets": per}
    checks = {
        f"M1 CV <= {cv_max:.0%} (hours 24-48)": hours >= 48 - 0.6 and cv is not None and cv <= cv_max,
        f"no market > {share_max:.0%} of M1 (last 24 h)": out["max_share_last24"] is not None and out["max_share_last24"] <= share_max,
    }
    out["checks"] = checks
    out["verdict"] = "PASS" if all(checks.values()) else "FAIL: " + ", ".join(k for k, v in checks.items() if not v)
    return out


def load_events(lp_dir, mode):
    path = os.path.join(lp_dir, f"{mode}.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f]


# --------------------------------------------------------------------------------------------------
# CLI entry points (called from polybot/__main__.py)

async def _live_exec():
    from .config import load_dotenv
    load_dotenv()
    key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    if not key:
        raise SystemExit("POLYMARKET_PRIVATE_KEY is not set")
    return await LiveExec.create(key, os.environ.get("FUNDER_ADDRESS", "").strip(),
                                 os.environ.get("POLYMARKET_RELAYER_KEY", "").strip(),
                                 os.environ.get("POLYMARKET_RELAYER_ADDRESS", "").strip())


def cli(cmd, args):
    from .config import load_dotenv
    load_dotenv()
    cfg = from_env()
    if cmd == "lp-select":
        for m in select(cfg):
            print(f"  ${m['m1_usd_day']:6.2f}/day  N={m['N']:.0f}  v={m['v']}c  mid {m['mid']:.3f}  cap ${m['quote_cap']:.0f}  {m['q'][:70]}")
        return
    if cmd == "lp-report":
        mode = args[0] if args else "live"
        s = summarize(load_events(cfg.lp_dir, mode), cfg)
        for k, v in s.items():
            print(f"{k}: {v}")
        return
    if cmd == "lp-phase1":
        # lp-phase1 [paper] [--exclude TEXT ...]: TEXT matches market questions in LP_DIR/markets.json
        mode = args[0] if args and not args[0].startswith("--") else "paper"
        texts = [args[i + 1] for i, a in enumerate(args) if a == "--exclude" and i + 1 < len(args)]
        with open(os.path.join(cfg.lp_dir, "markets.json")) as f:
            qs = {m["cond"]: m["q"] for m in json.load(f)["markets"]}
        ex = {c for c, q in qs.items() for t in texts if t.lower() in (q or "").lower()}
        r = phase1(load_events(cfg.lp_dir, mode), exclude=ex)
        print("excluded:", [qs[c][:60] for c in ex])
        for k, v in r.items():
            if k != "markets":
                print(f"{k}: {v}")
        for c, v in (r.get("markets") or {}).items():
            print(" ", (qs.get(c) or c)[:50], {k: (round(x, 3) if isinstance(x, float) else x) for k, x in v.items()})
        return
    if cmd == "lp-earnings":
        # fetch actual earnings for every UTC day of the live test and append them to the journal
        ev = load_events(cfg.lp_dir, "live")
        t0 = min((e["ts"] for e in ev if e["event"] == "start"), default=None)
        if t0 is None:
            raise SystemExit("no live journal")
        conds = next(e["markets"] for e in ev if e["event"] == "start")

        async def go():
            ex = await _live_exec()
            j = Journal(os.path.join(cfg.lp_dir, "live.jsonl"))
            d = t0
            while d < time.time() - 86400:          # complete days only
                day = time.strftime("%Y-%m-%d", time.gmtime(d))
                e = await ex.earnings(day)
                if e is not None:
                    j.write("earnings", day=day, by_market={c: e.get(c, 0.0) for c in conds})
                    print(day, f"${sum(e.get(c, 0.0) for c in conds):.2f}")
                d += 86400
            await ex.c.close()
        asyncio.run(go())
        return
    sel = json.load(open(os.path.join(cfg.lp_dir, "markets.json")))["markets"]
    if len(sel) < cfg.markets_min:
        raise SystemExit(f"only {len(sel)} markets selected; need {cfg.markets_min}")
    if cmd == "lp-paper":
        asyncio.run(LPTest(cfg, PaperExec(every_s=cfg.fills_every_s), Journal(os.path.join(cfg.lp_dir, "paper.jsonl")), sel).run())
        return
    if cmd == "lp-live":
        if "--i-understand-live" not in args:
            raise SystemExit("lp-live sends real orders. Re-run with --i-understand-live.")
        async def go():
            ex = await _live_exec()
            try:
                bal = await ex.c.get_balance_allowance(asset_type="COLLATERAL")
                if bal.balance / 1e6 < cfg.capital_usd * 0.9:
                    raise SystemExit(f"collateral ${bal.balance / 1e6:.2f} is below the planned ${cfg.capital_usd:.0f}")
                if bal.balance / 1e6 > cfg.capital_usd * 1.5:
                    print(f"warning: wallet holds ${bal.balance / 1e6:.2f}; the stop protects only ${cfg.stop_loss_usd:.0f} of MTM loss")
                await LPTest(cfg, ex, Journal(os.path.join(cfg.lp_dir, "live.jsonl")), sel).run()
            finally:
                await ex.close()
        asyncio.run(go())
        return
    raise SystemExit(f"unknown command {cmd}")
