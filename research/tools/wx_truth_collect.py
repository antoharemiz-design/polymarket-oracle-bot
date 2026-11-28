"""H11 data (28 Nov): the resolution truth and the forecasts that were available, for every station of the daily
temperature markets.

    python research/tools/wx_truth_collect.py STATIONS.json OUT_DIR --from 2026-11-03 --to 2026-11-28

STATIONS.json: city -> {icao, unit, lat, lon} (the ICAO comes from the market description: Polymarket resolves on the
routine METARs of aviationweather.gov for that station). Writes:
  OUT_DIR/metar.jsonl        every METAR / SPECI of aviationweather.gov for the stations (icao, time, raw, temp)
  OUT_DIR/runs/<model>_<run>.json   the archived forecast run (Open-Meteo single-runs API): hourly 2 m temperature
                                    of every station for 3 days from the run time; models ecmwf_ifs025 and
                                    gfs_seamless, runs 00z and 12z
Existing files are kept, so the collection resumes. Public endpoints, no keys.
"""
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MODELS = ("ecmwf_ifs025", "gfs_seamless")


class RateLimited(Exception):
    pass


def get(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research", "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 429:                             # the shared daily quota of Open-Meteo: back off longer
                if i == tries - 1:
                    raise RateLimited(url[:60])
                time.sleep(120)
                continue
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def days(a, b):
    t = time.mktime(time.strptime(a, "%Y-%m-%d")) - time.timezone
    end = time.mktime(time.strptime(b, "%Y-%m-%d")) - time.timezone
    while t <= end:
        yield time.strftime("%Y-%m-%d", time.gmtime(t))
        t += 86400


def main():
    st = {c: v for c, v in json.load(open(sys.argv[1])).items() if v.get("icao") and v.get("lat") is not None}
    out = sys.argv[2]
    arg = lambda k, d: sys.argv[sys.argv.index(k) + 1] if k in sys.argv else d
    d0, d1 = arg("--from", "2026-11-03"), arg("--to", "2026-11-28")
    os.makedirs(os.path.join(out, "runs"), exist_ok=True)
    ids = sorted({v["icao"] for v in st.values()})
    mpath = os.path.join(out, "metar.jsonl")
    have = set()
    if os.path.exists(mpath):
        for line in open(mpath):
            x = json.loads(line)
            have.add((x["icao"], x["time"]))
    with open(mpath, "a") as f:
        for day in (days(d0, d1) if "--no-metar" not in sys.argv else []):
            for k in range(0, len(ids), 4):               # the API answers at most ~400 reports per request
                q = {"ids": ",".join(ids[k:k + 4]), "format": "json", "date": f"{day}T23:59:59Z", "hours": 26}
                for x in get("https://aviationweather.gov/api/data/metar?" + urllib.parse.urlencode(q)):
                    key = (x["icaoId"], x["reportTime"])
                    if key in have:
                        continue
                    have.add(key)
                    f.write(json.dumps({"icao": x["icaoId"], "time": x["reportTime"], "raw": x.get("rawOb"),
                                        "temp": x.get("temp")}) + "\n")
                time.sleep(0.2)
            print(f"metar {day}: {len(have)} reports", flush=True)
    lat = ",".join(str(st[c]["lat"]) for c in st)
    lon = ",".join(str(st[c]["lon"]) for c in st)
    order = list(st)
    models = arg("--models", ",".join(MODELS)).split(",")
    for model in models:                               # one model over all days first (the quota may run out)
      for day in days(d0, d1):
        for hh in ("00", "12"):
            if True:
                path = os.path.join(out, "runs", f"{model}_{day}T{hh}.json")
                if os.path.exists(path):
                    continue
                q = {"latitude": lat, "longitude": lon, "hourly": "temperature_2m", "run": f"{day}T{hh}:00",
                     "models": model, "forecast_days": 3}
                try:
                    d = get("https://single-runs-api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q))
                except Exception as e:
                    print(f"{model} {day}T{hh} failed: {e!r:.100}", flush=True)
                    continue
                d = d if isinstance(d, list) else [d]
                res = {st[c]["icao"]: {"time": x["hourly"]["time"], "t": x["hourly"]["temperature_2m"]}
                       for c, x in zip(order, d)}
                json.dump(res, open(path, "w"))
                time.sleep(1.0)
        print(f"runs {model} {day}", flush=True)


if __name__ == "__main__":
    main()
