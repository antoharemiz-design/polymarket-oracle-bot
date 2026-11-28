"""H11: fair value of Polymarket's daily temperature markets (research/PREREG_H11.md).

Polymarket resolves "highest / lowest temperature in <city> on <date>" on the routine METARs of the city's airport
(aviationweather.gov; the station is named in the market description): the highest (lowest) temperature of the
hourly T-group remark (tenths of a degree C; the body's whole degrees when a report has no remark) over the local
calendar day, SPECI reports excluded, rounded half up to a whole degree C, or converted to F and rounded half up.

The fair value of a bucket at time t:
    Y = round(max(M, R))   (highest; min for lowest)
    M = the qualifying value of the routine METARs of the local day published so far (None before the day starts)
    R = the extreme over the remaining hours of the local day: forecast extreme of the latest ECMWF IFS and GFS runs
        that were published by t (fixed publication delays), plus a bias, with a normal error of sigma; bias and
        sigma by lead (hours from t to the end of the local day), calibrated on past days (CALIB)
    P(bucket [lo, hi]) = P(Y <= hi) - P(Y <= lo - 1), with P(max(M, R) < x) = 1[M < x] * Phi((x - mu) / sigma)

Live sources (no keys): aviationweather.gov METAR JSON, Open-Meteo forecast API. Research replays pass the same
data from files (research/tools/wx_truth_collect.py), so the backtest and the bot use one model.
"""
import bisect
import datetime
import json
import math
import re
import time
import urllib.parse
import urllib.request
import zoneinfo

