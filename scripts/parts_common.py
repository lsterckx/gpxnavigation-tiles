"""
Gesplitste run: elk gebied wordt in een eigen job gebouwd (build_tiles.py --part, build_water.py --part) en
merge_parts.py voegt daarna de tegels op de grens tussen gebieden samen.

Waarom: een tegel op de grens van twee gebieden moet de wegen van beide bevatten, maar een job heeft alleen
zijn eigen gebied. Daarom splitst elke job zijn tegels in:
- eigen tegels ("final"): ver genoeg van elk ander gebied; die zijn klaar en worden meteen opgeladen;
- randtegels ("border"): de tegels die een ander gebied (vergroot met BORDER_MARGIN_DEGREES) raken. Van die tegels
  gaan de elementen naar merge_parts.py, dat ze met die van de buren samenvoegt.

merge_parts.py controleert dat de marge groot genoeg was: geen eigen tegel van een gebied mag geraakt worden door
een ander gebied (zijn omtrek, zijn wegen, of een buurtegel met zijn wegen). Zo is het resultaat byte voor byte
hetzelfde als wanneer alle gebieden in één keer gebouwd worden (build_tiles.py zonder --part).

Volgorde: komt een element in twee gebieden voor, dan geldt de versie van het gebied dat eerst staat in
regions.yml (zoals bij één run).
"""

from __future__ import annotations

import gzip
import json
import os

import shapely
import yaml
from shapely.ops import unary_union

from tiles_common import tile_bounds

# Standaardmarge rond de andere gebieden (graden, ±4–5 km). In regions.yml te overschrijven met
# border_margin_degrees.
BORDER_MARGIN_DEGREES = 0.05


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def enabled_regions(config: dict) -> list[dict]:
    return [r for r in config["regions"] if r.get("enabled", True)]


def border_margin(config: dict) -> float:
    return float(config.get("border_margin_degrees", BORDER_MARGIN_DEGREES))


def part_dir(parts: str, region_id: str) -> str:
    return os.path.join(parts, region_id)


def border_zone(other_shapes: list, margin: float):
    """De andere gebieden, vergroot met `margin` graden (None als er geen andere gebieden zijn)."""
    if not other_shapes:
        return None
    zone = unary_union(other_shapes).buffer(margin)
    shapely.prepare(zone)
    return zone


def cells_in_zone(cells: set[tuple[int, int]], zone) -> set[tuple[int, int]]:
    """De tegels die de zone raken."""
    if zone is None or not cells:
        return set()
    ordered = sorted(cells)
    boxes = [shapely.box(*tile_bounds(x, y)) for x, y in ordered]
    hits = shapely.intersects(zone, boxes)
    return {c for c, hit in zip(ordered, hits) if hit}


def neighbors(cells: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """De tegels zelf en hun 8 buren."""
    return {(x + dx, y + dy) for (x, y) in cells for dx in (-1, 0, 1) for dy in (-1, 0, 1)}


def cell_key(cell: tuple[int, int]) -> str:
    return f"{cell[0]}/{cell[1]}"


def key_cell(key: str) -> tuple[int, int]:
    x, y = key.split("/")
    return int(x), int(y)


def cells_to_list(cells) -> list[list[int]]:
    return [[x, y] for x, y in sorted(cells)]


def list_to_cells(items) -> set[tuple[int, int]]:
    return {(int(x), int(y)) for x, y in items}


def write_json_gz(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    with open(path, "wb") as f:
        f.write(gzip.compress(text.encode("utf-8"), compresslevel=6, mtime=0))


def read_json_gz(path: str):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)
