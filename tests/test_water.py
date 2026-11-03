#!/usr/bin/env python3
"""
Test zonder internet voor build_water.py: een beek met naam over een tegelrand, een naamloze gracht, een vijver,
een rivierbedding (moet wegvallen) en een te klein naamloos vlak. Gebruik:  python3 tests/test_water.py
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile

import osmium

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from tiles_common import lonlat_to_tile, tile_path  # noqa: E402


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(f"MISLUKT: {msg}")
    print(f"ok  {msg}")


def make_sample(path: str, lon: float, lat: float) -> None:
    w = osmium.SimpleWriter(path)
    nid = [1]

    def node(x, y):
        i = nid[0]; nid[0] += 1
        w.add_node(osmium.osm.mutable.Node(id=i, location=(x, y), tags={}))
        return i

    beek = [node(lon - 0.01, lat), node(lon + 0.03, lat)]          # ±2,8 km: over een tegelrand
    gracht = [node(lon, lat + 0.002), node(lon + 0.002, lat + 0.002)]
    s = 0.0008                                                        # ±60 x 90 m
    vijver = [node(lon, lat - 0.003), node(lon + s, lat - 0.003), node(lon + s, lat - 0.003 + s), node(lon, lat - 0.003 + s)]
    bedding = [node(lon + 0.005, lat), node(lon + 0.006, lat), node(lon + 0.006, lat + 0.001), node(lon + 0.005, lat + 0.001)]
    t = 0.0001                                                        # ±7 x 11 m = te klein
    plas = [node(lon + 0.01, lat + 0.01), node(lon + 0.01 + t, lat + 0.01), node(lon + 0.01 + t, lat + 0.01 + t), node(lon + 0.01, lat + 0.01 + t)]
    w.add_way(osmium.osm.mutable.Way(id=10, nodes=beek, tags={"waterway": "stream", "name": "Voer", "name:fr": "Voer"}))
    w.add_way(osmium.osm.mutable.Way(id=11, nodes=gracht, tags={"waterway": "ditch"}))
    w.add_way(osmium.osm.mutable.Way(id=12, nodes=vijver + [vijver[0]], tags={"natural": "water", "water": "pond", "name": "Grote Vijver"}))
    w.add_way(osmium.osm.mutable.Way(id=13, nodes=bedding + [bedding[0]], tags={"natural": "water", "water": "river"}))
    w.add_way(osmium.osm.mutable.Way(id=14, nodes=plas + [plas[0]], tags={"natural": "water"}))
    w.add_way(osmium.osm.mutable.Way(id=15, nodes=[vijver[0], vijver[1]], tags={"highway": "footway"}))
    w.close()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data, out = os.path.join(tmp, "data"), os.path.join(tmp, "out")
        os.makedirs(data)
        lon, lat = 4.51, 50.82
        make_sample(os.path.join(data, "test.osm.pbf"), lon, lat)
        regions = os.path.join(tmp, "regions.yml")
        with open(regions, "w") as f:
            f.write("regions:\n  - id: test\n    name: Test\n    geofabrik: test/test\n")
        env = dict(os.environ, PATH="/nonexistent")
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_water.py"),
                        "--regions", regions, "--data", data, "--out", out], check=True, env=env)

        def tile(x, y):
            p = os.path.join(out, "water", tile_path(x, y))
            if not os.path.exists(p):
                return None
            with gzip.open(p, "rt", encoding="utf-8") as f:
                return json.load(f)

        x, y = lonlat_to_tile(lon, lat)
        t = tile(x, y)
        check(t is not None and t["version"] == 1, "watertegel bestaat")
        by = {(e["type"], str(e["id"])): e for e in t["elements"]}
        check(("waterway", "10") in by and by[("waterway", "10")]["name"] == "Voer", "beek met naam")
        check(by[("waterway", "10")]["geometry"][0][0] < by[("waterway", "10")]["geometry"][-1][0], "stroomrichting behouden")
        check(("waterway", "11") in by and "name" not in by[("waterway", "11")], "naamloze gracht")
        # Alle watervlakken in alle tegels (de vijver kan in de tegel ten zuiden liggen).
        alle = []
        for root, _, files in os.walk(os.path.join(out, "water")):
            for name in files:
                if name.endswith(".json.gz"):
                    with gzip.open(os.path.join(root, name), "rt", encoding="utf-8") as f:
                        alle += json.load(f)["elements"]
        vijvers = list({e["id"]: e for e in alle if e["type"] == "water"}.values())
        check(len(vijvers) == 1 and vijvers[0]["name"] == "Grote Vijver" and vijvers[0]["kind"] == "pond", "alleen de vijver als vlak")
        check(4000 < vijvers[0]["area"] < 7000, "oppervlakte van de vijver")
        x2, y2 = lonlat_to_tile(lon + 0.03, lat)
        t2 = tile(x2, y2)
        check(t2 is not None and any(e["id"] == 10 for e in t2["elements"]), "beek ook in de buurtegel")
        check(all(e["type"] in ("waterway", "water") for e in alle), "geen wegen")
    print("\nAlle tests geslaagd.")


if __name__ == "__main__":
    main()
