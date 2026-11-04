#!/usr/bin/env python3
"""
Maakt een aparte laag tegels met openbaar vervoer, taxi en veerboten, voor de app ("Haltes in de buurt" en
meldingen onderweg):

    out/transit/14/{x}/{y}.json.gz     (zelfde raster als de wegtegels, alleen tegels met haltes)

Uit dezelfde Geofabrik-bestanden als de wegtegels. Elke halte is een punt en staat in één tegel.

    kind       role       OpenStreetMap
    bus        platform   highway=bus_stop; public_transport=platform met bus=yes
    bus        station    amenity=bus_station
    tram       platform   public_transport=platform met tram=yes
    tram       stop       railway=tram_stop (op het spoor)
    subway     station    station=subway; railway=station met subway=yes
    subway     entrance   railway=subway_entrance (de ingang: waar je als voetganger naartoe moet)
    train      station    railway=station / halt (niet metro)
    train      platform   railway=platform; public_transport=platform met train=yes (met ref = perron)
    taxi       station    amenity=taxi
    ferry      station    amenity=ferry_terminal

Een halte kan dubbel getekend zijn (de plaats waar je wacht én de plaats van het voertuig, bv. tram): beide staan
erin, met hun role; de app kiest. Een weg (perron, station als vlak) krijgt als positie het punt in het midden.
Niet: haltes met disused/abandoned, en relaties (stations als multipolygoon; zeldzaam).

Formaat (version 1):
    {"version":1,"elements":[
      {"type":"stop","id":"n123","kind":"bus","role":"platform","lon":4.51,"lat":50.82,"name":"Tervuren Station",
       "names":{"fr":..},"route_ref":"410;830","network":"De Lijn","operator":..,"ref":..,"local_ref":..,
       "wheelchair":"yes","tactile_paving":"yes","shelter":"yes"}]}
    Alleen de velden die in OpenStreetMap staan; elementen gesorteerd op id.

Gebruik (na fetch_regions.py):
    python3 scripts/build_transit.py --regions regions.yml --data data --out out
Gesplitste run (zie parts_common.py en merge_parts.py):
    python3 scripts/build_transit.py --part belgium --data data --parts parts
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
import yaml
from shapely.geometry import LineString, Polygon

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tiles_common import ZOOM, lonlat_to_tile, tile_path  # noqa: E402
import parts_common as pc  # noqa: E402

FORMAT_VERSION = 1
LAYER = "transit"
NAME_LANGUAGES = ("nl", "fr", "de", "en", "es", "pt")
KEEP = ("ref", "local_ref", "route_ref", "network", "operator", "wheelchair", "tactile_paving", "shelter")
SKIP_PREFIXES = ("disused:", "abandoned:", "razed:", "demolished:")


def classify(tags) -> tuple[str, str] | None:
    """(kind, role) of None als het geen halte is."""
    def yes(k):
        return tags.get(k) == "yes"
    highway, railway = tags.get("highway"), tags.get("railway")
    pt, amenity, station = tags.get("public_transport"), tags.get("amenity"), tags.get("station")
    if railway == "subway_entrance":
        return "subway", "entrance"
    if station == "subway" or (railway in ("station", "halt") and yes("subway")):
        return "subway", "station"
    if railway in ("station", "halt"):
        if station in ("light_rail", "monorail", "funicular") or not station or station == "train":
            return "train", "station"
        return None
    if railway == "tram_stop":
        return "tram", "stop"
    if highway == "bus_stop":
        return "bus", "platform"
    if pt == "platform":
        if yes("tram"):
            return "tram", "platform"
        if yes("bus") or yes("trolleybus"):
            return "bus", "platform"
        if yes("train") or yes("light_rail"):
            return "train", "platform"
        if yes("subway"):
            return "subway", "platform"
        return None
    if railway == "platform":
        return "train", "platform"
    if amenity == "bus_station":
        return "bus", "station"
    if amenity == "taxi":
        return "taxi", "station"
    if amenity == "ferry_terminal":
        return "ferry", "station"
    return None


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
    out = pbf.replace(".osm.pbf", ".transit.osm.pbf")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(pbf):
        return out
    print(f"  filteren met osmium-tool -> {os.path.basename(out)}")
    subprocess.run([exe, "tags-filter", "--overwrite", "-o", out, pbf,
                    "n/highway=bus_stop", "nw/public_transport=platform",
                    "n/railway=tram_stop,subway_entrance", "nw/railway=station,halt,platform",
                    "nw/station=subway", "nw/amenity=taxi,ferry_terminal,bus_station"], check=True)
    return out


class Collector(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.elements: dict[str, str] = {}
        self.tiles: dict[tuple[int, int], set[str]] = defaultdict(set)
        self.counts: dict[str, int] = defaultdict(int)

    def _add(self, key: str, tags, lon: float, lat: float) -> None:
        if key in self.elements:
            return
        if any(t.k.startswith(SKIP_PREFIXES) for t in tags):
            return
        kr = classify(tags)
        if kr is None:
            return
        kind, role = kr
        lon, lat = round(lon, 6), round(lat, 6)
        element = {"type": "stop", "id": key, "kind": kind, "role": role, "lon": lon, "lat": lat}
        if tags.get("name"):
            element["name"] = tags["name"]
            names = {lang: tags[f"name:{lang}"] for lang in NAME_LANGUAGES if f"name:{lang}" in tags}
            if names:
                element["names"] = names
        for k in KEEP:
            if tags.get(k):
                element[k] = tags[k]
        self.elements[key] = json.dumps(element, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        self.tiles[lonlat_to_tile(lon, lat)].add(key)
        self.counts[kind] += 1

    def node(self, n):
        if n.tags and n.location.valid():
            self._add(f"n{n.id}", n.tags, n.location.lon, n.location.lat)

    def way(self, w):
        if not w.tags or classify(w.tags) is None:
            return
        coords = []
        for nr in w.nodes:
            if not nr.location.valid():
                return
            coords.append((nr.location.lon, nr.location.lat))
        if not coords:
            return
        if len(coords) == 1:
            lon, lat = coords[0]
        elif len(coords) >= 4 and coords[0] == coords[-1]:
            p = Polygon(coords).buffer(0).representative_point()   # gesloten: station of perron als vlak
            lon, lat = p.x, p.y
        else:
            p = LineString(coords).interpolate(0.5, normalized=True)  # perron als lijn: het midden
            lon, lat = p.x, p.y
        self._add(f"w{w.id}", w.tags, lon, lat)


def tile_bytes(elements: list[str]) -> bytes:
    text = f'{{"version":{FORMAT_VERSION},"elements":[' + ",".join(elements) + "]}"
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def write_cells(root: str, cells, collector: Collector) -> list[int]:
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
    """Eén gebied: eigen tegels schrijven, randtegels bewaren voor merge_parts.py."""
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
    sizes = write_cells(os.path.join(out, "final", LAYER), final, collector)
    cells = {pc.cell_key(c): sorted(collector.tiles[c]) for c in sorted(border)}
    keys = {k for ks in cells.values() for k in ks}
    pc.write_json_gz(os.path.join(out, f"{LAYER}_border.json.gz"),
                     {"cells": cells, "elements": {k: collector.elements[k] for k in sorted(keys)}})
    meta = {"id": region["id"], "filled": pc.cells_to_list(filled), "border": pc.cells_to_list(border),
            "final": [tile_path(x, y) for x, y in sorted(final)], "final_sizes": sizes,
            # Alle elementen van dit gebied: merge_parts.py ziet zo of een tegel geraakt door een buur geen verschil maakt.
            "keys": sorted(collector.elements),
            "counts": dict(sorted(collector.counts.items())), "duration_seconds": round(time.time() - started)}
    pc.write_json_gz(os.path.join(out, f"{LAYER}_meta.json.gz"), meta)
    print(f"  {len(final)} eigen tegels ({sum(sizes) / 1e6:.1f} MB), {len(border)} randtegels, "
          f"{dict(collector.counts)}, {meta['duration_seconds']} s")


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
        collector.apply_file(prefilter(pbf), locations=True, idx="flex_mem")
    root = os.path.join(args.out, LAYER)
    total = sum(write_cells(root, set(collector.tiles), collector))
    print(f"\n{len(collector.tiles)} tegels, samen {total / 1e6:.1f} MB, {dict(collector.counts)}, "
          f"{time.time() - started:.0f} s")
    with open(os.path.join(root, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"tiles": len(collector.tiles), "bytes_total": total, "counts": dict(sorted(collector.counts.items())),
                   "duration_seconds": round(time.time() - started)}, f, indent=2)


if __name__ == "__main__":
    main()
