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
import bisect
import collections
import dataclasses
import gzip
import itertools
import json
import os
import re
import sys
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
    select_snapshots: int = 48         # selection (PREREG_LP_PHASE1B): 48 book snapshots 30 minutes apart = one day,
    select_gap_s: float = 1800.0       # median M1 per market; a snapshot where we cannot quote counts as 0
    min_comp_levels: float = 3.0       # hard filter: median competitor price levels in the reward zone >= 3
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
    # H10 closure rule (PREREG_LP_PHASE1B): portfolio M1 in hours 24-48 per $100 of quote capital
    m1_per_100_min: float = 10.0
    # phase 2 (research/PREREG_PHASE2.md): the portfolio is re-selected every rebalance_h hours from the book
    # snapshots of the last lookback_h hours; markets leave the portfolio through an exit (reduce-only quotes,
    # then a taker order). phase = 1 keeps the one-off selection of phases 1 / 1B.
    phase: int = 1
    lookback_h: float = 24.0           # warm-up and selection window
    snapshot_gap_s: float = 1800.0     # snapshots of every rewarded market's book, 30 minutes apart
    rebalance_h: float = 3.0
    kmax: int = 1                      # quote size up to kmax x rewards_min_size, allocated step by step
    exclude_categories: str = ""       # fee categories never quoted (comma-separated feeType values)
    min_hours_left: float = 0.0        # never select a market this close to its endDate
    exit_at_end: int = 0               # 1: exit by endDate (reduce from exit_reduce_h, close by exit_flatten_h before it);
                                       # 0: no endDate exit (weather trades its measured day after endDate)
    exit_reduce_h: float = 1.0         # last hour before endDate: only the side that reduces the position is quoted
    exit_window_h: float = 1.0         # a market leaving the portfolio has this long to exit through quotes
    exit_flatten_h: float = 0.25       # then (or this close to endDate) the rest is closed by a taker order
    dust_shares: float = 5.0           # positions below this are left alone (the exchange's minimum order)
    inv_reduce_h: float = 0.0          # a position held this long: only the reducing side is quoted (0 = off)
    inv_flatten_h: float = 0.0         # a position held this long is closed by a taker order (0 = off)
    flatten_retry_s: float = 60.0      # after a taker close that did not fill, the next try waits this long (the quotes
                                       # are cancelled before each try and would otherwise never rest)
    exit_improve_ticks: int = 0        # phase 3: quotes that reduce a position sit this many ticks inside the spread
                                       # (first in the queue), never crossing; 0 = join the best price
    reduce_improve_always: int = 0     # 1: also while not exiting / aged, the reducing side is improved (when |pos| >=
                                       # dust) and quoted even outside the reward zone
    inv_fifo: int = 0                  # 1: position age by lots (oldest contract first); only lots older than
                                       # inv_flatten_h are closed by a taker; 0: age since |pos| last was below dust
    exit_mid_lo: float = 0.10          # phase 2: the mid leaving [exit_mid_lo, exit_mid_hi] (the outcome becoming
    exit_mid_hi: float = 0.90          # clear before resolution) starts the market's exit
    pos_limit_usd: float = 1e9         # per market: the position may not grow beyond this many dollars
    snapshots_keep_h: float = 30.0     # snapshot files older than this are deleted
    paper_lifetime: int = 0            # phase 3: paper fills an order by the trades of its whole lifetime (PaperExec)
    related_filter: int = 0            # phase 3: events whose titles share >= 2 rare words (related_groups) count as
                                       # one event, so the portfolio holds one market of them
    flow_filter: int = 0               # phase 3: selection uses the public taker flow of the lookback (flow_stats)
    flow_one_sided: float = 0.80       #   share of contracts bought YES above this or below 1 - this: left out
    flow_min_opp60: float = 0.75       #   share of contracts followed by an opposite trade within 60 min below: left out
    flow_size_mult: float = 1.0        #   N <= this x the smaller side's contracts per 30 min (k is cut to fit)
    flow_top: int = 60                 #   the tape is fetched for this many best candidates (M1 per dollar at k = 1)


# phase 2 settings (research/PREREG_PHASE2.md, user's decisions of 20 November); LP_* variables still override them
PHASE2 = {"quote_budget_usd": 300.0, "capital_usd": 500.0, "stop_loss_usd": 100.0, "markets_min": 1, "markets_max": 40,
          "max_min_size": 200.0, "min_age_days": 0.0, "min_days_left": 0.0, "kmax": 5, "min_hours_left": 1.0,
          "exclude_categories": "economics_fees,finance_prices_fees,mentions_fees,culture_fees",
          "pos_limit_usd": 50.0, "fills_every_s": 30.0, "test_days": 2.0,
          # exit rule (user, 20 Nov: no position into resolution, out within an hour): a position is never held
          # longer than 1 h (reduce-only quotes after 30 min, a taker order at 1 h); markets leave on rebalance,
          # reward removal or the mid leaving [0.10, 0.90]
          "exit_at_end": 0, "inv_reduce_h": 0.5, "inv_flatten_h": 1.0}


# phase 3 (research/PREREG_PHASE3.md, user's decisions of 22-23 November): the phase 2 loop with the new exit rules
# (reducing quotes one tick inside the spread, also before the position ages; position age by FIFO lots, only
# lots older than 1 h are closed by taker), the corrected paper fill model, and the live capital of $300
PHASE3 = {"exit_improve_ticks": 1, "reduce_improve_always": 1, "inv_fifo": 1, "paper_lifetime": 1,
          "capital_usd": 300.0, "quote_budget_usd": 200.0, "stop_loss_usd": 60.0}


def from_env(phase=None) -> LPConfig:
    """Settings from LP_* environment variables. The phase (argument, else LP_PHASE, else 3) picks the defaults:
    phase 3 starts from PHASE2 + PHASE3, phase 2 from PHASE2, phase 1 from the LPConfig defaults."""
    c = LPConfig()
    c.phase = int(phase or os.environ.get("LP_PHASE") or 3)
    if c.phase >= 2:
        for k, v in PHASE2.items():
            setattr(c, k, v)
    if c.phase >= 3:
        for k, v in PHASE3.items():
            setattr(c, k, v)
    for f in dataclasses.fields(c):
        raw = os.environ.get("LP_" + f.name.upper())
        if raw is not None and f.name != "phase":
            setattr(c, f.name, type(getattr(c, f.name))(raw))
    return c


def phase3_config(**kw) -> LPConfig:
    """LPConfig with the phase 3 settings (no environment), for tests and research tools."""
    c = phase2_config(phase=3)
    for k, v in PHASE3.items():
        setattr(c, k, v)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def phase2_config(**kw) -> LPConfig:
    """LPConfig with the phase 2 settings (no environment), for tests and research tools."""
    c = LPConfig(phase=2)
    for k, v in PHASE2.items():
        setattr(c, k, v)
    for k, v in kw.items():
        setattr(c, k, v)
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
# market selection (research/PREREG_LP_PHASE1B.md)

class SelectionStopped(Exception):
    """Raised by the sleep function given to select() when the service stops; the snapshots taken so far are on
    disk and the next start continues the same selection."""


def comp_levels(bids, asks, mid, v_c, min_size) -> int:
    """Competitors in the reward zone: price levels of other orders within v cents of the midpoint that carry at
    least rewards_min_size, both sides together. The same measure as comp_levels in the test loop."""
    n = sum(1 for p, z in bids if 0 <= (mid - p) * 100 < v_c and z + 1e-9 >= min_size)
    return n + sum(1 for p, z in asks if 0 <= (p - mid) * 100 < v_c and z + 1e-9 >= min_size)


def _rows(cand, rw, bk, cfg):
    """Quote, M1 prediction, competitor levels and capital per candidate market for one book snapshot. Markets
    where we cannot quote (no reward, best prices outside the zone, mid outside [mid_lo, mid_hi]) are absent."""
    rows = {}
    for c, m in cand:
        tok = json.loads(m["clobTokenIds"])[0]
        if tok not in bk or c not in rw:
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
                   "comp_levels": comp_levels(bids, asks, mid, r["v"], r["min"]),
                   "quote_cap": n * (bid + 1 - ask), "neg_risk": bool(m.get("negRisk")), "end": m["endDate"]}
    return rows


def median(vals):
    v = sorted(vals)
    n = len(v)
    return 0.0 if not n else v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


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


def _candidates(meta, cfg, now):
    """Stable (classification A), active, at least min_age_days old, at least min_days_left to go, not a game."""
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
        cand.append((c, {"question": q, "clobTokenIds": m["clobTokenIds"], "endDate": m["endDate"],
                         "negRisk": bool(m.get("negRisk")), "events": [{"id": (m.get("events") or [{}])[0].get("id")}]}))
    return cand


_SNAP_FIELDS = ("m1_usd_day", "comp_levels", "mid", "N", "v", "min", "rate", "tick", "quote_cap")


