# Данные контейнерных (запасных) прогонов фазы 1

`lp_container_runs.tgz` — копия журналов запасных прогонов из контейнера Claude. Без этой копии они
пропадут вместе с контейнером. Финальная версия: прогоны остановились 17 ноября около 17:39 UTC
на 30.3 ч (lp1) и 30.7 ч (lp_shadow).

- `lp1/` — 5 рынков утреннего отбора: `paper.jsonl` (журнал `polybot/lp.py`), `markets.json`, `select.log`
- `lp_shadow/` — 30 рынков, только для распределения markout
- `health.log` — проверки здоровья и перезапуски

Отчёт по журналу:

```bash
mkdir -p /tmp/lp && tar -xzf research/data/lp_container_runs.tgz -C /tmp/lp
```

```bash
LP_LP_DIR=/tmp/lp/lp1 python -m polybot lp-phase1 paper
```
