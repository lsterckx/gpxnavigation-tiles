#!/usr/bin/env python3
"""
Laatste stap van de gesplitste run: voegt de randtegels van alle gebieden samen (zie parts_common.py).

Leest per gebied uit parts/<id>/ (of parts/part-<id>/, zoals GitHub Actions de artefacten neerzet):
    tiles_meta.json.gz, tiles_border.json.gz, water_meta.json.gz, water_border.json.gz,
    transit_meta.json.gz, transit_border.json.gz, <id>.poly, areas/<id>.json.gz en areas/index.json
en schrijft naar out/:
    14/{x}/{y}.json.gz          de samengevoegde randtegels (wegen)
    water/14/{x}/{y}.json.gz    de samengevoegde randtegels (water)
    transit/14/{x}/{y}.json.gz  de samengevoegde randtegels (haltes)
    areas/<id>.json.gz, areas/index.json
    regions.json, status.json, water/status.json, transit/status.json
    expected-14.txt, expected-water.txt, expected-transit.txt   alle tegels die er na deze run moeten zijn (eigen tegels van alle
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
from build_transit import tile_bytes as transit_tile_bytes  # noqa: E402
from build_water import tile_bytes as water_tile_bytes  # noqa: E402

# Lagen met elementen per tegel zonder 'complete' (water, haltes): zelfde manier van samenvoegen.
LAYERS = {"water": water_tile_bytes, "transit": transit_tile_bytes}
from tiles_common import ZOOM, tile_bounds, tile_path  # noqa: E402


def find_part(parts: str, region_id: str) -> str:
    for name in (region_id, f"part-{region_id}"):
        d = os.path.join(parts, name)
        if os.path.isfile(os.path.join(d, "tiles_meta.json.gz")):
            return d
    sys.exit(f"Deel ontbreekt voor {region_id} in {parts}/ (is die job mislukt?)")


def element_points(text: str) -> list[tuple[float, float]]:
    """De punten (lon, lat) van een element uit een tegel: weg, knoop, waterloop, watervlak of halte."""
    e = json.loads(text)
    if "geometry" in e and e["geometry"] and isinstance(e["geometry"][0], dict):
        return [(g["lon"], g["lat"]) for g in e["geometry"]]          # weg
    if "geometry" in e:
        return [(g[0], g[1]) for g in e["geometry"]]                    # waterloop
    if "polygons" in e:
        return [(q[0], q[1]) for poly in e["polygons"] for ring in poly for q in ring]   # watervlak
    return [(e["lon"], e["lat"])]                                       # knoop of halte


def has_point_in(texts: list[str], shape, cache: dict) -> bool:
    """Heeft elk element minstens één punt in `shape`? Dan heeft het gebied van `shape` het element zelf ook
    (Geofabrik neemt een weg volledig op als minstens één knoop in het gebied ligt)."""
    for t in texts:
        key = (id(shape), t)
        if key not in cache:
            pts = element_points(t)
            cache[key] = bool(pts) and bool(shapely.intersects(shape, shapely.points(pts)).any())
        if not cache[key]:
            return False
    return True


def check_exclusive(name: str, parts: list[dict], influence_of, harmless) -> None:
    """
    Geen eigen tegel van een gebied mag in het invloedsgebied van een ander gebied liggen, tenzij dat geen verschil
    maakt (`harmless`): de elementen van het andere gebied in en rond die tegel heeft het gebied zelf ook (bv. een
    lange weg over de grens, die in beide Geofabrik-bestanden volledig staat).
    """
    owner: dict[tuple[int, int], set[int]] = defaultdict(set)
    for i, p in enumerate(parts):
        for c in influence_of(p):
            owner[c].add(i)
    problems, ok = [], 0
    for i, p in enumerate(parts):
        for c in p["exclusive"]:
            others = [j for j in owner.get(c, ()) if j != i]
            if not others:
                continue
            bad = [j for j in others if not harmless(p, parts[j], c)]
            if bad:
                problems.append((p["id"], c, sorted(parts[j]["id"] for j in bad)))
            else:
                ok += 1
    if problems:
        print(f"FOUT ({name}): {len(problems)} eigen tegels worden ook door een ander gebied geraakt; "
              f"de marge (border_margin_degrees in regions.yml) is te klein. Voorbeelden:", file=sys.stderr)
        for rid, (x, y), others in problems[:10]:
            lo0, la0, lo1, la1 = tile_bounds(x, y)
            print(f"  {tile_path(x, y)} van {rid}, geraakt door {', '.join(others)} "
                  f"(rond {(la0 + la1) / 2:.4f}, {(lo0 + lo1) / 2:.4f})", file=sys.stderr)
        sys.exit(3)
    extra = f" ({ok} geraakt door elementen die beide gebieden hebben)" if ok else ""
    print(f"controle {name}: geen eigen tegel geraakt door een ander gebied{extra}")


def roads_harmless(cache: dict):
    """Eigen tegel c van r, geraakt door s: geen verschil als s c niet raakt met zijn omtrek en r alle elementen van
    s in c en de buurtegels zelf ook heeft."""
    def check(r: dict, s: dict, c: tuple[int, int]) -> bool:
        if c in s["touching"]:
            return False
        for k in pc.neighbors({c}) & s["filled"]:
            entry = s["bundle"]["cells"].get(pc.cell_key(k))
            if entry is None:          # s gaf die tegel niet door: niet na te gaan
                return False
            texts = [s["bundle"]["nodes"][str(i)] for i in entry["n"]] + \
                    [s["bundle"]["ways"][str(i)] for i in entry["w"]]
            if not has_point_in(texts, r["shape_check"], cache):
                return False
        return True
    return check


def layer_harmless(layer: str):
    """Zelfde voor water en haltes (alleen de tegel zelf telt): r heeft alle elementen van s in die tegel zelf ook
    (de lijst met alle elementen van r staat in zijn meta; een vereenvoudigd watervlak heeft niet al zijn punten)."""
    def check(r: dict, s: dict, c: tuple[int, int]) -> bool:
        keys = s["layers"][layer]["bundle"]["cells"].get(pc.cell_key(c))
        if keys is None:
            return False
        own = r["layers"][layer].setdefault("keyset", set(r["layers"][layer]["meta"].get("keys", ())))
        return all(k in own for k in keys)
    return check


def merge_roads(parts: list[dict], union_shape, out: str) -> list[tuple[str, int, int]]:
    filled_all = set().union(*(p["filled"] for p in parts))
    touching_all = set().union(*(p["touching"] for p in parts))
    near_all = pc.neighbors(filled_all)
    # Niet de eigen tegels van een gebied (ook niet als een ander gebied ze raakte met elementen die beide hebben):
    # die heeft dat gebied al volledig gemaakt.
    border_all = set().union(*(p["border"] for p in parts)) - set().union(*(p["excl_roads"] for p in parts))
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


def merge_layer(parts: list[dict], layer: str, out: str) -> list[tuple[str, int]]:
    """Randtegels van een laag zonder 'complete' (water, haltes): alle elementen, eerste gebied wint."""
    elements: dict[str, str] = {}
    cell_keys: dict[str, set[str]] = defaultdict(set)
    for p in parts:
        b = p["layers"][layer]["bundle"]
        for k, v in b["elements"].items():
            elements.setdefault(k, v)
        for cell, keys in b["cells"].items():
            cell_keys[cell].update(keys)
    written = []
    exclusive = set().union(*(p["excl_" + layer] for p in parts))
    for cell in sorted(cell_keys, key=pc.key_cell):
        if pc.key_cell(cell) in exclusive:
            continue
        x, y = pc.key_cell(cell)
        data = LAYERS[layer]([elements[k] for k in sorted(cell_keys[cell])])
        rel = tile_path(x, y)
        path = os.path.join(out, layer, rel)
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
        p = {
            "id": r["id"], "dir": d, "meta": meta,
            "touching": pc.list_to_cells(meta["touching"]),
            "filled": pc.list_to_cells(meta["filled"]),
            "border": pc.list_to_cells(meta["border"]),
            "bundle": pc.read_json_gz(os.path.join(d, "tiles_border.json.gz")),
            "shape": read_poly(os.path.join(d, f"{r['id']}.poly")),
            "layers": {},
        }
        for layer in LAYERS:
            lmeta = pc.read_json_gz(os.path.join(d, f"{layer}_meta.json.gz"))
            p["layers"][layer] = {
                "meta": lmeta,
                "filled": pc.list_to_cells(lmeta["filled"]),
                "border": pc.list_to_cells(lmeta["border"]),
                "bundle": pc.read_json_gz(os.path.join(d, f"{layer}_border.json.gz")),
            }
        p["filled_near"] = pc.neighbors(p["filled"])
        parts.append(p)
        print(f"{r['name']}: {len(meta['final'])} eigen tegels, {len(p['border'])} randtegels")

    # Controle: wegen (omtrek, wegen en hun buurtegels) en water (alleen tegels met water).
    for p in parts:
        p["exclusive"] = p["excl_roads"] = (p["touching"] | p["filled"]) - p["border"]
    cache: dict = {}
    for p in parts:
        # Iets ruimer dan de omtrek: coördinaten in de tegels zijn afgerond (water op 5 decimalen, ±1 m).
        p["shape_check"] = p["shape"].buffer(0.00002)
        shapely.prepare(p["shape_check"])
    check_exclusive("wegen", parts, lambda p: p["touching"] | p["filled_near"], roads_harmless(cache))
    for layer in LAYERS:
        for p in parts:
            p["exclusive"] = p["excl_" + layer] = p["layers"][layer]["filled"] - p["layers"][layer]["border"]
        check_exclusive(layer, parts, lambda p, layer=layer: p["layers"][layer]["filled"], layer_harmless(layer))

    for sub in [str(ZOOM), "areas"] + [os.path.join(layer, str(ZOOM)) for layer in LAYERS]:
        if os.path.isdir(os.path.join(args.out, sub)):
            shutil.rmtree(os.path.join(args.out, sub))
    os.makedirs(args.out, exist_ok=True)

    union_shape = unary_union([p["shape"] for p in parts])
    merged = merge_roads(parts, union_shape, args.out)
    merged_layers = {layer: merge_layer(parts, layer, args.out) for layer in LAYERS}
    print(f"{len(merged)} samengevoegde randtegels; " +
          ", ".join(f"{layer}: {len(m)}" for layer, m in merged_layers.items()))

    # Lijsten van alle tegels na deze run (paden zoals onder 14/ en water/14/ in de bucket).
    prefix = f"{ZOOM}/"
    expected = sorted({f for p in parts for f in p["meta"]["final"]} | {rel for rel, *_ in merged})
    with open(os.path.join(args.out, "expected-14.txt"), "w") as f:
        f.writelines(rel[len(prefix):] + "\n" for rel in expected)
    for layer, merged_layer in merged_layers.items():
        files = sorted({f for p in parts for f in p["layers"][layer]["meta"]["final"]} | {rel for rel, _ in merged_layer})
        with open(os.path.join(args.out, f"expected-{layer}.txt"), "w") as f:
            f.writelines(rel[len(prefix):] + "\n" for rel in files)

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

    layer_bytes = 0
    for layer, merged_layer in merged_layers.items():
        sizes_l = [s for p in parts for s in p["layers"][layer]["meta"]["final_sizes"]] + [s for _, s in merged_layer]
        layer_bytes += sum(sizes_l)
        metas = [p["layers"][layer]["meta"] for p in parts]
        lstatus = {"tiles": len(sizes_l), "bytes_total": sum(sizes_l),
                   "duration_seconds": max(m["duration_seconds"] for m in metas)}
        if layer == "water":
            lstatus.update(waterways=sum(m["waterways"] for m in metas), water=sum(m["water"] for m in metas))
        else:
            counts: dict[str, int] = defaultdict(int)
            for m in metas:
                for k, v in m["counts"].items():
                    counts[k] += v
            lstatus["counts"] = dict(sorted(counts.items()))
        os.makedirs(os.path.join(args.out, layer), exist_ok=True)
        with open(os.path.join(args.out, layer, "status.json"), "w", encoding="utf-8") as f:
            json.dump(lstatus, f, indent=2)
        print(f"{layer}: {lstatus['tiles']} tegels, samen {lstatus['bytes_total'] / 1e6:.1f} MB")

    print(f"{status['tiles']} wegtegels samen {status['bytes_total'] / 1e6:.1f} MB")
    total = status["bytes_total"] + layer_bytes
    if total > args.max_gb * 1e9:
        print(f"\nFOUT: de tegels zijn samen {total / 1e9:.2f} GB, meer dan de grens van {args.max_gb} GB.",
              file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
