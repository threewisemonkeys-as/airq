#!/usr/bin/env python3
"""Print the outdoor data sources nearest a location, for filling in config.json.

usage: python3 find_stations.py <lat> <lon>
"""
import json
import math
import os
import ssl
import sys
import urllib.request

SSL_CTX = ssl.create_default_context()
if not SSL_CTX.get_ca_certs() and os.path.exists("/etc/ssl/cert.pem"):
    SSL_CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
UA = "airq-local-dashboard (personal use)"


def fetch(url, accept=None):
    headers = {"User-Agent": UA, **({"Accept": accept} if accept else {})}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60,
                                context=SSL_CTX) as r:
        return r.read().decode("latin-1")


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip())
    lat, lon = float(sys.argv[1]), float(sys.argv[2])
    km = lambda la, lo: math.hypot((la - lat) * 111, (lo - lon) * 111 * math.cos(math.radians(lat)))

    print("Nearest NWS observation stations (nws_station):")
    pts = json.loads(fetch(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", "application/geo+json"))
    stations = json.loads(fetch(pts["properties"]["observationStations"], "application/geo+json"))
    for f in stations["features"][:5]:
        lo, la = f["geometry"]["coordinates"]
        print(f"  {f['properties']['stationIdentifier']:6} {km(la, lo):6.1f} km  {f['properties']['name']}")

    print("\nNearest active AirNow PM2.5 monitors (airnow_sites):")
    text = fetch("https://files.airnowtech.org/airnow/today/Monitoring_Site_Locations_V2.dat")
    sites = {}
    for line in text.splitlines()[1:]:
        f = line.split("|")
        if len(f) > 12 and f[3] == "PM2.5" and f[7] == "Active":
            try:
                sites[f[0]] = (km(float(f[11]), float(f[12])), f[6], f[9])
            except ValueError:
                pass
    for sid, (d, name, agency) in sorted(sites.items(), key=lambda kv: kv[1][0])[:5]:
        print(f"  {sid:12} {d:6.1f} km  {name} ({agency})")


if __name__ == "__main__":
    main()
