#!/usr/bin/env python3
"""Serve the dashboard and a small JSON API over the collected SQLite data (localhost only)."""
import bisect
import csv
import io
import json
import math
import sqlite3
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import os
import sys
sys.path.insert(0, str(Path.home() / "portal-tools"))
from dashboard_security import allowed, secure_page, security_headers
os.umask(0o077)

ROOT = Path(__file__).resolve().parent
if not (ROOT / "config.json").exists():
    raise SystemExit("config.json not found: copy config.example.json to config.json and edit it (see README).")
CONFIG = json.loads((ROOT / "config.json").read_text())
DB_PATH = ROOT / CONFIG["db_path"]
REF_SITE = next(iter(CONFIG["airnow_sites"]))  # first listed AirNow site = regional reference

# Below this outdoor PM2.5 the indoor/outdoor ratio is mostly sensor noise.
RATIO_MIN_OUTDOOR = 2.0

# series name -> (table, source, column/metric)
SERIES = {
    "in_pm25": ("indoor", None, "pm25"),
    "in_temp": ("indoor", None, "temp"),
    "in_rh": ("indoor", None, "rh"),
    "in_dew": ("indoor", None, "dew"),
    "in_co2": ("indoor", None, "co2"),
    "in_tvoc": ("indoor", None, "tvoc_index"),
    "in_nox": ("indoor", None, "nox_index"),
    "out_pm25": ("outdoor", "purpleair", "pm25"),
    "out_pm25_n": ("outdoor", "purpleair", "n_sensors"),
    "ref_pm25": ("outdoor", f"airnow:{REF_SITE}", "pm25"),
    "out_temp": ("outdoor", "nws", "temp"),
    "out_rh": ("outdoor", "nws", "rh"),
    "out_dew": ("outdoor", "nws", "dewpoint"),
}


def dew_point(t, rh):
    if t is None or not rh:
        return None
    a, b = 17.62, 243.12  # Magnus coefficients
    g = math.log(rh / 100) + a * t / (b + t)
    return b * g / (a - g)


# Outdoor temperature forecast shown ahead of "now", by selected range (hours -> hours ahead).
# Longer ranges show everything NWS provides (~7 days).
FORECAST_AHEAD = {24: 3, 72: 24, 168: 72}

BUCKETS = (600, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400)
MAX_POINTS = 3000

INDOOR_COLS = {"in_pm25": "pm25", "in_temp": "temp", "in_rh": "rh", "in_co2": "co2",
               "in_tvoc": "tvoc_index", "in_nox": "nox_index"}


def connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def span_start(db, hours):
    """hours <= 0 means all history."""
    if hours > 0:
        return int(time.time()) - hours * 3600
    return db.execute("SELECT MIN(ts) FROM indoor").fetchone()[0] or int(time.time()) - 86400


def bucket_for(since):
    span = time.time() - since
    return next((b for b in BUCKETS if span / b <= MAX_POINTS), BUCKETS[-1])


