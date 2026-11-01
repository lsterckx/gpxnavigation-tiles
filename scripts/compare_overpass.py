#!/usr/bin/env python3
"""
Vergelijkt voor een route wat de tegels geven met wat Overpass geeft (de huidige bron van de app).

Neemt dezelfde strook als de app: wegen tot 20 m van de route, oversteekknopen tot 15 m.
Toont welke wegen of knopen in de ene bron staan en niet in de andere, en welke tags verschillen.

Kleine verschillen zijn normaal: Overpass is live, de tegels zijn tot een week oud. Daarom staat
bij elk verschil hoe ver het van de route ligt; een weg op ongeveer 20 m is een grensgeval.

Gebruik (de route blijft op je eigen computer; zet geen routes in de repository):
    python3 scripts/compare_overpass.py ~/routes/tervuren.gpx --tiles out
    python3 scripts/compare_overpass.py ~/routes/leuven.gpx --tiles https://tiles.data-tech-up.com
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from shapely.geometry import LineString, Point

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tiles_common import (  # noqa: E402
    NODE_CORRIDOR_METERS, WAY_CORRIDOR_METERS, corridor_tiles, filter_tags, tile_path,
)

# Zelfde servers en vraag als OverpassClient.swift.
OVERPASS_ENDPOINTS = [
    "https://z.overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
MAX_QUERY_POINTS = 400
USER_AGENT = "gpxnavigation-tiles compare (https://github.com/lsterckx/gpxnavigation-tiles)"


# ---------- route ----------

def read_gpx(path: str) -> list[tuple[float, float]]:
    root = ET.parse(path).getroot()
    pts = {"trkpt": [], "rtept": []}
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in pts:
            pts[tag].append((float(el.get("lon")), float(el.get("lat"))))
    route = pts["trkpt"] or pts["rtept"]
    if len(route) < 2:
        sys.exit("De GPX bevat geen route (trkpt of rtept).")
    return route


class Projector:
    """Eenvoudige projectie naar meters rond het midden van de route (ruim nauwkeurig genoeg)."""

    def __init__(self, route):
        self.lat0 = sum(lat for _, lat in route) / len(route)
        self.kx = 111_320.0 * math.cos(math.radians(self.lat0))
        self.ky = 110_574.0

    def xy(self, lon, lat):
        return lon * self.kx, lat * self.ky


# ---------- tegels ----------

def read_tile(source: str, x: int, y: int) -> dict | None:
    rel = tile_path(x, y)
    if source.startswith("http://") or source.startswith("https://"):
        url = source.rstrip("/") + "/" + rel
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return json.loads(data)
    path = os.path.join(source, rel)
    if not os.path.exists(path):
        return None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


# ---------- Overpass ----------

def overpass_query(route) -> str:
    step = max(1, math.ceil(len(route) / MAX_QUERY_POINTS))
    pts = route[::step]
    if pts[-1] != route[-1]:
        pts.append(route[-1])
    line = ",".join(f"{lat:.6f},{lon:.6f}" for lon, lat in pts)
    return (f"[out:json][timeout:45];\n"
            f"way[\"highway\"](around:{WAY_CORRIDOR_METERS},{line});\n"
            f"out body geom;\n"
            f"(\n  node[\"highway\"=\"crossing\"](around:{NODE_CORRIDOR_METERS},{line});\n"
            f"  node[\"crossing\"](around:{NODE_CORRIDOR_METERS},{line});\n);\n"
            f"out body;")


def fetch_overpass(route) -> dict:
    body = urllib.parse.urlencode({"data": overpass_query(route)}).encode()
    last = None
    for url in OVERPASS_ENDPOINTS:
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=90) as r:
                result = json.loads(r.read())
            if not any(e.get("type") == "way" for e in result.get("elements", [])):
                raise RuntimeError("leeg antwoord")
            print(f"Overpass: {url.split('/')[2]} antwoordde in {time.time() - t0:.0f} s")
            return result
        except Exception as e:  # noqa: BLE001
            print(f"Overpass: {url.split('/')[2]} faalt ({e})")
            last = e
    sys.exit(f"Geen enkele Overpass-server gaf een antwoord: {last}")


# ---------- vergelijken ----------

def describe(e: dict) -> str:
    tags = e.get("tags", {})
    kind = tags.get("highway", "?")
    name = tags.get("name")
    return f"{e['type']} {e['id']} ({kind}{', ' + name if name else ''})"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gpx", help="de route (GPX)")
    ap.add_argument("--tiles", default="out", help="map met de tegels, of het webadres van de tegels")
    ap.add_argument("--overpass-file", help="Overpass-antwoord uit dit bestand lezen in plaats van opvragen")
    ap.add_argument("--save-overpass", help="Overpass-antwoord in dit bestand bewaren (buiten de repository)")
    args = ap.parse_args()

    route = read_gpx(args.gpx)
    proj = Projector(route)
    route_line = LineString([proj.xy(lon, lat) for lon, lat in route])
    print(f"Route: {len(route)} punten, {route_line.length / 1000:.1f} km")

    # Tegels
    t0 = time.time()
    cells = sorted(corridor_tiles(route))
    tile_elements: dict[tuple[str, int], dict] = {}
    missing, partial, total_bytes = [], [], 0
    for x, y in cells:
        tile = read_tile(args.tiles, x, y)
        if tile is None:
            missing.append(tile_path(x, y))
            continue
        if not tile.get("complete", False):
            partial.append(tile_path(x, y))
        for e in tile["elements"]:
            tile_elements[(e["type"], e["id"])] = e
    print(f"Tegels: {len(cells)} langs de route, {len(missing)} ontbreken, {len(partial)} op een gebiedsrand,"
          f" gelezen in {time.time() - t0:.1f} s")

    def distance(e: dict) -> float:
        if e["type"] == "node":
            return Point(proj.xy(e["lon"], e["lat"])).distance(route_line)
        geom = [proj.xy(p["lon"], p["lat"]) for p in e["geometry"] if p]
        return (LineString(geom) if len(geom) >= 2 else Point(geom[0])).distance(route_line)

    def near(e: dict) -> bool:
        limit = NODE_CORRIDOR_METERS if e["type"] == "node" else WAY_CORRIDOR_METERS
        return distance(e) <= limit

    from_tiles = {k: e for k, e in tile_elements.items() if near(e)}

    # Overpass
    if args.overpass_file:
        with open(args.overpass_file, encoding="utf-8") as f:
            op = json.load(f)
    else:
        op = fetch_overpass(route)
        if args.save_overpass:
            with open(args.save_overpass, "w", encoding="utf-8") as f:
                json.dump(op, f)
    from_overpass = {(e["type"], e["id"]): e for e in op["elements"]}

    # Verschillen
    only_overpass = sorted(set(from_overpass) - set(from_tiles))
    only_tiles = sorted(set(from_tiles) - set(from_overpass))
    tag_diffs = []
    for key in sorted(set(from_tiles) & set(from_overpass)):
        a, b = from_tiles[key], from_overpass[key]
        tags_b = filter_tags(b.get("tags", {}).items())
        if a.get("tags", {}) != tags_b or (key[0] == "way" and a["nodes"] != b.get("nodes")):
            tag_diffs.append((key, a.get("tags", {}), tags_b, a.get("nodes") != b.get("nodes")))

    ways_t = sum(1 for k in from_tiles if k[0] == "way")
    ways_o = sum(1 for k in from_overpass if k[0] == "way")
    print(f"\nWegen:          tegels {ways_t}, Overpass {ways_o}")
    print(f"Oversteekknopen: tegels {len(from_tiles) - ways_t}, Overpass {len(from_overpass) - ways_o}")

    def show(title, keys, source):
        if not keys:
            return
        print(f"\n{title} ({len(keys)}):")
        for k in keys:
            e = source[k]
            d = distance(e) if "geometry" in e or e["type"] == "node" else float("nan")
            limit = NODE_CORRIDOR_METERS if k[0] == "node" else WAY_CORRIDOR_METERS
            edge = "  <- grensgeval" if abs(d - limit) <= 2 else ""
            print(f"  {describe(e)}: {d:.1f} m van de route{edge}")

    show("Alleen in Overpass", only_overpass, from_overpass)
    show("Alleen in de tegels", only_tiles, from_tiles)
    if tag_diffs:
        print(f"\nVerschillende tags of knopen ({len(tag_diffs)}):")
        for key, ta, tb, nodes_differ in tag_diffs:
            changed = sorted(k for k in set(ta) | set(tb) if ta.get(k) != tb.get(k))
            parts = [f"{k}: tegel={ta.get(k)!r} overpass={tb.get(k)!r}" for k in changed]
            if nodes_differ:
                parts.append("andere knopen (weg gewijzigd)")
            print(f"  {key[0]} {key[1]}: " + "; ".join(parts))

    real = [k for k in only_overpass + only_tiles
            if abs(distance((from_overpass | from_tiles)[k])
                   - (NODE_CORRIDOR_METERS if k[0] == "node" else WAY_CORRIDOR_METERS)) > 2]
    print()
    if not real and not tag_diffs:
        print("Resultaat: GELIJK (op grensgevallen na).")
    else:
        print(f"Resultaat: {len(real)} verschillen en {len(tag_diffs)} tagverschillen. Kijk of ze verklaard "
              "worden door recente wijzigingen in OpenStreetMap (zoek het id op openstreetmap.org).")
        sys.exit(1)


if __name__ == "__main__":
    main()
