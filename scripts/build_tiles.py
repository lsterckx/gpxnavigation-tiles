#!/usr/bin/env python3
"""
Maakt de tegels voor gpxnavigation uit OpenStreetMap-bestanden van Geofabrik.

Per tegel (zoom 14) een gzip-JSON in hetzelfde formaat als een antwoord van Overpass
("out body geom"), zodat de app OverpassClient.parse en RouteFeatureMatcher ongewijzigd kan gebruiken:

    {"version":1,"complete":true,"elements":[{"type":"node",...},{"type":"way",...}]}

- Wegen: alle wegen met highway=*, met knoop-id's en volledige geometrie. Een weg die over een
  tegelrand loopt, staat volledig in elke tegel die hij raakt.
- Knopen: oversteekknopen (highway=crossing of crossing=*).
- Tags: alleen wat de app gebruikt (zie tiles_common.KEEP_TAG_KEYS).
- complete: true als de tegel volledig binnen de gebieden ligt. Aan de rand (bv. de grens met
  Frankrijk) ontbreken wegen van over de grens: dan false, en gebruikt de app Overpass.

Er komt een tegel voor elk vak dat de gebieden raakt, ook als er geen weg in ligt (dan is
"elements" leeg), maar alleen naast een tegel met wegen. Een tegel die niet bestaat (404) betekent dus:
buiten de gebieden, of ver van elke weg (zee).

De tegels bevatten geen datum, zodat een tegel waar niets in veranderde byte voor byte gelijk
blijft en bij het opladen overgeslagen wordt. De datum van de gegevens staat in regions.json.

Gebruik (na fetch_regions.py):
    python3 scripts/build_tiles.py --regions regions.yml --data data --out out

Gesplitste run (GitHub Actions, één job per gebied; zie parts_common.py en merge_parts.py):
    python3 scripts/build_tiles.py --part belgium --data data --parts parts
schrijft de eigen tegels naar parts/belgium/final/14/ en de randtegels naar parts/belgium/tiles_border.json.gz.
Nodig: het .osm.pbf van dat gebied en de .poly van alle gebieden (fetch_regions.py --polys-only).
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections import defaultdict

import osmium
import shapely
import yaml
from shapely.geometry import Polygon
from shapely.ops import unary_union

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tiles_common import (  # noqa: E402
    ZOOM, filter_tags, is_crossing_node, line_tiles, lonlat_to_tile, tile_bounds, tile_path,
)
import parts_common as pc  # noqa: E402

FORMAT_VERSION = 1


# ---------- gebieden ----------

def load_regions(path: str, only: list[str] | None) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        regions = yaml.safe_load(f)["regions"]
    if only:
        regions = [r for r in regions if r["id"] in only]
    else:
        regions = [r for r in regions if r.get("enabled", True)]
    if not regions:
        sys.exit("Geen gebieden gekozen.")
    return regions


def read_poly(path: str):
    """Leest een .poly-bestand van Geofabrik (Osmosis-formaat) als shapely-vorm."""
    outers, holes = [], []
    with open(path, encoding="utf-8") as f:
        lines = [l.strip() for l in f]
    i = 1  # eerste regel = naam
    while i < len(lines):
        header = lines[i]
        i += 1
        if header == "END" or header == "":
            continue
        ring = []
        while i < len(lines) and lines[i] != "END":
            parts = lines[i].split()
            if len(parts) >= 2:
                ring.append((float(parts[0]), float(parts[1])))
            i += 1
        i += 1  # END van de ring
        if len(ring) >= 3:
            (holes if header.startswith("!") else outers).append(Polygon(ring).buffer(0))
    shape = unary_union(outers)
    if holes:
        shape = shape.difference(unary_union(holes))
    return shape


def osm_timestamp(pbf: str) -> str | None:
    try:
        reader = osmium.io.Reader(pbf, osmium.osm.osm_entity_bits.NOTHING)
        try:
            return reader.header().get("osmosis_replication_timestamp") or None
        finally:
            reader.close()
    except Exception:
        return None


def prefilter(pbf: str) -> str:
    """
    Met het programma osmium (osmium-tool), als het geinstalleerd is: alleen de wegen, de
    oversteekknopen en de knopen die de wegen nodig hebben. Veel minder geheugen en sneller.
    Zonder osmium-tool werkt het ook, alleen trager.
    """
    exe = shutil.which("osmium")
    if not exe:
        print("  (osmium-tool niet gevonden: het volledige bestand wordt gelezen)")
        return pbf
    out = pbf.replace(".osm.pbf", ".filtered.osm.pbf")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(pbf):
        return out
    print(f"  filteren met osmium-tool -> {os.path.basename(out)}")
    subprocess.run([exe, "tags-filter", "--overwrite", "-o", out, pbf,
                    "w/highway", "n/highway=crossing", "n/crossing"], check=True)
    return out


# ---------- inlezen ----------

class Collector(osmium.SimpleHandler):
    """Verzamelt wegen en oversteekknopen, als JSON-tekst per id, en per tegel de id's."""

    def __init__(self):
        super().__init__()
        self.ways: dict[int, str] = {}
        self.nodes: dict[int, str] = {}
        self.tile_ways: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.tile_nodes: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.skipped_ways = 0

    def node(self, n):
        if n.id in self.nodes or not n.tags:
            return
        tags = {t.k: t.v for t in n.tags}
        if not is_crossing_node(tags) or not n.location.valid():
            return
        lon, lat = round(n.location.lon, 7), round(n.location.lat, 7)
        self.nodes[n.id] = json.dumps(
            {"type": "node", "id": n.id, "lat": lat, "lon": lon, "tags": filter_tags(tags.items())},
            separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        self.tile_nodes[lonlat_to_tile(lon, lat)].append(n.id)

    def way(self, w):
        if w.id in self.ways or "highway" not in w.tags:
            return
        ids, coords = [], []
        for nr in w.nodes:
            if not nr.location.valid():
                # Een knoop buiten het bestand: zoals de app bij Overpass, de weg overslaan.
                self.skipped_ways += 1
                return
            ids.append(nr.ref)
            coords.append((round(nr.location.lon, 7), round(nr.location.lat, 7)))
        if len(coords) < 2:
            return
        self.ways[w.id] = json.dumps(
            {"type": "way", "id": w.id, "nodes": ids,
             "geometry": [{"lat": lat, "lon": lon} for lon, lat in coords],
             "tags": filter_tags((t.k, t.v) for t in w.tags)},
            separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        for cell in line_tiles(coords):
            self.tile_ways[cell].append(w.id)


# ---------- tegels bepalen ----------

def area_tiles(shape) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """(alle tegels die de gebieden raken, de tegels die er volledig binnen liggen)."""
    shapely.prepare(shape)
    parts = list(shape.geoms) if hasattr(shape, "geoms") else [shape]
    touching, inside = set(), set()
    for part in parts:
        min_lon, min_lat, max_lon, max_lat = part.bounds
        x0, y1 = lonlat_to_tile(min_lon, min_lat)
        x1, y0 = lonlat_to_tile(max_lon, max_lat)
        cells = [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
        boxes = [shapely.box(*tile_bounds(x, y)) for x, y in cells]
        hits = shapely.intersects(shape, boxes)
        full = shapely.contains(shape, boxes)
        for cell, hit, f in zip(cells, hits, full):
            if hit:
                touching.add(cell)
            if f:
                inside.add(cell)
    return touching, inside


# ---------- schrijven ----------

def tile_bytes(elements: list[str], complete: bool) -> bytes:
    """Eén tegel: knopen en wegen al gesorteerd (eerst knopen op id, dan wegen op id), gzip zonder datum."""
    text = (f'{{"version":{FORMAT_VERSION},"complete":{"true" if complete else "false"},"elements":['
            + ",".join(elements) + "]}")
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def cells_to_write(filled: set, touching: set) -> set:
    """
    De tegels met wegen of oversteekknopen, plus de lege tegels in de gebieden naast een tegel met wegen.
    Een lege tegel alleen naast een tegel met wegen: de app vraagt alleen tegels op langs een route of rond
    je plaats, en die liggen altijd op of naast een tegel met wegen. Zo vallen de honderdduizenden lege
    tegels op zee binnen de omtrek van Geofabrik weg (Azoren, Canarische Eilanden, Waddenzee).
    """
    return filled | (touching & pc.neighbors(filled))


def write_cells(out_dir: str, cells, collector: Collector, inside: set) -> list[tuple[str, int, int]]:
    """Schrijft de tegels `cells` (map 14/ eerst leeg); geeft (pad, bytes, aantal elementen) per tegel terug."""
    tiles_dir = os.path.join(out_dir, str(ZOOM))
    if os.path.isdir(tiles_dir):
        shutil.rmtree(tiles_dir)
    written = []
    for (x, y) in sorted(cells):
        node_ids = sorted(set(collector.tile_nodes.get((x, y), ())))
        way_ids = sorted(set(collector.tile_ways.get((x, y), ())))
        elements = [collector.nodes[i] for i in node_ids] + [collector.ways[i] for i in way_ids]
        data = tile_bytes(elements, (x, y) in inside)
        rel = tile_path(x, y)
        path = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        written.append((rel, len(data), len(elements)))
    return written


def write_tiles(out_dir: str, collector: Collector, touching: set, inside: set) -> list[tuple[str, int, int]]:
    """Schrijft alle tegels (één run met alle gebieden)."""
    filled = set(collector.tile_ways) | set(collector.tile_nodes)
    return write_cells(out_dir, cells_to_write(filled, touching), collector, inside)


def size_stats(written: list[tuple[str, int, int]]) -> dict:
    sizes = sorted(s for _, s, _ in written)
    largest = sorted(written, key=lambda t: (-t[1], t[0]))[:10]
    return {
        "bytes_total": sum(sizes),
        "bytes_median": int(statistics.median(sizes)) if sizes else 0,
        "bytes_p95": sizes[int(len(sizes) * 0.95)] if sizes else 0,
        "bytes_max": sizes[-1] if sizes else 0,
        "largest_tiles": [{"tile": p, "bytes": s, "elements": n} for p, s, n in largest],
    }


# ---------- gesplitste run: één gebied ----------

def build_part(args) -> None:
    """Eén gebied: eigen tegels schrijven, randtegels en de tegelverzamelingen voor merge_parts.py bewaren."""
    started = time.time()
    config = pc.load_config(args.regions)
    regions = pc.enabled_regions(config)
    ids = [r["id"] for r in regions]
    if args.part not in ids:
        sys.exit(f"{args.part} staat niet (aan) in {args.regions}")
    index = ids.index(args.part)
    region = regions[index]
    out = pc.part_dir(args.parts, region["id"])
    os.makedirs(out, exist_ok=True)

    pbf = os.path.join(args.data, f"{region['id']}.osm.pbf")
    print(f"{region['name']} ({region['id']}), deel {index + 1} van {len(regions)}")
    shape = read_poly(os.path.join(args.data, f"{region['id']}.poly"))
    others = []
    for r in regions:
        if r["id"] != region["id"]:
            poly = os.path.join(args.data, f"{r['id']}.poly")
            if not os.path.exists(poly):
                sys.exit(f"  ontbreekt: {poly} (eerst fetch_regions.py --polys-only)")
            others.append(read_poly(poly))
    shutil.copyfile(os.path.join(args.data, f"{region['id']}.poly"), os.path.join(out, f"{region['id']}.poly"))

    collector = Collector()
    collector.apply_file(prefilter(pbf), locations=True, idx="flex_mem")
    print(f"  {len(collector.ways)} wegen, {len(collector.nodes)} oversteekknopen ({time.time() - started:.0f} s)")

    touching, inside = area_tiles(shape)
    filled = set(collector.tile_ways) | set(collector.tile_nodes)
    zone = pc.border_zone(others, pc.border_margin(config))
    border = pc.cells_in_zone(touching | filled, zone)
    final = cells_to_write(filled, touching) - border
    print(f"  {len(final)} eigen tegels, {len(border)} randtegels")
    written = write_cells(os.path.join(out, "final"), final, collector, inside)

    # Randtegels: per tegel de id's, en de elementen zelf (als JSON-tekst, zoals in de tegel).
    cells, node_texts, way_texts = {}, {}, {}
    for cell in sorted(border & filled):
        node_ids = sorted(set(collector.tile_nodes.get(cell, ())))
        way_ids = sorted(set(collector.tile_ways.get(cell, ())))
        cells[pc.cell_key(cell)] = {"n": node_ids, "w": way_ids}
        for i in node_ids:
            node_texts[str(i)] = collector.nodes[i]
        for i in way_ids:
            way_texts[str(i)] = collector.ways[i]
    pc.write_json_gz(os.path.join(out, "tiles_border.json.gz"),
                     {"cells": cells, "nodes": node_texts, "ways": way_texts})

    meta = {
        "id": region["id"], "name": region["name"], "index": index,
        "bbox": [round(v, 5) for v in shape.bounds],
        "osm_timestamp": osm_timestamp(pbf),
        "touching": pc.cells_to_list(touching),
        "filled": pc.cells_to_list(filled),
        "border": pc.cells_to_list(border),
        "final": [p for p, _, _ in written],
        "final_sizes": [s for _, s, _ in written],
        "final_complete": sum(1 for p, _, _ in written if tuple(map(int, p[:-8].split("/")[1:])) in inside),
        "final_empty": sum(1 for _, _, n in written if n == 0),
        "final_largest": size_stats(written)["largest_tiles"],
        "ways": len(collector.ways),
        "crossing_nodes": len(collector.nodes),
        "ways_skipped_incomplete": collector.skipped_ways,
        "duration_seconds": round(time.time() - started),
    }
    pc.write_json_gz(os.path.join(out, "tiles_meta.json.gz"), meta)
    total = sum(meta["final_sizes"])
    print(f"  eigen tegels samen {total / 1e6:.1f} MB, {meta['duration_seconds']} s")
    if total > args.max_gb * 1e9:
        print(f"\nFOUT: de tegels zijn samen {total / 1e9:.2f} GB, meer dan de grens van {args.max_gb} GB.",
              file=sys.stderr)
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regions", default="regions.yml")
    ap.add_argument("--data", default="data", help="map met <id>.osm.pbf en <id>.poly")
    ap.add_argument("--out", default="out", help="map voor de tegels, regions.json en status.json")
    ap.add_argument("--only", nargs="*", help="alleen deze gebieden (id's uit regions.yml)")
    ap.add_argument("--max-gb", type=float, default=8.0,
                    help="mislukken (exit-code 2) als alle tegels samen groter zijn dan dit")
    ap.add_argument("--part", help="gesplitste run: alleen dit gebied (id uit regions.yml)")
    ap.add_argument("--parts", default="parts", help="map voor de delen van een gesplitste run")
    args = ap.parse_args()
    if args.part:
        build_part(args)
        return

    started = time.time()
    regions = load_regions(args.regions, args.only)
    collector = Collector()
    shapes, region_info = [], []

    for r in regions:
        pbf = os.path.join(args.data, f"{r['id']}.osm.pbf")
        poly = os.path.join(args.data, f"{r['id']}.poly")
        print(f"{r['name']} ({r['id']})")
        if not os.path.exists(pbf) or not os.path.exists(poly):
            sys.exit(f"  ontbreekt: {pbf} of {poly} (eerst fetch_regions.py)")
        shape = read_poly(poly)
        shapes.append(shape)
        t0 = time.time()
        source = prefilter(pbf)
        before_w, before_n = len(collector.ways), len(collector.nodes)
        collector.apply_file(source, locations=True, idx="flex_mem")
        print(f"  {len(collector.ways) - before_w} wegen, {len(collector.nodes) - before_n} oversteekknopen"
              f" ({time.time() - t0:.0f} s)")
        region_info.append({
            "id": r["id"], "name": r["name"],
            "bbox": [round(v, 5) for v in shape.bounds],
            "osm_timestamp": osm_timestamp(pbf),
        })

    print("tegels bepalen ...")
    touching, inside = area_tiles(unary_union(shapes))
    print("tegels schrijven ...")
    written = write_tiles(args.out, collector, touching, inside)

    stats = size_stats(written)
    total = stats["bytes_total"]
    largest = sorted(written, key=lambda t: (-t[1], t[0]))[:10]
    generated = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    status = {
        "generated": generated,
        "duration_seconds": round(time.time() - started),
        "regions": [r["id"] for r in regions],
        "tiles": len(written),
        "tiles_complete": sum(1 for p, _, _ in written if tuple(map(int, p[:-8].split("/")[1:])) in inside),
        "tiles_empty": sum(1 for _, _, n in written if n == 0),
        "ways": len(collector.ways),
        "crossing_nodes": len(collector.nodes),
        "ways_skipped_incomplete": collector.skipped_ways,
        **stats,
    }
    regions_json = {
        "version": FORMAT_VERSION,
        "zoom": ZOOM,
        "tiles": "{z}/{x}/{y}.json.gz",
        "generated": generated,
        "regions": region_info,
        "attribution": "© OpenStreetMap-bijdragers, ODbL (https://www.openstreetmap.org/copyright)",
    }
    with open(os.path.join(args.out, "regions.json"), "w", encoding="utf-8") as f:
        json.dump(regions_json, f, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    mb = total / 1e6
    print(f"\n{len(written)} tegels, samen {mb:.1f} MB "
          f"(mediaan {status['bytes_median'] / 1000:.1f} kB, 95% onder {status['bytes_p95'] / 1000:.1f} kB, "
          f"grootste {status['bytes_max'] / 1000:.1f} kB), {status['tiles_empty']} leeg, "
          f"{status['duration_seconds']} s")
    print("grootste tegels:")
    for p, s, n in largest[:5]:
        x, y = map(int, p[:-8].split("/")[1:])
        lo0, la0, lo1, la1 = tile_bounds(x, y)
        print(f"  {p}: {s / 1000:.0f} kB, {n} elementen, rond {(la0 + la1) / 2:.4f}, {(lo0 + lo1) / 2:.4f}")

    if total > args.max_gb * 1e9:
        print(f"\nFOUT: de tegels zijn samen {total / 1e9:.2f} GB, meer dan de grens van {args.max_gb} GB.",
              file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
