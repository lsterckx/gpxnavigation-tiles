#!/usr/bin/env python3
"""
Test zonder internet voor build_transit.py: bushalte, tramhalte (perron en spoor), metrostation en -ingang,
treinstation met perron (als lijn), taxistandplaats, veerboot, en wat er niet in mag (verlaten halte, stop_position
van een bus, gewone weg). Gebruik:  python3 tests/test_transit.py
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

LON, LAT = 4.51, 50.82

NODES = [
    (1, 0.000, 0.000, {"highway": "bus_stop", "public_transport": "platform", "bus": "yes", "name": "Tervuren Station",
                       "name:fr": "Tervuren Gare", "route_ref": "410;830", "network": "De Lijn", "shelter": "yes",
                       "bench": "yes"}),
    (2, 0.001, 0.000, {"public_transport": "platform", "tram": "yes", "name": "Tervuren", "tactile_paving": "yes"}),
    (3, 0.0012, 0.000, {"railway": "tram_stop", "public_transport": "stop_position", "name": "Tervuren"}),
    (4, 0.002, 0.000, {"railway": "station", "station": "subway", "name": "Merode"}),
    (5, 0.0021, 0.0003, {"railway": "subway_entrance", "name": "Merode", "ref": "2", "wheelchair": "no"}),
    (6, 0.003, 0.000, {"railway": "station", "name": "Leuven"}),
    (7, 0.004, 0.000, {"amenity": "taxi", "name": "Taxi Leuven"}),
    (8, 0.005, 0.000, {"amenity": "ferry_terminal", "name": "Veer"}),
    (9, 0.006, 0.000, {"disused:highway": "bus_stop", "highway": "bus_stop", "name": "Oud"}),
    (10, 0.007, 0.000, {"public_transport": "stop_position", "bus": "yes", "name": "Rijweg"}),
    (11, 0.0030, 0.0005, {}),
    (12, 0.0034, 0.0005, {}),
    (13, 2.0, 1.0, {"amenity": "ferry_terminal", "name": "Ver weg"}),   # buiten de omtrek (veerdienst)
]


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(f"MISLUKT: {msg}")
    print(f"ok  {msg}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data, out = os.path.join(tmp, "data"), os.path.join(tmp, "out")
        os.makedirs(data)
        w = osmium.SimpleWriter(os.path.join(data, "test.osm.pbf"))
        for nid, dx, dy, tags in NODES:
            w.add_node(osmium.osm.mutable.Node(id=nid, location=(LON + dx, LAT + dy), tags=tags))
        w.add_way(osmium.osm.mutable.Way(id=100, nodes=[11, 12], tags={"railway": "platform", "ref": "3"}))
        w.add_way(osmium.osm.mutable.Way(id=101, nodes=[11, 12], tags={"highway": "footway"}))
        w.close()
        with open(os.path.join(data, "test.poly"), "w") as f:
            f.write("test\n1\n")
            for dx, dy in [(-0.05, -0.05), (0.05, -0.05), (0.05, 0.05), (-0.05, 0.05), (-0.05, -0.05)]:
                f.write(f"   {LON + dx:.6f}   {LAT + dy:.6f}\n")
            f.write("END\nEND\n")
        regions = os.path.join(tmp, "regions.yml")
        with open(regions, "w") as f:
            f.write("regions:\n  - id: test\n    name: Test\n    geofabrik: test/test\n")
        env = dict(os.environ, PATH="/nonexistent")
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_transit.py"),
                        "--regions", regions, "--data", data, "--out", out], check=True, env=env)

        x, y = lonlat_to_tile(LON, LAT)
        with gzip.open(os.path.join(out, "transit", tile_path(x, y)), "rt", encoding="utf-8") as f:
            tile = json.load(f)
        check(tile["version"] == 1, "haltetegel bestaat")
        by = {e["id"]: e for e in tile["elements"]}
        check([e["id"] for e in tile["elements"]] == sorted(by), "gesorteerd op id")
        bus = by.get("n1", {})
        check(bus.get("kind") == "bus" and bus.get("role") == "platform", "bushalte")
        check(bus.get("route_ref") == "410;830" and bus.get("network") == "De Lijn" and bus.get("shelter") == "yes"
              and bus.get("names") == {"fr": "Tervuren Gare"} and "bench" not in bus, "bushalte: lijnen, net, vertaling")
        check(by.get("n2", {}).get("kind") == "tram" and by["n2"]["role"] == "platform", "tramperron")
        check(by.get("n3", {}).get("kind") == "tram" and by["n3"]["role"] == "stop", "tramhalte op het spoor")
        check(by.get("n4", {}).get("kind") == "subway" and by["n4"]["role"] == "station", "metrostation")
        entrance = by.get("n5", {})
        check(entrance.get("kind") == "subway" and entrance.get("role") == "entrance" and entrance.get("ref") == "2"
              and entrance.get("wheelchair") == "no", "metro-ingang met nummer")
        check(by.get("n6", {}).get("kind") == "train" and by["n6"]["role"] == "station", "treinstation")
        perron = by.get("w100", {})
        check(perron.get("kind") == "train" and perron.get("role") == "platform" and perron.get("ref") == "3"
              and abs(perron["lon"] - (LON + 0.0032)) < 1e-6, "perron als lijn: punt in het midden, met nummer")
        check(by.get("n7", {}).get("kind") == "taxi", "taxistandplaats")
        check(by.get("n8", {}).get("kind") == "ferry", "veerboot")
        check("n9" not in by, "geen verlaten halte")
        check("n10" not in by, "geen stop_position van een bus")
        check("w101" not in by, "geen gewone weg")
        far = lonlat_to_tile(LON + 2.0, LAT + 1.0)
        check(not os.path.exists(os.path.join(out, "transit", tile_path(*far))), "geen halte buiten het gebied")
        status = json.load(open(os.path.join(out, "transit", "status.json")))
        check(status["counts"].get("bus") == 1 and status["counts"].get("subway") == 2, "status.json telt per soort")
    print("\nAlle tests geslaagd.")


if __name__ == "__main__":
    main()
