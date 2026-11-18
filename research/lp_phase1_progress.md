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

## 2026-11-17 05:16 UTC — контейнерные прогоны (запасные данные), 18 ч

### lp1, 5 рынков (утренний отбор)

```
excluded: []
hours: 17.8
windows_2nd_half: 0
excluded: []
downtime_h: 0.0
process_starts: 4
portfolio_m1_usd_day_2nd_half: 0.0
cv: None
max_share_last24: 0.3136691862665554
fills: 6
contracts: 85.99
contracts_per_market_day: 23.244867306057742
markout60: {'n_fills': 6, 'mean_c': 0.6221072217699732, 'p5_c': 0.5, 'p25_c': 0.5, 'p50_c': 0.5, 'share_worse_than_-2.1c': 0.0}
expected_week_usd: 0.0
checks: {'M1 CV <= 50% (hours 24-48)': False, 'no market > 40% of M1 (last 24 h)': True}
verdict: FAIL: M1 CV <= 50% (hours 24-48)
  Will PAN win the most seats in the 2027 Chihuahua  {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.029, 'comp_levels': None, 'places': 19, 'cancels': 19, 'fills': 0, 'contracts': 0.0, 'markout60_mean_c': None, 'markout60_p5_c': None}
  Will the 10-year Treasury yield hit 5.17% in Novem {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.314, 'comp_levels': None, 'places': 891, 'cancels': 887, 'fills': 0, 'contracts': 0.0, 'markout60_mean_c': None, 'markout60_p5_c': None}
  Will Donald Trump publicly insult Hasan Piker by N {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.152, 'comp_levels': None, 'places': 80, 'cancels': 76, 'fills': 1, 'contracts': 1.0, 'markout60_mean_c': 5.0, 'markout60_p5_c': 5.0}
  Will the median home value in the US be between $4 {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.297, 'comp_levels': None, 'places': 521, 'cancels': 513, 'fills': 5, 'contracts': 84.99, 'markout60_mean_c': 0.571, 'markout60_p5_c': 0.5}
  Will the Ornn B200 Index hit $5.00 (LOW) by Decemb {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.209, 'comp_levels': None, 'places': 259, 'cancels': 253, 'fills': 0, 'contracts': 0.0, 'markout60_mean_c': None, 'markout60_p5_c': None}
```

### lp1 без Hasan Piker

```
excluded: ['Will Donald Trump publicly insult Hasan Piker by November 30']
hours: 17.8
windows_2nd_half: 0
excluded: ['0x8e0d13a3035e7505f7dd93731f5a8c50511ae843e6dca94b0c024b3578939835']
downtime_h: 0.0
process_starts: 4
portfolio_m1_usd_day_2nd_half: 0.0
cv: None
max_share_last24: 0.36991482087538224
fills: 5
contracts: 84.99
contracts_per_market_day: 28.718183398387133
markout60: {'n_fills': 5, 'mean_c': 0.5705965407695023, 'p5_c': 0.5, 'p25_c': 0.5, 'p50_c': 0.5, 'share_worse_than_-2.1c': 0.0}
expected_week_usd: 0.0
checks: {'M1 CV <= 50% (hours 24-48)': False, 'no market > 40% of M1 (last 24 h)': True}
verdict: FAIL: M1 CV <= 50% (hours 24-48)
```

### lp_shadow, 30 рынков (только распределение markout)

```
excluded: []
hours: 18.2
windows_2nd_half: 0
excluded: []
downtime_h: 0.0
process_starts: 4
portfolio_m1_usd_day_2nd_half: 0.0
cv: None
max_share_last24: 0.10862556178994692
fills: 24
contracts: 296.87
contracts_per_market_day: 13.04026753365718
markout60: {'n_fills': 24, 'mean_c': 1.3571596995317814, 'p5_c': 0.5, 'p25_c': 0.5, 'p50_c': 1.0, 'share_worse_than_-2.1c': 0.0}
expected_week_usd: 0.0
checks: {'M1 CV <= 50% (hours 24-48)': False, 'no market > 40% of M1 (last 24 h)': True}
verdict: FAIL: M1 CV <= 50% (hours 24-48)
```

## 2026-11-17 11:17 UTC — контейнерные прогоны (запасные данные), 24 ч

### lp1, 5 рынков (утренний отбор)