# city of the question -> (ICAO of the resolution station, unit, lat, lon, time zone). From the market descriptions
# (28 Nov 2026); Hong Kong resolves on the Hong Kong Observatory, not a METAR, and is left out.
WX_STATIONS = {
    "amsterdam": ("EHAM", "C", 52.315, 4.79, "Europe/Amsterdam"), "ankara": ("LTAC", "C", 40.128, 32.995, "Europe/Istanbul"),
    "atlanta": ("KATL", "F", 33.6297, -84.4422, "America/New_York"), "austin": ("KAUS", "F", 30.1831, -97.6806, "America/Chicago"),
    "beijing": ("ZBAA", "C", 40.082, 116.603, "Asia/Shanghai"), "buenos-aires": ("SAEZ", "C", -34.822, -58.536, "America/Argentina/Buenos_Aires"),
    "busan": ("RKPK", "C", 35.179, 128.938, "Asia/Seoul"), "cape-town": ("FACT", "C", -33.965, 18.602, "Africa/Johannesburg"),
    "chengdu": ("ZUUU", "C", 30.576, 103.95, "Asia/Shanghai"), "chicago": ("KORD", "F", 41.9602, -87.9316, "America/Chicago"),
    "chongqing": ("ZUCK", "C", 29.718, 106.639, "Asia/Shanghai"), "dallas": ("KDAL", "F", 32.8384, -96.8358, "America/Chicago"),
    "denver": ("KDEN", "F", 39.8466, -104.6562, "America/Denver"), "guangzhou": ("ZGGG", "C", 23.392, 113.307, "Asia/Shanghai"),
    "helsinki": ("EFHK", "C", 60.327, 24.957, "Europe/Helsinki"), "houston": ("KHOU", "F", 29.6458, -95.2821, "America/Chicago"),
    "istanbul": ("LTFM", "C", 41.262, 28.74, "Europe/Istanbul"), "jeddah": ("OEJN", "C", 21.685, 39.166, "Asia/Riyadh"),
    "karachi": ("OPKC", "C", 24.902, 67.139, "Asia/Karachi"), "kuala-lumpur": ("WMKK", "C", 2.747, 101.714, "Asia/Kuala_Lumpur"),
    "london": ("EGLC", "C", 51.505, 0.055, "Europe/London"), "los-angeles": ("KLAX", "F", 33.9382, -118.3866, "America/Los_Angeles"),
    "lucknow": ("VILK", "C", 26.761, 80.889, "Asia/Kolkata"), "madrid": ("LEMD", "C", 40.466, -3.555, "Europe/Madrid"),
    "manila": ("RPLL", "C", 14.507, 121.004, "Asia/Manila"), "mexico-city": ("MMMX", "C", 19.436, -99.072, "America/Mexico_City"),
    "miami": ("KMIA", "F", 25.7881, -80.3169, "America/New_York"), "milan": ("LIMC", "C", 45.631, 8.728, "Europe/Rome"),
    "moscow": ("UUWW", "C", 55.592, 37.261, "Europe/Moscow"), "munich": ("EDDM", "C", 48.348, 11.813, "Europe/Berlin"),
    "nyc": ("KLGA", "F", 40.7794, -73.8803, "America/New_York"), "panama-city": ("MPMG", "C", 8.967, -79.555, "America/Panama"),
    "paris": ("LFPB", "C", 48.967, 2.428, "Europe/Paris"), "qingdao": ("ZSQD", "C", 36.362, 120.087, "Asia/Shanghai"),
    "san-francisco": ("KSFO", "F", 37.6196, -122.3656, "America/Los_Angeles"), "sao-paulo": ("SBGR", "C", -23.432, -46.469, "America/Sao_Paulo"),
    "seattle": ("KSEA", "F", 47.4447, -122.3144, "America/Los_Angeles"), "seoul": ("RKSI", "C", 37.469, 126.451, "Asia/Seoul"),
    "shanghai": ("ZSPD", "C", 31.146, 121.8, "Asia/Shanghai"), "shenzhen": ("ZGSZ", "C", 22.639, 113.803, "Asia/Shanghai"),
    "singapore": ("WSSS", "C", 1.368, 103.982, "Asia/Singapore"), "taipei": ("RCSS", "C", 25.069, 121.552, "Asia/Taipei"),
    "tel-aviv": ("LLBG", "C", 32.011, 34.887, "Asia/Jerusalem"), "tokyo": ("RJTT", "C", 35.553, 139.781, "Asia/Tokyo"),
    "toronto": ("CYYZ", "C", 43.679, -79.629, "America/Toronto"), "warsaw": ("EPWA", "C", 52.163, 20.961, "Europe/Warsaw"),
    "wellington": ("NZWN", "C", -41.331, 174.806, "Pacific/Auckland"), "wuhan": ("ZHHH", "C", 30.783, 114.205, "Asia/Shanghai"),
}
ALIASES = {"new-york-city": "nyc", "new-york": "nyc"}
MODELS = {"ecmwf_ifs025": 7.0, "gfs_seamless": 5.0}     # model -> hours from the run time until it is published
# (bias, sigma) in degrees C of the remaining-hours extreme, by hours from t to the end of the local day. Defaults
# before calibration; research/tools/h11_eval.py --calibrate fits them on the training days and prints the dict.
CALIB = {"highest": [(6, 0.0, 0.8), (12, 0.0, 1.0), (24, 0.0, 1.4), (48, 0.0, 1.9), (999, 0.0, 2.4)],
         "lowest": [(6, 0.0, 0.8), (12, 0.0, 1.0), (24, 0.0, 1.4), (48, 0.0, 1.9), (999, 0.0, 2.4)]}

_Q = re.compile(r"(highest|lowest) temperature in (.+?) be (?:between )?(-?\d+)(?:\s*-\s*(-?\d+))?\s*°\s*([CF])"
                r"(\s+or\s+(?:below|lower|higher|above))?\s+on ([A-Z][a-z]+) (\d{1,2})")
_MONTHS = {m: i for i, m in enumerate(("January", "February", "March", "April", "May", "June", "July", "August",
                                         "September", "October", "November", "December"), 1)}
_TGROUP = re.compile(r"\sT([01])(\d{3})([01])(\d{3})\b")
_BODY = re.compile(r"\s(M?\d{2})/(M?\d{2})?\s")


