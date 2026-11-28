"""H12 candidate (28 Nov): decided buckets of daily temperature markets. The qualifying highest (lowest) temperature
of the local day can only rise (fall), so once a published routine METAR puts the rounded running maximum above a
bucket's top, that bucket's YES is worth 0 for certain; the open top bucket ("X or higher") is worth 1 once the
running maximum reaches it (mirror rules for "lowest"). No forecast, only the resolution rules (polybot.wxfair).

    python research/tools/wx_certainty.py WX_DIR TRUTH_DIR [--from D --to D] [--delay 600] [--min-edge 0.02]

For every market: the moment its outcome is decided by the METARs (report time + --delay seconds for publication and
our reaction), then every public taker trade after it at a price that is still wrong by at least --min-edge: a taker
BUYING the dead YES (or selling the certain YES / NO of the other side) at y means a seller could have sold at y a
contract worth 0 (or bought for 1 - y one worth 1). Prints the trades, contracts and dollars left on the table per
day (gross, before fees), how long the mid needs to reach the decided value, and a check of the rule against the
recorded outcome (a decided bucket that resolved the other way would be a data or rule error).
"""
import bisect
import collections
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import wx_adverse as A  # noqa: E402
from polybot import wxfair as W  # noqa: E402


def decided_at(p, reports, delay):
    """(time the bucket's outcome is certain, value 0 or 1) or None."""
    start, end = W.day_bounds(p["date"], p["tz"])
    ext = None
    for ts, raw in reports:
        if not start <= ts < end:
            continue
        v = W.metar_temp(raw)
        if v is None:
            continue
        ext = v if ext is None else (max(ext, v) if p["kind"] == "highest" else min(ext, v))
        k = W.to_unit(ext, p["unit"])
        if p["kind"] == "highest":
            if p["hi"] is not None and k > p["hi"]:
                return ts + delay, 0.0
            if p["hi"] is None and k >= p["lo"]:
                return ts + delay, 1.0
        else:
            if p["lo"] is not None and k < p["lo"]:
                return ts + delay, 0.0
            if p["lo"] is None and k <= p["hi"]:
                return ts + delay, 1.0
    return None


def main():
    wx_dir, truth = sys.argv[1:3]
    arg = lambda k, d: type(d)(sys.argv[sys.argv.index(k) + 1]) if k in sys.argv else d
    d0, d1, delay, min_edge = arg("--from", "0000"), arg("--to", "9999"), arg("--delay", 600), arg("--min-edge", 0.02)
    metar_fn, _ = W.file_sources_metar(truth)
    stats = collections.Counter()
    per_day = collections.defaultdict(float)
    lag = []
    sizes = []
    for f in sorted(glob.glob(os.path.join(wx_dir, "*.json"))):
        x = A.load(f)
        if x is None:
            continue
        d, res, pt, pp = x
        day = (d["meta"].get("event_end") or "")[:10]
        if not d0 <= day <= d1:
            continue
        p = W.parse_market(d["meta"]["question"], pt[0])
        if p is None:
            continue
        stats["markets"] += 1
        start, end = W.day_bounds(p["date"], p["tz"])
        reps = metar_fn([p["icao"]], end + 3600).get(p["icao"], [])
        dec = decided_at(p, reps, delay)
        if dec is None:
            continue
        t_dec, val = dec
        stats["decided"] += 1
        stats["agree" if res == val else "DISAGREE"] += 1
        if res != val:
            continue
        j = bisect.bisect_left(pt, t_dec)                # how long the mid takes to reach the decided value
        reach = next((pt[i] for i in range(j, len(pt)) if abs(pp[i] - val) <= 0.01), None)
        if reach is not None:
            lag.append((reach - t_dec) / 60)
        for ts, sg, y, z in A.maker_fills(d):
            if ts < t_dec:
                continue
            # the maker side that gains: short YES when it is dead (val 0), long YES when certain (val 1)
            gain = (y - val) * (-sg) if val == 0.0 else (val - y) * sg
            if gain >= min_edge:
                stats["trades"] += 1
                stats["contracts"] += z
                stats["usd"] += gain * z
                per_day[day] += gain * z
                sizes.append(z)
    n_days = max(1, len(per_day))
    print(f"markets {stats['markets']}; decided by the METARs before the end of the day: {stats['decided']} "
          f"(outcome agrees {stats['agree']}, disagrees {stats['DISAGREE']})")
    print(f"taker trades still wrong by >= {min_edge:.2f} after the decision + {delay} s: {stats['trades']}, "
          f"{stats['contracts']:.0f} contracts, ${stats['usd']:.2f} gross (${stats['usd'] / n_days:.2f} a day over "
          f"{n_days} days)")
    if lag:
        lag.sort()
        q = lambda a: lag[min(len(lag) - 1, int(a * len(lag)))]
        print(f"minutes until the mid is within 1 c of the decided value: median {q(0.5):.0f}, p75 {q(0.75):.0f}, "
              f"p90 {q(0.9):.0f} (n {len(lag)})")
    if sizes:
        sizes.sort()
        print(f"trade size: median {sizes[len(sizes) // 2]:.0f}, p90 {sizes[int(0.9 * len(sizes))]:.0f}")
    for k in sorted(per_day):
        print(f"   {k}  ${per_day[k]:8.2f}")


if __name__ == "__main__":
    main()
