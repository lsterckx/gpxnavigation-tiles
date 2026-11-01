"""
Gemeenschappelijke functies voor de tegels van gpxnavigation.

Tegels zijn gewone webkaart-tegels (slippy map, zoals OpenStreetMap ze nummert) op zoomniveau 14:
ongeveer 1,6 x 1,6 km in Belgie. Een tegel staat op  14/{x}/{y}.json.gz.

De app op de iPhone moet precies dezelfde rekenregels gebruiken (TileStore.swift):
- lonlat_to_tile / tile_bounds: de nummering van de tegels;
- corridor_tiles: welke tegels een route raakt, met een strook van CORRIDOR_METERS aan elke kant.
"""

from __future__ import annotations

import math
from typing import Iterable

ZOOM = 14

# Zelfde breedtes als OverpassClient.swift: wegen in een strook van 20 m, oversteekknopen in 15 m.
WAY_CORRIDOR_METERS = 20
NODE_CORRIDOR_METERS = 15
# De tegels langs een route: de bredere van de twee stroken.
CORRIDOR_METERS = WAY_CORRIDOR_METERS

# Alleen deze tags gaan in de tegels: wat de app (RouteFeatures.swift) leest, plus 'area'.
# Een sleutel die eindigt op ':' of '_' neemt alle sleutels met dat begin mee (bv. 'handrail:' -> 'handrail:left').
KEEP_TAG_KEYS = {
    "highway", "name", "area",
    "footway", "path", "cycleway", "bicycle", "foot", "service",
    "incline", "step_count", "handrail",
    "crossing", "crossing_ref", "traffic_signals", "tactile_paving",
}
KEEP_TAG_PREFIXES = ("handrail:", "crossing:", "traffic_signals:")


def keep_tag(key: str) -> bool:
    return key in KEEP_TAG_KEYS or key.startswith(KEEP_TAG_PREFIXES)


def filter_tags(tags: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {k: v for k, v in tags if keep_tag(k)}


def is_crossing_node(tags: dict[str, str]) -> bool:
    """Zelfde keuze als de Overpass-vraag van de app: node[highway=crossing] of node[crossing]."""
    return tags.get("highway") == "crossing" or "crossing" in tags


# ---------- tegelnummering ----------

def lonlat_to_tile_f(lon: float, lat: float, zoom: int = ZOOM) -> tuple[float, float]:
    """Tegelcoordinaten als kommagetal (het gehele deel is het tegelnummer)."""
    n = 2 ** zoom
    lat = max(min(lat, 85.05112878), -85.05112878)
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    return x, y


def lonlat_to_tile(lon: float, lat: float, zoom: int = ZOOM) -> tuple[int, int]:
    x, y = lonlat_to_tile_f(lon, lat, zoom)
    n = 2 ** zoom
    return min(max(int(math.floor(x)), 0), n - 1), min(max(int(math.floor(y)), 0), n - 1)


def tile_bounds(x: int, y: int, zoom: int = ZOOM) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) van een tegel."""
    n = 2 ** zoom

    def lat_of(ty: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))

    return x / n * 360.0 - 180.0, lat_of(y + 1), (x + 1) / n * 360.0 - 180.0, lat_of(y)


def segment_tiles(lon0: float, lat0: float, lon1: float, lat1: float, zoom: int = ZOOM) -> set[tuple[int, int]]:
    """Alle tegels die een recht stuk raakt (rasterdoorloop volgens Amanatides en Woo)."""
    x0, y0 = lonlat_to_tile_f(lon0, lat0, zoom)
    x1, y1 = lonlat_to_tile_f(lon1, lat1, zoom)
    ix, iy = int(math.floor(x0)), int(math.floor(y0))
    ex, ey = int(math.floor(x1)), int(math.floor(y1))
    cells = {(ix, iy)}
    if (ix, iy) == (ex, ey):
        return cells

    dx, dy = x1 - x0, y1 - y0
    step_x = 1 if dx > 0 else -1
    step_y = 1 if dy > 0 else -1
    inf = float("inf")
    t_max_x = ((ix + (1 if step_x > 0 else 0)) - x0) / dx if dx != 0 else inf
    t_max_y = ((iy + (1 if step_y > 0 else 0)) - y0) / dy if dy != 0 else inf
    t_delta_x = abs(1.0 / dx) if dx != 0 else inf
    t_delta_y = abs(1.0 / dy) if dy != 0 else inf

    # Hoogstens zoveel stappen als er tegelgrenzen tussen begin en einde liggen (+ marge).
    for _ in range(abs(ex - ix) + abs(ey - iy) + 2):
        if (ix, iy) == (ex, ey):
            break
        if abs(t_max_x - t_max_y) < 1e-12:
            # Precies door een hoekpunt: de twee buren ook meenemen.
            cells.add((ix + step_x, iy))
            cells.add((ix, iy + step_y))
            ix += step_x
            iy += step_y
            t_max_x += t_delta_x
            t_max_y += t_delta_y
        elif t_max_x < t_max_y:
            ix += step_x
            t_max_x += t_delta_x
        else:
            iy += step_y
            t_max_y += t_delta_y
        cells.add((ix, iy))
    cells.add((ex, ey))
    return cells


def line_tiles(coords: list[tuple[float, float]], zoom: int = ZOOM) -> set[tuple[int, int]]:
    """Alle tegels die een lijn (lijst van (lon, lat)) raakt."""
    if len(coords) == 1:
        return {lonlat_to_tile(coords[0][0], coords[0][1], zoom)}
    cells: set[tuple[int, int]] = set()
    for (lon0, lat0), (lon1, lat1) in zip(coords, coords[1:]):
        cells |= segment_tiles(lon0, lat0, lon1, lat1, zoom)
    return cells


def corridor_tiles(route: list[tuple[float, float]], meters: float = CORRIDOR_METERS,
                   zoom: int = ZOOM) -> set[tuple[int, int]]:
    """
    De tegels die de app voor een route nodig heeft: de lijn zelf, en dezelfde lijn
    verschoven met `meters` naar noord, zuid, oost en west (en de vier schuine richtingen).
    Omdat een tegel 1,6 km breed is en de strook maar 20 m, volstaat dat ruim.
    """
    if not route:
        return set()
    cells = line_tiles(route, zoom)
    for dn, de in [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]:
        shifted = []
        for lon, lat in route:
            dlat = dn * meters / 111_320.0
            dlon = de * meters / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
            shifted.append((lon + dlon, lat + dlat))
        cells |= line_tiles(shifted, zoom)
    return cells


def tile_path(x: int, y: int, zoom: int = ZOOM) -> str:
    return f"{zoom}/{x}/{y}.json.gz"