def select(cfg: LPConfig, now=None, log=print, sleep=time.sleep, clock=time.time,
           rewards_fn=None, gamma_fn=None, books_fn=None):
    """Market selection over one day (research/PREREG_LP_PHASE1B.md).

    The candidate set is fixed at the first snapshot. Then select_snapshots book snapshots are taken
    select_gap_s apart; each snapshot re-reads the reward config, so a market whose reward is removed, whose
    best prices leave the reward zone or whose mid leaves [mid_lo, mid_hi] counts as M1 = 0 and 0 competitor
    levels there. Per market: M1 = median over all snapshots, competitor levels = median over all snapshots.
    Hard filter: median competitor levels >= min_comp_levels. Then choose(): M1 per dollar, one market per
    event, quote budget, no market above max_share of the predicted M1.

    Every snapshot is appended to <lp_dir>/selection/snapshots.jsonl, so a restart (update, reboot) continues
    the selection instead of starting the day again. sleep() may raise SelectionStopped."""
    rewards_fn, gamma_fn, books_fn = rewards_fn or rewards_config, gamma_fn or gamma_markets, books_fn or books
    n_snap = max(1, cfg.select_snapshots)
    sdir = os.path.join(cfg.lp_dir, "selection")
    os.makedirs(sdir, exist_ok=True)
    cpath, spath = os.path.join(sdir, "candidates.json"), os.path.join(sdir, "snapshots.jsonl")
    rewarded = lambda: {c: r for c, r in rewards_fn().items() if r["rate"] > 0 and r["min"] <= cfg.max_min_size}
    saved = None
    if os.path.exists(cpath):
        with open(cpath) as f:
            saved = json.load(f)
    if saved:
        cand = [(c, m) for c, m in saved["cand"]]
    else:
        now = now or clock()
        cand = _candidates(gamma_fn(rewarded()), cfg, now)
        with open(cpath + ".tmp", "w") as f:
            json.dump({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "cand": cand}, f)
        os.replace(cpath + ".tmp", cpath)
    snaps = []
    if os.path.exists(spath):
        with open(spath) as f:
            for line in f:
                try:
                    snaps.append(json.loads(line))
                except ValueError:
                    pass                                   # a line cut by a crash; that snapshot is taken again
    if snaps or saved:
        log(f"selection continues: {len(cand)} candidates fixed at {saved['time'] if saved else '?'}, "
            f"{len(snaps)}/{n_snap} snapshots on disk")
    else:
        log(f"selection starts: {len(cand)} candidates, {n_snap} snapshots every {cfg.select_gap_s / 60:.0f} min")
    while len(snaps) < n_snap:
        if snaps:
            wait = snaps[-1]["ts"] + cfg.select_gap_s - clock()
            if wait > 0:
                sleep(wait)
        try:
            rw = rewarded()
            live = [(c, m) for c, m in cand if c in rw]
            rows = _rows(live, rw, books_fn([json.loads(m["clobTokenIds"])[0] for _, m in live]), cfg)
        except SelectionStopped:
            raise
        except Exception as e:                              # the public API failed even after retries
            log(f"selection snapshot failed ({e!r:.200}); next try in 5 minutes")
            sleep(min(300.0, cfg.select_gap_s))
            continue
        snap = {"k": len(snaps) + 1, "ts": clock(),
                "rows": {c: [round(x[k], 6) if isinstance(x[k], float) else x[k] for k in _SNAP_FIELDS] for c, x in rows.items()}}
        with open(spath, "a") as f:
            f.write(json.dumps(snap) + "\n")
            f.flush()
            os.fsync(f.fileno())
        snaps.append(snap)
        log(f"selection snapshot {len(snaps)}/{n_snap}: {len(rows)} markets quotable")
    # aggregate
    rows, stats = [], {}
    for c, m in cand:
        vals = [s["rows"].get(c) for s in snaps]
        seen = [v for v in vals if v]
        if not seen:
            continue
        last = dict(zip(_SNAP_FIELDS, seen[-1]))            # the latest reward parameters and quote
        m1s = [v[0] if v else 0.0 for v in vals]
        comps = [v[1] if v else 0 for v in vals]
        x = {"cond": c, "q": m.get("question"), "yes": json.loads(m["clobTokenIds"])[0], "no": json.loads(m["clobTokenIds"])[1],
             "event": str((m.get("events") or [{}])[0].get("id") or c), "N": last["N"], "v": last["v"], "min": last["min"],
             "rate": last["rate"], "tick": last["tick"], "mid": last["mid"], "quote_cap": last["quote_cap"],
             "neg_risk": bool(m.get("negRisk")), "end": m["endDate"],
             "m1_usd_day": median(m1s), "comp_levels_median": median(comps), "quotable_share": len(seen) / len(vals),
             "m1_snapshots": [round(v, 3) for v in m1s]}
        stats[c] = [(m.get("question") or "")[:90], round(x["m1_usd_day"], 3), x["comp_levels_median"], round(x["quotable_share"], 3)]
        rows.append(x)
    passed = [x for x in rows if x["comp_levels_median"] + 1e-9 >= cfg.min_comp_levels]
    out = choose(passed, cfg)
    cap = sum(x["quote_cap"] for x in out)
    tot = sum(x["m1_usd_day"] for x in out)
    span_h = (snaps[-1]["ts"] - snaps[0]["ts"]) / 3600 if snaps else 0.0
    gap_h = max((b["ts"] - a["ts"] for a, b in zip(snaps, snaps[1:])), default=0.0) / 3600
    log(f"selection over {len(snaps)} snapshots ({span_h:.1f} h, longest gap {gap_h:.1f} h): candidates quotable at least "
        f"once {len(rows)}; median competitor levels >= {cfg.min_comp_levels:g}: {len(passed)}; with median M1 > 0: "
        f"{sum(1 for x in passed if x['m1_usd_day'] > 0)}")
    log(f"selected {len(out)} markets, quote capital ${cap:.0f}, median M1 prediction ${tot:.2f}/day, "
        f"largest share {max((x['m1_usd_day'] / tot for x in out), default=0):.0%}")
    if len(out) < cfg.markets_min:
        log(f"fewer than {cfg.markets_min} markets pass the filters; the test should not start")
    with open(os.path.join(sdir, "summary.json"), "w") as f:
        json.dump({"snapshots": len(snaps), "span_h": span_h, "longest_gap_h": gap_h, "min_comp_levels": cfg.min_comp_levels,
                   "candidates_quotable": len(rows), "passed_filter": len(passed), "selected": [x["cond"] for x in out],
                   "stats": stats}, f)
    with open(os.path.join(cfg.lp_dir, "markets.json"), "w") as f:
        json.dump({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock())), "markets": out}, f, indent=1)
    return out


# --------------------------------------------------------------------------------------------------
# phase 2: rolling selection (research/PREREG_PHASE2.md). The same functions run in the bot and in the
# simulator research/tools/lp_levers.py, so the prediction and the paper test are comparable.

def evaluate(rw, book, k, mid_lo=0.15, mid_hi=0.85):
    """(M1 $/day, competitor levels, quote capital $) of quoting k x rewards_min_size at the best prices of one
    book snapshot, or None where we cannot quote (no reward, best prices outside the zone, mid outside
    [mid_lo, mid_hi]). rw = [rate, v, min]; book = [tick, bids, asks] (YES book, best first)."""
    rate, v, mn = rw
    if v <= 0 or mn <= 0 or rate <= 0:
        return None
    tick, bids, asks = book
    b, a = targets(bids, asks, tick, v, 0.0, 1.0)
    if b is None or a is None:
        return None
    mid = (b + a) / 2
    if not mid_lo <= mid <= mid_hi:
        return None
    size = k * mn
    return rate * m1_fraction(bids, asks, b, a, size, v, mn), comp_levels(bids, asks, mid, v, mn), size * (b + 1 - a)


def eligible(meta_c, now, exclude=(), topic=True, category="", min_age_days=0.0, min_hours_left=0.0):
    """Permanent filters for one market's metadata (SnapStore / research/tools/lp_scan.py format): active, not a
    game, not a news topic (classification A), fee category not excluded, old enough, far enough from its end."""
    if not meta_c or not meta_c.get("active", True) or meta_c.get("game"):
        return False
    if topic and topic_news(meta_c.get("question") or ""):
        return False
    ft = meta_c.get("fee_type") or "none"
    if (category and ft != category) or ft in exclude:
        return False
    try:
        if now - _ts(meta_c["created"]) < min_age_days * 86400 or _ts(meta_c["end"]) - now < min_hours_left * 3600:
            return False
    except Exception:
        return False
    return True


def snap_rows(rw, books_by_cond, conds, kmax, mid_lo=0.15, mid_hi=0.85):
    """One snapshot -> {cond: [m1 at k = 1..kmax, capital at k = 1..kmax, competitor levels]} for the markets of
    `conds` we can quote there; the others are absent (M1 0, 0 competitor levels)."""
    out = {}
    for c in conds:
        r, bk = rw.get(c), books_by_cond.get(c)
        if not r or not bk:
            continue
        ev = [evaluate(r, bk, k, mid_lo, mid_hi) for k in range(1, kmax + 1)]
        if ev[0] is None:
            continue
        out[c] = [[round(e[0], 6) for e in ev], [round(e[2], 4) for e in ev], ev[0][1]]
    return out


def allocate(rows, budget, kmax, markets_max, max_share):
    """Quote budget in steps of one rewards_min_size: each step goes to the option with the best marginal M1 per
    dollar (a new market at k = 1, one market per event, or one more unit on a chosen market, up to kmax). No
    market takes more than max_share of the budget; if one carries more than max_share of the predicted M1, its
    size is cut by one unit (dropped at k = 1) and the budget is spread again.
    rows: [{cond, event, m1_k, cap_k, ...}] -> the chosen rows with k, m1_usd_day and quote_cap at that k."""
    banned, kcap = set(), {}
    while True:
        k, events, cap = {}, set(), 0.0
        while True:
            best = None
            for x in rows:
                c = x["cond"]
                if c in banned:
                    continue
                cur = k.get(c, 0)
                if cur >= min(kmax, kcap.get(c, kmax)) or (cur == 0 and (x["event"] in events or len(k) >= markets_max)):
                    continue
                if x["cap_k"][cur] > max(budget * max_share, x["cap_k"][0]):
                    continue
                gain = x["m1_k"][cur] - (x["m1_k"][cur - 1] if cur else 0.0)
                cost = x["cap_k"][cur] - (x["cap_k"][cur - 1] if cur else 0.0)
                if gain <= 0 or cost <= 0 or cap + cost > budget:
                    continue
                if best is None or gain / cost > best[0]:
                    best = (gain / cost, c, cost, x["event"])
            if best is None:
                break
            _, c, cost, e = best
            k[c] = k.get(c, 0) + 1; cap += cost; events.add(e)
        out = []
        for x in rows:
            if x["cond"] in k:
                kk = k[x["cond"]]
                out.append(dict(x, k=kk, m1_usd_day=x["m1_k"][kk - 1], quote_cap=x["cap_k"][kk - 1]))
        tot = sum(x["m1_usd_day"] for x in out)
        top = max(out, key=lambda x: x["m1_usd_day"], default=None)
        if top is None or tot <= 0 or top["m1_usd_day"] / tot <= max_share or len(out) < 2:
            return out
        if top["k"] > 1:
            kcap[top["cond"]] = top["k"] - 1
        else:
            banned.add(top["cond"])


def rank(snaps, events, kmax, comp_min, budget, markets_max, max_share, cands=None, adjust=None):
    """Portfolio from the snapshot rows of the selection window (snap_rows of each snapshot, oldest first).
    Per market: M1 at each k = median over all snapshots (0 where we could not quote), capital at each k = median
    over the snapshots where we could quote, competitor levels = median (0 where we could not quote).
    Kept: median competitor levels >= comp_min and median M1 at k = 1 above 0; then allocate().
    cands: the markets considered (default: every market quotable at least once in the window).
    adjust: phase 3, rows -> rows before allocate() (the flow filter drops markets and cuts k).
    Returns (all kept rows, chosen rows)."""
    cands = sorted({c for s in snaps for c in s}) if cands is None else cands
    rows = []
    for c in cands:
        vals = [s.get(c) for s in snaps]
        seen = [v for v in vals if v]
        if not seen:
            continue
        comp = median([v[2] if v else 0 for v in vals])
        m1_k = [median([v[0][i] if v else 0.0 for v in vals]) for i in range(kmax)]
        cap_k = [median([v[1][i] for v in seen]) for i in range(kmax)]
        if comp + 1e-9 < comp_min or m1_k[0] <= 0:
            continue
        rows.append({"cond": c, "event": events.get(c, c), "m1_usd_day": m1_k[0], "quote_cap": cap_k[0], "comp": comp,
                     "m1_k": m1_k, "cap_k": cap_k, "k": 1, "quotable_share": len(seen) / len(vals)})
    if adjust is not None:
        rows = adjust(rows)
    return rows, allocate(rows, budget, kmax, markets_max, max_share)


WORD = re.compile(r"[a-z]{4,}")


def _words(title):
    return {w[:-1] if w.endswith("s") and len(w) > 4 else w for w in WORD.findall((title or "").lower())}


def related_groups(titles, max_df=0.002, min_shared=2):
    """Phase 3: events that are one bet in substance (\"Which math problems will AI solve in 2026?\" and \"How many
    more Millennium Prize Problems will AI solve in 2026?\"). Words of 4+ letters (plural s dropped) that appear in
    at most max(2, max_df x events) titles are rare; events sharing >= min_shared rare words are linked, and linked
    events form one group. titles: {event id: title} over all rewarded events -> {event id: group id}."""
    words = {e: _words(t) for e, t in titles.items()}
    df = collections.Counter(w for ws in words.values() for w in ws)
    lim = max(2.0, max_df * len(words))
    by_word = collections.defaultdict(list)
    for e, ws in words.items():
        for w in ws:
            if df[w] <= lim:
                by_word[w].append(e)
    shared = collections.Counter()
    for es in by_word.values():
        for a, b in itertools.combinations(sorted(es), 2):
            shared[(a, b)] += 1
    parent = {e: e for e in titles}

    def find(e):
        while parent[e] != e:
            parent[e] = parent[parent[e]]
            e = parent[e]
        return e
    for (a, b), n in sorted(shared.items()):
        if n >= min_shared:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    return {e: find(e) for e in titles}


