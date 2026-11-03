#!/usr/bin/env python3
"""
Test zonder internet voor build_areas.py: drie verzonnen gemeenten (A en B grenzen aan elkaar, C ligt apart),
twee deelgemeenten in A, en plaatsen. Gebruik:  python3 tests/test_areas.py
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


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(f"MISLUKT: {msg}")
    print(f"ok  {msg}")


def make_sample(path: str) -> None:
    w = osmium.SimpleWriter(path)
    nodes: dict[tuple[float, float], int] = {}
    nid, wid, rid = [1], [100], [1000]

    def node(lon, lat, tags=None):
        if tags is None and (lon, lat) in nodes:
            return nodes[(lon, lat)]
        i = nid[0]; nid[0] += 1
        w.add_node(osmium.osm.mutable.Node(id=i, location=(lon, lat), tags=tags or {}))
        if tags is None:
            nodes[(lon, lat)] = i
        return i

    ways_by_coords: dict[tuple, int] = {}

    def way(coords):
        key = tuple(coords)
        rkey = tuple(reversed(coords))
        if key in ways_by_coords:
            return ways_by_coords[key]
        if rkey in ways_by_coords:
            return ways_by_coords[rkey]
        ids = [node(*c) for c in coords]
        i = wid[0]; wid[0] += 1
        w.add_way(osmium.osm.mutable.Way(id=i, nodes=ids, tags={}))
        ways_by_coords[key] = i
        return i

    def square(x0, y0, x1, y1, tags):
        # Vier zijden als aparte lijnen, zodat buren dezelfde lijn delen (zoals in OpenStreetMap).
        sides = [[(x0, y0), (x1, y0)], [(x1, y0), (x1, y1)], [(x1, y1), (x0, y1)], [(x0, y1), (x0, y0)]]
        members = [("w", way(s), "outer") for s in sides]
        i = rid[0]; rid[0] += 1
        w.add_relation(osmium.osm.mutable.Relation(id=i, members=members,
                                                   tags=dict(tags, type="boundary", boundary="administrative")))

    # Plaatsen eerst (knopen vóór lijnen in een OSM-bestand).
    node(4.505, 50.805, {"place": "town", "name": "Alfa", "name:fr": "Alpha", "population": "21.000"})
    node(4.502, 50.802, {"place": "village", "name": "Alfa-Zuid"})
    node(4.515, 50.805, {"place": "village", "name": "Beta"})
    node(4.60, 50.90, {"place": "city", "name": "Grootstad", "name:nl": "Grote Stad", "population": "100000"})
    square(4.50, 50.80, 4.51, 50.81, {"admin_level": "8", "name": "Alfa", "name:fr": "Alpha"})
    square(4.51, 50.80, 4.52, 50.81, {"admin_level": "8", "name": "Beta"})
    square(4.60, 50.80, 4.61, 50.81, {"admin_level": "8", "name": "Gamma"})
    square(4.50, 50.80, 4.505, 50.805, {"admin_level": "9", "name": "Alfa-Zuid"})
    square(4.505, 50.80, 4.51, 50.805, {"admin_level": "9", "name": "Alfa-Oost"})
    w.close()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        data, out = os.path.join(tmp, "data"), os.path.join(tmp, "out")
        os.makedirs(data)
        make_sample(os.path.join(data, "test.osm.pbf"))
        regions = os.path.join(tmp, "regions.yml")
        with open(regions, "w") as f:
            f.write("regions:\n  - id: test\n    name: Test\n    geofabrik: test/test\n")
        env = dict(os.environ, PATH="/nonexistent")  # zonder osmium-tool
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "build_areas.py"),
                        "--regions", regions, "--data", data, "--out", out], check=True, env=env)

        with gzip.open(os.path.join(out, "areas", "test.json.gz"), "rt", encoding="utf-8") as f:
            r = json.load(f)
        m = {a["name"]: (i, a) for i, a in enumerate(r["municipalities"])}
        check(set(m) == {"Alfa", "Beta", "Gamma"}, "drie gemeenten")
        check(m["Beta"][0] in m["Alfa"][1]["neighbors"] and m["Alfa"][0] in m["Beta"][1]["neighbors"],
              "Alfa en Beta zijn buren")
        check(m["Alfa"][1]["neighbors"] == [m["Beta"][0]], "Gamma is geen buur van Alfa")
        check(m["Alfa"][1]["names"].get("fr") == "Alpha", "vertaalde naam")
        check(m["Alfa"][1]["center"] == [4.505, 50.805], "centrum = de plaats met dezelfde naam")
        check(len(m["Alfa"][1]["polygons"]) == 1 and len(m["Alfa"][1]["polygons"][0][0]) >= 4, "grens als polygoon")
        s = {a["name"]: a for a in r["sections"]}
        check(set(s) == {"Alfa-Zuid", "Alfa-Oost"}, "twee deelgemeenten")
        check(s["Alfa-Zuid"]["municipality"] == m["Alfa"][0], "deelgemeente hoort bij Alfa")
        check(s["Alfa-Zuid"]["center"] == [4.502, 50.802], "centrum deelgemeente = het dorp")
        p = {x["name"]: x for x in r["places"]}
        check(p["Grootstad"]["names"]["nl"] == "Grote Stad" and p["Grootstad"]["population"] == 100000, "stad met naam en inwoners")
        check(p["Alfa"]["population"] == 21000, "inwoners met punt als duizendtal")
        idx = json.load(open(os.path.join(out, "areas", "index.json")))
        check(idx["regions"][0]["file"] == "areas/test.json.gz", "index.json")
    print("\nAlle tests geslaagd.")


if __name__ == "__main__":
    main()