def parse_market(question, now=None):
    """Weather market of a known station: {kind, city, icao, unit, lat, lon, tz, lo, hi, date (local, ISO)}; lo / hi
    are whole degrees of the market's unit, inclusive, None for an open end. None for any other market."""
    m = _Q.search(question or "")
    if not m or m.group(7) not in _MONTHS:
        return None
    city = m.group(2).split(" (")[0].strip().lower().replace(" ", "-")
    city = ALIASES.get(city, city)
    if city not in WX_STATIONS:
        return None
    icao, unit, lat, lon, tz = WX_STATIONS[city]
    if m.group(5) != unit:
        return None
    a = int(m.group(3))
    b = int(m.group(4)) if m.group(4) is not None else a
    lo, hi = a, b
    tail = (m.group(6) or "").strip()
    if tail.endswith(("below", "lower")):
        lo = None
    elif tail.endswith(("higher", "above")):
        hi = None
    now = time.time() if now is None else now
    z = zoneinfo.ZoneInfo(tz)
    local = datetime.datetime.fromtimestamp(now, z)
    month, day = _MONTHS[m.group(7)], int(m.group(8))
    year = local.year + (1 if month < local.month - 6 else -1 if month > local.month + 6 else 0)
    return {"kind": m.group(1), "city": city, "icao": icao, "unit": unit, "lat": lat, "lon": lon, "tz": tz,
            "lo": lo, "hi": hi, "date": f"{year:04d}-{month:02d}-{day:02d}"}


def day_bounds(date, tz):
    """UTC timestamps of the local day [start, end)."""
    z = zoneinfo.ZoneInfo(tz)
    y, mo, d = (int(x) for x in date.split("-"))
    a = datetime.datetime(y, mo, d, tzinfo=z)
    b = a + datetime.timedelta(days=1)
    return a.timestamp(), datetime.datetime(b.year, b.month, b.day, tzinfo=z).timestamp()


def metar_temp(raw):
    """Temperature of a METAR as the resolution counts it: the T-group (tenths of a degree C), else the body's whole
    degrees; None for a SPECI or a report without a temperature."""
    if not raw or raw.lstrip().startswith("SPECI"):
        return None
    t = _TGROUP.search(" " + raw + " ")
    if t:
        return (-1 if t.group(1) == "1" else 1) * int(t.group(2)) / 10
    b = _BODY.search(" " + raw.split(" RMK")[0] + " ")
    if b:
        v = b.group(1)
        return -int(v[1:]) if v.startswith("M") else int(v)
    return None


def round_half_up(x):
    return math.floor(x + 0.5)


def to_unit(c, unit):
    return round_half_up(c * 9 / 5 + 32 if unit == "F" else c)


def threshold_c(k, unit):
    """The value v in degrees C below which the market's whole-degree value is at most k (round half up)."""
    x = k + 0.5
    return (x - 32) * 5 / 9 if unit == "F" else x


def _phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def calib_at(kind, h_end, calib=None, icao=None):
    """(bias, sigma) for the lead bucket of h_end; the bias of the station when calibrated (calib["station"])."""
    calib = calib or CALIB
    row = next((r for r in calib[kind] if h_end <= r[0]), calib[kind][-1])
    top, bias, sig = row[0], row[1], row[2]
    sb = ((calib.get("station") or {}).get(icao) or {}).get(kind) or {}
    return sb.get(str(top), sb.get(top, bias)), sig


def bucket_prob(kind, unit, lo, hi, m_obs, mu, sigma):
    """P(round(extreme) in [lo, hi]) with extreme = max(M, R) (highest) or min(M, R) (lowest), R ~ N(mu, sigma);
    mu None: no hours left (the extreme is M). M None: no observation yet."""
    def below(k):                                   # P(value <= k) = P(extreme < threshold)
        if k is None:
            return 1.0
        x = threshold_c(k, unit)
        if kind == "highest":
            if m_obs is not None and m_obs >= x:
                return 0.0
            return 1.0 if mu is None else _phi((x - mu) / max(sigma, 1e-6))
        if m_obs is not None and m_obs < x:
            return 1.0
        return 0.0 if mu is None else _phi((x - mu) / max(sigma, 1e-6))
    p_hi = below(hi)
    p_lo = below(lo - 1) if lo is not None else 0.0
    return min(1.0, max(0.0, p_hi - p_lo))


