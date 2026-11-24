"""Market discovery and resolution lookups over public REST endpoints (no authentication)."""
import json
import os
import time
import urllib.request

GAMMA = "https://gamma-api.polymarket.com/markets?slug="
RESOLUTIONS = "https://data-api.polymarket.com/v2/resolutions?condition="
DUR = {"5m": 300, "15m": 900}


def _get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "polybot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def slug_for(asset: str, tf: str, start: int) -> str:
    return f"{asset}-updown-{tf}-{start}"


def lookup(asset: str, tf: str, start: int):
    """Return {slug, condition_id, token_up, token_down, tick, min_size} or None if not listed yet."""
    d = _get(GAMMA + slug_for(asset, tf, start))
    if not d:
        return None
    m = d[0]
    toks = json.loads(m["clobTokenIds"])
    outs = json.loads(m.get("outcomes") or '["Up","Down"]')
    if outs[0] != "Up":                       # never assume token order silently
        raise ValueError(f"unexpected outcome order {outs} for {m['slug']}")
    return {"slug": m["slug"], "condition_id": m["conditionId"], "token_up": toks[0], "token_down": toks[1],
            "tick": float(m.get("orderPriceMinTickSize") or 0.01), "min_size": float(m.get("orderMinSize") or 5),
            "accepting": bool(m.get("acceptingOrders", True))}


def winners(condition_ids):
    """{condition_id: 0 (Up) | 1 (Down)} for the ones that are resolved."""
    out = {}
    ids = list(condition_ids)
    for i in range(0, len(ids), 20):
        for r in _get(RESOLUTIONS + ",".join(ids[i:i + 20]), 30).get("data") or []:
            pay = r.get("payouts") or []
            if r.get("status") == "resolved" and len(pay) == 2 and pay[0] != pay[1]:
                out[r["condition_id"]] = 0 if pay[0] > pay[1] else 1
    return out


def closed_winner(asset: str, tf: str, start: int):
    """Winner (0 Up / 1 Down) of a closed market, or None while it is unresolved.
    Gamma lists closed markets only when asked with closed=true."""
    d = _get(GAMMA + slug_for(asset, tf, start) + "&closed=true")
    if not d or not d[0].get("closed"):
        return None
    prices = json.loads(d[0].get("outcomePrices") or "[]")
    if len(prices) == 2 and {float(prices[0]), float(prices[1])} == {0.0, 1.0}:
        return 0 if float(prices[0]) == 1.0 else 1
    return None


def resolve(asset: str, tf: str, start: int, condition_id: str | None = None):
    """Winner of a finished market, or None while unresolved. The resolutions endpoint knows the
    result first; Gamma lists a market as closed only several minutes later."""
    if condition_id:
        try:
            w = winners([condition_id]).get(condition_id)
            if w is not None:
                return w
        except Exception:
            pass
    return closed_winner(asset, tf, start)


def update_outcomes(rec_dir: str, asset="btc", tf="5m") -> dict:
    """Maintain REC_DIR/outcomes.json = {start: winner} for every recorded market that has resolved."""
    from .replay import list_markets
    path = os.path.join(rec_dir, "outcomes.json")
    out = json.load(open(path)) if os.path.exists(path) else {}
    now = time.time()
    for start, _ in list_markets(rec_dir):
        if str(start) in out or now < start + DUR[tf] + 90:
            continue
        meta = os.path.join(rec_dir, "pm", f"{slug_for(asset, tf, start)}.json")
        cond = json.load(open(meta)).get("condition_id") if os.path.exists(meta) else None
        try:
            w = resolve(asset, tf, start, cond)
        except Exception:
            continue
        if w is not None:
            out[str(start)] = w
    json.dump(out, open(path, "w"))
    return out


if __name__ == "__main__":
    import sys
    o = update_outcomes(sys.argv[1])
    print(f"{len(o)} resolved markets recorded; Up won {sum(1 for v in o.values() if v == 0)}")