def flow_stats(trades, t0, t1, n):
    """Phase 3: the public taker flow of a market over [t0, t1). trades: taker_trades() rows. Each trade counts as
    min(size, n) contracts (what it could fill of a quote of n). Returns {trades, buy_yes_share (contracts where the
    taker bought YES or sold NO), opp60 (share of contracts followed by an opposite-direction trade within 60 min;
    only trades at least 60 min before t1 count), side_per_30m (the smaller direction's contracts per 30 min)}."""
    rows = sorted((ts, (sd == "B") == (oi == 0), float(sz)) for ts, oi, sd, px, sz, *_ in trades if t0 <= ts < t1)
    if not rows:
        return {"trades": 0, "buy_yes_share": None, "opp60": None, "side_per_30m": 0.0}
    tot = sum(min(z, n) for _, _, z in rows)
    buy = sum(min(z, n) for _, b, z in rows if b)
    ts_b = [t for t, b, _ in rows if b]
    ts_s = [t for t, b, _ in rows if not b]
    den = ok = 0.0
    for t, b, z in rows:
        if t > t1 - 3600:
            continue
        opp = ts_s if b else ts_b
        j = bisect.bisect_right(opp, t)
        den += min(z, n)
        ok += min(z, n) if j < len(opp) and opp[j] - t <= 3600 else 0.0
    return {"trades": len(rows), "buy_yes_share": buy / tot if tot else None, "opp60": ok / den if den else None,
            "side_per_30m": min(buy, tot - buy) / ((t1 - t0) / 1800)}


def flow_keep(f, n_unit, cfg):
    """Phase 3 flow filter for one market: the largest k allowed (0 = left out). n_unit: rewards_min_size."""
    if not f or not f["trades"] or f["buy_yes_share"] is None or f["opp60"] is None:
        return 0
    if not (1 - cfg.flow_one_sided <= f["buy_yes_share"] <= cfg.flow_one_sided) or f["opp60"] < cfg.flow_min_opp60:
        return 0
    return min(cfg.kmax, int(cfg.flow_size_mult * f["side_per_30m"] / n_unit + 1e-9))


def grid(entries, gap_s=1800.0, key=lambda e: e["ts"]):
    """Snapshots on the selection grid: oldest first, each at least gap_s - 10 s after the previous kept one."""
    out = []
    for e in sorted(entries, key=key):
        if not out or key(e) - key(out[-1]) >= gap_s - 10:
            out.append(e)
    return out


def meta_of(gm):
    """Metadata kept per market from a gamma market object (the lp_scan format plus the NO token)."""
    ev = (gm.get("events") or [{}])[0]
    fs = gm.get("feeSchedule") or {}
    toks = json.loads(gm["clobTokenIds"]) if gm.get("clobTokenIds") else [None, None]
    return {"question": gm.get("question"), "event": str(ev.get("id") or gm.get("conditionId")), "token": toks[0],
            "event_title": ev.get("title"),
            "no": toks[1] if len(toks) > 1 else None,
            "fee_type": gm.get("feeType") if gm.get("feesEnabled") else None,
            "fee_rate": float(fs.get("rate") or 0.0) if gm.get("feesEnabled") else 0.0,
            "rebate_rate": float(fs.get("rebateRate") or 0.0) if gm.get("feesEnabled") else 0.0,
            "created": gm.get("createdAt") or gm.get("startDate"), "end": gm.get("endDate"),
            "neg_risk": bool(gm.get("negRisk")), "game": bool(gm.get("gameStartTime")),
            "active": bool(gm.get("active")) and not gm.get("closed") and bool(gm.get("acceptingOrders"))}


def _trim(levels, mid, v_c, side):
    out = []
    for p, s in levels:
        if ((mid - p) if side == "bid" else (p - mid)) * 100 > 2 * v_c or len(out) >= 40:
            break
        out.append([p, round(s, 2)])
    return out


