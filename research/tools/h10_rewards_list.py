"""Download the current liquidity-rewards configuration of every market.  usage: h10_rewards_list.py OUT.json"""
import json
import sys
import urllib.parse

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from h2_negrisk_scan import get  # noqa: E402

rows, cur = [], None
while True:
    d = get("https://clob.polymarket.com/rewards/markets/current?" + urllib.parse.urlencode({"next_cursor": cur} if cur else {}))
    rows += d.get("data") or []
    cur = d.get("next_cursor")
    if not cur or cur == "LTE=" or not d.get("data"):
        break
json.dump(rows, open(sys.argv[1], "w"))
print(f"rewarded markets {len(rows)}, total ${sum(r.get('total_daily_rate') or 0 for r in rows):,.0f}/day")
