"""H1 on BTC 15m: what is traded after the window ends (the outcome is fixed by the Chainlink TWAP).

usage: h1_15m_postwindow.py H6_DIR   (uses m15.json and fills15.csv written by h6_nested.py)
"""
import collections
import json
import sys

d = sys.argv[1]
m15 = json.load(open(f"{d}/m15.json"))
win_buy = collections.Counter(); lose_buy = collections.Counter(); after_n = collections.Counter()
mk_win, mk_any = set(), set()
for line in open(f"{d}/fills15.csv"):
    s, ts, oi, side, px, sz = line.rstrip("\n").split(",")
    s, ts, oi, px, sz = int(s), int(ts), int(oi), float(px), float(sz)
    if ts < s + 900:
        continue
    w = m15[str(s)]["winner"]
    lo, lp = (oi, px) if side == "B" else (1 - oi, 1 - px)
    mk_any.add(s)
    if lo == w:
        win_buy[round(lp, 3)] += sz; mk_win.add(s)
    else:
        lose_buy[round(lp, 3)] += sz
tot_w = sum(win_buy.values()); tot_l = sum(lose_buy.values())
cheap_w = sum(v for p, v in win_buy.items() if p <= 0.99)
print(f"15m markets {len(m15)}; with post-window taker trades {len(mk_any)}")
print(f"taker ends long the WINNER after the window: {tot_w:,.0f} contracts in {len(mk_win)} markets; "
      f"at price <= 0.99: {cheap_w:,.0f} contracts, value of the gap sum((1-p)*q) = "
      f"${sum((1 - p) * v for p, v in win_buy.items() if p <= 0.99):,.2f}")
print(f"taker ends long the LOSER after the window: {tot_l:,.0f} contracts; at <= 0.01: "
      f"{sum(v for p, v in lose_buy.items() if p <= 0.01):,.0f}; maker on the other side earns 1 - p_winner = p_loser: "
      f"${sum(p * v for p, v in lose_buy.items()):,.2f} in total over {len(m15)} markets")
