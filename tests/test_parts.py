#!/usr/bin/env python3
"""
Test zonder internet voor de gesplitste run (parts_common.py, build_tiles.py --part, build_water.py --part,
merge_parts.py): twee verzonnen gebieden A en B die elkaar overlappen, zoals de omtrekken van Geofabrik, met een weg,
een oversteekknoop en een beek over de grens.

1. Alles in één keer gebouwd en per gebied gebouwd + samengevoegd geeft byte voor byte dezelfde tegels.
2. Met een te kleine marge stopt merge_parts.py met een fout (een weg van B loopt ver in A).

Gebruik:  python3 tests/test_parts.py
"""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import subprocess
import sys
import tempfile

import osmium

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
LAT = 50.85


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise SystemExit(f"MISLUKT: {msg}")
    print(f"ok  {msg}")


def write_poly(path: str, lon0: float, lon1: float) -> None:
    with open(path, "w") as f:
        f.write("test\n1\n")
        for lon, lat in [(lon0, LAT - 0.05), (lon1, LAT - 0.05), (lon1, LAT + 0.05), (lon0, LAT + 0.05),
                         (lon0, LAT - 0.05)]:
            f.write(f"   {lon:.6f}   {lat:.6f}\n")
        f.write("END\nEND\n")


def write_pbf(path: str, ways: list[tuple[int, list[tuple[float, float]], dict]], crossings: bool) -> None:
    """Knoop-id's volgen uit de coördinaten, zodat dezelfde weg in A en B dezelfde knopen heeft."""
    w = osmium.SimpleWriter(path)
    nodes = {}
    for _, coords, _ in ways:
        for c in coords:
            nodes.setdefault(c, 1000 + len(nodes))
    # Vaste id's per coördinaat over beide bestanden heen.
    for c in sorted(nodes, key=lambda c: NODE_IDS[c]):
        tags = {"highway": "crossing"} if crossings and c == (4.60, LAT) else {}
        w.add_node(osmium.osm.mutable.Node(id=NODE_IDS[c], location=c, tags=tags))
    for wid, coords, tags in sorted(ways, key=lambda w: w[0]):
        w.add_way(osmium.osm.mutable.Way(id=wid, nodes=[NODE_IDS[c] for c in coords], tags=tags))
    w.close()


STREET = {"highway": "residential", "name": "Grensstraat"}
# Gebied A: lengte 4,40–4,62; gebied B: 4,58–4,80 (overlap zoals bij Geofabrik).
WAYS_A = [
    (100, [(4.45, LAT), (4.50, LAT)], {"highway": "residential", "name": "Weststraat"}),
    (200, [(4.56, LAT), (4.60, LAT), (4.64, LAT)], STREET),                      # over de grens: in A en B
    (400, [(4.55, LAT + 0.01), (4.65, LAT + 0.01)], {"waterway": "stream", "name": "Grensbeek"}),
]
WAYS_B = [
    (200, [(4.56, LAT), (4.60, LAT), (4.64, LAT)], STREET),
    (300, [(4.70, LAT), (4.75, LAT)], {"highway": "residential", "name": "Ooststraat"}),
    (302, [(4.60, LAT - 0.02), (4.565, LAT - 0.02)], {"highway": "track"}),      # iets buiten de omtrek van B
    (400, [(4.55, LAT + 0.01), (4.65, LAT + 0.01)], {"waterway": "stream", "name": "Grensbeek"}),
]
FAR = (301, [(4.60, LAT + 0.03), (4.47, LAT + 0.03)], {"highway": "service"})      # ver in A: marge te klein

NODE_IDS = {}
for _, coords, _ in WAYS_A + WAYS_B + [FAR]:
    for c in coords:
        NODE_IDS.setdefault(c, 1000 + len(NODE_IDS))


