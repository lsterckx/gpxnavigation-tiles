#!/usr/bin/env python3
"""
Laatste stap van de gesplitste run: voegt de randtegels van alle gebieden samen (zie parts_common.py).

Leest per gebied uit parts/<id>/ (of parts/part-<id>/, zoals GitHub Actions de artefacten neerzet):
    tiles_meta.json.gz, tiles_border.json.gz, water_meta.json.gz, water_border.json.gz, <id>.poly,
    areas/<id>.json.gz en areas/index.json
en schrijft naar out/:
    14/{x}/{y}.json.gz          de samengevoegde randtegels (wegen)
    water/14/{x}/{y}.json.gz    de samengevoegde randtegels (water)
    areas/<id>.json.gz, areas/index.json
    regions.json, status.json, water/status.json
    expected-14.txt, expected-water.txt   alle tegels die er na deze run moeten zijn (eigen tegels van alle
                                          gebieden + samengevoegde), om verdwenen tegels in R2 te wissen.

Controle: een eigen tegel van een gebied mag niet geraakt worden door een ander gebied (zijn omtrek, zijn wegen of
water, of voor lege tegels: een buurtegel met zijn wegen). Anders was de marge te klein en stopt dit script met een
fout (exit-code 3): verhoog dan border_margin_degrees in regions.yml.

Gebruik:
    python3 scripts/merge_parts.py --regions regions.yml --parts parts --out out
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import statistics
import sys
import time
from collections import defaultdict

import shapely
from shapely.ops import unary_union

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parts_common as pc  # noqa: E402
from build_tiles import FORMAT_VERSION, read_poly, tile_bytes  # noqa: E402
from build_water import tile_bytes as water_tile_bytes  # noqa: E402
from tiles_common import ZOOM, tile_bounds, tile_path  # noqa: E402


def find_part(parts: str, region_id: str) -> str:
    for name in (region_id, f"part-{region_id}"):
        d = os.path.join(parts, name)
        if os.path.isfile(os.path.join(d, "tiles_meta.json.gz")):
            return d
    sys.exit(f"Deel ontbreekt voor {region_id} in {parts}/ (is die job mislukt?)")


def check_exclusive(name: str, parts: list[dict], influence_of) -> None:
    """Geen eigen tegel van een gebied mag in het invloedsgebied van een ander gebied liggen."""
    owner: dict[tuple[int, int], set[int]] = defaultdict(set)
    for i, p in enumerate(parts):
        for c in influence_of(p):
            owner[c].add(i)
    problems = []
    for i, p in enumerate(parts):
        for c in p["exclusive"]:
            others = owner.get(c, set()) - {i}
            if others:
                problems.append((p["id"], c, sorted(parts[j]["id"] for j in others)))
    if problems:
        print(f"FOUT ({name}): {len(problems)} eigen tegels worden ook door een ander gebied geraakt; "
              f"de marge (border_margin_degrees in regions.yml) is te klein. Voorbeelden:", file=sys.stderr)
        for rid, (x, y), others in problems[:10]:
            lo0, la0, lo1, la1 = tile_bounds(x, y)
            print(f"  {tile_path(x, y)} van {rid}, geraakt door {', '.join(others)} "
                  f"(rond {(la0 + la1) / 2:.4f}, {(lo0 + lo1) / 2:.4f})", file=sys.stderr)
        sys.exit(3)
    print(f"controle {name}: geen eigen tegel geraakt door een ander gebied")


def merge_roads(parts: list[dict], union_shape, out: str) -> list[tuple[str, int, int]]:
    filled_all = set().union(*(p["filled"] for p in parts))
    touching_all = set().union(*(p["touching"] for p in parts))
    near_all = pc.neighbors(filled_all)
    border_all = set().union(*(p["border"] for p in parts))
    cells = sorted(c for c in border_all if c in filled_all or (c in touching_all and c in near_all))

    # Per element de versie van het eerste gebied in regions.yml (parts staat in die volgorde).
    nodes, ways = {}, {}
    for p in parts:
        for k, v in p["bundle"]["nodes"].items():
            nodes.setdefault(int(k), v)
        for k, v in p["bundle"]["ways"].items():
            ways.setdefault(int(k), v)

    shapely.prepare(union_shape)
    boxes = [shapely.box(*tile_bounds(x, y)) for x, y in cells]
    inside = shapely.contains(union_shape, boxes) if boxes else []

    written = []
    for (x, y), complete in zip(cells, inside):
        node_ids, way_ids = set(), set()
        for p in parts:
            entry = p["bundle"]["cells"].get(pc.cell_key((x, y)))
            if entry:
                node_ids.update(entry["n"])
                way_ids.update(entry["w"])
        elements = [nodes[i] for i in sorted(node_ids)] + [ways[i] for i in sorted(way_ids)]
        data = tile_bytes(elements, bool(complete))
        rel = tile_path(x, y)
        path = os.path.join(out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        written.append((rel, len(data), len(elements), bool(complete)))
    return written


def merge_water(parts: list[dict], out: str) -> list[tuple[str, int]]:
    elements: dict[str, str] = {}
    cell_keys: dict[str, set[str]] = defaultdict(set)
    for p in parts:
        w = p["water_bundle"]
        for k, v in w["elements"].items():
            elements.setdefault(k, v)
        for cell, keys in w["cells"].items():
            cell_keys[cell].update(keys)
    written = []
    for cell in sorted(cell_keys, key=pc.key_cell):
        x, y = pc.key_cell(cell)
        data = water_tile_bytes([elements[k] for k in sorted(cell_keys[cell])])
        rel = tile_path(x, y)
        path = os.path.join(out, "water", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        written.append((rel, len(data)))
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regions", default="regions.yml")
    ap.add_argument("--parts", default="parts")
    ap.add_argument("--out", default="out")
    ap.add_argument("--max-gb", type=float, default=8.0,
                    help="mislukken (exit-code 2) als alle tegels samen groter zijn dan dit")
    args = ap.parse_args()

    started = time.time()
    regions = pc.enabled_regions(pc.load_config(args.regions))
    parts = []
    for r in regions:
        d = find_part(args.parts, r["id"])
        meta = pc.read_json_gz(os.path.join(d, "tiles_meta.json.gz"))
        wmeta = pc.read_json_gz(os.path.join(d, "water_meta.json.gz"))
        p = {
            "id": r["id"], "dir": d, "meta": meta, "water_meta": wmeta,
            "touching": pc.list_to_cells(meta["touching"]),
            "filled": pc.list_to_cells(meta["filled"]),
            "border": pc.list_to_cells(meta["border"]),
            "bundle": pc.read_json_gz(os.path.join(d, "tiles_border.json.gz")),
            "wfilled": pc.list_to_cells(wmeta["filled"]),
            "wborder": pc.list_to_cells(wmeta["border"]),
            "water_bundle": pc.read_json_gz(os.path.join(d, "water_border.json.gz")),
            "shape": read_poly(os.path.join(d, f"{r['id']}.poly")),
        }
        p["filled_near"] = pc.neighbors(p["filled"])
        parts.append(p)
        print(f"{r['name']}: {len(meta['final'])} eigen tegels, {len(p['border'])} randtegels")

    # Controle: wegen (omtrek, wegen en hun buurtegels) en water (alleen tegels met water).
    for p in parts:
        p["exclusive"] = (p["touching"] | p["filled"]) - p["border"]
    check_exclusive("wegen", parts, lambda p: p["touching"] | p["filled_near"])
    for p in parts:
        p["exclusive"] = p["wfilled"] - p["wborder"]
    check_exclusive("water", parts, lambda p: p["wfilled"])

    for sub in (str(ZOOM), os.path.join("water", str(ZOOM)), "areas"):
        if os.path.isdir(os.path.join(args.out, sub)):
            shutil.rmtree(os.path.join(args.out, sub))
    os.makedirs(args.out, exist_ok=True)

    union_shape = unary_union([p["shape"] for p in parts])
    merged = merge_roads(parts, union_shape, args.out)
    merged_water = merge_water(parts, args.out)
    print(f"{len(merged)} samengevoegde randtegels, {len(merged_water)} samengevoegde watertegels")

    # Lijsten van alle tegels na deze run (paden zoals onder 14/ en water/14/ in de bucket).
    prefix = f"{ZOOM}/"
    expected = sorted({f for p in parts for f in p["meta"]["final"]} | {rel for rel, *_ in merged})
    expected_water = sorted({f for p in parts for f in p["water_meta"]["final"]} | {rel for rel, _ in merged_water})
    with open(os.path.join(args.out, "expected-14.txt"), "w") as f:
        f.writelines(rel[len(prefix):] + "\n" for rel in expected)
    with open(os.path.join(args.out, "expected-water.txt"), "w") as f:
        f.writelines(rel[len(prefix):] + "\n" for rel in expected_water)

    # Omgevingsgegevens: de bestanden van de delen, één index.
    areas_out = os.path.join(args.out, "areas")
    os.makedirs(areas_out, exist_ok=True)
    generated = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    area_index = {"version": 1, "generated": generated, "regions": []}
    for p in parts:
        idx_path = os.path.join(p["dir"], "areas", "index.json")
        if not os.path.exists(idx_path):
            continue
        with open(idx_path, encoding="utf-8") as f:
            for info in json.load(f)["regions"]:
                shutil.copyfile(os.path.join(p["dir"], "areas", f"{info['id']}.json.gz"),
                                os.path.join(areas_out, f"{info['id']}.json.gz"))
                area_index["regions"].append(info)
    with open(os.path.join(areas_out, "index.json"), "w", encoding="utf-8") as f:
        json.dump(area_index, f, ensure_ascii=False, indent=2)

    # regions.json en status.json (zelfde velden als bij één run).
    regions_json = {
        "version": FORMAT_VERSION, "zoom": ZOOM, "tiles": "{z}/{x}/{y}.json.gz", "generated": generated,
        "regions": [{"id": p["id"], "name": p["meta"]["name"], "bbox": p["meta"]["bbox"],
                     "osm_timestamp": p["meta"]["osm_timestamp"]} for p in parts],
        "attribution": "© OpenStreetMap-bijdragers, ODbL (https://www.openstreetmap.org/copyright)",
    }
    with open(os.path.join(args.out, "regions.json"), "w", encoding="utf-8") as f:
        json.dump(regions_json, f, ensure_ascii=False, indent=2)

    sizes = sorted([s for p in parts for s in p["meta"]["final_sizes"]] + [s for _, s, _, _ in merged])
    largest = [t for p in parts for t in p["meta"]["final_largest"]] + \
              [{"tile": rel, "bytes": s, "elements": n} for rel, s, n, _ in merged]
    largest = sorted(largest, key=lambda t: (-t["bytes"], t["tile"]))[:10]
    status = {
        "generated": generated,
        # De langste job (die bepaalt hoe lang de run duurt) plus het samenvoegen.
        "duration_seconds": max(p["meta"]["duration_seconds"] for p in parts) + round(time.time() - started),
        "regions": [p["id"] for p in parts],
        "tiles": len(sizes),
        "tiles_complete": sum(p["meta"]["final_complete"] for p in parts) + sum(1 for *_, c in merged if c),
        "tiles_empty": sum(p["meta"]["final_empty"] for p in parts) + sum(1 for _, _, n, _ in merged if n == 0),
        "tiles_merged": len(merged),
        # Een weg over een grens telt in beide gebieden mee.
        "ways": sum(p["meta"]["ways"] for p in parts),
        "crossing_nodes": sum(p["meta"]["crossing_nodes"] for p in parts),
        "ways_skipped_incomplete": sum(p["meta"]["ways_skipped_incomplete"] for p in parts),
        "bytes_total": sum(sizes),
        "bytes_median": int(statistics.median(sizes)) if sizes else 0,
        "bytes_p95": sizes[int(len(sizes) * 0.95)] if sizes else 0,
        "bytes_max": sizes[-1] if sizes else 0,
        "largest_tiles": largest,
    }
    with open(os.path.join(args.out, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    water_sizes = [s for p in parts for s in p["water_meta"]["final_sizes"]] + [s for _, s in merged_water]
    os.makedirs(os.path.join(args.out, "water"), exist_ok=True)
    with open(os.path.join(args.out, "water", "status.json"), "w", encoding="utf-8") as f:
        json.dump({"tiles": len(water_sizes), "bytes_total": sum(water_sizes),
                   "waterways": sum(p["water_meta"]["waterways"] for p in parts),
                   "water": sum(p["water_meta"]["water"] for p in parts),
                   "duration_seconds": max(p["water_meta"]["duration_seconds"] for p in parts)}, f, indent=2)

    print(f"\n{status['tiles']} tegels samen {status['bytes_total'] / 1e6:.1f} MB, "
          f"{len(water_sizes)} watertegels samen {sum(water_sizes) / 1e6:.1f} MB")
    total = status["bytes_total"] + sum(water_sizes)
    if total > args.max_gb * 1e9:
        print(f"\nFOUT: de tegels zijn samen {total / 1e9:.2f} GB, meer dan de grens van {args.max_gb} GB.",
              file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
