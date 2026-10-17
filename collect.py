#!/usr/bin/env python3
"""Collect indoor (AirGradient local API) and outdoor (PurpleAir, NWS, AirNow) data into SQLite.

Meant to run once a minute (see launchd/). Indoor is sampled every run; each outdoor
source is fetched only when its interval has elapsed, so one schedule drives everything.
"""
import fcntl
import json
import logging
import math
import os
import re
import sqlite3
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if not (ROOT / "config.json").exists():
    raise SystemExit("config.json not found: copy config.example.json to config.json and edit it (see README).")
CONFIG = json.loads((ROOT / "config.json").read_text())
DB_PATH = ROOT / CONFIG["db_path"]
UA = "airq-local-dashboard (personal use)"

# Some Python installs (python.org, some Homebrew setups) find no CA bundle;
# fall back to the macOS system bundle.
SSL_CTX = ssl.create_default_context()
if not SSL_CTX.get_ca_certs() and os.path.exists("/etc/ssl/cert.pem"):
    SSL_CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")

log = logging.getLogger("airq")

SCHEMA = """
CREATE TABLE IF NOT EXISTS indoor (
    ts INTEGER PRIMARY KEY,      -- unix seconds, UTC, floored to the minute
    pm25 REAL,                   -- EPA-corrected (pm02Compensated)
    pm25_raw REAL,
    pm10 REAL,
    co2 REAL,
    temp REAL,
    rh REAL,
    tvoc_index REAL,
    nox_index REAL
);
CREATE TABLE IF NOT EXISTS outdoor (
    ts INTEGER NOT NULL,
    source TEXT NOT NULL,        -- purpleair | nws | airnow:<site id>
    metric TEXT NOT NULL,        -- pm25, temp, dewpoint, rh, ...
    value REAL,
    PRIMARY KEY (ts, source, metric)
);
CREATE TABLE IF NOT EXISTS forecast (
    ts INTEGER NOT NULL,         -- hour start, unix seconds, UTC; replaced wholesale on each fetch
    metric TEXT NOT NULL,        -- temp
    value REAL,
    PRIMARY KEY (ts, metric)
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


def http_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.load(r)


def http_text(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read().decode("latin-1")


def get_state(db, key, default=None):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(db, key, value):
    db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, str(value)))


def due(db, key, minutes):
    last = float(get_state(db, key, 0))
    return time.time() - last >= minutes * 60 - 5


def put_outdoor(db, ts, source, metrics):
    db.executemany(
        "INSERT OR REPLACE INTO outdoor VALUES (?, ?, ?, ?)",
        [(int(ts), source, k, v) for k, v in metrics.items() if v is not None],
    )


# ---------------------------------------------------------------- indoor

def collect_indoor(db):
    data = None
    for host in (CONFIG["monitor_host"], CONFIG.get("monitor_fallback_ip")):
        if not host:
            continue
        try:
            data = http_json(f"http://{host}/measures/current", timeout=8)
            break
        except Exception as e:
            log.warning("indoor: %s unreachable (%s)", host, e)
    if data is None:
        return
    ts = int(time.time()) // 60 * 60
    db.execute(
        "INSERT OR REPLACE INTO indoor VALUES (?,?,?,?,?,?,?,?,?)",
        (
            ts,
            data.get("pm02Compensated", data.get("pm02")),
            data.get("pm02"),
            data.get("pm10"),
            data.get("rco2"),
            data.get("atmpCompensated", data.get("atmp")),
            data.get("rhumCompensated", data.get("rhum")),
            data.get("tvocIndex"),
            data.get("noxIndex"),
        ),
    )


# ---------------------------------------------------------------- PurpleAir

def epa_correction(cf1, rh):
    """US-wide PurpleAir correction (Barkjohn et al.), extended for smoke.

    Same algorithm the AirGradient applies to its own pm02 ("epa_2021"), so indoor
    and outdoor PM2.5 are on the same footing.
    """
    if cf1 < 30:
        v = 0.524 * cf1 - 0.0862 * rh + 5.75
    elif cf1 < 50:
        w = cf1 / 20 - 1.5
        v = (0.786 * w + 0.524 * (1 - w)) * cf1 - 0.0862 * rh + 5.75
    elif cf1 < 210:
        v = 0.786 * cf1 - 0.0862 * rh + 5.75
    elif cf1 < 260:
        w = cf1 / 50 - 4.2
        v = ((0.69 * w + 0.786 * (1 - w)) * cf1 - 0.0862 * rh * (1 - w)
             + 2.966 * w + 5.75 * (1 - w) + 8.84e-4 * cf1 ** 2 * w)
    else:
        v = 2.966 + 0.69 * cf1 + 8.84e-4 * cf1 ** 2
    return max(v, 0.0)


def collect_purpleair(db):
    key = CONFIG.get("purpleair_api_key")
    if not key or not due(db, "purpleair_last", CONFIG.get("purpleair_interval_min", 15)):
        return
    lat, lon = CONFIG["location"]["lat"], CONFIG["location"]["lon"]
    dlat = CONFIG["purpleair_radius_km"] / 111.0
    dlon = dlat / math.cos(math.radians(lat))
    params = {
        "fields": "pm2.5_cf_1_a,pm2.5_cf_1_b,humidity",
        "location_type": 0,  # outside only
        "max_age": 1800,     # skip sensors silent for >30 min
        "nwlat": lat + dlat, "selat": lat - dlat,
        "nwlng": lon - dlon, "selng": lon + dlon,
    }
    url = "https://api.purpleair.com/v1/sensors?" + urllib.parse.urlencode(params)
    resp = http_json(url, headers={"X-API-Key": key})
    fields = resp["fields"]
    values = []
    for row in resp["data"]:
        r = dict(zip(fields, row))
        a, b, rh = r.get("pm2.5_cf_1_a"), r.get("pm2.5_cf_1_b"), r.get("humidity")
        if rh is None or (a is None and b is None):
            continue
        if a is not None and b is not None:
            # EPA QA: drop sensors whose two laser channels disagree.
            if abs(a - b) >= 5 and abs(a - b) / ((a + b) / 2) > 0.7:
                continue
            cf1 = (a + b) / 2
        else:
            cf1 = a if a is not None else b
        values.append(epa_correction(cf1, rh))
    set_state(db, "purpleair_last", time.time())
    if not values:
        log.warning("purpleair: no usable sensors in radius")
        return
    put_outdoor(db, resp.get("data_time_stamp", time.time()), "purpleair", {
        "pm25": round(statistics.median(values), 2),
        "pm25_min": round(min(values), 2),
        "pm25_max": round(max(values), 2),
        "n_sensors": len(values),
    })


# ---------------------------------------------------------------- NWS

def collect_nws(db):
    if not due(db, "nws_last", 10):
        return
    station = CONFIG["nws_station"]
    last = db.execute("SELECT MAX(ts) FROM outdoor WHERE source='nws'").fetchone()[0]
    start = (datetime.fromtimestamp(last, timezone.utc) if last
             else datetime.now(timezone.utc) - timedelta(days=7))
    url = (f"https://api.weather.gov/stations/{station}/observations?"
           + urllib.parse.urlencode({"start": start.isoformat(timespec="seconds")}))
    feats = []
    for _ in range(10):  # paginated, newest first, 500 per page
        page = http_json(url, headers={"Accept": "application/geo+json"})
        feats += page["features"]
        url = (page.get("pagination") or {}).get("next")
        if not page["features"] or not url:
            break

    def val(p, k, scale=1.0):
        v = (p.get(k) or {}).get("value")
        return None if v is None else round(v * scale, 2)

    for f in feats:
        p = f["properties"]
        ts = datetime.fromisoformat(p["timestamp"]).timestamp()
        put_outdoor(db, ts, "nws", {
            "temp": val(p, "temperature"),
            "dewpoint": val(p, "dewpoint"),
            "rh": val(p, "relativeHumidity"),
            "wind_kmh": val(p, "windSpeed"),
            "pressure_hpa": val(p, "barometricPressure", 0.01),
        })
    set_state(db, "nws_last", time.time())


def iso_hours(duration):
    """Hours in an ISO 8601 duration like PT3H or P1DT2H (NWS grid validTime)."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?)?", duration)
    return int(m[1] or 0) * 24 + int(m[2] or 0) if m else 1


