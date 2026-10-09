# airq — Bedroom Air Monitor

A small, dependency-free (Python stdlib) logger and dashboard that puts an
[AirGradient](https://www.airgradient.com/) indoor monitor side by side with outdoor
conditions from nearby public stations (US).

- Indoor readings every minute from the monitor's **local API** (no cloud needed).
- Outdoor PM2.5 from **PurpleAir** sensors near you (optional, needs a free API key) and the
  nearest **EPA AirNow** regulatory monitor.
- Outdoor temperature / humidity / dew point from the nearest **NWS** station.
- Dashboard with synced charts, indoor ÷ outdoor PM2.5 ratio, health-threshold bands
  (EPA AQI for PM2.5, CO₂ ventilation guidance, AirGradient TVOC/NOx categories, humidity
  comfort), light/dark themes and CSV export.
- Optional import of the monitor's full history from the AirGradient cloud, with matching
  outdoor history.

## Data sources

| What | Source | Notes |
|---|---|---|
| Indoor everything | AirGradient local API `http://airgradient_<serial>.local/measures/current` (firmware 3.x) | 1-min resolution. PM2.5 uses `pm02Compensated` (the EPA correction configured on the device). |
| Outdoor PM2.5 (local) | PurpleAir outdoor sensors within `purpleair_radius_km` — median, EPA-corrected with the same formula AirGradient uses | Densest network in most US towns. **Needs an API key.** |
| Outdoor PM2.5 (reference) | EPA AirNow hourly files for `airnow_sites` | Reference-grade instruments, but may be far away. No key. |
| Outdoor temp / humidity / dew point | NWS `api.weather.gov` observations for `nws_station`; history from the Iowa Environmental Mesonet ASOS archive | No key. |

## Setup

```sh
cp config.example.json config.json
python3 find_stations.py <lat> <lon>   # prints nearest NWS stations and AirNow PM2.5 sites
```

Edit `config.json`:

- `monitor_host`: `airgradient_<serial>.local` (find it with `dns-sd -B _airgradient._tcp`), and optionally its IP as `monitor_fallback_ip`.
- `location`: a display name and coordinates (used for the PurpleAir search box).
- `nws_station`, `airnow_sites`: from `find_stations.py`. The first AirNow site is plotted as the regional reference.

Then install the background agents (macOS launchd):

```sh
./install.sh            # collector every minute + dashboard at http://localhost:8765
./install.sh uninstall  # remove agents (keeps data)
```

`config.json` and `data/` are git-ignored — they hold your tokens, monitor serial, location
and readings.

### PurpleAir (recommended)

The nearest regulatory monitor is often tens of km away. For local outdoor PM2.5, create a
**READ** key at https://develop.purpleair.com and set `purpleair_api_key`. Requests cost
points per sensor and field; raise `purpleair_interval_min` or shrink `purpleair_radius_km`
if needed.

### Import the monitor's history

The monitor keeps no history itself; the AirGradient cloud does.

1. AirGradient dashboard → Place settings → enable API access, copy the token.
2. Set `airgradient_api_token` in `config.json`.
3. `python3 collect.py --backfill`

This imports all cloud history (5-min or hourly averages, never overwriting local per-minute
readings), hourly weather history for the NWS station, and daily-mean PM2.5 for the AirNow
sites. Safe to re-run. With the token set, the collector also pulls the last 3 days every 6 h
to fill gaps (e.g. while the computer slept).

## Viewing from other devices

The server binds to `127.0.0.1` only. To reach it from your other devices without exposing it
publicly, use [Tailscale Serve](https://tailscale.com/kb/1312/serve):

```sh
tailscale serve --bg 8765        # https://<machine>.<tailnet>.ts.net, tailnet only
tailscale serve --https=443 off  # undo
```

## Files

- `collect.py` — run every minute by launchd; indoor every run, each outdoor source on its own interval. `--backfill` for history.
- `server.py` — read-only JSON API (`/api/data?hours=N`, `/api/csv?hours=N`, `hours=0` = all) + static dashboard.
- `web/index.html` — dashboard (uPlot from jsDelivr, pinned with SRI).
- `find_stations.py` — finds nearby outdoor stations for the config.
- `data/airq.sqlite` — all readings (`indoor`, `outdoor` tables); `data/*.log` — logs.

The computer has to be awake for minute-by-minute logging; gaps are filled from the cloud if a
token is configured.
