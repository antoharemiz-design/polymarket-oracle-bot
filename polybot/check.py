"""Pre-flight for live trading: authenticates and reads account state. Places no orders."""
import time

from .config import from_env
from .markets import DUR, lookup


async def main():
    cfg = from_env()
    if not cfg.private_key:
        print("POLYMARKET_PRIVATE_KEY is not set"); return
    from polymarket import AsyncSecureClient
    from polymarket.auth import RelayerApiKey
    api_key = RelayerApiKey(key=cfg.relayer_key, address=cfg.relayer_address) if cfg.relayer_key and cfg.relayer_address else None
    t = time.perf_counter()
    async with await AsyncSecureClient.create(private_key=cfg.private_key, wallet=cfg.wallet or None, api_key=api_key) as c:
        print(f"authenticated in {1000 * (time.perf_counter() - t):.0f} ms; wallet type: {c.wallet_type}")
        bal = await c.get_balance_allowance(asset_type="COLLATERAL")
        print(f"collateral balance: ${bal.balance / 1e6:.2f}; allowances set: {sum(1 for v in bal.allowances.values() if v > 0)}/{len(bal.allowances)}")
        print("closed-only mode:", await c.get_closed_only_mode())
        n = 0
        async for _ in c.list_open_orders().iter_items():
            n += 1
        print("open orders:", n)
        dur = DUR[cfg.tf]
        m = lookup(cfg.asset, cfg.tf, (int(time.time()) // dur) * dur)
        lat = []
        for _ in range(5):
            t = time.perf_counter(); await c.get_order_book(token_id=m["token_up"]); lat.append(1000 * (time.perf_counter() - t))
        print(f"REST round trip, ms: min {min(lat):.0f} median {sorted(lat)[2]:.0f}  (cancel must land within 250 ms of a spot move)")
