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

Gesplitste run (één job per gebied; zie parts_common.py en merge_parts.py):
    python3 scripts/build_water.py --part belgium --data data --parts parts
schrijft de eigen watertegels naar parts/belgium/final/water/14/ en de randtegels naar
parts/belgium/water_border.json.gz.
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
import parts_common as pc  # noqa: E402

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


def tile_bytes(elements: list[str]) -> bytes:
    text = f'{{"version":{FORMAT_VERSION},"elements":[' + ",".join(elements) + "]}"
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def write_cells(root: str, cells, collector: Collector) -> list[int]:
    """Schrijft de watertegels `cells` onder root/14/ (eerst leeg); geeft de grootte per tegel terug."""
    tiles_dir = os.path.join(root, str(ZOOM))
    if os.path.isdir(tiles_dir):
        shutil.rmtree(tiles_dir)
    sizes = []
    for (x, y) in sorted(cells):
        data = tile_bytes([collector.elements[k] for k in sorted(collector.tiles[(x, y)])])
        path = os.path.join(root, tile_path(x, y))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        sizes.append(len(data))
    return sizes


def build_part(args) -> None:
    """Eén gebied: eigen watertegels schrijven, randtegels bewaren voor merge_parts.py."""
    from build_tiles import read_poly
    started = time.time()
    config = pc.load_config(args.regions)
    regions = pc.enabled_regions(config)
    ids = [r["id"] for r in regions]
    if args.part not in ids:
        sys.exit(f"{args.part} staat niet (aan) in {args.regions}")
    region = regions[ids.index(args.part)]
    out = pc.part_dir(args.parts, region["id"])
    others = [read_poly(os.path.join(args.data, f"{r['id']}.poly")) for r in regions if r["id"] != region["id"]]

    print(f"{region['name']} ({region['id']})")
    collector = Collector()
    collector.apply_file(prefilter(os.path.join(args.data, f"{region['id']}.osm.pbf")), locations=True, idx="flex_mem")
    filled = set(collector.tiles)
    border = pc.cells_in_zone(filled, pc.border_zone(others, pc.border_margin(config)))
    final = filled - border
    sizes = write_cells(os.path.join(out, "final", "water"), final, collector)
    cells = {pc.cell_key(c): sorted(collector.tiles[c]) for c in sorted(border)}
    keys = {k for ks in cells.values() for k in ks}
    pc.write_json_gz(os.path.join(out, "water_border.json.gz"),
                     {"cells": cells, "elements": {k: collector.elements[k] for k in sorted(keys)}})
    meta = {"id": region["id"], "filled": pc.cells_to_list(filled), "border": pc.cells_to_list(border),
            "final": [tile_path(x, y) for x, y in sorted(final)], "final_sizes": sizes,
            # Alle elementen van dit gebied: merge_parts.py ziet zo of een tegel geraakt door een buur geen verschil maakt.
            "keys": sorted(collector.elements), **collector.counts,
            "duration_seconds": round(time.time() - started)}
    pc.write_json_gz(os.path.join(out, "water_meta.json.gz"), meta)
    print(f"  {len(final)} eigen watertegels ({sum(sizes) / 1e6:.1f} MB), {len(border)} randtegels, "
          f"{meta['duration_seconds']} s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regions", default="regions.yml")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="out")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--part", help="gesplitste run: alleen dit gebied (id uit regions.yml)")
    ap.add_argument("--parts", default="parts")
    args = ap.parse_args()
    if args.part:
        build_part(args)
        return

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
    total = sum(write_cells(root, set(collector.tiles), collector))
    print(f"\n{len(collector.tiles)} watertegels, samen {total / 1e6:.1f} MB, {time.time() - started:.0f} s")
    with open(os.path.join(root, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"tiles": len(collector.tiles), "bytes_total": total, **collector.counts,
                   "duration_seconds": round(time.time() - started)}, f, indent=2)


if __name__ == "__main__":
    main()
