# Фаза 1: промежуточные выгрузки

Каждые 6 часов. Контейнерные прогоны — запасные данные; официальная фаза 1 — на VPS.
Критерии (CV M1 и доля рынка) оцениваются только на часах 24–48 и последних 24 ч, поэтому до 48 ч
вердикт `FAIL` по CV — следствие отсутствия данных, а не результат.

## 2026-11-16 17:13 UTC — контейнерные прогоны (запасные данные)

### lp1, 5 рынков (утренний отбор)

```
excluded: []
hours: 5.8
windows_2nd_half: 0
excluded: []
downtime_h: 0.0
process_starts: 2
portfolio_m1_usd_day_2nd_half: 0.0
cv: None
max_share_last24: 0.3751754639281994
fills: 4
contracts: 46.0
contracts_per_market_day: 38.35534650787622
markout60: {'n_fills': 4, 'mean_c': 0.7282608695652174, 'p5_c': 0.5, 'p25_c': 0.5, 'p50_c': 0.5, 'share_worse_than_-2.1c': 0.0}
expected_week_usd: 0.0
checks: {'M1 CV <= 50% (hours 24-48)': False, 'no market > 40% of M1 (last 24 h)': True}
verdict: FAIL: M1 CV <= 50% (hours 24-48)
  Will PAN win the most seats in the 2027 Chihuahua  {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.046, 'comp_levels': None, 'places': 19, 'cancels': 17, 'fills': 0, 'contracts': 0.0, 'markout60_mean_c': None, 'markout60_p5_c': None}
  Will the 10-year Treasury yield hit 5.17% in Novem {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.219, 'comp_levels': None, 'places': 278, 'cancels': 278, 'fills': 0, 'contracts': 0.0, 'markout60_mean_c': None, 'markout60_p5_c': None}
  Will Donald Trump publicly insult Hasan Piker by N {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.152, 'comp_levels': None, 'places': 26, 'cancels': 24, 'fills': 1, 'contracts': 1.0, 'markout60_mean_c': 5.0, 'markout60_p5_c': 5.0}
  Will the median home value in the US be between $4 {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.375, 'comp_levels': None, 'places': 508, 'cancels': 505, 'fills': 3, 'contracts': 45.0, 'markout60_mean_c': 0.633, 'markout60_p5_c': 0.5}
  Will the Ornn B200 Index hit $5.00 (LOW) by Decemb {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.207, 'comp_levels': None, 'places': 126, 'cancels': 124, 'fills': 0, 'contracts': 0.0, 'markout60_mean_c': None, 'markout60_p5_c': None}
```

### lp1 без Hasan Piker

```
excluded: ['Will Donald Trump publicly insult Hasan Piker by November 30']
hours: 5.8
windows_2nd_half: 0
excluded: ['0x8e0d13a3035e7505f7dd93731f5a8c50511ae843e6dca94b0c024b3578939835']
downtime_h: 0.0
process_starts: 2
portfolio_m1_usd_day_2nd_half: 0.0
cv: None
max_share_last24: 0.44253600640087004
fills: 3
contracts: 45.0
contracts_per_market_day: 46.90191828408776
markout60: {'n_fills': 3, 'mean_c': 0.6333333333333333, 'p5_c': 0.5, 'p25_c': 0.5, 'p50_c': 0.5, 'share_worse_than_-2.1c': 0.0}
expected_week_usd: 0.0
checks: {'M1 CV <= 50% (hours 24-48)': False, 'no market > 40% of M1 (last 24 h)': False}
verdict: FAIL: M1 CV <= 50% (hours 24-48), no market > 40% of M1 (last 24 h)
```

### lp_shadow, 30 рынков (только распределение markout)

```
excluded: []
hours: 6.2
windows_2nd_half: 0
excluded: []
downtime_h: 0.0
process_starts: 2
portfolio_m1_usd_day_2nd_half: 0.0
cv: None
max_share_last24: 0.14203129708133494
fills: 16
contracts: 166.87
contracts_per_market_day: 21.48825590813386
markout60: {'n_fills': 16, 'mean_c': 1.3147959489422905, 'p5_c': 0.5, 'p25_c': 1.0, 'p50_c': 1.0, 'share_worse_than_-2.1c': 0.0}
expected_week_usd: 0.0
checks: {'M1 CV <= 50% (hours 24-48)': False, 'no market > 40% of M1 (last 24 h)': True}
verdict: FAIL: M1 CV <= 50% (hours 24-48)
```

