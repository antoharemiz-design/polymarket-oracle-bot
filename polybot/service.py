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
                 geoblock_fn=geoblock):
        self.data = data or env("POLYBOT_DATA", "/var/lib/polybot")
        self.mode = (mode or env("POLYBOT_MODE", "paper")).lower()
        self.clock = clock
        self.run_dir = os.path.join(self.data, self.mode)
        os.makedirs(self.run_dir, exist_ok=True)
        os.makedirs(os.path.join(self.data, "logs"), exist_ok=True)
        self.books_fn, self.rewards_fn, self.trades_fn = books_fn, rewards_fn, trades_fn
        self.geoblock_fn = geoblock_fn
        self.log = self._logger()
        self.test = None
        self.phase, self.detail = "starting", ""
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

        def sleep(sec):
            end = time.time() + sec
            while time.time() < end:
                self.write_status()
                time.sleep(min(20.0, max(0.0, end - time.time())))
        return lp.select(cfg, log=self.log.info, sleep=sleep)

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
        mpath = os.path.join(self.run_dir, "markets.json")
        if not run:
            self.phase = "selecting"
            self.write_status()
            markets = await asyncio.to_thread(self.select_markets, cfg)
            if len(markets) < cfg.markets_min:
                return await self._idle(stop, "error", f"only {len(markets)} markets pass the filters, need {cfg.markets_min}")
            _write_json(mpath, {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "markets": markets})
            run = {"started": now, "test_days": cfg.test_days, "mode": self.mode}
            self.save_run(run)
            self.log.info(f"new run: {len(markets)} markets for {cfg.test_days} days")
        else:
            markets = _read_json(mpath, {}).get("markets") or []
            self.log.info(f"resuming run started {time.strftime('%Y-%m-%d %H:%M', time.gmtime(run['started']))} UTC")
        cfg.test_days = (run["started"] + run["test_days"] * 86400 - now) / 86400
        if self.mode == "paper":
            ex = lp.PaperExec(**({"trades_fn": self.trades_fn} if self.trades_fn else {}), every_s=cfg.fills_every_s)
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
        self.test = lp.LPTest(cfg, ex, journal, markets, log=self.log.info, clock=self.clock,
                              control_fn=self.control_for_test, **kw)
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
           f"Обновлено: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))} (сервер пишет этот файл каждые 5 минут)", ""]
    if hs.get("alert"):
        out += [f"> ⚠ **ТРЕВОГА:** {hs.get('message', '')}", ""]
    run_line = "—"
    if st.get("run_started"):
        el = (now - st["run_started"]) / 3600
        tot = (st["run_ends"] - st["run_started"]) / 3600
        run_line = (f"начат {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(st['run_started']))}, "
                    f"прошло {min(el, tot):.1f} ч из {tot:.0f}" + (", завершён" if st.get("run_completed") else f", осталось {max(0, tot - el):.1f} ч"))
    rows = [("Бот", f"✅ работает ({mode})" if alive else f"❌ не отвечает {_ago(age)}"),
            ("Фаза", f"{st.get('phase', '—')}" + (f" — {st['detail']}" if st.get("detail") else "")),
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
    if events:
        r = lp.phase1(events)
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
