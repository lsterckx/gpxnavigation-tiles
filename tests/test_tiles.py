#!/usr/bin/env python3
"""
Kleine test zonder internet: maakt een verzonnen OSM-bestand en een gebied, draait build_tiles.py
en controleert de tegels. Gebruik:  python3 tests/test_tiles.py
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
from tiles_common import (  # noqa: E402
    corridor_tiles, lonlat_to_tile, segment_tiles, tile_bounds, tile_path,
)


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(f"MISLUKT: {msg}")
    print(f"ok  {msg}")


def test_tile_math() -> None:
    # Brussel, Grote Markt: bekende tegel op zoom 14.
    check(lonlat_to_tile(4.3524, 50.8467) == (8390, 5496), "tegelnummer Grote Markt Brussel")
    lo0, la0, lo1, la1 = tile_bounds(8390, 5496)
    check(lo0 <= 4.3524 <= lo1 and la0 <= 50.8467 <= la1, "tegelgrenzen bevatten het punt")
    # Een lijn schuin over een hoekpunt raakt de drie of vier tegels rond dat punt.
    x0, _, _, y1 = tile_bounds(8390, 5496)  # linksboven = (min_lon, max_lat)
    cells = segment_tiles(x0 - 0.001, y1 + 0.0006, x0 + 0.001, y1 - 0.0006)
    check({(8389, 5495), (8390, 5496)} <= cells, "schuine lijn over een tegelhoek")
    # Een route vlak naast een tegelrand: de strook van 20 m neemt de buurtegel mee.
    route = [(x0 + 0.0001, 50.84), (x0 + 0.0001, 50.85)]  # ~7 m oost van de rand
    check(any(c[0] == 8389 for c in corridor_tiles(route)), "strook neemt buurtegel mee")


def make_sample(path_pbf: str, path_poly: str, base_lon: float, base_lat: float) -> None:
    w = osmium.SimpleWriter(path_pbf)
    nid = 1
    def node(lon, lat, tags=None):
        nonlocal nid
        w.add_node(osmium.osm.mutable.Node(id=nid, location=(lon, lat), tags=tags or {}))
        nid += 1
        return nid - 1

    a = node(base_lon - 0.01, base_lat)
    b = node(base_lon, base_lat, {"highway": "crossing", "crossing": "traffic_signals",
                                  "traffic_signals:sound": "yes", "note": "weg"})
    c = node(base_lon + 0.03, base_lat)  # ~2 km verder: over een tegelrand
    d = node(base_lon, base_lat + 0.001)
    e = node(base_lon + 0.0005, base_lat + 0.001)
    w.add_way(osmium.osm.mutable.Way(id=100, nodes=[a, b, c],
                                     tags={"highway": "residential", "name": "Teststraat",
                                           "surface": "asphalt", "maxspeed": "30"}))
    w.add_way(osmium.osm.mutable.Way(id=101, nodes=[d, e],
                                     tags={"highway": "steps", "incline": "up", "step_count": "12",
                                           "handrail:left": "yes"}))
    w.add_way(osmium.osm.mutable.Way(id=102, nodes=[d, e], tags={"building": "yes"}))
    w.close()

    s = 0.05
    with open(path_poly, "w") as f:
        f.write("test\n1\n")
        for lon, lat in [(base_lon - s, base_lat - s), (base_lon + s, base_lat - s),
                         (base_lon + s, base_lat + s), (base_lon - s, base_lat + s),
                         (base_lon - s, base_lat - s)]:
            f.write(f"   {lon:.6f}   {lat:.6f}\n")
        f.write("END\nEND\n")


def load(out: str, x: int, y: int) -> dict:
    with gzip.open(os.path.join(out, tile_path(x, y)), "rt", encoding="utf-8") as f:
        return json.load(f)


def test_build() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data, out = os.path.join(tmp, "data"), os.path.join(tmp, "out")
        os.makedirs(data)
        base_lon, base_lat = 4.5100, 50.8200  # Tervuren
        make_sample(os.path.join(data, "test.osm.pbf"), os.path.join(data, "test.poly"), base_lon, base_lat)
        regions = os.path.join(tmp, "regions.yml")
        with open(regions, "w") as f:
            f.write("regions:\n  - id: test\n    name: Test\n    geofabrik: test/test\n")
        env = dict(os.environ, PATH="/nonexistent")  # zonder osmium-tool: het eigen leespad testen
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_tiles.py"),
                        "--regions", regions, "--data", data, "--out", out],
                       check=True, env=env)

        x, y = lonlat_to_tile(base_lon, base_lat)
        tile = load(out, x, y)
        check(tile["version"] == 1 and tile["complete"] is True, "tegel in het midden is volledig")
        by_id = {(e["type"], e["id"]): e for e in tile["elements"]}
        check(("way", 100) in by_id and ("way", 101) in by_id, "straat en trap in de tegel")
        check(("way", 102) not in by_id, "gebouw niet in de tegel")
        street = by_id[("way", 100)]
        check(street["tags"] == {"highway": "residential", "name": "Teststraat"}, "alleen nodige tags")
        check(len(street["nodes"]) == len(street["geometry"]) == 3, "knoop-id's en geometrie even lang")
        crossing = by_id[("node", 2)]
        check(crossing["tags"].get("traffic_signals:sound") == "yes" and "note" not in crossing["tags"],
              "oversteekknoop met geluidssignaal, zonder overbodige tags")

        x2, y2 = lonlat_to_tile(base_lon + 0.03, base_lat)
        check((x2, y2) != (x, y), "het einde van de straat ligt in een andere tegel")
        check(any(e["id"] == 100 for e in load(out, x2, y2)["elements"]), "straat ook volledig in de buurtegel")

        x3, y3 = lonlat_to_tile(base_lon + 0.045, base_lat)
        check(load(out, x3, y3)["complete"] is False, "tegel op de rand van het gebied is niet volledig")
        check(load(out, x, y - 1)["elements"] == [], "lege tegel naast een tegel met wegen bestaat")
        x4, y4 = lonlat_to_tile(base_lon + 0.049, base_lat + 0.049)
        check(not os.path.exists(os.path.join(out, tile_path(x4, y4))), "geen lege tegel ver van elke weg")
        check(not os.path.exists(os.path.join(out, tile_path(x + 10, y))), "geen tegel buiten het gebied")

        status = json.load(open(os.path.join(out, "status.json")))
        check(status["ways"] == 2 and status["crossing_nodes"] == 1, "status.json telt wegen en knopen")
        regions_json = json.load(open(os.path.join(out, "regions.json")))
        check(regions_json["regions"][0]["id"] == "test", "regions.json bevat het gebied")

        # Twee keer bouwen geeft byte voor byte dezelfde tegels (nodig om alleen wijzigingen op te laden).
        first = open(os.path.join(out, tile_path(x, y)), "rb").read()
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_tiles.py"),
                        "--regions", regions, "--data", data, "--out", out],
                       check=True, env=env, stdout=subprocess.DEVNULL)
        check(open(os.path.join(out, tile_path(x, y)), "rb").read() == first, "tegels zijn reproduceerbaar")


if __name__ == "__main__":
    test_tile_math()
    test_build()
    print("\nAlle tests geslaagd.")
