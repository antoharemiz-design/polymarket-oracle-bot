"""Long-running bot service for a server (systemd unit `polybot`):  python -m polybot service

Runs the liquidity-rewards test of polybot/lp.py, paper mode by default. All state lives in POLYBOT_DATA:
  <mode>/run.json          start and length of the current run; a restart resumes it for the time left
  <mode>/markets.json      the markets of the run
  <mode>/<mode>.jsonl      journal (polybot/lp.py)
  <mode>/status.json       per-minute loop status (polybot/lp.py)
  service_status.json      heartbeat of this service, read by the health check and by status.md
  control.json             {"mode": "run" | "stop"}, written by polybot-report from polybot-reports/control.md
  health_state.json        written by the health check
  logs/service.log         rotating log

Environment (/etc/polybot/env, see .env.example):
  POLYBOT_MODE             paper (default) | live
  POLYBOT_DATA             /var/lib/polybot
  POLYBOT_MARKETS          optional fixed markets file (phase 1: deploy/phase1_markets.json)
  POLYBOT_BRANCH           branch the server deploys; live runs only from main
  POLYBOT_ALLOW_LIVE       must be "yes" for live (set by `polybot mode live`)
  POLYMARKET_PRIVATE_KEY, FUNDER_ADDRESS, POLYMARKET_RELAYER_KEY, POLYMARKET_RELAYER_ADDRESS   live only
  LP_*                     test parameters (polybot/lp.py, LPConfig)

Live is refused (the service idles and says why in status.md) unless every condition holds:
POLYBOT_ALLOW_LIVE=yes, a private key, branch main, and Polymarket's geoblock check says not blocked.
While live, the geoblock check repeats every 10 minutes; if it turns blocked, quotes are pulled.
"""
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import threading
import time
import urllib.request

from . import lp

