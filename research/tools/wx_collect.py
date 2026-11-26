"""Collect resolved daily-temperature markets of Polymarket for the adverse-selection study (26 Nov, after the live
loss): for every market of every closed "highest temperature in <city> on <date>" event in a date range, its
metadata (tokens, endDate, outcome, reward settings), every taker trade (data-api /v2/trades, full history) and
the 1-minute price history of the YES token (clob /prices-history).

    python research/tools/wx_collect.py OUT_DIR --days 21 [--kind highest] [--sleep 0.15] [--shard i/n]

One file per market: OUT_DIR/<conditionId>.json {"meta", "trades", "prices"}; existing files are skipped, so the
collection can be resumed. Public endpoints only, no keys.
"""
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research", "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def events(kind, days):
    """Closed daily-temperature events whose end is within the last `days` days, newest first."""
    out, off, stop = [], 0, time.time() - days * 86400
    while True:
        page = get(f"{GAMMA}/events?" + urllib.parse.urlencode(
            {"tag_slug": "daily-temperature", "closed": "true", "limit": 100, "offset": off,
             "order": "endDate", "ascending": "false"}))
        if not page:
            break
        for e in page:
            end = e.get("endDate") or ""
            ts = time.mktime(time.strptime(end[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone if end else 0
            if ts < stop:
                return out
            if e.get("slug", "").startswith(f"{kind}-temperature-in-"):
                out.append(e)
        off += 100
    return out


def trades(cond):
    out, cur = [], None
    while True:
        q = {"condition_id": cond, "limit": 1000, "taker_only": "true"}
        if cur:
            q["cursor"] = cur
        d = get(f"{DATA}/v2/trades?" + urllib.parse.urlencode(q))
        out += [{k: t[k] for k in ("side", "outcome_index", "price", "size", "timestamp", "proxy_wallet",
                                   "transaction_hash")} for t in d.get("data") or []]
        cur = (d.get("pagination") or {}).get("next_cursor")
        if not cur:
            return out


def main():
    outd = sys.argv[1]
    arg = lambda k, d: type(d)(sys.argv[sys.argv.index(k) + 1]) if k in sys.argv else d
    days, kind, pause = arg("--days", 21), arg("--kind", "highest"), arg("--sleep", 0.15)
    shard, nshard = (int(x) for x in arg("--shard", "0/1").split("/"))
    os.makedirs(outd, exist_ok=True)
    evs = events(kind, days)
    print(f"{len(evs)} events", flush=True)
    n = 0
    for i, e in enumerate(evs):
        if i % nshard != shard:
            continue
        for m in e.get("markets") or []:
            cond = m.get("conditionId")
            path = os.path.join(outd, f"{cond}.json")
            if not cond or os.path.exists(path):
                continue
            try:
                toks = json.loads(m.get("clobTokenIds") or "[]")
                meta = {k: m.get(k) for k in ("question", "groupItemTitle", "conditionId", "endDate", "startDate",
                                              "createdAt", "outcomePrices", "rewardsMinSize", "rewardsMaxSpread",
                                              "clobRewards", "orderPriceMinTickSize", "volumeNum", "feeSchedule")}
                meta.update(event=e.get("slug"), event_end=e.get("endDate"), tokens=toks)
                tr = trades(cond)
                time.sleep(pause)
                ph = get(f"{CLOB}/prices-history?" + urllib.parse.urlencode(
                    {"market": toks[0], "interval": "max", "fidelity": 1})).get("history", []) if toks else []
                time.sleep(pause)
                with open(path + ".tmp", "w") as f:
                    json.dump({"meta": meta, "trades": tr, "prices": ph}, f)
                os.replace(path + ".tmp", path)
                n += 1
                if n % 50 == 0:
                    print(f"{n} markets", flush=True)
            except Exception as ex:
                print(f"{cond[:12]} failed: {ex!r:.120}", flush=True)
    print(f"done: {n} new markets", flush=True)


if __name__ == "__main__":
    main()