class WxFair:
    """Fair values of weather markets from METARs and forecast runs.

    metar_fn(icaos, now) -> {icao: [(ts, raw)]} of the last ~30 hours; runs_fn(model, lat_lon_list, now) ->
    (run time ts, {icao: [(ts, temp_c)]}) of the latest run published by `now` (model latency in MODELS). Live uses
    the public APIs below; research passes the same from files. Results are cached: METAR every metar_every_s,
    forecasts every runs_every_s."""

    def __init__(self, metar_fn=None, runs_fn=None, calib=None, clock=time.time, metar_every_s=300.0,
                 runs_every_s=3600.0):
        self.metar_fn = metar_fn or live_metars
        self.runs_fn = runs_fn or live_runs
        self.calib = calib or CALIB
        self.clock = clock
        self.metar_every_s, self.runs_every_s = metar_every_s, runs_every_s
        self.obs = {}                                 # icao -> sorted [(ts, temp_c)] of routine reports
        self.fc = {}                                  # model -> (run ts, {icao: [(ts, temp_c)]})
        self.t_metar = self.t_runs = -1e18
        self.errors = 0

    def refresh(self, questions, now=None):
        """Fetch what the markets need (blocking: call from a thread). Returns the parsed markets."""
        now = self.clock() if now is None else now
        ps = [p for p in (parse_market(q, now) for q in questions) if p]
        icaos = sorted({p["icao"] for p in ps})
        if icaos and now - self.t_metar >= self.metar_every_s:
            try:
                got = self.metar_fn(icaos, now)
                for icao, reps in got.items():
                    self.obs[icao] = sorted((ts, v) for ts, raw in reps if ts <= now
                                            for v in [metar_temp(raw)] if v is not None)
                self.t_metar = now
            except Exception:
                self.errors += 1
        if icaos and now - self.t_runs >= self.runs_every_s:
            pts = sorted({(p["icao"], p["lat"], p["lon"]) for p in ps})
            for model in MODELS:
                try:
                    self.fc[model] = self.runs_fn(model, pts, now)
                except Exception:
                    self.errors += 1
            self.t_runs = now
        return ps

    def value(self, question, now=None):
        """{p, mu, sigma, m_obs, h_end, models} for the market at `now`, or None (not a weather market or no data)."""
        now = self.clock() if now is None else now
        p = parse_market(question, now)
        if p is None:
            return None
        start, end = day_bounds(p["date"], p["tz"])
        obs = [v for ts, v in self.obs.get(p["icao"], []) if start <= ts < min(end, now + 1)]
        ext = max if p["kind"] == "highest" else min
        m_obs = ext(obs) if obs else None
        mus = []
        for model, (run_ts, series) in self.fc.items():
            pts = [v for ts, v in series.get(p["icao"], []) if max(start, now) <= ts < end]
            if pts:
                mus.append(ext(pts))
        h_end = (end - now) / 3600
        if h_end <= 0:
            mu = None
        elif not mus:
            return None
        else:
            bias, sigma = calib_at(p["kind"], h_end, self.calib, p["icao"])
            mu = sum(mus) / len(mus) + (bias if p["kind"] == "highest" else -bias)
        bias, sigma = calib_at(p["kind"], max(h_end, 0.0), self.calib, p["icao"])
        prob = bucket_prob(p["kind"], p["unit"], p["lo"], p["hi"], m_obs, mu, sigma)
        return {"p": prob, "mu": mu, "sigma": sigma, "m_obs": m_obs, "h_end": h_end, "models": len(mus),
                "obs": len(obs)}


# ---- live sources ---------------------------------------------------------------------------------------------

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "polybot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _ts(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def live_metars(icaos, now):
    """aviationweather.gov: every report of the last 30 hours (4 stations a request: the API returns at most ~400)."""
    out = {}
    for k in range(0, len(icaos), 4):
        q = {"ids": ",".join(icaos[k:k + 4]), "format": "json", "hours": 30}
        for x in _get("https://aviationweather.gov/api/data/metar?" + urllib.parse.urlencode(q)):
            out.setdefault(x["icaoId"], []).append((_ts(x["reportTime"]), x.get("rawOb") or ""))
    return out