## 2026-11-16 23:16 UTC — контейнерные прогоны (запасные данные), 12 ч

### lp1, 5 рынков (утренний отбор)

```
excluded: []
hours: 11.8
windows_2nd_half: 0
excluded: []
downtime_h: 0.0
process_starts: 4
portfolio_m1_usd_day_2nd_half: 0.0
cv: None
max_share_last24: 0.31922543787105095
fills: 5
contracts: 65.99
contracts_per_market_day: 26.942267542224663
markout60: {'n_fills': 5, 'mean_c': 0.6591150174268828, 'p5_c': 0.5, 'p25_c': 0.5, 'p50_c': 0.5, 'share_worse_than_-2.1c': 0.0}
expected_week_usd: 0.0
checks: {'M1 CV <= 50% (hours 24-48)': False, 'no market > 40% of M1 (last 24 h)': True}
verdict: FAIL: M1 CV <= 50% (hours 24-48)
  Will PAN win the most seats in the 2027 Chihuahua  {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.036, 'comp_levels': None, 'places': 19, 'cancels': 19, 'fills': 0, 'contracts': 0.0, 'markout60_mean_c': None, 'markout60_p5_c': None}
  Will the 10-year Treasury yield hit 5.17% in Novem {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.306, 'comp_levels': None, 'places': 545, 'cancels': 543, 'fills': 0, 'contracts': 0.0, 'markout60_mean_c': None, 'markout60_p5_c': None}
  Will Donald Trump publicly insult Hasan Piker by N {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.141, 'comp_levels': None, 'places': 34, 'cancels': 30, 'fills': 1, 'contracts': 1.0, 'markout60_mean_c': 5.0, 'markout60_p5_c': 5.0}
  Will the median home value in the US be between $4 {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.319, 'comp_levels': None, 'places': 515, 'cancels': 508, 'fills': 4, 'contracts': 64.99, 'markout60_mean_c': 0.592, 'markout60_p5_c': 0.5}
  Will the Ornn B200 Index hit $5.00 (LOW) by Decemb {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.199, 'comp_levels': None, 'places': 215, 'cancels': 209, 'fills': 0, 'contracts': 0.0, 'markout60_mean_c': None, 'markout60_p5_c': None}
```

### lp1 без Hasan Piker

```
excluded: ['Will Donald Trump publicly insult Hasan Piker by November 30']
hours: 11.8
windows_2nd_half: 0
excluded: ['0x8e0d13a3035e7505f7dd93731f5a8c50511ae843e6dca94b0c024b3578939835']
downtime_h: 0.0
process_starts: 4
portfolio_m1_usd_day_2nd_half: 0.0
cv: None
max_share_last24: 0.37150832794568933
fills: 4
contracts: 64.99
contracts_per_market_day: 33.167486883792634
markout60: {'n_fills': 4, 'mean_c': 0.5923218956762579, 'p5_c': 0.5, 'p25_c': 0.5, 'p50_c': 0.5, 'share_worse_than_-2.1c': 0.0}
expected_week_usd: 0.0
checks: {'M1 CV <= 50% (hours 24-48)': False, 'no market > 40% of M1 (last 24 h)': True}
verdict: FAIL: M1 CV <= 50% (hours 24-48)
```

### lp_shadow, 30 рынков (только распределение markout)

```
excluded: []
hours: 12.2
windows_2nd_half: 0
excluded: []
downtime_h: 0.0
process_starts: 4
portfolio_m1_usd_day_2nd_half: 0.0
cv: None
max_share_last24: 0.09335085164809594
fills: 22
contracts: 256.87
contracts_per_market_day: 16.826680484984085
markout60: {'n_fills': 22, 'mean_c': 1.334916494724958, 'p5_c': 0.5, 'p25_c': 0.5, 'p50_c': 1.0, 'share_worse_than_-2.1c': 0.0}
expected_week_usd: 0.0
checks: {'M1 CV <= 50% (hours 24-48)': False, 'no market > 40% of M1 (last 24 h)': True}
verdict: FAIL: M1 CV <= 50% (hours 24-48)
```