class SnapStore:
    """Book snapshots of every rewarded market that passes the permanent filters, every snapshot_gap_s, kept on
    disk for snapshots_keep_h (a restart loses nothing):
      <lp_dir>/snaps/rows.jsonl              per snapshot {"ts", "n_rewarded", "n_eligible", "rows": snap_rows, "q":
                                             {cond: [rate, v, min, tick, mid]}} of the quotable markets
      <lp_dir>/snaps/books_<UTC>.json.gz     the books themselves (research/tools/lp_scan.py format), for audits
      <lp_dir>/snaps/meta.json               market metadata, refreshed every 6 hours"""

    META_EVERY_S = 6 * 3600

    def __init__(self, cfg: LPConfig, log=print, clock=time.time, rewards_fn=None, books_fn=None, gamma_fn=None,
                 trades_fn=None):
        self.cfg, self.log, self.clock = cfg, log, clock
        self.rewards_fn, self.books_fn, self.gamma_fn = rewards_fn or rewards_config, books_fn or books, gamma_fn or gamma_markets
        self.trades_fn = trades_fn or (lambda c: taker_trades(c, 1000))
        self.flow = {}                                     # phase 3: flow_stats of the last rank, for the journal
        self.dir = os.path.join(cfg.lp_dir, "snaps")
        os.makedirs(self.dir, exist_ok=True)
        self.rows_path, self.meta_path = os.path.join(self.dir, "rows.jsonl"), os.path.join(self.dir, "meta.json")
        self.exclude = tuple(x.strip() for x in cfg.exclude_categories.split(",") if x.strip())
        self.entries, self.meta, self.meta_ts = [], {}, 0.0
        self.load()

    def load(self):
        if os.path.exists(self.rows_path):
            with open(self.rows_path) as f:
                for line in f:
                    try:
                        self.entries.append(json.loads(line))
                    except ValueError:
                        pass                               # a line cut by a crash
        m = None
        if os.path.exists(self.meta_path):
            try:
                with open(self.meta_path) as f:
                    m = json.load(f)
            except ValueError:
                m = None
        if m:
            self.meta, self.meta_ts = m["meta"], m["ts"]
        self.prune(self.clock())

    def prune(self, now):
        keep = now - self.cfg.snapshots_keep_h * 3600
        if self.entries and self.entries[0]["ts"] < keep:
            self.entries = [e for e in self.entries if e["ts"] >= keep]
            with open(self.rows_path + ".tmp", "w") as f:
                for e in self.entries:
                    f.write(json.dumps(e, separators=(",", ":")) + "\n")
            os.replace(self.rows_path + ".tmp", self.rows_path)
        for name in os.listdir(self.dir):
            if name.startswith("books_") and name.endswith(".json.gz"):
                try:
                    t = time.mktime(time.strptime(name[6:21], "%Y%m%dT%H%M%S")) - time.timezone
                except ValueError:
                    continue
                if t < keep:
                    os.remove(os.path.join(self.dir, name))

    def last_ts(self):
        return self.entries[-1]["ts"] if self.entries else None

    def due(self, now):
        return not self.entries or now - self.entries[-1]["ts"] >= self.cfg.snapshot_gap_s - 5

    def window(self, now):
        """Snapshots of the selection window (lookback_h before now) on the 30-minute grid."""
        return grid([e for e in self.entries if now - self.cfg.lookback_h * 3600 <= e["ts"] <= now], self.cfg.snapshot_gap_s)

    def warm(self, now):
        """Enough history to select: the window spans lookback_h less one gap and holds at least 3/4 of its
        snapshots (a long outage during the warm-up extends the warm-up)."""
        w = self.window(now)
        need = self.cfg.lookback_h * 3600 / self.cfg.snapshot_gap_s
        return bool(w) and now - w[0]["ts"] >= self.cfg.lookback_h * 3600 - self.cfg.snapshot_gap_s - 60 and len(w) >= 0.75 * need

    def take(self, now=None):
        """One snapshot now. Returns its summary entry (without rows)."""
        now = now or self.clock()
        cfg = self.cfg
        rw = {c: r for c, r in self.rewards_fn().items() if r["rate"] > 0 and r["v"] > 0 and r["min"] <= cfg.max_min_size}
        missing = [c for c in rw if c not in self.meta]
        if not self.meta or now - self.meta_ts > self.META_EVERY_S or len(missing) > 50:
            gm = self.gamma_fn(list(rw))
            self.meta, self.meta_ts = {c: meta_of(m) for c, m in gm.items()}, now
            with open(self.meta_path + ".tmp", "w") as f:
                json.dump({"ts": now, "meta": self.meta}, f)
            os.replace(self.meta_path + ".tmp", self.meta_path)
        ok = [c for c in rw if eligible(self.meta.get(c), now, self.exclude, min_age_days=cfg.min_age_days,
                                        min_hours_left=cfg.min_days_left * 24)]
        toks = {c: self.meta[c]["token"] for c in ok if self.meta[c].get("token")}
        bk = self.books_fn(list(toks.values()))
        books_c, q = {}, {}
        for c, t in toks.items():
            if t not in bk:
                continue
            bids, asks, tick = bk[t]
            if not bids or not asks:
                books_c[c] = [tick, [], []]
                continue
            mid = (bids[0][0] + asks[0][0]) / 2
            books_c[c] = [tick, _trim(bids, mid, rw[c]["v"], "bid"), _trim(asks, mid, rw[c]["v"], "ask")]
        rwl = {c: [rw[c]["rate"], rw[c]["v"], rw[c]["min"]] for c in rw}
        rows = snap_rows(rwl, books_c, ok, cfg.kmax, cfg.mid_lo, cfg.mid_hi)
        for c in rows:
            tick, bids, asks = books_c[c]
            q[c] = rwl[c] + [tick, round((bids[0][0] + asks[0][0]) / 2, 6)]
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
        path = os.path.join(self.dir, f"books_{stamp}.json.gz")
        with gzip.open(path + ".tmp", "wt") as f:
            json.dump({"ts": now, "rw": rwl, "books": books_c}, f, separators=(",", ":"))
        os.replace(path + ".tmp", path)
        entry = {"ts": now, "n_rewarded": len(rw), "n_eligible": len(ok), "rows": rows, "q": q, "elig": sorted(ok)}
        with open(self.rows_path, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.entries.append(entry)
        self.prune(now)
        w = self.window(now)
        summary = {"ts": now, "n_rewarded": len(rw), "n_eligible": len(ok), "n_quotable": len(rows)}
        with open(os.path.join(self.dir, "state.json.tmp"), "w") as f:     # for status.md during the warm-up
            json.dump(dict(summary, window=len(w), first=w[0]["ts"] if w else None, warm=self.warm(now),
                           need=int(round(self.cfg.lookback_h * 3600 / self.cfg.snapshot_gap_s))), f)
        os.replace(os.path.join(self.dir, "state.json.tmp"), os.path.join(self.dir, "state.json"))
        return summary

    def rank(self, now):
        """Target portfolio now: rank() over the window, then the market dicts the test loop quotes (N = k x min,
        reward parameters and tick of the latest snapshot where the market was quotable). Markets closer than
        min_hours_left to their end are left out. Returns (target markets, summary)."""
        cfg = self.cfg
        w = self.window(now)
        snaps = [e["rows"] for e in w]
        # candidates: the markets rewarded and eligible at the window's first snapshot (as in the simulator runs of
        # 20 Nov: markets new within the window, still without competitors, did worse), still eligible now
        first = set(w[0].get("elig") or w[0]["rows"]) if w else set()
        left = cfg.min_hours_left if cfg.exit_at_end else -1e6
        cands = sorted(c for c in first if eligible(self.meta.get(c), now, self.exclude, min_hours_left=left))
        events = {c: (self.meta.get(c) or {}).get("event", c) for c in cands}
        groups_merged = 0
        if cfg.related_filter:
            titles = {}
            for c, m in self.meta.items():
                titles.setdefault(m.get("event", c), m.get("event_title") or m.get("question") or "")
            g = related_groups(titles)
            events = {c: g.get(e, e) for c, e in events.items()}
            groups_merged = len({(self.meta.get(c) or {}).get("event", c) for c in cands}) - len(set(events.values()))
        adjust = None
        if cfg.flow_filter:
            last_q = {}
            for e in w:
                last_q.update(e["q"])

            def adjust(rows):
                self.flow = {}
                best = sorted(rows, key=lambda x: -x["m1_k"][0] / max(x["cap_k"][0], 1e-9))[:cfg.flow_top]
                out = []
                for x in best:
                    c = x["cond"]
                    unit = last_q[c][2] if c in last_q else None
                    if not unit:
                        continue
                    try:
                        tr = self.trades_fn(c)
                    except Exception as e:                 # no tape: the market is left out this time
                        self.log(f"flow {c[:10]}: {e!r:.100}")
                        continue
                    t0 = now - cfg.lookback_h * 3600
                    if len(tr) >= 1000:                    # newest 1000 trades only: the window is what they cover
                        t0 = max(t0, min(r[0] for r in tr))
                    f = flow_stats(tr, t0, now, unit)
                    kc = flow_keep(f, unit, cfg)
                    self.flow[c] = dict(f, k_max=kc)
                    if kc < 1:
                        continue
                    if kc < cfg.kmax:                      # no gain beyond kc: allocate() stops there
                        x = dict(x, m1_k=x["m1_k"][:kc] + [x["m1_k"][kc - 1]] * (cfg.kmax - kc))
                    out.append(x)
                return out
        rows, chosen = rank(snaps, events, cfg.kmax, cfg.min_comp_levels, cfg.quote_budget_usd, cfg.markets_max,
                            cfg.max_share, cands=cands, adjust=adjust)
        out = []
        for x in chosen:
            c, m = x["cond"], self.meta[x["cond"]]
            last = next(e["q"][c] for e in reversed(w) if c in e["q"])
            rate, v, mn, tick, mid = last
            out.append({"cond": c, "q": m.get("question"), "yes": m["token"], "no": m["no"], "event": m.get("event", c),
                        "k": x["k"], "N": x["k"] * mn, "v": v, "min": mn, "rate": rate, "tick": tick, "mid": mid,
                        "quote_cap": round(x["quote_cap"], 4), "m1_usd_day": round(x["m1_usd_day"], 4),
                        "comp_levels_median": x["comp"], "end": m.get("end"), "neg_risk": m.get("neg_risk", False),
                        "fee_type": m.get("fee_type"), "fee_rate": m.get("fee_rate", 0.0), "rebate_rate": m.get("rebate_rate", 0.0)})
        tot = sum(x["m1_usd_day"] for x in out)
        summary = {"snapshots": len(w), "span_h": (w[-1]["ts"] - w[0]["ts"]) / 3600 if w else 0.0, "candidates": len(cands),
                   "passed": len(rows), "markets": len(out), "quote_cap": sum(x["quote_cap"] for x in out), "m1_pred": tot,
                   "largest_share": max((x["m1_usd_day"] / tot for x in out), default=0.0) if tot else 0.0}
        if cfg.related_filter:
            summary["groups_merged"] = groups_merged
        if cfg.flow_filter:
            summary["flow_checked"] = len(self.flow)
            summary["flow_dropped"] = sum(1 for f in self.flow.values() if f["k_max"] < 1)
        return out, summary


# --------------------------------------------------------------------------------------------------
# executors: paper (simulated fills from the public tape) and live (SDK)

class PaperExec:
    """Orders are virtual. A public taker trade through our price fills us first (worst case,
    the same model as the backtest). Interface shared with LiveExec:
      place(m, side, price, size) -> order id or None      side "bid" = BUY YES, "ask" = BUY NO at 1 - price
      cancel(m, oid) -> "one" | "market"                   "market": every order of ours in m is gone
      fills(markets) -> [{order, mkt, side, price, size, ts, done}]
    The tape is read every every_s. Phase 2 (lifetime=0): only the orders standing at that moment are filled, so an
    order cancelled in between never gets the trades that went through it while it stood. Phase 3 (lifetime=1): a
    cancelled order is kept for GONE_KEEP_S and filled by the trades of its lifetime, as on the exchange.
    guard (research replays): (ts, side, price) -> False skips a fill.
    """

    in_book = False                       # virtual orders never appear in the public book
    GONE_KEEP_S = 600.0                   # the public tape can lag; cancelled orders wait this long for their trades

    def __init__(self, trades_fn=taker_trades, every_s=0.0, lifetime=0, clock=time.time, guard=None):
        self.clock, self.lifetime, self.guard = clock, lifetime, guard
        self.n, self.orders, self.seen, self.since = 0, {}, set(), clock()
        self.gone = {}                                  # lifetime=1: cancelled order id -> order with its end time
        self.trades_fn, self.every_s, self.last_poll = trades_fn, every_s, 0.0
        self.skipped = 0

    async def place(self, m, side, price, size):
        self.n += 1
        oid = f"paper-{self.n}"
        self.orders[oid] = {"mkt": m["cond"], "side": side, "price": price, "rem": size, "t": self.clock()}
        return oid

    def _gone(self, oid, o):
        if o is not None and self.lifetime:
            o["end"] = self.clock()
            self.gone[oid] = o

    async def cancel(self, m, oid):
        self._gone(oid, self.orders.pop(oid, None))
        return "one"

    async def cancel_all(self):
        for oid in list(self.orders):
            self._gone(oid, self.orders.pop(oid))

    async def fills(self, markets):
        out = []
        now = self.clock()
        if now - self.last_poll < self.every_s:
            return out
        self.last_poll = now
        self.gone = {oid: o for oid, o in self.gone.items() if now - o["end"] <= self.GONE_KEEP_S}
        books = dict(self.orders, **self.gone)
        busy = [m for m in markets if any(o["mkt"] == m["cond"] for o in books.values())]
        sem = asyncio.Semaphore(8)                      # the tapes of many markets (phase 2: up to 40) in parallel

        async def tape(cond):
            async with sem:
                return await asyncio.to_thread(self.trades_fn, cond)
        tapes = await asyncio.gather(*(tape(m["cond"]) for m in busy), return_exceptions=True)
        for m, tp in zip(busy, tapes):
            if isinstance(tp, BaseException):
                raise tp
            mine = [(oid, o) for oid, o in books.items() if o["mkt"] == m["cond"]]
            if not mine:
                continue
            for ts, oi, side, px, sz, key in sorted(tp):
                if key in self.seen or ts < self.since:
                    continue
                self.seen.add(key)
                live = [(oid, o) for oid, o in mine if o["t"] <= ts + 1 and ts <= o.get("end", ts) and o["rem"] > 1e-9]
                live.sort(key=lambda x: "end" not in x[1])  # an order that stood at the trade before one placed after it
                bid = next((o["price"] for _, o in live if o["side"] == "bid"), None)
                ask = next((o["price"] for _, o in live if o["side"] == "ask"), None)
                hit = crosses((oi, side, px), bid, ask)
                if not hit:
                    continue
                oid, o = next((oid, o) for oid, o in live if o["side"] == hit)
                if self.guard is not None and not self.guard(ts, hit, o["price"]):
                    self.skipped += 1
                    continue
                q = min(o["rem"], sz); o["rem"] -= q
                done = o["rem"] <= 1e-9
                if done:
                    self.orders.pop(oid, None)
                    self.gone.pop(oid, None)
                out.append({"order": oid, "mkt": m["cond"], "side": hit, "price": o["price"], "size": q, "ts": ts, "done": done})
        return out

    async def merge(self, m, pairs):
        return pairs

    async def take(self, m, side, size, bids, asks):
        """Paper taker order of `size` against the other orders' YES book: side "ask" = BUY NO (closes a long YES)
        walking the YES bids, side "bid" = BUY YES walking the YES asks. Taker fee fee_rate x p x (1 - p) per share
        of the token bought. Returns {side, price (YES terms, size-weighted), size, fee} or None (empty book)."""
        levels = bids if side == "ask" else asks
        left, got, val = size, 0.0, 0.0
        for p, z in levels:
            q = min(left, z)
            got += q; val += q * p; left -= q
            if left <= 1e-9:
                break
        if got <= 1e-9:
            return None
        px = val / got
        tp = 1 - px if side == "ask" else px
        return {"side": side, "price": px, "size": got, "fee": got * float(m.get("fee_rate") or 0.0) * tp * (1 - tp)}

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

    async def collateral(self):
        """USDC of the wallet (on-chain balance; resting orders do not lock it on chain)."""
        b = await self.c.get_balance_allowance(asset_type="COLLATERAL")
        return b.balance / 1e6

    async def balances(self, m):
        """On-chain shares (YES, NO) held by the wallet in market m."""
        out = []
        for tok in (m["yes"], m["no"]):
            b = await self.c.get_balance_allowance(asset_type="CONDITIONAL", token_id=tok)
            out.append(b.balance / 1e6)
        return tuple(out)

    MIN_TAKE_USD = 1.0                    # the exchange's minimum spend of a marketable BUY order

    async def take(self, m, side, size, bids, asks, slippage=0.02, wait_s=10.0):
        """Taker order closing a position (phase 2 exit): a fill-and-kill BUY of the NO token (side "ask", long YES)
        or of the YES token (side "bid", long NO); the pairs are merged afterwards.
        A market BUY is sized in USDC (polymarket-client: `amount`; `shares` is refused for BUY, 25 Nov live):
        `size` shares at the best price of the other orders, at most `slippage` worse (`max_price`). Fills deeper
        in the book buy fewer shares, and the rest is closed at a later step. The spend is at least
        MIN_TAKE_USD, so a small lot may buy a few more shares than `size`.
        The bought shares and their price are the order's taking and making amounts; the on-chain balance change is
        the fallback, and reconcile() adopts any difference later."""
        levels = bids if side == "ask" else asks
        if not levels:
            return None
        best = levels[0][0]
        token, pay = (m["no"], 1 - best) if side == "ask" else (m["yes"], best)
        limit = min(0.99, round(pay + slippage, 2))
        amount = max(self.MIN_TAKE_USD, int(size * pay * 100 + 1e-6) / 100)
        try:
            before = await self.balances(m)
            r = await self.c.place_market_order(token_id=token, side="BUY", amount=f"{amount:.2f}",
                                                max_price=f"{limit:.2f}", order_type="FAK")
        except Exception as e:
            self.log(f"closing order failed: {e!r}")
            return None
        if not getattr(r, "ok", False):
            self.log(f"closing order rejected: {getattr(r, 'code', r)} {getattr(r, 'message', '')}".rstrip())
            return None
        try:
            shares, spent = float(r.taking_amount), float(r.making_amount)
        except Exception:
            shares = spent = 0.0
        if not 0 < shares <= 3 * amount / max(pay, 0.01):   # implausible amounts: trust the wallet instead
            shares = spent = 0.0
        got, t0 = 0.0, time.time()
        while time.time() - t0 < wait_s:              # wait for the settlement, so the merge finds the shares
            await asyncio.sleep(1.0)
            try:
                after = await self.balances(m)
            except Exception:
                continue
            got = (after[1] - before[1]) if side == "ask" else (after[0] - before[0])
            if got > 1e-9 and got >= 0.99 * shares:
                break
        got = shares or got
        if got <= 1e-9:
            return None
        paid = spent / shares if shares and 0 < spent < shares else pay   # average price of the bought token
        px = 1 - paid if side == "ask" else paid      # in YES terms
        tp = 1 - px if side == "ask" else px
        self.log(f"closing order filled: {side} {got:.2f} at {paid:.3f} (asked {size:.2f}, spent ${spent or amount:.2f})")
        return {"side": side, "price": px, "size": got, "fee": got * float(m.get("fee_rate") or 0.0) * tp * (1 - tp)}

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

    def start_heartbeat(self):
        """Exchange-side dead-man switch: if heartbeats stop (process or server dies), Polymarket cancels
        our open orders. Once started it cannot be disarmed for the session."""
        self.hb_task = asyncio.create_task(self._heartbeat())

    async def _heartbeat(self):
        from polymarket import OrderHeartbeatMismatchError
        hb = ""
        while True:
            try:
                try:
                    r = await self.c.send_order_heartbeat(heartbeat_id=hb)
                except OrderHeartbeatMismatchError as e:
                    r = await self.c.send_order_heartbeat(heartbeat_id=e.heartbeat_id)
                hb = r.heartbeat_id
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log(f"heartbeat error: {e!r}")
            await asyncio.sleep(5)

    async def close(self):
        await self.cancel_all()
        if getattr(self, "hb_task", None):
            self.hb_task.cancel()
        await self.c.close()


# --------------------------------------------------------------------------------------------------
# the test loop

class Journal:
    def __init__(self, path, clock=time.time):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.f = open(path, "a", buffering=1)
        self.clock = clock                              # research replays stamp events with their own clock

    def write(self, event, ts=None, **kw):
        kw.update(ts=ts if ts is not None else self.clock(), event=event)
        self.f.write(json.dumps(kw, separators=(",", ":")) + "\n")

    def close(self):
        self.f.close()


class MarketState:
    def __init__(self, m):
        self.m = m
        self.orders = {"bid": None, "ask": None}       # side -> (order id, YES price, placed at, size)
        self.yes = self.no = self.cash = 0.0
        self.mid = None
        self.mids = []                                  # (ts, mid of the other orders) for markouts
        self.exit_since = None                          # phase 2: left the portfolio (or lost its reward) at this time
        self.pos_since = None                           # phase 2: since when |YES - NO| >= dust_shares
        self.lots = []                                  # phase 3 (inv_fifo): [ts, signed YES - NO quantity], oldest first
        self.flatten_retry_at = 0.0                     # no taker close before this time (the last one did not fill)
        self.book = None                                # (bids, asks) of the other orders at the last step
        try:
            self.end_ts = _ts(m["end"]) if m.get("end") else None
        except Exception:
            self.end_ts = None

    def set_market(self, m):
        self.m = m
        try:
            self.end_ts = _ts(m["end"]) if m.get("end") else None
        except Exception:
            self.end_ts = None

    def lot_update(self, ts, dq):
        """Position change dq (YES - NO) at ts: the same direction as the lots adds a lot; the opposite direction
        consumes the oldest lots first, and what is left over opens a lot the other way."""
        while abs(dq) > 1e-9 and self.lots and self.lots[0][1] * dq < 0:
            take = min(abs(dq), abs(self.lots[0][1]))
            self.lots[0][1] += take if self.lots[0][1] < 0 else -take
            dq += -take if dq > 0 else take
            if abs(self.lots[0][1]) < 1e-9:
                self.lots.pop(0)
        if abs(dq) > 1e-9:
            self.lots.append([ts, dq])

    def aged_qty(self, now, age_s):
        """Contracts of the position held at least age_s (FIFO lots)."""
        return sum(abs(q) for t, q in self.lots if now - t >= age_s)

    def hours_left(self, now):
        return (self.end_ts - now) / 3600 if self.end_ts is not None else float("inf")

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
    """The quoting loop. Phase 1 / 1B: a fixed set of markets. Phase 2 and 3 (cfg.phase >= 2, with a SnapStore): the
    portfolio is re-selected every rebalance_h hours inside run(); markets that leave it, lose their reward or come
    within exit_reduce_h of their end quote only the side that reduces the position, and after exit_window_h (or
    within exit_flatten_h of the end) the rest is closed by a taker order. A market is dropped once it is flat."""

    def __init__(self, cfg: LPConfig, ex, journal: Journal, markets, books_fn=books, rewards_fn=rewards_config,
                 log=print, clock=time.time, control_fn=None, store=None):
        self.cfg, self.ex, self.j, self.log, self.clock = cfg, ex, journal, log, clock
        self.control_fn = control_fn                    # returns "run" or "stop"; "stop" pulls every quote
        self.paused = False
        self.end_reason = None
        self.books_fn, self.rewards_fn = books_fn, rewards_fn
        self.store = store                              # phase 2: snapshots and rebalances run inside run()
        self.st = {m["cond"]: MarketState(m) for m in markets}
        self.target = set(self.st)                      # the current portfolio; other markets in st are exiting
        self.pending = []                               # (due ts, horizon, cond, side, price, size)
        self.stopped = None
        self.last_sample = 0.0
        self.day = None
        self.win = None                                 # current 30-minute window index
        self.acc = self._new_acc()
        self.win_cap = []                               # quote capital of the target portfolio at each sample
        self.errors = 0
        self.closed_pnl = 0.0                           # mark-to-market of the markets that left the test
        self.last_rebalance = None
        self.pending_target = None                      # (markets, summary, ts) from the snapshot task, for step()
        self.last_equity = -1e18                        # live: wallet equity (USDC + positions at mid) every 10 min
        self.status_path = os.path.join(os.path.dirname(getattr(journal.f, "name", "") or "."), "status.json")

    @staticmethod
    def _fresh():
        return {"m1": [], "frac": [], "comp_levels": [], "comp_size": [], "places": 0, "cancels": 0,
                "fills": 0, "contracts": 0.0, "markout60": [], "rebate": 0.0}

    def _new_acc(self):
        return {c: self._fresh() for c in self.st}

    def _a(self, c):
        a = self.acc.get(c)
        if a is None:
            a = self.acc[c] = self._fresh()
        return a

    def _close_window(self, now):
        w = self.win
        per = {}
        mean = lambda xs: sum(xs) / len(xs) if xs else None
        for c, a in self.acc.items():
            per[c] = {"m1_usd_day": mean(a["m1"]), "frac": mean(a["frac"]), "comp_levels": mean(a["comp_levels"]),
                      "comp_size": mean(a["comp_size"]), "samples": len(a["m1"]), "places": a["places"],
                      "cancels": a["cancels"], "fills": a["fills"], "contracts": a["contracts"], "markout60": a["markout60"],
                      "rebate": a["rebate"]}
            s = self.st.get(c)
            if s is not None and self.cfg.phase >= 2:
                per[c].update(k=s.m.get("k"), exiting=s.exit_since is not None)
        tot = sum(v["m1_usd_day"] or 0.0 for v in per.values())
        extra = {}
        if self.cfg.phase >= 2:
            extra = {"cap_target": mean(self.win_cap), "target": len(self.target),
                     "exiting": sum(1 for s in self.st.values() if s.exit_since is not None)}
        self.j.write("window", ts=now, start=w * self.cfg.window_s, end=(w + 1) * self.cfg.window_s,
                     m1_usd_day=tot, markets=per, pnl=self.pnl(), errors=self.errors, **extra)
        self.acc = self._new_acc()
        self.win_cap = []

    def pnl(self):
        return self.closed_pnl + sum(s.mtm() for s in self.st.values())

    def restore(self, events):
        """Rebuild positions and cash of a resumed run from its own journal (fills, merges, closing orders), so a
        restart keeps the position limit and the mark-to-market stop. Phase 2: the markets (portfolio and exiting
        ones) come from the rebalance events. Returns the number of fills replayed."""
        n = 0
        for e in events:
            ev = e["event"]
            if ev == "rebalance":
                exiting = e.get("exiting") or {}
                for c, m in (e.get("markets") or {}).items():
                    s = self.st.get(c)
                    if s is None:
                        s = self.st[c] = MarketState(m)
                    else:
                        s.set_market(m)
                    s.exit_since = exiting.get(c)
                self.target = set(e.get("target") or [])
                self.last_rebalance = e["ts"]
                continue
            if ev == "exit":
                s = self.st.get(e.get("mkt"))
                if s is not None and s.exit_since is None:
                    s.exit_since = e["ts"]
                continue
            if ev == "retired":
                if self.st.pop(e["mkt"], None) is not None:
                    self.closed_pnl += e.get("pnl", 0.0)
                continue
            s = self.st.get(e.get("mkt"))
            if s is None:
                continue
            if ev in ("fill", "flatten"):
                if e["side"] == "bid":
                    s.yes += e["size"]; s.cash -= e["size"] * e["price"]
                else:
                    s.no += e["size"]; s.cash -= e["size"] * (1 - e["price"])
                s.cash -= e.get("fee", 0.0)
                s.lot_update(e.get("fill_ts", e.get("ts", 0.0)), e["size"] if e["side"] == "bid" else -e["size"])
                n += ev == "fill"
            elif ev == "merge":
                s.yes -= e["pairs"]; s.no -= e["pairs"]; s.cash += e["pairs"]
        if n or self.last_rebalance is not None:
            self.j.write("restored", fills=n, positions={c: [x.yes, x.no, round(x.cash, 6)] for c, x in self.st.items()})
        return n

    async def reconcile(self):
        """Live: adopt the on-chain YES / NO balances when they differ from the tracked position (a fill we
        never saw, or a restart without the journal). Cash is shared across markets and is not reconciled."""
        if not hasattr(self.ex, "balances"):
            return
        for cond, s in self.st.items():
            try:
                yes, no = await self.ex.balances(s.m)
            except Exception as e:
                self.log(f"balance read failed for {cond[:10]}: {e!r}")
                continue
            if abs(yes - s.yes) > 0.01 or abs(no - s.no) > 0.01:
                self.j.write("reconcile", mkt=cond, tracked=[s.yes, s.no], chain=[yes, no])
                s.yes, s.no = yes, no

    def apply_fill(self, f):
        s = self.st[f["mkt"]]
        a = self._a(f["mkt"])
        a["fills"] += 1
        a["contracts"] += f["size"]
        if f["side"] == "bid":
            s.yes += f["size"]; s.cash -= f["size"] * f["price"]
        else:
            s.no += f["size"]; s.cash -= f["size"] * (1 - f["price"])
        s.lot_update(f["ts"], f["size"] if f["side"] == "bid" else -f["size"])
        o = s.orders[f["side"]]
        if f.get("done") and o and o[0] == f["order"]:
            s.orders[f["side"]] = None
        for h in (60, 600):
            self.pending.append((f["ts"] + h, h, f["mkt"], f["side"], f["price"], f["size"]))
        # maker rebate: a share of the taker fee rate x p x (1 - p) per contract (estimate; feeSchedule of the market)
        p = f["price"] if f["side"] == "bid" else 1 - f["price"]
        rebate = f["size"] * float(s.m.get("fee_rate") or 0.0) * p * (1 - p) * float(s.m.get("rebate_rate") or 0.0)
        a["rebate"] += rebate
        extra = {"rebate": round(rebate, 6)} if self.cfg.phase >= 2 else {}
        self.j.write("fill", mkt=f["mkt"], side=f["side"], price=f["price"], size=f["size"], fill_ts=f["ts"],
                     order=f["order"], **extra)

    def due_markouts(self, now):
        keep = []
        for due, h, cond, side, price, size in self.pending:
            s = self.st.get(cond)
            if s is None:
                continue
            later = next((m for t, m in s.mids if t >= due), None)
            if later is None:
                if now - due <= 600:
                    keep.append((due, h, cond, side, price, size))
                continue                                # otherwise no book sample after the horizon: dropped
            mk = (later - price) if side == "bid" else (price - later)
            self.j.write("markout", mkt=cond, horizon=h, side=side, price=price, size=size, mid=later, markout=mk)
            if h == 60:
                self._a(cond)["markout60"].append([round(100 * mk, 3), size])
        self.pending = keep

    # ---- phase 2: portfolio changes and exits

    def apply_rebalance(self, target, summary, now):
        """New target portfolio: new markets start quoting at their size, markets whose size changed re-quote at
        the new size, markets no longer in the target start their exit. Written to the journal with every market
        dict, so a restart rebuilds the same state."""
        want = {m["cond"]: m for m in target}
        added, resized, retired, back = [], [], [], []
        for c, m in want.items():
            s = self.st.get(c)
            if s is None:
                self.st[c] = MarketState(m)
                self._a(c)
                added.append(c)
                continue
            if s.exit_since is not None:
                s.exit_since = None
                back.append(c)
            if abs(s.m["N"] - m["N"]) > 1e-9:
                resized.append(c)
            s.set_market(m)
        for c, s in self.st.items():
            if c not in want and s.exit_since is None:
                s.exit_since = now
                retired.append(c)
        self.target = set(want)
        self.last_rebalance = now
        cap = sum(float(m.get("quote_cap") or 0.0) for m in target)
        self.j.write("rebalance", ts=now, target=sorted(want), added=added, resized=resized, retired=retired, back=back,
                     quote_cap=cap, summary=summary, markets={c: s.m for c, s in self.st.items()},
                     exiting={c: s.exit_since for c, s in self.st.items() if s.exit_since is not None})
        self.log(f"rebalance: {len(want)} markets, quote capital ${cap:.0f}, predicted M1 "
                 f"${sum(float(m.get('m1_usd_day') or 0.0) for m in target):.2f}/day; +{len(added)} new, "
                 f"{len(resized)} resized, {len(retired)} exiting")

    def _exit_quotes(self, s, ob, oa):
        """Reduce-only quotes of an exiting market: join the best price on the side that shrinks |YES - NO|, for at
        most the position (no flip); nothing below dust_shares."""
        pos = s.pos()
        none = ({"bid": None, "ask": None}, {"bid": 0.0, "ask": 0.0})
        if abs(pos) < self.cfg.dust_shares:
            return none
        q = round(min(s.m["N"], abs(pos)), 2)
        if pos > 0:
            return ({"bid": None, "ask": self._reduce_price("ask", ob, oa, s.m.get("tick") or 0.01)}, {"bid": 0.0, "ask": q}) if oa else none
        return ({"bid": self._reduce_price("bid", ob, oa, s.m.get("tick") or 0.01), "ask": None}, {"bid": q, "ask": 0.0}) if ob else none

    def _reduce_price(self, side, ob, oa, tick):
        """Price of a quote that reduces the position: exit_improve_ticks inside the spread, never at or through the
        other side; the best price when the spread is one tick (or there is no other side)."""
        n = self.cfg.exit_improve_ticks
        if side == "ask":
            best = oa[0][0]
            if n <= 0 or not ob:
                return best
            return round(max(best - n * tick, ob[0][0] + tick), 6) if best - ob[0][0] > tick + 1e-9 else best
        best = ob[0][0]
        if n <= 0 or not oa:
            return best
        return round(min(best + n * tick, oa[0][0] - tick), 6) if oa[0][0] - best > tick + 1e-9 else best

    async def _flatten(self, s, now, ob, oa, reason="", qty=None):
        """Close the position of an exiting market with a taker order: BUY NO against the YES bids when long YES,
        BUY YES against the YES asks when long NO; the pairs are merged afterwards."""
        for side in ("bid", "ask"):
            cur = s.orders[side]
            if cur:
                res = await self.ex.cancel(s.m, cur[0])
                s.orders = {"bid": None, "ask": None} if res == "market" else dict(s.orders, **{side: None})
                self.j.write("cancel", mkt=s.m["cond"], side=side, price=cur[1], scope=res)
        pos = s.pos()
        side = "ask" if pos > 0 else "bid"
        want = round(min(abs(pos), qty) if qty else abs(pos), 2)
        r = await self.ex.take(s.m, side, want, ob, oa)
        if not r or r.get("size", 0.0) <= 1e-9:
            s.flatten_retry_at = now + self.cfg.flatten_retry_s
            self.errors += 1
            self.j.write("error", error=f"taker close did not fill ({reason}): {s.m.get('q', s.m['cond'])[:60]} "
                                        f"{side} {want:.2f}; next try in {self.cfg.flatten_retry_s:.0f} s")
            return
        if side == "bid":
            s.yes += r["size"]; s.cash -= r["size"] * r["price"]
        else:
            s.no += r["size"]; s.cash -= r["size"] * (1 - r["price"])
        s.lot_update(now, r["size"] if side == "bid" else -r["size"])
        s.cash -= r.get("fee", 0.0)
        mid = s.mid if s.mid is not None else r["price"]
        # cost against the mid: buying YES above it ("bid") or selling YES (buying NO) below it ("ask"), plus the fee
        loss = r["size"] * ((r["price"] - mid) if side == "bid" else (mid - r["price"])) + r.get("fee", 0.0)
        self.j.write("flatten", mkt=s.m["cond"], side=side, price=r["price"], size=r["size"], fee=r.get("fee", 0.0),
                     mid=s.mid, cost=round(-loss, 6), hours_left=round(s.hours_left(now), 3),
                     since_exit_h=round((now - s.exit_since) / 3600, 3) if s.exit_since else None, reason=reason,
                     held_h=round((now - s.pos_since) / 3600, 3) if s.pos_since else None)
        if abs(s.pos()) < self.cfg.dust_shares:
            s.pos_since = None

    def _drop_finished(self):
        """Exiting markets with no position (below dust), no orders, no unmerged pairs and no pending markout
        leave the test; their mark-to-market moves to closed_pnl."""
        for cond, s in list(self.st.items()):
            if s.exit_since is None or abs(s.pos()) >= self.cfg.dust_shares or s.orders["bid"] or s.orders["ask"]:
                continue
            if min(s.yes, s.no) >= self.cfg.dust_shares or any(p[2] == cond for p in self.pending):
                continue
            pnl = s.mtm()
            self.closed_pnl += pnl
            del self.st[cond]
            self.j.write("retired", mkt=cond, pnl=pnl, yes=s.yes, no=s.no)

    async def snapshot_tick(self, now):
        """Phase 2, one pass of the snapshot task: a snapshot when one is due; then, if rebalance_h has passed since
        the last rebalance (or there was none) and the store is warm, a new target for step() to apply."""
        if self.store.due(now):
            sm = await asyncio.to_thread(self.store.take, now)
            self.j.write("snapshot", **sm)
            now = self.clock()
        if self.pending_target is None and self.store.warm(now) and (
                self.last_rebalance is None or now - self.last_rebalance >= self.cfg.rebalance_h * 3600 - 60):
            target, summary = await asyncio.to_thread(self.store.rank, now)
            self.pending_target = (target, summary, now)

    async def _snapshots(self):
        while True:
            try:
                await self.snapshot_tick(self.clock())
            except asyncio.CancelledError:
                raise
            except Exception as e:                       # the public API failed even after retries
                self.errors += 1
                self.j.write("error", error=("snapshot: " + repr(e))[:300])
                await asyncio.sleep(300)
                continue
            await asyncio.sleep(5)

    # ---- the loop

    async def step(self, now, cfgs):
        w = int(now // self.cfg.window_s)               # close the window before anything of this step is counted
        if self.win is None:
            self.win = w
        elif w != self.win:
            self._close_window(now)
            self.win = w
        if self.pending_target is not None:
            target, summary, t = self.pending_target
            self.pending_target = None
            self.apply_rebalance(target, summary, t)
        p2 = self.cfg.phase >= 2
        bk = await asyncio.to_thread(self.books_fn, [s.m["yes"] for s in self.st.values()])
        reserve = self.cfg.capital_usd - self.cfg.quote_budget_usd
        over = sum(s.exposure() for s in self.st.values()) >= reserve
        sample = now - self.last_sample >= self.cfg.sample_s
        for cond, s in list(self.st.items()):
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
                return {round(o[1], 4): o[3]}
            ob, oa = without_mine(bids, mine("bid")), without_mine(asks, mine("ask"))
            s.book = (ob, oa)
            if ob and oa:
                s.mid = (ob[0][0] + oa[0][0]) / 2
                s.mids = (s.mids + [(now, s.mid)])[-400:]
            if cond in cfgs:
                c = cfgs[cond] or {"v": s.m["v"], "min": s.m["min"], "rate": 0.0}
            else:                                       # phase 2: a market added since the last reward refresh
                c = {"v": s.m["v"], "min": s.m["min"], "rate": float(s.m.get("rate") or 0.0) if p2 else 0.0}
            cap = self.cfg.pos_limit_mult * s.m["N"]
            if s.mid is not None and self.cfg.pos_limit_usd < 1e8:
                cap = min(cap, self.cfg.pos_limit_usd / max(s.mid, 1 - s.mid))
            if over:
                cap = min(cap, abs(s.pos()))           # capital guard: no side may grow the position
            exiting = aged = False
            if p2:
                why = ("reward removed" if c["rate"] <= 0 else
                       "end near" if self.cfg.exit_at_end and s.hours_left(now) < self.cfg.exit_reduce_h else
                       "mid out of range" if s.mid is not None and not self.cfg.exit_mid_lo <= s.mid <= self.cfg.exit_mid_hi else None)
                if s.exit_since is None and why:
                    s.exit_since = now
                    self.j.write("exit", mkt=cond, reason=why, hours_left=round(s.hours_left(now), 3), mid=s.mid)
                exiting = s.exit_since is not None
                if abs(s.pos()) < self.cfg.dust_shares:
                    s.pos_since = None
                    if self.cfg.inv_fifo:
                        s.lots = []
                elif self.cfg.inv_fifo:
                    s.pos_since = s.lots[0][0] if s.lots else now
                elif s.pos_since is None:
                    s.pos_since = now
                aged = (self.cfg.inv_reduce_h > 0 and s.pos_since is not None
                        and now - s.pos_since >= self.cfg.inv_reduce_h * 3600)
            if exiting or aged:
                want, size = self._exit_quotes(s, ob, oa)
            else:
                want = dict(zip(("bid", "ask"), targets(ob, oa, tick, c["v"], s.pos(), cap)))
                size = {"bid": s.m["N"], "ask": s.m["N"]}
                if p2 and self.cfg.reduce_improve_always and abs(s.pos()) >= self.cfg.dust_shares:
                    red = "ask" if s.pos() > 0 else "bid"
                    if (oa if red == "ask" else ob):       # quoted even outside the zone, at full size N
                        want[red] = self._reduce_price(red, ob, oa, tick)
            if self.stopped or self.paused or (c["rate"] <= 0 and not exiting):
                want = {"bid": None, "ask": None}
            for side in ("bid", "ask"):
                cur = s.orders[side]
                if cur and (want[side] is None or abs(cur[1] - want[side]) > 1e-9 or abs(cur[3] - size[side]) > 1e-9):
                    res = await self.ex.cancel(s.m, cur[0])
                    if res == "market":
                        s.orders = {"bid": None, "ask": None}
                    else:
                        s.orders[side] = None
                    self.j.write("cancel", mkt=cond, side=side, price=cur[1], scope=res)
                    self._a(cond)["cancels"] += 1
            for side in ("bid", "ask"):
                if s.orders[side] is None and want[side] is not None and size[side] > 0:
                    oid = await self.ex.place(s.m, side, want[side], size[side])
                    if oid:
                        s.orders[side] = (oid, want[side], now, size[side])
                        self.j.write("place", mkt=cond, side=side, price=want[side], size=size[side])
                        self._a(cond)["places"] += 1
            if p2 and not (self.stopped or self.paused) and abs(s.pos()) >= self.cfg.dust_shares:
                due = None
                if exiting and now - s.exit_since >= self.cfg.exit_window_h * 3600:
                    due = "exit window"
                elif exiting and self.cfg.exit_at_end and s.hours_left(now) < self.cfg.exit_flatten_h:
                    due = "end near"
                elif self.cfg.inv_flatten_h > 0 and s.pos_since is not None and now - s.pos_since >= self.cfg.inv_flatten_h * 3600:
                    due = "position held 1 h" if abs(self.cfg.inv_flatten_h - 1.0) < 1e-9 else "position held"
                qty = None
                if due and due.startswith("position held") and self.cfg.inv_fifo:
                    qty = s.aged_qty(now, self.cfg.inv_flatten_h * 3600)   # only the lots older than the limit
                    if qty < self.cfg.dust_shares:
                        due = None
                if due and now < s.flatten_retry_at:
                    due = None
                if due:
                    await self._flatten(s, now, ob, oa, due, qty)
            if sample:
                my_b = s.orders["bid"][1] if s.orders["bid"] else None
                my_a = s.orders["ask"][1] if s.orders["ask"] else None
                sz = s.m["N"] if not (exiting or aged) else max((o[3] for o in s.orders.values() if o), default=0.0)
                frac = m1_fraction(ob, oa, my_b, my_a, sz, c["v"], c["min"])
                # competitors seen in the book: price levels and resting size of other orders inside the zone
                lv = [(p, z) for p, z in ob if s.mid is not None and 0 <= (s.mid - p) * 100 < c["v"] and z + 1e-9 >= c["min"]]
                lv += [(p, z) for p, z in oa if s.mid is not None and 0 <= (p - s.mid) * 100 < c["v"] and z + 1e-9 >= c["min"]]
                self.j.write("m1", ts=now, mkt=cond, frac=frac, usd=c["rate"] * frac * self.cfg.sample_s / 86400,
                             bid=my_b, ask=my_a, mid=s.mid, comp_levels=len(lv), comp_size=sum(z for _, z in lv))
                a = self._a(cond)
                a["m1"].append(c["rate"] * frac); a["frac"].append(frac)
                a["comp_levels"].append(len(lv)); a["comp_size"].append(sum(z for _, z in lv))
        if sample and p2:
            self.win_cap.append(sum(float(self.st[c].m.get("quote_cap") or 0.0) for c in self.target if c in self.st))
        for f in await self.ex.fills([s.m for s in self.st.values()]):
            if f["mkt"] in self.st:
                self.apply_fill(f)
        self.due_markouts(now)
        for cond, s in self.st.items():
            pairs = min(s.yes, s.no)
            need = min(s.m["N"], self.cfg.dust_shares) if p2 and s.exit_since is not None else s.m["N"]
            if pairs >= need:
                done = await self.ex.merge(s.m, pairs)
                if done:
                    s.yes -= done; s.no -= done; s.cash += done
                    self.j.write("merge", mkt=cond, pairs=done)
        if p2:
            self._drop_finished()
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
                st = {"ts": now, "pnl": pnl, "errors": self.errors, "stopped": self.stopped,
                      "orders": sum(1 for x in self.st.values() for o in x.orders.values() if o),
                      "m1_usd_day_now": sum(a["m1"][-1] for a in self.acc.values() if a["m1"])}
                if p2:
                    st.update(target=len(self.target), exiting=sum(1 for x in self.st.values() if x.exit_since is not None),
                              quote_cap=self.win_cap[-1] if self.win_cap else 0.0, last_rebalance=self.last_rebalance,
                              next_rebalance=(self.last_rebalance + self.cfg.rebalance_h * 3600) if self.last_rebalance else None,
                              exposure=sum(x.exposure() for x in self.st.values()))
                with open(self.status_path + ".tmp", "w") as f:
                    json.dump(st, f)
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
        if hasattr(self.ex, "collateral") and now - self.last_equity >= 600:
            self.last_equity = now                      # the real result of a live run: rewards paid, fees and all
            try:
                cash = await self.ex.collateral()
                pos = sum(x.yes * x.mid + x.no * (1 - x.mid) for x in self.st.values() if x.mid is not None)
                self.j.write("equity", collateral=round(cash, 6), positions_usd=round(pos, 6), equity=round(cash + pos, 6))
            except Exception as e:
                self.log(f"equity read failed: {e!r}")
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if self.day and day != self.day:
            e = await self.ex.earnings(self.day)
            if e is not None:                           # every market of the day, also those that left the test
                self.j.write("earnings", day=self.day, by_market={c: float(v) for c, v in e.items()})
        self.day = day

    async def run(self):
        t_end = self.clock() + self.cfg.test_days * 86400
        cfgs, cfg_t = {}, -1e18
        self.j.write("start", markets=[s.m["cond"] for s in self.st.values()], config=dataclasses.asdict(self.cfg),
                     quote_cap=sum(float(s.m.get("quote_cap") or 0.0) for s in self.st.values()))
        await self.reconcile()
        snaps = asyncio.create_task(self._snapshots()) if self.store is not None else None
        try:
            while self.clock() < t_end and not self.stopped:
                now = self.clock()
                if self.control_fn is not None:
                    paused = self.control_fn() == "stop"
                    if paused != self.paused:
                        self.paused = paused
                        self.j.write("pause" if paused else "resume")
                try:
                    if now - cfg_t > 3600 or not set(self.st) <= set(cfgs):
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
        except asyncio.CancelledError:
            self.end_reason = "cancelled"
            raise
        finally:
            if snaps is not None:
                snaps.cancel()
                await asyncio.gather(snaps, return_exceptions=True)
            await self.ex.cancel_all()
            if self.win is not None:
                self._close_window(self.clock())
            self.end_reason = self.end_reason or self.stopped or "time"
            self.j.write("end", reason=self.end_reason, pnl=self.pnl())


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


def phase1(events, cv_max=0.5, share_max=0.4, exclude=(), m1_per_100_min=None, quote_cap_usd=None):
    """Phase 1 gate (research/PREREG_LP_PHASE1.md, amended 2026-11-16): stability of the portfolio M1 over
    the 30-minute windows of hours 24-48, and no market above share_max of M1 over the last 24 hours.
    Markouts are reported as data, not as a criterion. `exclude`: condition ids left out of everything.
    m1_per_100_min (research/PREREG_LP_PHASE1B.md): H10 closes if the portfolio M1 of hours 24-48 per $100 of
    quote capital is below it. Quote capital: quote_cap_usd, else the latest start event's quote_cap."""
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
    ticks = sorted({round(e["ts"]) for e in events if e["event"] in ("m1", "pnl")})
    downtime = sum(b - a for a, b in zip(ticks, ticks[1:]) if b - a > 300)
    out = {"hours": round(hours, 1), "windows_2nd_half": len(half), "excluded": sorted(ex),
           "downtime_h": round(downtime / 3600, 2), "process_starts": len(starts),
           "portfolio_m1_usd_day_2nd_half": mean, "cv": cv,
           "max_share_last24": max((v["share_last24"] or 0.0 for v in per.values()), default=None),
           "fills": sum(v["fills"] for v in per.values()), "contracts": sum(v["contracts"] for v in per.values()),
           "contracts_per_market_day": sum(v["contracts"] for v in per.values()) / (len(conds) * hours / 24) if conds and hours else None,
           "markout60": {"n_fills": len(mk_all), "mean_c": sum(v * w for v, w in mk_all) / q if q else None,
                         "p5_c": _wq(mk_all, 0.05) if mk_all else None, "p25_c": _wq(mk_all, 0.25) if mk_all else None,
                         "p50_c": _wq(mk_all, 0.5) if mk_all else None,
                         "share_worse_than_-2.1c": sum(w for v, w in mk_all if v < -2.1) / q if q else None},
           "expected_week_usd": 7 * mean, "markets": per}
    if quote_cap_usd is None:
        quote_cap_usd = next((e.get("quote_cap") for e in sorted((e for e in events if e["event"] == "start"),
                                                                  key=lambda e: -e["ts"]) if e.get("quote_cap")), None)
    out["quote_cap_usd"] = quote_cap_usd
    out["m1_per_100usd_2nd_half"] = mean * 100 / quote_cap_usd if quote_cap_usd and port else None
    full = hours >= 48 - 0.6
    checks = {
        f"M1 CV <= {cv_max:.0%} (hours 24-48)": full and cv is not None and cv <= cv_max,
        f"no market > {share_max:.0%} of M1 (last 24 h)": out["max_share_last24"] is not None and out["max_share_last24"] <= share_max,
    }
    out["h10_closed"] = None
    if m1_per_100_min is not None:
        keep = full and out["m1_per_100usd_2nd_half"] is not None and out["m1_per_100usd_2nd_half"] >= m1_per_100_min
        checks[f"M1 >= ${m1_per_100_min:g}/day per $100 of quotes (hours 24-48)"] = keep
        out["h10_closed"] = bool(full and not keep)
    out["checks"] = checks
    out["verdict"] = "PASS" if all(checks.values()) else "FAIL: " + ", ".join(k for k, v in checks.items() if not v)
    if out["h10_closed"]:
        out["verdict"] += " -> H10 closes (PREREG_LP_PHASE1B)"
    return out


def phase2(events, net_per100_min=20.0, cv_max=0.5, share_max=0.4, ratio_min=0.6, hours=48.0):
    """Phase 2 paper report (research/PREREG_PHASE2.md) over the first `hours` hours of quoting (from the first
    start event). Per $100 of quote capital (the target portfolio's capital, averaged over the 30-minute windows):
      M1 (reward model of the quotes that rested), maker rebates, mark-to-market change (fills, markouts, closing
      orders and their fees, valued at mid), and their sum, the net. Plus CV of the window M1, the largest market
      share of M1 over the last 24 hours, fact / prediction of rank(), fills, markouts, exits, turnover."""
    starts = [e["ts"] for e in events if e["event"] == "start"]
    if not starts:
        return {"verdict": "no data"}
    t0 = min(starts)
    t1 = t0 + hours * 3600
    wins = sorted((e for e in events if e["event"] == "window" and t0 <= e["start"] < t1), key=lambda e: e["start"])
    if not wins:
        return {"verdict": "no data", "t0": t0}
    t_last = wins[-1]["end"]
    port = [w["m1_usd_day"] for w in wins]
    m1 = sum(port) / len(port)
    caps = [w["cap_target"] for w in wins if w.get("cap_target")]
    cap = sum(caps) / len(caps) if caps else None
    cv = (sum((x - m1) ** 2 for x in port) / len(port)) ** 0.5 / m1 if m1 > 0 else None
    days = (t_last - wins[0]["start"]) / 86400
    per100 = (lambda x: x * 100 / cap / days) if cap and days > 0 else (lambda x: None)
    inside = [e for e in events if t0 <= e["ts"] <= t_last]
    rebates = sum(m.get("rebate", 0.0) or 0.0 for w in wins for m in w["markets"].values())
    pnls = [e["pnl"] for e in inside if e["event"] in ("pnl", "end")]
    mtm = (pnls[-1] - pnls[0]) if len(pnls) > 1 else 0.0
    flat = [e for e in inside if e["event"] == "flatten"]
    reb = [e for e in inside if e["event"] == "rebalance"]
    # prediction of rank() for the time each target was in force, against the window M1 of the same time
    pred = []
    for w in wins:
        r = next((e for e in reversed(reb) if e["ts"] <= w["start"] + 1), None)
        if r is not None:
            pred.append(float((r.get("summary") or {}).get("m1_pred") or 0.0))
    pred_m1 = sum(pred) / len(pred) if pred else None
    last24 = [w for w in wins if w["start"] >= t_last - 86400]
    tot24 = sum(w["m1_usd_day"] for w in last24)
    share = {}
    for w in last24:
        for c, m in w["markets"].items():
            share[c] = share.get(c, 0.0) + (m["m1_usd_day"] or 0.0)
    mk = [x for w in wins for m in w["markets"].values() for x in m.get("markout60", [])]
    q = sum(z for _, z in mk)
    mk600 = [e for e in inside if e["event"] == "markout" and e["horizon"] == 600]
    contracts = sum(m["contracts"] for w in wins for m in w["markets"].values())
    market_hours = sum(1 for w in wins for m in w["markets"].values() if m.get("samples")) * 0.5
    turnover = [len(e.get("added") or []) / max(1, len(e.get("target") or [])) for e in reb[1:]]
    ticks = sorted({round(e["ts"]) for e in inside if e["event"] in ("m1", "pnl")})
    out = {"hours": round((t_last - t0) / 3600, 1), "windows": len(wins), "quote_cap_usd": cap,
           "m1_usd_day": m1, "m1_per100": m1 * 100 / cap if cap else None,
           "rebates_usd": rebates, "rebates_per100": per100(rebates),
           "mtm_usd": mtm, "mtm_per100": per100(mtm),
           "net_usd_day": m1 + (rebates + mtm) / days if days > 0 else None,
           "net_per100": (m1 * 100 / cap + per100(rebates) + per100(mtm)) if cap and days > 0 else None,
           "cv": cv, "max_share_last24": max(share.values()) / tot24 if share and tot24 > 0 else None,
           "pred_m1_usd_day": pred_m1, "fact_over_pred": m1 / pred_m1 if pred_m1 else None,
           "fills": sum(m["fills"] for w in wins for m in w["markets"].values()), "contracts": contracts,
           "contracts_per_market_day": contracts / (market_hours / 24) if market_hours else None,
           "markout60_c": sum(v * z for v, z in mk) / q if q else None, "markout60_p5_c": _wq(mk, 0.05) if mk else None,
           "markout600_c": (100 * sum(e["markout"] * e["size"] for e in mk600) / sum(e["size"] for e in mk600)) if mk600 else None,
           "rebalances": len(reb), "markets_per_rebalance": sum(len(e.get("target") or []) for e in reb) / len(reb) if reb else None,
           "turnover": sum(turnover) / len(turnover) if turnover else None,
           "exits": sum(1 for e in inside if e["event"] == "exit") + sum(len(e.get("retired") or []) for e in reb),
           "flattens": len(flat), "flatten_contracts": sum(e["size"] for e in flat),
           "flatten_cost_usd": sum(e.get("cost", 0.0) for e in flat),
           "retired": sum(1 for e in inside if e["event"] == "retired"),
           "downtime_h": round(sum(b - a for a, b in zip(ticks, ticks[1:]) if b - a > 300) / 3600, 2),
           "process_starts": len(starts), "errors": sum(1 for e in inside if e["event"] == "error")}
    # live: rewards actually paid (the latest fetch of each day) and the wallet equity (USDC + positions at mid)
    earned = {}
    for e in events:
        if e["event"] == "earnings":
            for c, v in (e.get("by_market") or {}).items():
                earned[(e["day"], c)] = v
    if earned:
        out["reward_actual_usd"] = sum(earned.values())
        out["reward_actual_days"] = sorted({d for d, _ in earned})
        out["reward_actual_over_m1"] = out["reward_actual_usd"] / (m1 * days) if m1 > 0 and days > 0 else None
    eq = [e for e in inside if e["event"] == "equity"]
    if len(eq) > 1:
        out["equity_start"], out["equity_end"] = eq[0]["equity"], eq[-1]["equity"]
        out["equity_change_usd"] = eq[-1]["equity"] - eq[0]["equity"]
        out["equity_per100"] = per100(out["equity_change_usd"])
    full = out["hours"] >= hours - 0.6
    checks = {
        f"net >= ${net_per100_min:g}/day per $100 of quotes": full and out["net_per100"] is not None and out["net_per100"] >= net_per100_min,
        f"M1 CV <= {cv_max:.0%}": full and cv is not None and cv <= cv_max,
        f"no market > {share_max:.0%} of M1 (last 24 h)": out["max_share_last24"] is not None and out["max_share_last24"] <= share_max,
        f"M1 fact / prediction >= {ratio_min:g}": out["fact_over_pred"] is not None and out["fact_over_pred"] >= ratio_min,
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


async def live_check(cfg, out=print):
    """Before a live run (research/PREREG_PHASE3.md, step 0): the keys, the wallet, the exchange allowance and the
    order path, without spending money. One post-only BUY of the exchange minimum size rests 10 ticks below the
    best bid of a liquid rewarded market for a few seconds and is cancelled; it fills only if that market falls
    10 ticks within seconds (then it is a normal small position, closed by the bot's rules or by hand).
    Returns 0 when every step passed, 1 otherwise."""
    ok = True

    def bad(msg):
        nonlocal ok
        ok = False
        out("   !! " + msg)
    ex = await LiveExec.create(os.environ.get("POLYMARKET_PRIVATE_KEY", ""), os.environ.get("FUNDER_ADDRESS"),
                               os.environ.get("POLYMARKET_RELAYER_KEY"), os.environ.get("POLYMARKET_RELAYER_ADDRESS"),
                               log=out)
    cond = None
    try:
        b = await ex.c.get_balance_allowance(asset_type="COLLATERAL")
        cash = b.balance / 1e6
        out(f"1. wallet USDC ${cash:.2f} (planned capital ${cfg.capital_usd:.0f}); allowances {b.allowances}")
        if cash < 0.9 * cfg.capital_usd:
            bad(f"below 90% of the planned capital: the bot will refuse to start a live run")
        if not any(int(v) > 0 for v in (b.allowances or {}).values()):
            bad("no USDC allowance for the exchange: trade once on polymarket.com with this wallet, then check again")
        rw = await asyncio.to_thread(rewards_config)
        top = sorted((c for c, r in rw.items() if r["rate"] > 0 and r["min"] <= cfg.max_min_size),
                     key=lambda c: -rw[c]["rate"])[:40]
        gm = await asyncio.to_thread(gamma_markets, top)
        pick = None
        for c in top:
            g = gm.get(c)
            if not g or not g.get("active") or g.get("closed") or not g.get("acceptingOrders") or g.get("gameStartTime"):
                continue
            m = meta_of(g)
            bk = (await asyncio.to_thread(books, [m["token"]])).get(m["token"])
            if bk and bk[0] and bk[1] and 0.2 <= bk[0][0][0] <= 0.8:
                pick = (c, m, g, bk)
                break
        if pick is None:
            bad("no liquid rewarded market found for the order test")
            return 1
        cond, m, g, (bids, asks, tick) = pick
        size = max(5.0, float(g.get("orderMinSize") or 5.0))
        price = round(max(tick, bids[0][0] - 10 * tick), 4)
        out(f"2. order test on \"{(g.get('question') or cond)[:60]}\": best bid {bids[0][0]}, our post-only BUY YES "
            f"{size:g} at {price} (${size * price:.2f} would be spent only if it filled)")
        oid = await ex.place({"cond": cond, "yes": m["token"], "no": m["no"]}, "bid", price, size)
        if not oid:
            bad("the order was not accepted (see the reason above)")
        else:
            out(f"   accepted: {oid}")
            await asyncio.sleep(3)
            sc = await ex.scoring([oid])
            out(f"   reward scoring of the order: {sc}")
            res = await ex.cancel({"cond": cond}, oid)
            out(f"   cancelled ({res})")
        n = 0
        async for _ in ex.c.list_account_trades(after=str(int(time.time()) - 7 * 86400)).iter_items():
            n += 1
            if n >= 50:
                break
        out(f"3. account trade list readable: {n} trades in the last 7 days")
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        e = await ex.earnings(day)
        out(f"4. reward earnings readable: {day}: ${sum((e or {}).values()):.2f}" if e is not None else "4. earnings: failed")
        if e is None:
            bad("reward earnings could not be read")
        pct = await ex.reward_pct()
        out(f"5. reward percentages readable: {'yes' if pct is not None else 'no'}")
        if not (os.environ.get("POLYMARKET_RELAYER_KEY") and os.environ.get("POLYMARKET_RELAYER_ADDRESS")):
            out("   note: no relayer key: merging YES + NO pairs back into USDC may need gas (POL) in the wallet "
                "or may fail; unmerged pairs only lock capital, they do not lose money")
    except Exception as e:
        bad(f"failed: {e!r:.300}")
    finally:
        if cond:
            await ex._cancel_market(cond)
        await ex.c.close()
    out("RESULT: OK, the live path works" if ok else "RESULT: fix the problems marked !! before a live run")
    return 0 if ok else 1


def cli(cmd, args):
    from .config import load_dotenv
    load_dotenv()
    cfg = from_env()
    if cmd == "lp-live-check":
        sys.exit(asyncio.run(live_check(cfg)))
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
            ms = json.load(f)["markets"]
        qs = {m["cond"]: m["q"] for m in ms}
        ex = {c for c, q in qs.items() for t in texts if t.lower() in (q or "").lower()}
        qcap = sum(float(m.get("quote_cap") or 0.0) for m in ms if m["cond"] not in ex) or None
        r = phase1(load_events(cfg.lp_dir, mode), exclude=ex, m1_per_100_min=cfg.m1_per_100_min, quote_cap_usd=qcap)
        print("excluded:", [qs[c][:60] for c in ex])
        for k, v in r.items():
            if k != "markets":
                print(f"{k}: {v}")
        for c, v in (r.get("markets") or {}).items():
            print(" ", (qs.get(c) or c)[:50], {k: (round(x, 3) if isinstance(x, float) else x) for k, x in v.items()})
        return
    if cmd == "lp-phase2":
        # lp-phase2 [paper|live]: phase 2 report (research/PREREG_PHASE2.md) from LP_DIR/<mode>.jsonl
        mode = args[0] if args else "paper"
        r = phase2(load_events(cfg.lp_dir, mode))
        for k, v in r.items():
            print(f"{k}: {round(v, 4) if isinstance(v, float) else v}")
        return
    if cmd == "lp-earnings":
        # fetch the rewards actually paid for every complete UTC day of the live run (every market of the wallet,
        # the rolling portfolio included) and append them to the journal; a later fetch of a day replaces it
        lp_dir = os.path.join(os.environ["POLYBOT_DATA"], "live") if os.environ.get("POLYBOT_DATA") else cfg.lp_dir
        ev = load_events(lp_dir, "live")
        t0 = min((e["ts"] for e in ev if e["event"] == "start"), default=None)
        if t0 is None:
            raise SystemExit(f"no live journal in {lp_dir}")

        async def go():
            ex = await _live_exec()
            j = Journal(os.path.join(lp_dir, "live.jsonl"))
            day = time.strftime("%Y-%m-%d", time.gmtime(t0))
            last = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
            tot = 0.0
            while day <= last:                              # complete UTC days only
                e = await ex.earnings(day)
                if e is not None:
                    j.write("earnings", day=day, by_market={c: float(v) for c, v in e.items()})
                    tot += sum(float(v) for v in e.values())
                    print(day, f"${sum(float(v) for v in e.values()):.2f}")
                day = time.strftime("%Y-%m-%d", time.gmtime(time.mktime(time.strptime(day, "%Y-%m-%d")) - time.timezone + 86400))
            print(f"total ${tot:.2f}")
            j.close()
            await ex.c.close()
        asyncio.run(go())
        return
    sel = json.load(open(os.path.join(cfg.lp_dir, "markets.json")))["markets"]
    if len(sel) < cfg.markets_min:
        raise SystemExit(f"only {len(sel)} markets selected; need {cfg.markets_min}")
    if cmd == "lp-paper":
        prior = load_events(cfg.lp_dir, "paper") if os.environ.get("LP_RESUME") == "1" else []
        t = LPTest(cfg, PaperExec(every_s=cfg.fills_every_s, lifetime=cfg.paper_lifetime), Journal(os.path.join(cfg.lp_dir, "paper.jsonl")), sel)
        t.restore(prior)                                  # LP_RESUME=1: continue the same run after a restart
        asyncio.run(t.run())
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
