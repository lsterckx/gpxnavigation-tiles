#!/usr/bin/env python3
"""
Downloadt per gebied uit regions.yml het OpenStreetMap-bestand (.osm.pbf) en de omtrek (.poly)
bij Geofabrik, en controleert het bestand met de MD5-controlesom van Geofabrik.

Gebruik:
    python3 scripts/fetch_regions.py --regions regions.yml --data data
    python3 scripts/fetch_regions.py --only belgium
    python3 scripts/fetch_regions.py --polys-only      # alleen de omtrek van alle gebieden (gesplitste run)
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import sys
import time
import urllib.request

import yaml

BASE = "https://download.geofabrik.de"
USER_AGENT = "gpxnavigation-tiles (https://github.com/lsterckx/gpxnavigation-tiles)"


def retry(what: str, action, attempts: int = 3):
    """Voert `action` uit; mislukt ze (time-out of verbroken verbinding bij Geofabrik), dan na 30 en 60 s opnieuw."""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except (OSError, http.client.HTTPException) as e:
            if attempt == attempts:
                raise
            wait = 30 * attempt
            print(f"    {what}: {e} - opnieuw over {wait} s (poging {attempt + 1}/{attempts})", flush=True)
            time.sleep(wait)


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def download(url: str, path: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = path + ".part"
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, 0.0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if time.time() - last > 10:
                last = time.time()
                pct = f" ({100 * done / total:.0f}%)" if total else ""
                print(f"    {done / 1e6:.0f} MB{pct}", flush=True)
    os.replace(tmp, path)
    print(f"    klaar: {done / 1e6:.0f} MB in {time.time() - t0:.0f} s")


def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regions", default="regions.yml")
    ap.add_argument("--data", default="data")
    ap.add_argument("--only", nargs="*", help="alleen deze gebieden (id's uit regions.yml)")
    ap.add_argument("--polys-only", action="store_true", help="alleen de .poly-bestanden (klein), geen .osm.pbf")
    args = ap.parse_args()

    with open(args.regions, encoding="utf-8") as f:
        regions = yaml.safe_load(f)["regions"]
    regions = [r for r in regions if (r["id"] in args.only if args.only else r.get("enabled", True))]
    if not regions:
        sys.exit("Geen gebieden gekozen.")
    os.makedirs(args.data, exist_ok=True)

    for r in regions:
        src = r["geofabrik"]
        pbf = os.path.join(args.data, f"{r['id']}.osm.pbf")
        poly = os.path.join(args.data, f"{r['id']}.poly")
        print(f"{r['name']} ({src})")
        if args.polys_only:
            with open(poly, "wb") as f:
                f.write(retry("omtrek", lambda: get(f"{BASE}/{src}.poly")))
            continue

        expected = retry("controlesom", lambda: get(f"{BASE}/{src}-latest.osm.pbf.md5")).decode().split()[0]
        if os.path.exists(pbf) and md5(pbf) == expected:
            print("  .osm.pbf is al de nieuwste versie")
        else:
            print("  .osm.pbf downloaden")
            retry("download", lambda: download(f"{BASE}/{src}-latest.osm.pbf", pbf))
            if md5(pbf) != expected:
                os.remove(pbf)
                sys.exit(f"  FOUT: controlesom klopt niet voor {pbf}")

        with open(poly, "wb") as f:
            f.write(retry("omtrek", lambda: get(f"{BASE}/{src}.poly")))


if __name__ == "__main__":
    main()
