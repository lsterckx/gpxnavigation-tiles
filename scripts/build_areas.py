#!/usr/bin/env python3
"""
Maakt per gebied een klein bestand met gemeenten, deelgemeenten en plaatsen, voor "Omgeving" in de app:
in welke (deel)gemeente ligt een punt, welke gemeenten grenzen eraan, welke deelgemeenten en steden liggen
in de buurt.

    out/areas/<gebied>.json.gz   (gzip-JSON, zie hieronder)
    out/areas/index.json         per gebied: id, naam, omkadering, bestand, datum van de gegevens

Uit hetzelfde Geofabrik-bestand als de tegels:
- gemeenten: boundary=administrative met admin_level = municipality_level (standaard 8; Portugal 7);
- deelgemeenten: admin_level = section_level (standaard 9; Portugal 8 = freguesia);
  beide met een vereenvoudigde grens (±40 m, genoeg om te weten waar een punt ligt);
- buren: gemeenten waarvan de (niet vereenvoudigde) grenzen elkaar raken; OpenStreetMap tekent de grens
  tussen twee gemeenten met dezelfde lijn, dus ze raken echt;
- plaatsen: place=city, town of village met naam, vertalingen en inwonersaantal.

Formaat (version 1):
    {"version":1, "region":"belgium",
     "municipalities":[{"name":"Tervuren","names":{"nl":..,"fr":..},"center":[lon,lat],
                        "bbox":[minlon,minlat,maxlon,maxlat],"polygons":[[[[lon,lat],...] (buitenring), ...gaten]],
                        "neighbors":[index, ...]}],
     "sections":[{"name":"Vossem","names":{..},"center":[lon,lat],"bbox":[..],"polygons":[..],"municipality":index}],
     "places":[{"name":"Leuven","names":{..},"place":"city","population":101396,"lon":..,"lat":..}]}

Gebruik (na fetch_regions.py):
    python3 scripts/build_areas.py --regions regions.yml --data data --out out
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import shutil
import subprocess
import sys
import time

import osmium
import shapely
import yaml
from shapely import wkb

FORMAT_VERSION = 1
SIMPLIFY_DEGREES = 0.0004          # ±30–45 m
NAME_LANGUAGES = ("nl", "fr", "de", "en", "es", "pt")
PLACE_KINDS = {"city", "town", "village"}


def load_regions(path: str, only: list[str] | None) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        regions = yaml.safe_load(f)["regions"]
    if only:
        return [r for r in regions if r["id"] in only]
    return [r for r in regions if r.get("enabled", True)]


def prefilter(pbf: str) -> str:
    """Met osmium-tool: alleen de administratieve grenzen (met hun lijnen en knopen) en de plaatsen."""
    exe = shutil.which("osmium")
    if not exe:
        print("  (osmium-tool niet gevonden: het volledige bestand wordt gelezen)")
        return pbf
    out = pbf.replace(".osm.pbf", ".areas.osm.pbf")
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(pbf):
        return out
    print(f"  filteren met osmium-tool -> {os.path.basename(out)}")
    subprocess.run([exe, "tags-filter", "--overwrite", "-o", out, pbf,
                    "r/boundary=administrative", "n/place=city,town,village"], check=True)
    return out


def names(tags) -> dict[str, str]:
    return {lang: tags[f"name:{lang}"] for lang in NAME_LANGUAGES if f"name:{lang}" in tags}


def population(tags) -> int | None:
    v = tags.get("population", "").replace(".", "").replace(",", "").replace(" ", "")
    return int(v) if v.isdigit() else None


class Collector(osmium.SimpleHandler):
    def __init__(self, municipality_level: str, section_level: str | None):
        super().__init__()
        self.levels = {municipality_level: "municipality"}
        if section_level:
            self.levels[section_level] = "section"
        self.factory = osmium.geom.WKBFactory()
        self.areas: dict[str, list[dict]] = {"municipality": [], "section": []}
        self.places: list[dict] = []

    def node(self, n):
        place = n.tags.get("place")
        name = n.tags.get("name")
        if place in PLACE_KINDS and name and n.location.valid():
            self.places.append({
                "name": name, "names": names(n.tags), "place": place,
                "population": population(n.tags),
                "lon": round(n.location.lon, 5), "lat": round(n.location.lat, 5),
            })

    def area(self, a):
        if a.tags.get("boundary") != "administrative":
            return
        kind = self.levels.get(a.tags.get("admin_level", ""))
        name = a.tags.get("name")
        if not kind or not name:
            return
        try:
            geom = wkb.loads(self.factory.create_multipolygon(a), hex=True)
        except Exception:  # noqa: BLE001 - kapotte grens in OpenStreetMap: overslaan
            return
        if geom.is_empty:
            return
        self.areas[kind].append({"name": name, "names": names(a.tags), "geom": geom,
                                 "osm_id": a.orig_id(), "from_way": a.from_way()})


def rings(geom) -> list:
    """Vereenvoudigde polygonen als [[buitenring, gat, ...], ...] met [lon, lat] op 5 decimalen."""
    simple = geom.simplify(SIMPLIFY_DEGREES, preserve_topology=True)
    parts = list(simple.geoms) if hasattr(simple, "geoms") else [simple]
    result = []
    for p in parts:
        if p.is_empty or p.geom_type != "Polygon":
            continue
        result.append([[[round(x, 5), round(y, 5)] for x, y in ring.coords]
                       for ring in [p.exterior, *p.interiors]])
    return result


def center_of(area: dict, places: list[dict], tree, place_points) -> list[float]:
    """Het centrum: de plaats (dorp of stad) met dezelfde naam binnen de grens, anders een punt in de grens."""
    geom = area["geom"]
    for i in tree.query(geom, predicate="contains"):
        p = places[i]
        if p["name"] == area["name"] or area["name"] in p["names"].values():
            return [p["lon"], p["lat"]]
    pt = geom.representative_point()
    return [round(pt.x, 5), round(pt.y, 5)]


def build_region(region: dict, data: str) -> tuple[dict, dict]:
    pbf = os.path.join(data, f"{region['id']}.osm.pbf")
    if not os.path.exists(pbf):
        sys.exit(f"ontbreekt: {pbf} (eerst fetch_regions.py)")
    t0 = time.time()
    section_level = region.get("section_level", 9)   # None in regions.yml = geen deelgemeenten
    collector = Collector(str(region.get("municipality_level", 8)), str(section_level) if section_level else None)
    collector.apply_file(prefilter(pbf), locations=True, idx="flex_mem")

    places = sorted(collector.places, key=lambda p: (p["name"], p["lon"], p["lat"]))
    place_points = [shapely.Point(p["lon"], p["lat"]) for p in places]
    place_tree = shapely.STRtree(place_points)

    def dedupe(items: list[dict]) -> list[dict]:
        # Dezelfde grens als relatie én als gesloten lijn: één keer.
        seen, out = set(), []
        for a in sorted(items, key=lambda a: (a["name"], a["from_way"], a["osm_id"])):
            key = (a["name"], round(a["geom"].area, 9))
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
        return out

    municipalities = dedupe(collector.areas["municipality"])
    sections = dedupe(collector.areas["section"])

    # Buren: grenzen die elkaar raken.
    geoms = [m["geom"] for m in municipalities]
    for g in geoms:
        shapely.prepare(g)
    tree = shapely.STRtree(geoms)
    neighbors: list[set[int]] = [set() for _ in municipalities]
    for i, g in enumerate(geoms):
        for j in tree.query(g, predicate="intersects"):
            j = int(j)
            if j != i:
                neighbors[i].add(j)
                neighbors[j].add(i)

    # Deelgemeente -> gemeente: de gemeente die een punt van de deelgemeente bevat.
    section_parent: list[int | None] = []
    for s in sections:
        pt = s["geom"].representative_point()
        hits = [int(j) for j in tree.query(pt, predicate="within")] or \
               [int(j) for j in tree.query(pt, predicate="intersects")]
        section_parent.append(hits[0] if hits else None)

    def out_area(a: dict) -> dict:
        b = a["geom"].bounds
        return {"name": a["name"], "names": a["names"],
                "center": center_of(a, places, place_tree, place_points),
                "bbox": [round(v, 5) for v in b], "polygons": rings(a["geom"])}

    result = {
        "version": FORMAT_VERSION,
        "region": region["id"],
        "municipalities": [dict(out_area(m), neighbors=sorted(neighbors[i])) for i, m in enumerate(municipalities)],
        "sections": [dict(out_area(s), municipality=section_parent[i]) for i, s in enumerate(sections)],
        "places": places,
    }
    if municipalities:
        b = shapely.union_all([shapely.box(*m["geom"].bounds) for m in municipalities]).bounds
    elif places:
        b = (min(p["lon"] for p in places), min(p["lat"] for p in places),
             max(p["lon"] for p in places), max(p["lat"] for p in places))
    else:
        b = (0, 0, 0, 0)
    info = {"id": region["id"], "name": region["name"], "bbox": [round(v, 5) for v in b],
            "file": f"areas/{region['id']}.json.gz",
            "counts": {"municipalities": len(municipalities), "sections": len(sections), "places": len(places)},
            "seconds": round(time.time() - t0)}
    return result, info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--regions", default="regions.yml")
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="out")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()

    out_dir = os.path.join(args.out, "areas")
    os.makedirs(out_dir, exist_ok=True)
    index = {"version": FORMAT_VERSION,
             "generated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
             "regions": []}
    for region in load_regions(args.regions, args.only):
        print(f"{region['name']} ({region['id']})")
        result, info = build_region(region, args.data)
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        data = gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)
        with open(os.path.join(out_dir, f"{region['id']}.json.gz"), "wb") as f:
            f.write(data)
        info["bytes"] = len(data)
        index["regions"].append(info)
        c = info["counts"]
        print(f"  {c['municipalities']} gemeenten, {c['sections']} deelgemeenten, {c['places']} plaatsen, "
              f"{len(data) / 1000:.0f} kB, {info['seconds']} s")
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