def run(*args: str, ok: bool = True) -> int:
    env = dict(os.environ, PATH="/nonexistent")  # zonder osmium-tool
    r = subprocess.run([sys.executable, *args], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if ok and r.returncode != 0:
        raise SystemExit(f"MISLUKT: {' '.join(args)}\n{r.stderr}")
    return r.returncode


def tree(root: str) -> set[str]:
    files = set()
    for d, _, names in os.walk(root):
        for n in names:
            if n.endswith(".json.gz"):
                files.add(os.path.relpath(os.path.join(d, n), root))
    return files


def build_all(tmp: str, ways_b, margin: float | None) -> tuple[str, str, int]:
    data = os.path.join(tmp, "data")
    os.makedirs(data, exist_ok=True)
    write_poly(os.path.join(data, "a.poly"), 4.40, 4.62)
    write_poly(os.path.join(data, "b.poly"), 4.58, 4.80)
    write_pbf(os.path.join(data, "a.osm.pbf"), WAYS_A, crossings=True)
    write_pbf(os.path.join(data, "b.osm.pbf"), ways_b, crossings=True)
    regions = os.path.join(tmp, "regions.yml")
    with open(regions, "w") as f:
        if margin is not None:
            f.write(f"border_margin_degrees: {margin}\n")
        f.write("regions:\n  - id: a\n    name: A\n    geofabrik: test/a\n"
                "  - id: b\n    name: B\n    geofabrik: test/b\n")

    single = os.path.join(tmp, "single")
    run(os.path.join(SCRIPTS, "build_tiles.py"), "--regions", regions, "--data", data, "--out", single)
    run(os.path.join(SCRIPTS, "build_water.py"), "--regions", regions, "--data", data, "--out", single)

    parts = os.path.join(tmp, "parts")
    for rid in ("a", "b"):
        run(os.path.join(SCRIPTS, "build_tiles.py"), "--regions", regions, "--data", data, "--part", rid,
            "--parts", parts)
        run(os.path.join(SCRIPTS, "build_water.py"), "--regions", regions, "--data", data, "--part", rid,
            "--parts", parts)
    # Zoals GitHub Actions: elk deel in parts/part-<id>/.
    for rid in ("a", "b"):
        os.rename(os.path.join(parts, rid), os.path.join(parts, f"part-{rid}"))
    merged = os.path.join(tmp, "merged")
    code = run(os.path.join(SCRIPTS, "merge_parts.py"), "--regions", regions, "--parts", parts, "--out", merged,
               ok=False)
    # Samenvoegen zoals in de bucket: eigen tegels van de delen + samengevoegde randtegels.
    combined = os.path.join(tmp, "combined")
    for rid in ("a", "b"):
        final = os.path.join(parts, f"part-{rid}", "final")
        if os.path.isdir(final):
            shutil.copytree(final, combined, dirs_exist_ok=True)
    if code == 0:
        for sub in ("14", "water"):
            if os.path.isdir(os.path.join(merged, sub)):
                shutil.copytree(os.path.join(merged, sub), os.path.join(combined, sub), dirs_exist_ok=True)
    return single, combined if code == 0 else merged, code


def test_same_as_single_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        single, combined, code = build_all(tmp, WAYS_B, None)
        check(code == 0, "samenvoegen lukt met de standaardmarge")
        a, b = tree(single), tree(combined)
        check(a == b, f"zelfde tegels ({len(a)} wegen- en watertegels)")
        diff = [f for f in sorted(a) if not filecmp.cmp(os.path.join(single, f), os.path.join(combined, f),
                                                        shallow=False)]
        check(not diff, "alle tegels byte voor byte gelijk" + (f" (verschil: {diff[:3]})" if diff else ""))
        merged = os.path.join(tmp, "merged")
        s1 = json.load(open(os.path.join(single, "status.json")))
        s2 = json.load(open(os.path.join(merged, "status.json")))
        check(s2["tiles_merged"] > 0, f"er zijn randtegels samengevoegd ({s2['tiles_merged']})")
        for k in ("tiles", "tiles_complete", "tiles_empty", "bytes_total"):
            check(s1[k] == s2[k], f"status.json: {k} gelijk ({s1[k]})")
        r1 = json.load(open(os.path.join(single, "regions.json")))
        r2 = json.load(open(os.path.join(merged, "regions.json")))
        check(r1["regions"] == r2["regions"], "regions.json: zelfde gebieden")
        expected = open(os.path.join(merged, "expected-14.txt")).read().split()
        check(len(expected) == s1["tiles"] and all(f.endswith(".json.gz") for f in expected),
              "expected-14.txt bevat alle tegels")
        expected_w = open(os.path.join(merged, "expected-water.txt")).read().split()
        check(len(expected_w) == len(tree(os.path.join(single, "water"))), "expected-water.txt bevat alle watertegels")


def test_margin_too_small() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _, _, code = build_all(tmp, WAYS_B + [FAR], 0.0)
        check(code == 3, "te kleine marge: merge_parts.py stopt met een fout")


if __name__ == "__main__":
    test_same_as_single_run()
    test_margin_too_small()
    print("\nAlle tests geslaagd.")
