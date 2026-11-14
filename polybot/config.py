"""Configuration from environment variables (optionally loaded from a .env file next to the repo)."""
import dataclasses
import os

from .risk import RiskLimits
from .strategy import Params


def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _cast(cur, raw):
    if isinstance(cur, bool):
        return raw.lower() in ("1", "true", "yes")
    return type(cur)(raw)


@dataclasses.dataclass
class Config:
    mode: str = "paper"                 # "paper" or "live"; live also needs --i-understand-live on the CLI
    asset: str = "btc"
    tf: str = "5m"
    lat_ms: int = 150                   # paper: assumed round-trip latency of every action
    queue_mult: float = 1.0             # paper: multiplier on the queue ahead of us
    journal_dir: str = "journal"
    heartbeat: bool = False             # live: exchange-side dead-man switch (cannot be disarmed once started)
    params: Params = dataclasses.field(default_factory=Params)
    limits: RiskLimits = dataclasses.field(default_factory=RiskLimits)
    private_key: str = ""
    wallet: str = ""
    relayer_key: str = ""
    relayer_address: str = ""


def from_env() -> Config:
    load_dotenv()
    c = Config()
    env = os.environ
    for f in ("mode", "asset", "tf", "lat_ms", "queue_mult", "journal_dir", "heartbeat"):
        raw = env.get("BOT_" + f.upper())
        if raw is not None:
            setattr(c, f, _cast(getattr(c, f), raw))
    for obj, prefix in ((c.params, "STRAT_"), (c.limits, "RISK_")):
        for f in dataclasses.fields(obj):
            raw = env.get(prefix + f.name.upper())
            if raw is not None:
                setattr(obj, f.name, _cast(getattr(obj, f.name), raw))
    c.private_key = env.get("POLYMARKET_PRIVATE_KEY", "").strip()
    c.wallet = env.get("FUNDER_ADDRESS", "").strip()
    c.relayer_key = env.get("POLYMARKET_RELAYER_KEY", "").strip()
    c.relayer_address = env.get("POLYMARKET_RELAYER_ADDRESS", "").strip()
    return c