def collect_forecast(db):
    """NWS gridpoint forecast for the configured location, expanded to hourly values."""
    if not due(db, "forecast_last", 60):
        return
    grid = get_state(db, "nws_grid_url")
    if not grid:
        loc = CONFIG["location"]
        grid = http_json(f"https://api.weather.gov/points/{loc['lat']:.4f},{loc['lon']:.4f}",
                         headers={"Accept": "application/geo+json"})["properties"]["forecastGridData"]
        set_state(db, "nws_grid_url", grid)
    temp = http_json(grid, headers={"Accept": "application/geo+json"})["properties"]["temperature"]
    if temp["uom"] != "wmoUnit:degC":
        raise ValueError(f"unexpected forecast unit {temp['uom']}")
    rows = []
    for v in temp["values"]:
        if v["value"] is None:
            continue
        start, dur = v["validTime"].split("/")
        ts = int(datetime.fromisoformat(start).timestamp())
        rows += [(ts + h * 3600, "temp", round(v["value"], 2)) for h in range(iso_hours(dur))]
    db.execute("DELETE FROM forecast")
    db.executemany("INSERT OR REPLACE INTO forecast VALUES (?, ?, ?)", rows)
    set_state(db, "forecast_last", time.time())


# ---------------------------------------------------------------- AirNow