def bucketed(db, since, size, daily_fill=True):
    """Bucket means computed in SQLite. Returns (t, {series: [values]})."""
    start = since // size * size
    t = list(range(start, int(time.time()) // size * size + 1, size))
    idx = {x: i for i, x in enumerate(t)}
    out = {name: [None] * len(t) for name in SERIES}

    cols = ", ".join(f"AVG({c})" for c in INDOOR_COLS.values())
    for b, *vals in db.execute(
        f"SELECT ts / ? * ? AS b, {cols} FROM indoor WHERE ts >= ? GROUP BY b", (size, size, start)
    ):
        i = idx.get(b)
        if i is None:
            continue
        for name, v in zip(INDOOR_COLS, vals):
            out[name][i] = None if v is None else round(v, 2)
        dp = dew_point(out["in_temp"][i], out["in_rh"][i])
        out["in_dew"][i] = None if dp is None else round(dp, 2)

    lookup = {(src, m): name for name, (tbl, src, m) in SERIES.items() if tbl == "outdoor"}
    daily_ref = {}
    for b, src, metric, v in db.execute(
        "SELECT ts / ? * ? AS b, source, metric, AVG(value) FROM outdoor "
        "WHERE ts >= ? GROUP BY b, source, metric", (size, size, start - 86400)
    ):
        if src == f"airnow:{REF_SITE}" and metric == "pm25_daily":
            daily_ref[b] = v
            continue
        name, i = lookup.get((src, metric)), idx.get(b)
        if name and i is not None and v is not None:
            out[name][i] = round(v, 2)

    # Before the hourly AirNow window, show the regional monitor's daily means as steps.
    if daily_fill and daily_ref:
        days = sorted(daily_ref)
        for i, x in enumerate(t):
            j = bisect.bisect_right(days, x) - 1
            if out["ref_pm25"][i] is None and j >= 0 and x - days[j] < 86400:
                out["ref_pm25"][i] = round(daily_ref[days[j]], 2)
    return t, out


def latest(db):
    res = {}
    row = db.execute(f"SELECT ts, {', '.join(INDOOR_COLS.values())} FROM indoor "
                     "ORDER BY ts DESC LIMIT 1").fetchone()
    if row:
        for name, v in zip(INDOOR_COLS, row[1:]):
            if v is not None:
                res[name] = {"ts": row[0], "value": round(v, 2)}
        dp = dew_point(res.get("in_temp", {}).get("value"), res.get("in_rh", {}).get("value"))
        if dp is not None:
            res["in_dew"] = {"ts": row[0], "value": round(dp, 2)}
    for name, (tbl, src, m) in SERIES.items():
        if tbl != "outdoor":
            continue
        r = db.execute("SELECT ts, value FROM outdoor WHERE source=? AND metric=? AND value IS NOT NULL "
                       "ORDER BY ts DESC LIMIT 1", (src, m)).fetchone()
        if r:
            res[name] = {"ts": r[0], "value": round(r[1], 2)}
    return res


def forecast(db, hours):
    now = int(time.time())
    end = now + FORECAST_AHEAD.get(hours, 24 * 14) * 3600
    try:
        rows = db.execute("SELECT ts, value FROM forecast WHERE metric='temp' AND ts >= ? AND ts <= ? "
                          "ORDER BY ts", (now // 3600 * 3600, end)).fetchall()
    except sqlite3.OperationalError:  # collector hasn't created the table yet
        rows = []
    if len(rows) >= 2 and rows[0][0] < now:  # start the line at "now", interpolated within the hour
        (t0, v0), (t1, v1) = rows[0], rows[1]
        rows[0] = (now, round(v0 + (v1 - v0) * (now - t0) / (t1 - t0), 2))
    return {"t": [r[0] for r in rows], "temp": [r[1] for r in rows]}


def build(hours):
    db = connect()
    since = span_start(db, hours)
    size = bucket_for(since)
    t, series = bucketed(db, since, size)

    # Indoor/outdoor PM2.5 ratio at >= hourly resolution, from measured outdoor values only
    # (no daily-mean fill). Basis: local PurpleAir median when available, else regional monitor.
    ht, hourly = bucketed(db, since, max(size, 3600), daily_fill=False)
    basis = "out_pm25" if any(v is not None for v in hourly["out_pm25"]) else "ref_pm25"
    ratio = [round(i_v / o_v, 2) if i_v is not None and o_v and o_v >= RATIO_MIN_OUTDOOR else None
             for i_v, o_v in zip(hourly["in_pm25"], hourly[basis])]

    result = {
        "bucket": size,
        "t": t,
        "series": series,
        "ratio": {"t": ht, "v": ratio, "basis": basis, "min_outdoor": RATIO_MIN_OUTDOOR},
        "forecast": forecast(db, hours),
        "latest": latest(db),
        "meta": {
            "location": CONFIG["location"]["name"],
            "purpleair": bool(CONFIG.get("purpleair_api_key")),
            "purpleair_radius_km": CONFIG["purpleair_radius_km"],
            "ref_site": CONFIG["airnow_sites"][REF_SITE],
            "nws_station": CONFIG["nws_station"],
            "first_ts": db.execute("SELECT MIN(ts) FROM indoor").fetchone()[0],
        },
    }
    db.close()
    return result


def to_csv(hours):
    db = connect()
    since = span_start(db, hours)
    t, series = bucketed(db, since, max(3600, bucket_for(since)))
    db.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_utc", *SERIES])
    for i, ts in enumerate(t):
        w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                    *("" if series[k][i] is None else series[k][i] for k in SERIES)])
    return buf.getvalue()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT / "web"), **kw)

    def log_message(self, *a):
        pass

    def end_headers(self):
        for key, value in security_headers(secure_page((ROOT / "web/index.html").read_bytes())).items():
            self.send_header(key, value)
        super().end_headers()

    def send_body(self, body, ctype):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not allowed(self.headers, self.server.server_address[1]):
            return self.send_error(403)
        if urlparse(self.path).path in ("/", "/index.html"):
            body = secure_page((ROOT / "web/index.html").read_bytes())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            hours = int((q.get("hours") or ["24"])[0])  # <= 0: all history
        except ValueError:
            return self.send_error(400, "hours must be an integer")
        hours = min(hours, 24 * 3660)  # cap at ~10 years
        if u.path == "/api/data":
            return self.send_body(json.dumps(build(hours)), "application/json")
        if u.path == "/api/csv":
            return self.send_body(to_csv(hours), "text/csv; charset=utf-8")
        return super().do_GET()


if __name__ == "__main__":
    port = CONFIG.get("server_port", 8765)
    print(f"airq dashboard on http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
