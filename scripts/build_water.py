#!/usr/bin/env python3
"""
Maakt een aparte laag tegels met water, voor "Water in de omgeving" in de app:

    out/water/14/{x}/{y}.json.gz     (zelfde raster als de wegtegels, alleen tegels met water)

Uit dezelfde Geofabrik-bestanden als de wegtegels:
- waterlopen (lijnen): waterway=river, stream, canal, ditch, drain, tidal_channel; met en zonder naam
  (de app noemt een naamloze waterloop alleen als hij binnen 100 m ligt). De lijn staat in de stroomrichting.
- watervlakken: natural=water (vijvers, meren, ...) en landuse=reservoir/basin, als vereenvoudigde omtrek.
  Niet: water=river/canal/stream/... (de bedding van een rivier: die is al een waterloop), zwembaden, en naamloze
  vlakken kleiner dan 200 m².

Een waterloop of vlak staat in elke tegel die zijn lijn of omtrek raakt.

Formaat (version 1):
    {"version":1,"elements":[
      {"type":"waterway","id":123,"kind":"stream","name":"Voer","names":{"nl":..},"geometry":[[lon,lat],...]},
      {"type":"water","id":"r456","kind":"pond","name":"Grote Vijver","names":{..},"area":12000,
       "polygons":[[[[lon,lat],...] (omtrek), ...gaten]]}]}

Gebruik (na fetch_regions.py):
    python3 scripts/build_water.py --regions regions.yml --data data --out out
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict

import osmium
import shapely
import yaml
from shapely import wkb
from shapely.geometry import LineString

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tiles_common import ZOOM, line_tiles, tile_path  # noqa: E402

FORMAT_VERSION = 1
WATERWAYS = {"river", "stream", "canal", "ditch", "drain", "tidal_channel"}
# Bedding van een waterloop of iets dat geen vijver/meer is: niet als vlak.
SKIP_WATER = {"river", "canal", "stream", "ditch", "drain", "wastewater", "moat", "lock", "stream_pool"}
MIN_UNNAMED_AREA_M2 = 200
NAME_LANGUAGES = ("nl", "fr", "de", "en", "es", "pt")
LINE_SIMPLIFY = 0.00003     # ±2–3 m
AREA_SIMPLIFY = 0.00005     # ±4–5 m


def load_regions(path: str, only: list[str] | None) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        regions = yaml.safe_load(f)["regions"]
    if only:
        return [r for r in regions if r["id"] in only]
    return [r for r in regions if r.get("enabled", True)]


def prefilter(pbf: str) -> str:
    exe = shutil.which("osmium")
    if not exe:
        print("  (osmium-tool niet gevonden: het volledige bestand wordt gelezen)")
        return pbf
    out = pbf.replace(".osm.pbf", ".water.osm.pbf")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(pbf):
        return out
    print(f"  filteren met osmium-tool -> {os.path.basename(out)}")
    subprocess.run([exe, "tags-filter", "--overwrite", "-o", out, pbf,
                    "w/waterway=" + ",".join(sorted(WATERWAYS)),
                    "wr/natural=water", "wr/landuse=reservoir,basin"], check=True)
    return out


def names(tags) -> dict[str, str]:
    return {lang: tags[f"name:{lang}"] for lang in NAME_LANGUAGES if f"name:{lang}" in tags}


def area_m2(geom) -> float:
    """Oppervlakte in m² (benadering met een vlakke projectie rond het midden)."""
    import math
    lat = (geom.bounds[1] + geom.bounds[3]) / 2
    kx = 111_320 * math.cos(math.radians(lat))
    return abs(geom.area) * kx * 110_574


class Collector(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.factory = osmium.geom.WKBFactory()
        self.elements: dict[str, str] = {}                     # sleutel -> JSON
        self.tiles: dict[tuple[int, int], set[str]] = defaultdict(set)
        self.counts = {"waterways": 0, "water": 0}

    def _add(self, key: str, element: dict, coords_for_tiles: list[list[tuple[float, float]]]):
        if key in self.elements:
            return
        self.elements[key] = json.dumps(element, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        for coords in coords_for_tiles:
            for cell in line_tiles(coords):
                self.tiles[cell].add(key)

    def way(self, w):
        kind = w.tags.get("waterway")
        if kind not in WATERWAYS:
            return
        coords = []
        for n in w.nodes:
            if not n.location.valid():
                return
            coords.append((n.location.lon, n.location.lat))
        if len(coords) < 2:
            return
        simple = list(LineString(coords).simplify(LINE_SIMPLIFY, preserve_topology=False).coords)
        geometry = [[round(x, 5), round(y, 5)] for x, y in simple]
        element = {"type": "waterway", "id": w.id, "kind": kind, "geometry": geometry}
        if w.tags.get("name"):
            element["name"] = w.tags["name"]
            element["names"] = names(w.tags)
        self.counts["waterways"] += 1
        self._add(f"w{w.id}", element, [[(x, y) for x, y in geometry]])

    def area(self, a):
        natural, landuse = a.tags.get("natural"), a.tags.get("landuse")
        if natural == "water":
            kind = a.tags.get("water", "water")
            if kind in SKIP_WATER:
                return
        elif landuse in ("reservoir", "basin"):
            kind = landuse
        else:
            return
        if a.tags.get("leisure") == "swimming_pool" or a.tags.get("amenity") == "swimming_pool":
            return
        try:
            geom = wkb.loads(self.factory.create_multipolygon(a), hex=True)
        except Exception:  # noqa: BLE001 - kapot vlak in OpenStreetMap: overslaan
            return
        m2 = area_m2(geom)
        name = a.tags.get("name")
        if not name and m2 < MIN_UNNAMED_AREA_M2:
            return
        simple = geom.simplify(AREA_SIMPLIFY, preserve_topology=True)
        parts = list(simple.geoms) if hasattr(simple, "geoms") else [simple]
        polygons = []
        for p in parts:
            if p.is_empty or p.geom_type != "Polygon":
                continue
            polygons.append([[[round(x, 5), round(y, 5)] for x, y in ring.coords]
                             for ring in [p.exterior, *p.interiors]])
        if not polygons:
            return
        key = ("w" if a.from_way() else "r") + str(a.orig_id())
        element = {"type": "water", "id": key, "kind": kind, "area": int(round(m2)), "polygons": polygons}
        if name:
            element["name"] = name
            element["names"] = names(a.tags)
        self.counts["water"] += 1
        # Tegels: die welke de omtrek raakt (wie in de buurt is, is in de buurt van de oever).
        self._add(f"a{key}", element, [[(x, y) for x, y in poly[0]] for poly in polygons])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regions", default="regions.yml")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="out")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()

    started = time.time()
    collector = Collector()
    for region in load_regions(args.regions, args.only):
        pbf = os.path.join(args.data, f"{region['id']}.osm.pbf")
        if not os.path.exists(pbf):
            sys.exit(f"ontbreekt: {pbf} (eerst fetch_regions.py)")
        print(f"{region['name']} ({region['id']})")
        before = dict(collector.counts)
        collector.apply_file(prefilter(pbf), locations=True, idx="flex_mem")
        print(f"  {collector.counts['waterways'] - before['waterways']} waterlopen, "
              f"{collector.counts['water'] - before['water']} watervlakken")

    root = os.path.join(args.out, "water")
    tiles_dir = os.path.join(root, str(ZOOM))
    if os.path.isdir(tiles_dir):
        shutil.rmtree(tiles_dir)
    total = 0
    for (x, y), keys in sorted(collector.tiles.items()):
        elements = [collector.elements[k] for k in sorted(keys)]
        text = f'{{"version":{FORMAT_VERSION},"elements":[' + ",".join(elements) + "]}"
        data = gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)
        path = os.path.join(root, tile_path(x, y))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        total += len(data)
    print(f"\n{len(collector.tiles)} watertegels, samen {total / 1e6:.1f} MB, {time.time() - started:.0f} s")
    with open(os.path.join(root, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"tiles": len(collector.tiles), "bytes_total": total, **collector.counts,
                   "duration_seconds": round(time.time() - started)}, f, indent=2)


if __name__ == "__main__":
    main()