def live_runs(model, pts, now):
    """Open-Meteo forecast API: the model's latest run, hourly 2 m temperature for 3 days, all stations at once."""
    q = {"latitude": ",".join(str(p[1]) for p in pts), "longitude": ",".join(str(p[2]) for p in pts),
         "hourly": "temperature_2m", "models": model, "forecast_days": 3, "past_days": 1, "timezone": "GMT"}
    d = _get("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(q))
    d = d if isinstance(d, list) else [d]
    series = {}
    for p, x in zip(pts, d):
        h = x["hourly"]
        series[p[0]] = [(_ts(t + ":00Z" if len(t) == 16 else t), v) for t, v in zip(h["time"], h["temperature_2m"])
                        if v is not None]
    return now, series


def file_sources(truth_dir):
    """Research: METARs and archived runs written by research/tools/wx_truth_collect.py, served as they were known."""
    import glob
    import os
    reps = {}
    for line in open(os.path.join(truth_dir, "metar.jsonl")):
        x = json.loads(line)
        reps.setdefault(x["icao"], []).append((_ts(x["time"]), x.get("raw") or ""))
    for v in reps.values():
        v.sort()
    keys = {k: [r[0] for r in v] for k, v in reps.items()}
    runs = {}
    for path in glob.glob(os.path.join(truth_dir, "runs", "*.json")):
        name = os.path.basename(path)[:-5]
        model, run = name.rsplit("_", 1)
        runs.setdefault(model, []).append((_ts(run + ":00Z"), path))
    for v in runs.values():
        v.sort()
    cache = {}

    def metar_fn(icaos, now):
        out = {}
        for i in icaos:
            ts = keys.get(i, [])
            a, b = bisect.bisect_left(ts, now - 30 * 3600), bisect.bisect_right(ts, now)
            out[i] = reps[i][a:b]
        return out

    def runs_fn(model, pts, now):
        lst = runs.get(model, [])
        j = bisect.bisect_right([t for t, _ in lst], now - MODELS[model] * 3600) - 1
        if j < 0:
            raise LookupError("no run published yet")
        run_ts, path = lst[j]
        if path not in cache:
            d = json.load(open(path))
            cache[path] = {k: [(_ts(t + ":00Z"), v) for t, v in zip(x["time"], x["t"]) if v is not None]
                           for k, x in d.items()}
        return run_ts, cache[path]
    return metar_fn, runs_fn


def prev_sources(truth_dir):
    """Research: METARs as file_sources, forecasts from the previous-runs files (research/tools/wx_truth_collect.py:
    every hour's forecast as issued 1..4 days before it). For a decision at t, the forecast of hour h is the one
    issued at least the model's publication delay before t: previous_day d = ceil((h - t + delay) / 24 h), 1..4.
    That is older than the latest run a live bot would have, so the replay is conservative for the model."""
    import os
    metar_fn, _ = file_sources_metar(truth_dir)
    cache = {}

    def load(icao):
        if icao not in cache:
            path = os.path.join(truth_dir, "prev", f"{icao}.json")
            if not os.path.exists(path):
                cache[icao] = None
            else:
                h = json.load(open(path))
                cache[icao] = ([_ts(t + ":00Z") for t in h["time"]], h)
        return cache[icao]

    def runs_fn(model, pts, now):
        out = {}
        for icao, lat, lon in pts:
            x = load(icao)
            if x is None:
                continue
            times, h = x
            ser = []
            for i, t in enumerate(times):
                if t < now - 6 * 3600 or t > now + 4 * 86400:
                    continue
                d = min(4, max(1, math.ceil((t - now + MODELS[model] * 3600) / 86400)))
                v = (h.get(f"temperature_2m_previous_day{d}_{model}") or [None] * len(times))[i]
                if v is not None:
                    ser.append((t, v))
            out[icao] = ser
        if not any(out.values()):
            raise LookupError("no forecast")
        return now, out
    return metar_fn, runs_fn


def file_sources_metar(truth_dir):
    import os
    reps = {}
    for line in open(os.path.join(truth_dir, "metar.jsonl")):
        x = json.loads(line)
        reps.setdefault(x["icao"], []).append((_ts(x["time"]), x.get("raw") or ""))
    for v in reps.values():
        v.sort()
    keys = {k: [r[0] for r in v] for k, v in reps.items()}

    def metar_fn(icaos, now):
        out = {}
        for i in icaos:
            ts = keys.get(i, [])
            a, b = bisect.bisect_left(ts, now - 30 * 3600), bisect.bisect_right(ts, now)
            out[i] = reps.get(i, [])[a:b]
        return out
    return metar_fn, None
