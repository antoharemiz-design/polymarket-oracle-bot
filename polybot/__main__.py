"""Command line.

  python -m polybot paper [--markets N]            paper trading on live data, simulated fills
  python -m polybot live --i-understand-live       real orders (reads keys from .env)
  python -m polybot check                          verify keys, balance and API access; places nothing
  python -m polybot sim REC_DIR [...]              backtest on recorded ticks (see polybot/sim.py)
  python -m polybot outcomes REC_DIR               fetch resolutions for recorded markets
  python -m polybot service                        long-running server service (systemd unit polybot), see polybot/service.py
  python -m polybot status-md                      status.md of the server service
  python -m polybot lp-select | lp-paper | lp-live --i-understand-live | lp-earnings | lp-report | lp-phase1 | lp-phase2
                                                   liquidity-rewards test (H10), see polybot/lp.py
"""
import asyncio
import sys


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "help"
    if cmd == "sim":
        from . import sim
        sys.argv = ["polybot.sim"] + args[1:]
        sim.main(); return
    if cmd == "outcomes":
        from .markets import update_outcomes
        o = update_outcomes(args[1]); print(f"{len(o)} resolved markets"); return
    if cmd in ("paper", "live"):
        from .config import from_env
        from .run import Bot
        cfg = from_env()
        cfg.mode = cmd
        if cmd == "live":
            if "--i-understand-live" not in args:
                sys.exit("live mode sends real orders. Re-run with --i-understand-live after paper validation.")
            if not cfg.private_key:
                sys.exit("POLYMARKET_PRIVATE_KEY is not set")
        n = int(args[args.index("--markets") + 1]) if "--markets" in args else None
        try:
            totals = asyncio.run(Bot(cfg).run(n))
            print("totals:", totals)
        except KeyboardInterrupt:
            print("stopped")
        return
    if cmd in ("service", "status-md"):
        from .service import main as service_main
        service_main(args); return
    if cmd.startswith("lp-"):
        from .lp import cli
        cli(cmd, args[1:]); return
    if cmd == "check":
        from .check import main as check
        asyncio.run(check()); return
    print(__doc__)


if __name__ == "__main__":
    main()