```
excluded: []
hours: 23.8
windows_2nd_half: 0
excluded: []
downtime_h: 0.0
process_starts: 4
portfolio_m1_usd_day_2nd_half: 0.0
cv: None
max_share_last24: 0.3072581768867003
fills: 7
contracts: 96.50999999999999
contracts_per_market_day: 19.49968276297451
markout60: {'n_fills': 7, 'mean_c': 0.8813076365143508, 'p5_c': 0.5, 'p25_c': 0.5, 'p50_c': 0.5, 'share_worse_than_-2.1c': 0.0}
expected_week_usd: 0.0
checks: {'M1 CV <= 50% (hours 24-48)': False, 'no market > 40% of M1 (last 24 h)': True}
verdict: FAIL: M1 CV <= 50% (hours 24-48)
  Will PAN win the most seats in the 2027 Chihuahua  {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.026, 'comp_levels': None, 'places': 19, 'cancels': 19, 'fills': 0, 'contracts': 0.0, 'markout60_mean_c': None, 'markout60_p5_c': None}
  Will the 10-year Treasury yield hit 5.17% in Novem {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.307, 'comp_levels': None, 'places': 1226, 'cancels': 1222, 'fills': 0, 'contracts': 0.0, 'markout60_mean_c': None, 'markout60_p5_c': None}
  Will Donald Trump publicly insult Hasan Piker by N {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.168, 'comp_levels': None, 'places': 81, 'cancels': 77, 'fills': 1, 'contracts': 1.0, 'markout60_mean_c': 5.0, 'markout60_p5_c': 5.0}
  Will the median home value in the US be between $4 {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.283, 'comp_levels': None, 'places': 617, 'cancels': 609, 'fills': 5, 'contracts': 84.99, 'markout60_mean_c': 0.571, 'markout60_p5_c': 0.5}
  Will the Ornn B200 Index hit $5.00 (LOW) by Decemb {'m1_usd_day_2nd_half': 0.0, 'cv': None, 'share_last24': 0.216, 'comp_levels': None, 'places': 312, 'cancels': 306, 'fills': 1, 'contracts': 10.52, 'markout60_mean_c': 3.0, 'markout60_p5_c': 3.0}
```

### lp1 без Hasan Piker

```
excluded: ['Will Donald Trump publicly insult Hasan Piker by November 30']
hours: 23.8
windows_2nd_half: 0
excluded: ['0x8e0d13a3035e7505f7dd93731f5a8c50511ae843e6dca94b0c024b3578939835']
downtime_h: 0.0
process_starts: 4
portfolio_m1_usd_day_2nd_half: 0.0
cv: None
max_share_last24: 0.3694059801611955
fills: 6
contracts: 95.50999999999999
contracts_per_market_day: 24.12204306149227
markout60: {'n_fills': 6, 'mean_c': 0.838184483300178, 'p5_c': 0.5, 'p25_c': 0.5, 'p50_c': 0.5, 'share_worse_than_-2.1c': 0.0}
expected_week_usd: 0.0
checks: {'M1 CV <= 50% (hours 24-48)': False, 'no market > 40% of M1 (last 24 h)': True}
verdict: FAIL: M1 CV <= 50% (hours 24-48)
```

### lp_shadow, 30 рынков (только распределение markout)

```
excluded: []
hours: 24.2
windows_2nd_half: 0
excluded: []
downtime_h: 0.0
process_starts: 4
portfolio_m1_usd_day_2nd_half: 0.0
cv: None
max_share_last24: 0.10585495641166238
fills: 38
contracts: 452.26
contracts_per_market_day: 14.943019260565423
markout60: {'n_fills': 38, 'mean_c': 1.4082496793879626, 'p5_c': 0.5, 'p25_c': 0.5, 'p50_c': 1.0, 'share_worse_than_-2.1c': 0.0}
expected_week_usd: 0.0
checks: {'M1 CV <= 50% (hours 24-48)': False, 'no market > 40% of M1 (last 24 h)': True}
verdict: FAIL: M1 CV <= 50% (hours 24-48)
```


## 2026-11-18 18:20 UTC — запасные данные, финал (прогоны остановились на 30.3 / 30.7 ч)

Контейнерные прогоны остановились 17 ноября около 17:39 UTC: сессия Claude почти сутки не выполняла
проверки и перезапуски. 48 ч не набрано, критерии по ним не считаются. Официальный итог фазы 1 (VPS) —
`research/lp_phase1_report.md`.

| Прогон | Часов | Окон 24+ ч | M1 часы 24+, $/день | CV часы 24+ | Крупнейшая доля 24 ч | Исполнений | Markout 60 с, среднее / p5 |
|---|---|---|---|---|---|---|---|
| lp1, 5 рынков | 30.3 | 12 | 2.42 | 42% | 36% | 8 | +0.82 / +0.50 ц |
| lp1 без Hasan Piker | 30.3 | 12 | 1.63 | 31% | 47% | 7 | +0.78 / +0.50 ц |
| lp_shadow, 30 рынков | 30.7 | 13 | 20.51 | 34% | 12% | 42 | +1.39 / +0.50 ц |

Распределение markout (взвешено по контрактам) — в отчёте VPS, раздел «Markout». Через 600 с у lp1 17%
контрактов хуже −2.1 ц, у теневого — 4%.
