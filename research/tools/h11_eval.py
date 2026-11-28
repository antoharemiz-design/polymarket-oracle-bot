"""H11 (research/PREREG_H11.md): the weather fair value (polybot/wxfair.py) against the market, on resolved markets.

    python research/tools/h11_eval.py calibrate WX_DIR TRUTH_DIR --from D --to D
    python research/tools/h11_eval.py evaluate  WX_DIR TRUTH_DIR --from D --to D [--calib CALIB.json] [--out OUT.json]

WX_DIR: markets of research/tools/wx_collect.py (question, outcome, 1-minute mid, taker tape); TRUTH_DIR: METARs and
archived forecast runs of research/tools/wx_truth_collect.py. Dates select markets by the end of their event (UTC).

calibrate: for every station day of the markets and every decision time on a 30-minute grid from 72 h before the end
of the local day to its end: the actual extreme of the routine METARs over the remaining hours of the local day
against the forecast extreme over the same hours (mean of the latest published ECMWF IFS and GFS runs). Fits bias and
sigma per lead bucket (hours to the end of the local day) and prints them as a CALIB dict (JSON).

evaluate: at every 30 minutes of a market's life while its mid is in [0.02, 0.98]: the model's p, the mid, the outcome.
  - Brier score of the model, of the mid, by lead bucket;
  - edge = p - mid: for |edge| above thresholds, the outcome minus the mid in the edge's direction (cents per contract:
    what buying on the model's side at the mid would have made at resolution) and the mid's move over the next hour in
    the same direction (what an LP on that side gains or loses while holding);
  - the maker fills of the tape inside the reward zone (as wx_adverse2): markout at 60 min when the maker's side agrees
    with the model (edge >= 0.03 for a maker long YES, <= -0.03 for a maker short) against when it disagrees.
"""
import bisect
import collections
import glob
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import wx_adverse as A  # noqa: E402
from polybot import wxfair as W  # noqa: E402

LEADS = [6, 12, 24, 48, 999]
ONLY = set()                                           # stations with forecasts (--stations-with-data)
STEP = 1800


def lead_bucket(h):
    for top in LEADS:
        if h <= top:
            return top
    return LEADS[-1]


def markets(wx_dir, d0, d1):
    for f in sorted(glob.glob(os.path.join(wx_dir, "*.json"))):
        x = A.load(f)
        if x is None:
            continue
        d, res, pt, pp = x
        if not d0 <= (d["meta"].get("event_end") or "")[:10] <= d1:
            continue
        q = d["meta"]["question"]
        p = W.parse_market(q, pt[0])
        if p is None:
            continue
        if ONLY and p["icao"] not in ONLY:
            continue
        yield d, res, pt, pp, q, p


def model_at(fair, q, t):
    fair.t_metar = fair.t_runs = -1e18                 # replay: the sources answer as of t
    fair.refresh([q], t)
    return fair.value(q, t)


def sources(truth):
    return W.prev_sources(truth) if "--runs" not in sys.argv else W.file_sources(truth)


def calibrate(wx_dir, truth, d0, d1):
    metar_fn, runs_fn = sources(truth)
    days = {}
    for d, res, pt, pp, q, p in markets(wx_dir, d0, d1):
        days.setdefault((p["kind"], p["icao"], p["date"]), p)
    res_by = collections.defaultdict(list)
    for (kind, icao, date), p in days.items():
        start, end = W.day_bounds(date, p["tz"])
        reps = metar_fn([icao], end + 3600).get(icao, [])
        allobs = [(ts, v) for ts, raw in reps for v in [W.metar_temp(raw)] if v is not None and start <= ts < end]
        if len(allobs) < 12:
            continue
        ext = max if kind == "highest" else min
        t = end - 72 * 3600
        while t < end - 1800:
            rem = [v for ts, v in allobs if ts >= t]
            mus = []
            for model in W.MODELS:
                try:
                    run_ts, series = runs_fn(model, [(icao, p["lat"], p["lon"])], t)
                except LookupError:
                    continue
                pts = [v for ts, v in series.get(icao, []) if max(start, t) <= ts < end]
                if pts:
                    mus.append(ext(pts))
            if rem and mus:
                r = ext(rem) - sum(mus) / len(mus)
                res_by[(kind, lead_bucket((end - t) / 3600))].append((icao, r if kind == "highest" else -r))
            t += STEP
    # bias per station and lead bucket, shrunk toward the bucket's mean (k pseudo-observations); sigma per lead bucket
    # from the residuals minus their station's bias
    K = 20.0
    calib = {"station": {}}
    for kind in ("highest", "lowest"):
        rows = []
        for top in LEADS:
            xs = res_by.get((kind, top), [])
            if len(xs) < 30:
                rows.append((top, 0.0, W.calib_at(kind, top)[1]))
                continue
            g = sum(r for _, r in xs) / len(xs)
            per = collections.defaultdict(list)
            for icao, r in xs:
                per[icao].append(r)
            dev = []
            for icao, rs in per.items():
                b = (sum(rs) + K * g) / (len(rs) + K)
                calib["station"].setdefault(icao, {}).setdefault(kind, {})[str(top)] = round(b, 3)
                dev += [r - b for r in rs]
            sd = math.sqrt(sum(x * x for x in dev) / max(1, len(dev) - 1))
            raw = math.sqrt(sum((r - g) ** 2 for _, r in xs) / (len(xs) - 1))
            rows.append((top, round(g, 3), round(sd, 3)))
            print(f"{kind:8s} lead <= {top:4d} h: n {len(xs):6d}  bias {g:+.2f} C  sigma {sd:.2f} C (without station "
                  f"bias {raw:.2f})  stations {len(per)}")
        calib[kind] = rows
    print(json.dumps(calib))
    return calib


