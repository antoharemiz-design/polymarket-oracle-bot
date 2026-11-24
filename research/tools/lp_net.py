"""Phase 2 research: reward model minus fill cost for the rolling-selection results of lp_levers.py.

    python research/tools/lp_net.py ROLLING.json FILLS.json [--paper 0.25]

ROLLING.json: lp_levers.py --rolling ... --json; FILLS.json: lp_fills.py --conds (the same markets).
Fill cost of a market for the hours it was quoted at size k: contracts per day at k (worst case: every taker
trade fills us first) x hours / 24 x (-(markout after 1 h) - rebate), cents -> dollars. `--paper` scales the
worst-case volume by what the phase 1 paper runs saw (about 1/4 of every taker trade at our price).
Net per $100 of quotes = (mean M1 - fill cost per day) x 100 / average quote capital.
"""
import json
import sys


def main():
    rolling, fills = json.load(open(sys.argv[1])), {r["cond"]: r for r in json.load(open(sys.argv[2]))}
    paper = float(sys.argv[sys.argv.index("--paper") + 1]) if "--paper" in sys.argv else 0.25
    print(f"{'configuration':55s} {'M1/$100':>8s} {'cost wc':>8s} {'net wc':>8s} {'net paper':>9s} {'no fill data':>12s}")
    for r in rolling:
        if not r.get("steps"):
            continue
        hours = r["hours"]
        cost, missing = 0.0, 0
        for c, by_k in r["khours"].items():
            f = fills.get(c)
            if not f or f.get("markout3600_c") is None:
                missing += 1
                continue
            per_contract = max(0.0, -(f["markout3600_c"]) - (f.get("rebate_c") or 0.0)) / 100
            for k, h in by_k.items():
                kk = str(min([1, 2, 3, 5], key=lambda x: abs(x - int(k))))
                cost += f["contracts_day"][kk] * h / 24 * per_contract
        cost_day = cost / (hours / 24) if hours else 0.0
        m1, cap = r["real"], r["cap"]
        f100 = lambda x: x * 100 / cap if cap else float("nan")
        print(f"{r['conf']['name']:55s} {f100(m1):8.2f} {f100(cost_day):8.2f} {f100(m1 - cost_day):8.2f} "
              f"{f100(m1 - paper * cost_day):9.2f} {missing:5d}/{len(r['khours'])}")


if __name__ == "__main__":
    main()