AIRNOW_PARAMS = {"PM2.5": "pm25", "OZONE": "o3", "PM10": "pm10"}


def collect_airnow(db, backfill_hours=72):
    """Keyless EPA AirNow hourly files. Each hour's file is re-fetched for 3 hours
    (late data lands in it), then marked done."""
    if not due(db, "airnow_last", 20):
        return
    sites = set(CONFIG["airnow_sites"])
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for h in range(backfill_hours, 0, -1):
        hour = now - timedelta(hours=h)
        tag = hour.strftime("%Y%m%d%H")
        if get_state(db, f"airnow:{tag}"):
            continue
        url = (f"https://files.airnowtech.org/airnow/{hour:%Y}/{hour:%Y%m%d}/"
               f"HourlyData_{tag}.dat")
        try:
            text = http_text(url)
        except Exception as e:
            log.info("airnow: %s not available (%s)", tag, e)
            continue
        for line in text.splitlines():
            f = line.split("|")
            if len(f) < 8 or f[2] not in sites or f[5] not in AIRNOW_PARAMS:
                continue
            ts = datetime.strptime(f"{f[0]} {f[1]}", "%m/%d/%y %H:%M").replace(
                tzinfo=timezone.utc).timestamp()
            try:
                v = float(f[7])
            except ValueError:
                continue
            put_outdoor(db, ts, f"airnow:{f[2]}", {AIRNOW_PARAMS[f[5]]: v})
        if h > 3:
            set_state(db, f"airnow:{tag}", 1)
        db.commit()
    set_state(db, "airnow_last", time.time())


# ---------------------------------------------------------------- history

AG_API = "https://api.airgradient.com/public/api/v1"


def ag_location_id(db, token):
    cached = get_state(db, "ag_location_id")
    if cached:
        return cached
    serial = CONFIG["monitor_host"].removeprefix("airgradient_").split(".")[0]
    for loc in http_json(f"{AG_API}/locations/measures/current?token={token}"):
        if loc.get("serialno") == serial:
            set_state(db, "ag_location_id", loc["locationId"])
            return str(loc["locationId"])
    raise RuntimeError(f"monitor {serial} not found in this AirGradient place")