GEOBLOCK = "https://polymarket.com/api/geoblock"


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def geoblock(timeout=15):
    """Polymarket's own check for the server's IP: {"blocked": bool, "country": ..., ...} or None."""
    try:
        req = urllib.request.Request(GEOBLOCK, headers={"User-Agent": "polybot-service/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def release():
    """Commit of the running release: /opt/polybot/releases/<sha>/polybot/service.py -> <sha>."""
    return os.path.basename(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


class Service:
    def __init__(self, data=None, mode=None, clock=time.time, books_fn=None, rewards_fn=None, trades_fn=None,
                 geoblock_fn=geoblock, gamma_fn=None):
        self.data = data or env("POLYBOT_DATA", "/var/lib/polybot")
        self.mode = (mode or env("POLYBOT_MODE", "paper")).lower()
        self.clock = clock
        self.run_dir = os.path.join(self.data, self.mode)
        os.makedirs(self.run_dir, exist_ok=True)
        os.makedirs(os.path.join(self.data, "logs"), exist_ok=True)
        self.books_fn, self.rewards_fn, self.trades_fn, self.gamma_fn = books_fn, rewards_fn, trades_fn, gamma_fn
        self.geoblock_fn = geoblock_fn
        self.log = self._logger()
        self.test = None
        self.phase, self.detail = "starting", ""
        self._sel_stop = threading.Event()
        self.geo = {"t": 0.0, "blocked": None, "country": None}

    def _logger(self):
        lg = logging.getLogger(f"polybot.service.{id(self)}")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
        fmt.converter = time.gmtime
        fh = logging.handlers.RotatingFileHandler(os.path.join(self.data, "logs", "service.log"),
                                                  maxBytes=5_000_000, backupCount=5)
        fh.setFormatter(fmt)
        lg.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        lg.addHandler(sh)
        return lg

    # ---- state files

    def run_path(self):
        return os.path.join(self.run_dir, "run.json")

    def load_run(self):
        return _read_json(self.run_path())

    def save_run(self, run):
        _write_json(self.run_path(), run)

    def control(self):
        c = _read_json(os.path.join(self.data, "control.json"), {}) or {}
        return "stop" if str(c.get("mode", "run")).lower() == "stop" else "run"

    def write_status(self):
        st = {"ts": self.clock(), "mode": self.mode, "phase": self.phase, "detail": self.detail,
              "release": release(), "branch": env("POLYBOT_BRANCH", "main"), "pid": os.getpid()}
        run = self.load_run()
        if run:
            st.update(run_started=run["started"], run_ends=run["started"] + run["test_days"] * 86400,
                      run_completed=run.get("completed"))
        if self.test is not None:
            st.update(pnl=self.test.pnl(), paused=self.test.paused, stopped=self.test.stopped, errors=self.test.errors,
                      orders=sum(1 for s in self.test.st.values() for o in s.orders.values() if o))
        if self.mode == "live":
            st.update(geoblocked=self.geo["blocked"], country=self.geo["country"])
        try:
            _write_json(os.path.join(self.data, "service_status.json"), st)
        except OSError as e:
            self.log.warning(f"status write failed: {e!r}")

    # ---- live safety

    def live_blockers(self):
        reasons = []
        if env("POLYBOT_ALLOW_LIVE") != "yes":
            reasons.append("POLYBOT_ALLOW_LIVE is not 'yes' (run: sudo polybot mode live)")
        if not env("POLYMARKET_PRIVATE_KEY"):
            reasons.append("no POLYMARKET_PRIVATE_KEY (run: sudo polybot keys)")
        if env("POLYBOT_BRANCH", "main") != "main":
            reasons.append(f"live runs only from the main branch, this server deploys {env('POLYBOT_BRANCH')}")
        g = self.geoblock_fn()
        if g is None:
            reasons.append("Polymarket geoblock check did not answer")
        elif g.get("blocked"):
            reasons.append(f"Polymarket blocks this server's IP (country {g.get('country')})")
        return reasons

    def control_for_test(self):
        if self.control() == "stop":
            self.detail = "paused by control.md"
            return "stop"
        if self.mode == "live":
            now = self.clock()
            if now - self.geo["t"] > 600:
                g = self.geoblock_fn()
                self.geo = {"t": now, "blocked": None if g is None else bool(g.get("blocked")),
                            "country": None if g is None else g.get("country")}
                if self.geo["blocked"]:
                    self.log.error(f"geoblock: this IP is now blocked ({self.geo['country']}); quotes pulled")
            if self.geo["blocked"]:
                self.detail = f"paused: geoblocked ({self.geo['country']})"
                return "stop"
        self.detail = ""
        return "run"

    # ---- main

    def select_markets(self, cfg):
        path = env("POLYBOT_MARKETS")
        if path and os.path.exists(path):
            with open(path) as f:
                ms = json.load(f)["markets"]
            self.log.info(f"markets from {path}: {len(ms)}")
            return ms

        return lp.select(cfg, log=self.log.info, sleep=lambda sec: self._sleep(sec))

    def _sleep(self, sec):
        """Sleep in a worker thread; wakes every few seconds so status stays fresh and a stop is seen at once."""
        end = time.time() + sec
        while time.time() < end:
            if self._sel_stop.is_set():
                raise lp.SelectionStopped()
            self.write_status()
            time.sleep(min(2.0, max(0.0, end - time.time())))
        if self._sel_stop.is_set():
            raise lp.SelectionStopped()

    def snap_store(self, cfg):
        return lp.SnapStore(cfg, log=self.log.info, clock=self.clock, rewards_fn=self.rewards_fn,
                            books_fn=self.books_fn, gamma_fn=self.gamma_fn)

    def warm_up(self, store):
        """Phase 2 warm-up (research/PREREG_PHASE2.md): a snapshot of every rewarded market's book every
        snapshot_gap_s until the store holds lookback_h of history; no quotes meanwhile. Runs in a thread; the
        snapshots are on disk, so a restart continues the warm-up. A stop raises lp.SelectionStopped."""
        cfg = store.cfg
        while True:
            now = self.clock()
            if store.warm(now):
                w = store.window(now)
                self.log.info(f"warm-up done: {len(w)} snapshots over {(w[-1]['ts'] - w[0]['ts']) / 3600:.1f} h")
                return
            if store.due(now):
                try:
                    sm = store.take(now)
                except lp.SelectionStopped:
                    raise
                except Exception as e:                  # the public API failed even after retries
                    self.log.warning(f"warm-up snapshot failed ({e!r:.200}); next try in 5 minutes")
                    self._sleep(min(300.0, cfg.snapshot_gap_s))
                    continue
                self.log.info(f"warm-up snapshot {len(store.window(now))}: {sm['n_quotable']} markets quotable, "
                              f"{sm['n_eligible']} eligible, {sm['n_rewarded']} rewarded")
                continue
            self._sleep(max(1.0, store.last_ts() + cfg.snapshot_gap_s - self.clock()))

    async def _status_loop(self):
        while True:
            self.write_status()
            await asyncio.sleep(30)

    async def _idle(self, stop, phase, detail=""):
        self.phase, self.detail = phase, detail
        self.log.info(f"{phase}: {detail}" if detail else phase)
        self.write_status()
        await stop.wait()

    async def run(self, stop=None):
        stop = stop or asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        status = asyncio.create_task(self._status_loop())
        try:
            await self._main(stop)
        finally:
            status.cancel()
            self.write_status()

    async def _in_thread(self, fn, stop, *args):
        """Run fn in a worker thread until it returns or `stop` is set (then it is told to stop and None is returned;
        what it wrote to disk is kept)."""
        self._sel_stop.clear()
        job = asyncio.create_task(asyncio.to_thread(fn, *args))
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({job, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if job not in done:
            self._sel_stop.set()
            await asyncio.gather(job, return_exceptions=True)
            return None, False
        waiter.cancel()
        return job.result(), True

    async def _main(self, stop):
        cfg = lp.from_env()
        cfg.lp_dir = self.run_dir
        self.log.info(f"service start: mode {self.mode}, release {release()}, branch {env('POLYBOT_BRANCH', 'main')}")
        if self.mode not in ("paper", "live"):
            return await self._idle(stop, "error", f"unknown POLYBOT_MODE {self.mode!r}")
        if self.mode == "live":
            why = self.live_blockers()
            if why:
                return await self._idle(stop, "refused", "; ".join(why))
        run, now = self.load_run(), self.clock()
        if run and (run.get("completed") or now >= run["started"] + run["test_days"] * 86400):
            if not run.get("completed"):
                run["completed"] = run["started"] + run["test_days"] * 86400
                self.save_run(run)
            return await self._idle(stop, "complete", "run finished; start a new one with: sudo polybot new-run")
        if run and int(run.get("phase", 1)) != cfg.phase:     # a run keeps the phase (and defaults) it started with
            cfg = lp.from_env(phase=int(run.get("phase", 1)))
            cfg.lp_dir = self.run_dir
        mpath = os.path.join(self.run_dir, "markets.json")
        resumed = bool(run)
        store = None
        if cfg.phase == 2:
            store = self.snap_store(cfg)
            markets = []
            if not run:
                self.phase = "warming"
                self.write_status()
                _, finished = await self._in_thread(self.warm_up, stop, store)
                if not finished:
                    self.log.info("stop requested during the warm-up; the snapshots taken so far are kept")
                    return
                now = self.clock()                      # the run's clock starts when quoting starts, after the warm-up
                run = {"started": now, "test_days": cfg.test_days, "mode": self.mode, "phase": 2}
                self.save_run(run)
                self.log.info(f"new phase 2 run for {cfg.test_days} days: rolling selection every {cfg.rebalance_h:g} h")
            else:
                self.log.info(f"resuming phase 2 run started {time.strftime('%Y-%m-%d %H:%M', time.gmtime(run['started']))} UTC")
        elif not run:
            self.phase = "selecting"
            self.write_status()
            markets, finished = await self._in_thread(self.select_markets, stop, cfg)
            if not finished:
                self.log.info("stop requested during selection; the snapshots taken so far are kept")
                return
            if len(markets) < cfg.markets_min:
                return await self._idle(stop, "error", f"only {len(markets)} markets pass the filters, need {cfg.markets_min}")
            now = self.clock()                          # the run's clock starts when quoting starts, after selection
            _write_json(mpath, {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "markets": markets})
            run = {"started": now, "test_days": cfg.test_days, "mode": self.mode, "phase": 1}
            self.save_run(run)
            self.log.info(f"new run: {len(markets)} markets for {cfg.test_days} days")
        else:
            markets = _read_json(mpath, {}).get("markets") or []
            self.log.info(f"resuming run started {time.strftime('%Y-%m-%d %H:%M', time.gmtime(run['started']))} UTC")
        cfg.test_days = (run["started"] + run["test_days"] * 86400 - now) / 86400
        if self.mode == "paper":
            ex = lp.PaperExec(**({"trades_fn": self.trades_fn} if self.trades_fn else {}), every_s=cfg.fills_every_s,
                              lifetime=cfg.paper_lifetime)
        else:
            ex = await lp.LiveExec.create(env("POLYMARKET_PRIVATE_KEY"), env("FUNDER_ADDRESS"),
                                          env("POLYMARKET_RELAYER_KEY"), env("POLYMARKET_RELAYER_ADDRESS"), log=self.log.info)
            ex.start_heartbeat()
        journal = lp.Journal(os.path.join(self.run_dir, f"{self.mode}.jsonl"))
        kw = {}
        if self.books_fn:
            kw["books_fn"] = self.books_fn
        if self.rewards_fn:
            kw["rewards_fn"] = self.rewards_fn
        prior = lp.load_events(self.run_dir, self.mode) if resumed else []
        self.test = lp.LPTest(cfg, ex, journal, markets, log=self.log.info, clock=self.clock,
                              control_fn=self.control_for_test, store=store, **kw)
        if prior:
            self.log.info(f"positions restored from the journal: {self.test.restore(prior)} fills")
        self.phase = "running"
        self.write_status()
        task = asyncio.create_task(self.test.run())
        waiter = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if task not in done:
                self.log.info("stop requested: cancelling quotes")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return
            waiter.cancel()
            task.result()
        finally:
            await ex.close()
            journal.close()
        if self.test.stopped:
            run["completed"] = self.clock(); run["reason"] = self.test.stopped
            self.save_run(run)
            return await self._idle(stop, "stopped", self.test.stopped)
        run["completed"] = self.clock(); run["reason"] = "time"
        self.save_run(run)
        await self._idle(stop, "complete", "run finished; start a new one with: sudo polybot new-run")


# --------------------------------------------------------------------------------------------------
# status.md

def _ago(sec):
    if sec is None:
        return "—"
    sec = max(0, int(sec))
    return f"{sec} с" if sec < 120 else f"{sec // 60} мин" if sec < 7200 else f"{sec / 3600:.1f} ч"


def _phase2_lines(events, now):
    """status.md sections of a phase 2 run: the report so far (lp.phase2), the portfolio, exits, last errors."""
    r = lp.phase2(events)
    fmt = lambda x, f="{:.2f}": "—" if x is None else f.format(x)
    reb = [e for e in events if e["event"] == "rebalance"]
    last = reb[-1] if reb else None
    wins = [e for e in events if e["event"] == "window"]
    lastw = wins[-1]["markets"] if wins else {}
    pos = {}
    for e in events:
        if e["event"] == "pnl":
            pos = e.get("positions") or pos
    out = ["## Ход прогона (фаза 2)", ""]
    if last:
        nxt = last["ts"] + lp.from_env(phase=2).rebalance_h * 3600
        out.append(f"- переотбор: последний {time.strftime('%m-%d %H:%M UTC', time.gmtime(last['ts']))}, следующий ~"
                   f"{time.strftime('%H:%M UTC', time.gmtime(nxt))}; рынков {len(last.get('target') or [])}, котировки на "
                   f"${last.get('quote_cap', 0):.0f}, прогноз M1 ${(last.get('summary') or {}).get('m1_pred', 0):.2f}/день; "
                   f"переотборов {len(reb)}, обновление портфеля {fmt(r.get('turnover'), '{:.0%}')}")
    else:
        out.append("- переотбора ещё не было")
    out += [f"- 30-минутных окон: {r.get('windows', 0)}; M1 портфеля ${fmt(r.get('m1_usd_day'))}/день, на $100 котировок "
            f"${fmt(r.get('m1_per100'))}/день; CV {fmt(r.get('cv'), '{:.0%}')}; крупнейшая доля за 24 ч "
            f"{fmt(r.get('max_share_last24'), '{:.0%}')} (порог 40%); факт / прогноз {fmt(r.get('fact_over_pred'))}",
            f"- на $100 котировок в день: M1 ${fmt(r.get('m1_per100'))} + рибейты ${fmt(r.get('rebates_per100'))} + "
            f"переоценка позиций ${fmt(r.get('mtm_per100'))} = **${fmt(r.get('net_per100'))}** (критерий: не меньше $20)",
            f"- исполнений {r.get('fills', 0)}, контрактов {r.get('contracts', 0):.0f} "
            f"({fmt(r.get('contracts_per_market_day'), '{:.0f}')} на рынок в сутки); markout 60 с {fmt(r.get('markout60_c'))} ц, "
            f"p5 {fmt(r.get('markout60_p5_c'))} ц; 600 с {fmt(r.get('markout600_c'))} ц",
            f"- выходы из рынков {r.get('exits', 0)}, закрытий тейкером {r.get('flattens', 0)} "
            f"({r.get('flatten_contracts', 0):.0f} контрактов, ${r.get('flatten_cost_usd', 0.0):.2f}), рынков завершено {r.get('retired', 0)}",
            f"- простой {fmt(r.get('downtime_h'))} ч; запусков процесса {r.get('process_starts', 0)}; ошибок {r.get('errors', 0)}", ""]
    if last:
        ms = last.get("markets") or {}
        target = set(last.get("target") or [])
        rows = sorted(ms, key=lambda c: -float(ms[c].get("m1_usd_day") or 0.0))
        out += ["## Портфель", "", "| Рынок | k | прогноз M1, $/день | M1 последнего окна | котировки, $ | до конца, ч | YES / NO |",
                "|---|---|---|---|---|---|---|"]
        for c in rows[:25]:
            m = ms[c]
            try:
                hl = (lp._ts(m["end"]) - now) / 3600 if m.get("end") else None
            except Exception:
                hl = None
            yn = pos.get(c) or [0, 0]
            name = (m.get("q") or c)[:60] + ("" if c in target else " (выход)")
            out.append(f"| {name} | {m.get('k', 1)} | {fmt(m.get('m1_usd_day'))} | {fmt((lastw.get(c) or {}).get('m1_usd_day'))} | "
                       f"{fmt(m.get('quote_cap'), '{:.0f}')} | {fmt(hl, '{:.1f}')} | {yn[0]:.0f} / {yn[1]:.0f} |")
        if len(rows) > 25:
            out.append(f"| … ещё {len(rows) - 25} | | | | | | |")
        out.append("")
    errs = [e for e in events if e["event"] == "error"][-5:]
    out += ["## Последние ошибки", ""] + ([f"- {time.strftime('%m-%d %H:%M', time.gmtime(e['ts']))} {e['error'][:160]}" for e in errs] or ["- нет"]) + [""]
    return out


def render_status(data, now=None, log_lines=50):
    now = now or time.time()
    st = _read_json(os.path.join(data, "service_status.json"), {}) or {}
    hs = _read_json(os.path.join(data, "health_state.json"), {}) or {}
    mode = st.get("mode") or env("POLYBOT_MODE", "paper")
    run_dir = os.path.join(data, mode)
    loop_st = _read_json(os.path.join(run_dir, "status.json"), {}) or {}
    markets = {m["cond"]: m for m in (_read_json(os.path.join(run_dir, "markets.json"), {}) or {}).get("markets", [])}
    age = now - st["ts"] if st.get("ts") else None
    alive = age is not None and age < 180
    out = ["# polybot — статус", "",
           f"Обновлено: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))} (сервер публикует этот файл раз в 15 минут, "
           "сразу — если бот упал, сменилась фаза или есть тревога)", ""]
    if hs.get("alert"):
        out += [f"> ⚠ **ТРЕВОГА:** {hs.get('message', '')}", ""]
    run_line = "—"
    if st.get("run_started"):
        el = (now - st["run_started"]) / 3600
        tot = (st["run_ends"] - st["run_started"]) / 3600
        run_line = (f"начат {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(st['run_started']))}, "
                    f"прошло {min(el, tot):.1f} ч из {tot:.0f}" + (", завершён" if st.get("run_completed") else f", осталось {max(0, tot - el):.1f} ч"))
    run = _read_json(os.path.join(run_dir, "run.json"), {}) or {}
    cfg = lp.from_env(phase=run.get("phase"))
    warm_line = "warming — прогрев: снимки книг всех рынков с наградой, без котировок"
    ws = _read_json(os.path.join(run_dir, "snaps", "state.json"), {}) or {}
    if ws:
        left = max(0.0, cfg.lookback_h - (now - ws["first"]) / 3600) if ws.get("first") else cfg.lookback_h
        warm_line = (f"warming — прогрев без котировок: снимок {ws.get('window', 0)} из {ws.get('need', 48)} "
                     f"(каждые {cfg.snapshot_gap_s / 60:.0f} мин), "
                      f"до старта котирования ~{left:.1f} ч; последний снимок {_ago(now - ws['ts'])} назад, "
                      f"рынков котируемо {ws.get('n_quotable', 0)} из {ws.get('n_eligible', 0)}")
    sel_line = "selecting — отбор рынков"
    spath = os.path.join(run_dir, "selection", "snapshots.jsonl")
    if os.path.exists(spath):
        with open(spath) as f:
            k = sum(1 for _ in f)
        left = max(0, cfg.select_snapshots - k) * cfg.select_gap_s / 3600
        sel_line += f": снимок {k} из {cfg.select_snapshots} (каждые {cfg.select_gap_s / 60:.0f} мин), до старта котирования ~{left:.1f} ч"
    else:
        sel_line += f": {cfg.select_snapshots} снимков книги каждые {cfg.select_gap_s / 60:.0f} мин"
    rows = [("Бот", f"✅ работает ({mode})" if alive else f"❌ не отвечает {_ago(age)}"),
            ("Фаза", {"selecting": sel_line, "warming": warm_line, "running": "running — котирует",
                      "complete": "complete — прогон завершён"}.get(st.get("phase"), st.get("phase", "—"))
             + (f" — {st['detail']}" if st.get("detail") else "")),
            ("Прогон", run_line),
            ("Версия", f"ветка {st.get('branch', '—')}, коммит {str(st.get('release', '—'))[:10]}"),
            ("Последние данные рынка", _ago(now - loop_st["ts"]) + " назад" if loop_st.get("ts") else "—"),
            ("PnL (переоценка по mid, без наград)", f"${loop_st.get('pnl', st.get('pnl', 0.0)) or 0.0:.2f}"),
            ("Ордеров стоит", str(loop_st.get("orders", st.get("orders", "—")))),
            ("M1 сейчас (модель награды)", f"${loop_st.get('m1_usd_day_now', 0.0) or 0.0:.2f}/день"),
            ("Проверка здоровья", "⚠ " + hs["message"] if hs.get("alert") else
             f"ок, перезапусков подряд {hs.get('count', 0)}, проверка {_ago(now - hs['ts']) + ' назад' if hs.get('ts') else '—'}")]
    if mode == "live":
        rows.append(("Геоблок", "заблокирован" if st.get("geoblocked") else f"не заблокирован ({st.get('country') or '—'})"))
    out += ["| | |", "|---|---|"] + [f"| {a} | {b} |" for a, b in rows] + [""]
    events = lp.load_events(run_dir, mode)
    if events and cfg.phase == 2:
        out += _phase2_lines(events, now)
    elif events:
        qcap = sum(float(m.get("quote_cap") or 0.0) for m in markets.values()) or None
        r = lp.phase1(events, m1_per_100_min=cfg.m1_per_100_min, quote_cap_usd=qcap)
        wins = [e for e in events if e["event"] == "window"]
        port = [w["m1_usd_day"] for w in wins]
        mu = sum(port) / len(port) if port else 0.0
        cv_all = ((sum((x - mu) ** 2 for x in port) / len(port)) ** 0.5 / mu) if port and mu > 0 else None
        mk = r.get("markout60") or {}
        fmt = lambda x, f="{:.2f}": "—" if x is None else f.format(x)
        ticks = sorted({round(e["ts"]) for e in events if e["event"] in ("m1", "pnl")})
        down_h = sum(b - a for a, b in zip(ticks, ticks[1:]) if b - a > 300) / 3600
        n_starts = sum(1 for e in events if e["event"] == "start")
        gone = [e for e in events if e["event"] == "reward_removed"]
        removed = set(gone[-1]["markets"]) if gone and now - gone[-1]["ts"] < 7200 else set()
        out += ["## Ход прогона", "",
                f"- 30-минутных окон: {len(wins)}; M1 портфеля в среднем ${mu:.2f}/день; CV по всем окнам {fmt(cv_all, '{:.0%}')}; "
                f"CV в часы 24–48: {fmt(r.get('cv'), '{:.0%}') if r.get('windows_2nd_half') else 'ещё нет'}",
                f"- крупнейшая доля рынка в M1 за последние 24 ч: {fmt(r.get('max_share_last24'), '{:.0%}')} (порог 40%)",
                f"- M1 в часы 24–48 на $100 котировок: "
                + (f"${r['m1_per_100usd_2nd_half']:.2f}/день" if r.get("m1_per_100usd_2nd_half") is not None else "ещё нет")
                + f" (меньше ${cfg.m1_per_100_min:g} — H10 закрывается; котировки на {fmt(qcap, '${:.0f}')})",
                f"- простой: {down_h:.2f} ч; запусков процесса: {n_starts}",
                f"- исполнений: {r.get('fills', 0)}, контрактов {r.get('contracts', 0):.0f}; markout 60 с: среднее {fmt(mk.get('mean_c'))} ц, "
                f"p5 {fmt(mk.get('p5_c'))} ц, доля хуже −2.1 ц {fmt(mk.get('share_worse_than_-2.1c'), '{:.0%}')}", ""]
        last = wins[-1]["markets"] if wins else {}
        pos = {}
        for e in events:
            if e["event"] == "pnl":
                pos = e.get("positions") or pos
        out += ["## Рынки", "", "| Рынок | M1 последнего окна, $/день | доля M1 за 24 ч | уровней конкурентов | исполнений | YES / NO |",
                "|---|---|---|---|---|---|"]
        per = r.get("markets") or {}
        for c in markets or per:
            m = last.get(c) or {}
            p = per.get(c) or {}
            yn = pos.get(c) or [0, 0]
            name = (markets.get(c) or {}).get("q", c)[:70] + (" (награда снята, не котируем)" if c in removed else "")
            out.append(f"| {name} | {fmt(m.get('m1_usd_day'))} | {fmt(p.get('share_last24'), '{:.0%}')} | "
                       f"{fmt(m.get('comp_levels'), '{:.1f}')} | {p.get('fills', 0)} | {yn[0]:.0f} / {yn[1]:.0f} |")
        out.append("")
        errs = [e for e in events if e["event"] == "error"][-5:]
        out += ["## Последние ошибки", ""] + ([f"- {time.strftime('%m-%d %H:%M', time.gmtime(e['ts']))} {e['error'][:160]}" for e in errs] or ["- нет"]) + [""]
    logp = os.path.join(data, "logs", "service.log")
    if os.path.exists(logp):
        with open(logp, errors="replace") as f:
            tail = f.readlines()[-log_lines:]
        out += [f"## Лог (последние {len(tail)} строк)", "", "```"] + [x.rstrip("\n") for x in tail] + ["```", ""]
    return "\n".join(out)


def main(args):
    if args and args[0] == "status-md":
        print(render_status(env("POLYBOT_DATA", "/var/lib/polybot")))
        return
    asyncio.run(Service().run())