def evaluate(wx_dir, truth, d0, d1, calib, out=None):
    metar_fn, runs_fn = sources(truth)
    fair = W.WxFair(metar_fn, runs_fn, calib=calib)
    brier = collections.defaultdict(lambda: [0, 0.0, 0.0])
    edges = collections.defaultdict(lambda: [0, 0.0, 0.0, 0])
    fills = collections.defaultdict(lambda: [0.0, 0.0])
    series = {}
    n_mk = 0
    for d, res, pt, pp, q, p in markets(wx_dir, d0, d1):
        start, end = W.day_bounds(p["date"], p["tz"])
        n_mk += 1
        grid = []
        t = max(pt[0], end - 72 * 3600)
        t = t - t % STEP + STEP
        while t <= min(pt[-1], end + 3600):
            m = A.mid_at(t, pt, pp, t)
            v = model_at(fair, q, t)
            if v is not None and m is not None:
                grid.append((t, v["p"]))
                if 0.02 <= m <= 0.98:
                    lb = lead_bucket(max(0.0, (end - t) / 3600))
                    b = brier[lb]
                    b[0] += 1; b[1] += (v["p"] - res) ** 2; b[2] += (m - res) ** 2
                    e = v["p"] - m
                    m1 = A.mid_at(t, pt, pp, t + 3600)
                    for thr in (0.03, 0.05, 0.10, 0.20):
                        if abs(e) >= thr:
                            sg = 1 if e > 0 else -1
                            x = edges[(thr, lb)]
                            x[0] += 1; x[1] += sg * (res - m)
                            if m1 is not None and pt[-1] >= t + 3600:
                                x[2] += sg * (m1 - m); x[3] += 1
            t += STEP
        series[d["meta"]["conditionId"]] = grid
        gt = [g[0] for g in grid]
        for ts, sg, y, z in A.maker_fills(d):          # tape maker fills inside the zone: with or against the model
            m0 = A.mid_at(ts, pt, pp, ts - 60)
            if m0 is None or not 0.10 <= m0 <= 0.90 or abs(y - m0) > 0.045 or pt[-1] < ts + 3600:
                continue
            j = bisect.bisect_right(gt, ts) - 1
            if j < 0:
                continue
            e = grid[j][1] - m0
            side = "agrees" if e * sg >= 0.03 else "against" if e * sg <= -0.03 else "neutral"
            w = min(z, 20.0)
            fills[side][0] += w
            fills[side][1] += sg * (A.mid_at(ts, pt, pp, ts + 3600) - y) * w
    print(f"{n_mk} markets, {sum(b[0] for b in brier.values())} decision points")
    print(f"\nBrier score by lead (hours to the end of the local day): model vs market mid (lower is better)")
    for lb in LEADS:
        b = brier.get(lb)
        if b and b[0]:
            print(f"  <= {lb:4d} h  n {b[0]:6d}  model {b[1] / b[0]:.4f}  market {b[2] / b[0]:.4f}")
    tot = [sum(b[i] for b in brier.values()) for i in range(3)]
    print(f"  all       n {tot[0]:6d}  model {tot[1] / tot[0]:.4f}  market {tot[2] / tot[0]:.4f}")
    print(f"\nedge = p - mid: outcome - mid and the next hour's mid move, in the edge's direction (cents per contract)")
    rows = []
    for thr in (0.03, 0.05, 0.10, 0.20):
        for lb in LEADS + ["all"]:
            if lb == "all":
                xs = [edges[(thr, l)] for l in LEADS]
                x = [sum(v[i] for v in xs) for i in range(4)]
            else:
                x = edges.get((thr, lb), [0, 0, 0, 0])
            if x[0]:
                rows.append({"thr": thr, "lead": lb, "n": x[0], "outcome_c": round(100 * x[1] / x[0], 2),
                             "next_hour_c": round(100 * x[2] / x[3], 2) if x[3] else None})
                print(f"  |edge| >= {thr:.2f}  lead <= {lb!s:>4s}  n {x[0]:6d}  outcome {100 * x[1] / x[0]:+6.2f}  "
                      f"next hour {100 * x[2] / x[3] if x[3] else float('nan'):+6.2f}")
    print(f"\ntape maker fills inside the reward zone, markout at 60 min, by the model's view of the maker's side")
    for k in ("agrees", "neutral", "against"):
        w, s = fills[k]
        if w:
            print(f"  {k:8s} contracts {w:9.0f}  m60 {100 * s / w:+6.2f} c")
    if out:
        json.dump({"markets": n_mk, "brier": {str(k): v for k, v in brier.items()}, "edges": rows,
                   "fills": {k: v for k, v in fills.items()}, "series": series}, open(out, "w"))


def main():
    mode, wx_dir, truth = sys.argv[1:4]
    if "--stations-with-data" in sys.argv:              # stage 2a: only the stations whose forecasts were collected
        ONLY.update(f[:-5] for f in os.listdir(os.path.join(truth, "prev")))
    arg = lambda k, dflt: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else dflt
    d0, d1 = arg("--from", "0000"), arg("--to", "9999")
    if mode == "calibrate":
        c = calibrate(wx_dir, truth, d0, d1)
        if arg("--save", ""):
            json.dump(c, open(arg("--save", ""), "w"))
    else:
        cp = arg("--calib", "")
        calib = json.load(open(cp)) if cp else None
        if calib:
            calib = {k: ([tuple(r) for r in v] if k != "station" else v) for k, v in calib.items()}
        evaluate(wx_dir, truth, d0, d1, calib, arg("--out", ""))


if __name__ == "__main__":
    main()