def ag_import_window(db, token, loc, start, end):
    """Import AirGradient cloud history (5-min or hourly averages) for [start, end).
    Never overwrites minute data logged locally. Returns rows received."""
    fmt = "%Y%m%dT%H%M%SZ"
    url = f"{AG_API}/locations/{loc}/measures/past?" + urllib.parse.urlencode(
        {"token": token, "from": start.strftime(fmt), "to": end.strftime(fmt)})
    try:
        rows = http_json(url, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # "no data available"
            return 0
        raise
    first = lambda r, *ks: next((r[k] for k in ks if r.get(k) is not None), None)
    db.executemany("INSERT OR IGNORE INTO indoor VALUES (?,?,?,?,?,?,?,?,?)", [
        (int(datetime.fromisoformat(r["timestamp"]).timestamp()) // 60 * 60,
         first(r, "pm02_corrected", "pm02"), r.get("pm02"), first(r, "pm10_corrected", "pm10"),
         first(r, "rco2_corrected", "rco2"), first(r, "atmp_corrected", "atmp"),
         first(r, "rhum_corrected", "rhum"), r.get("tvocIndex"), r.get("noxIndex"))
        for r in rows if r.get("timestamp")
    ])
    return len(rows)


def collect_ag_gapfill(db):
    """Every 6 h, pull the last 3 days from the cloud to fill gaps (e.g. while this Mac slept)."""
    token = CONFIG.get("airgradient_api_token")
    if not token or not due(db, "ag_gapfill_last", 360):
        return
    end = datetime.now(timezone.utc)
    ag_import_window(db, token, ag_location_id(db, token), end - timedelta(days=3), end)
    set_state(db, "ag_gapfill_last", time.time())


def import_weather_history(db, start, end):
    """Hourly METAR archive for the NWS station from the Iowa Environmental Mesonet."""
    station = CONFIG["nws_station"].removeprefix("K")
    year = start.year
    while year <= end.year:
        a, b = max(start, datetime(year, 1, 1, tzinfo=timezone.utc)), min(
            end, datetime(year + 1, 1, 1, tzinfo=timezone.utc))
        q = [("station", station), ("data", "tmpc"), ("data", "dwpc"), ("data", "relh"),
             ("data", "sknt"), ("tz", "Etc/UTC"), ("format", "onlycomma"), ("latlon", "no"),
             ("missing", "empty"), ("trace", "empty"), ("report_type", 3), ("report_type", 4),
             ("year1", a.year), ("month1", a.month), ("day1", a.day),
             ("year2", b.year), ("month2", b.month), ("day2", b.day)]
        text = http_text("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
                         + urllib.parse.urlencode(q), timeout=300)
        n = 0
        for line in text.splitlines()[1:]:
            _, valid, t, dp, rh, kt = (line.split(",") + [""] * 6)[:6]
            ts = int(datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())
            num = lambda s, k=1.0: round(float(s) * k, 2) if s else None
            db.executemany("INSERT OR IGNORE INTO outdoor VALUES (?, 'nws', ?, ?)", [
                (ts, m, v) for m, v in (("temp", num(t)), ("dewpoint", num(dp)), ("rh", num(rh)),
                                        ("wind_kmh", num(kt, 1.852))) if v is not None])
            n += 1
        db.commit()
        log.info("weather history %s: %d observations", year, n)
        year += 1


def import_airnow_daily(db, start, end):
    """Daily-mean PM2.5 for the AirNow sites (hourly archives are ~0.7 MB per hour; daily
    files are ~0.6 MB per day). Stored at local-standard midnight as metric pm25_daily."""
    sites = set(CONFIG["airnow_sites"])
    day = start.date()
    while day <= end.date():
        tag = f"airnow_daily:{day:%Y%m%d}"
        if not get_state(db, tag):
            try:
                text = http_text(f"https://files.airnowtech.org/airnow/{day:%Y}/{day:%Y%m%d}/daily_data_v2.dat")
                for line in text.splitlines():
                    f = line.split("|")
                    if len(f) > 5 and f[1] in sites and f[3] == "PM2.5-24hr":
                        ts = datetime(day.year, day.month, day.day, 5, tzinfo=timezone.utc).timestamp()
                        put_outdoor(db, ts, f"airnow:{f[1]}", {"pm25_daily": float(f[5])})
                set_state(db, tag, 1)
                db.commit()
            except Exception as e:
                log.info("airnow daily %s unavailable (%s)", day, e)
        if day.day == 1:
            log.info("airnow daily: %s", day)
        day += timedelta(days=1)


def backfill():
    """One-off: import the monitor's full cloud history plus matching outdoor history."""
    token = CONFIG.get("airgradient_api_token")
    if not token:
        sys.exit("Set airgradient_api_token in config.json first (see README).")
    db = sqlite3.connect(DB_PATH, timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    loc = ag_location_id(db, token)
    db.commit()
    log.info("AirGradient location %s", loc)

    # Walk backwards in 10-day windows (the API's maximum) until ~4 empty months in a row.
    end, empty, oldest = datetime.now(timezone.utc), 0, None
    while empty < 12:
        start = end - timedelta(days=10)
        n = ag_import_window(db, token, loc, start, end)
        db.commit()
        log.info("indoor %s .. %s: %d rows", start.date(), end.date(), n)
        if n:
            empty, oldest = 0, start
        else:
            empty += 1
        end = start
        time.sleep(0.5)
    if not oldest:
        sys.exit("No cloud history found for this monitor.")
    first_ts = db.execute("SELECT MIN(ts) FROM indoor").fetchone()[0]
    first = datetime.fromtimestamp(first_ts, timezone.utc)
    log.info("indoor history starts %s", first)

    now = datetime.now(timezone.utc)
    import_weather_history(db, first - timedelta(days=1), now)
    import_airnow_daily(db, first - timedelta(days=1), now - timedelta(days=1))
    db.close()
    log.info("backfill done")


# ---------------------------------------------------------------- main

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if "--backfill" in sys.argv:
        return backfill()
    lock = open(DB_PATH.parent / ".collect.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("previous run still going; skipping")
        return
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    for fn in (collect_indoor, collect_purpleair, collect_nws, collect_forecast, collect_airnow, collect_ag_gapfill):
        try:
            fn(db)
            db.commit()
        except Exception as e:
            log.error("%s failed: %s", fn.__name__, e)
            db.rollback()
    db.close()


if __name__ == "__main__":
    sys.exit(main())
