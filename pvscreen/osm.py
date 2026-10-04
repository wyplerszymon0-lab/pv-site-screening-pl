"""Built-up land, transport, water, forest and the power grid from OpenStreetMap.

One Overpass query per gmina (its bounding box plus a margin, so that buffers
around features just outside the border still reach in), cached as JSON.
Features become exclusion polygons, each buffered by a documented distance;
power lines and substations form a separate grid layer used later for scoring.

Data © OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from pvscreen import WORK_CRS
from pvscreen.boundary import USER_AGENT

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
DEFAULT_CACHE = Path("data/osm")
MARGIN_M = 100  # larger than the largest buffer below

ROADS = "motorway|trunk|primary|secondary|tertiary|unclassified|residential"
BUILT_UP = "residential|cemetery|allotments|commercial|retail"
STATEMENTS = (
    'way["building"]',
    'relation["building"]',
    f'way["highway"~"^({ROADS})(_link)?$"]',
    'way["railway"~"^(rail|light_rail|narrow_gauge)$"]',
    'way["natural"="water"]',
    'relation["natural"="water"]',
    'way["landuse"="reservoir"]',
    'way["waterway"~"^(river|canal|stream)$"]',
    'way["landuse"="forest"]',
    'relation["landuse"="forest"]',
    'way["natural"="wood"]',
    'relation["natural"="wood"]',
    f'way["landuse"~"^({BUILT_UP})$"]',
    f'relation["landuse"~"^({BUILT_UP})$"]',
    'way["power"~"^(line|minor_line|cable)$"]',
    'node["power"="substation"]',
    'way["power"="substation"]',
)

AREA_KEYS = ("building", "landuse", "natural", "power")  # closed ways with these tags are areas


@dataclass(frozen=True)
class Rule:
    feature: str
    buffer_m: float
    basis: str


# Road setbacks: Public Roads Act (ustawa o drogach publicznych) art. 43, distances
# from the carriageway edge outside built-up areas. OSM lines are centrelines, so
# an assumed half carriageway is added (12 m for motorways/expressways, 3 m otherwise).
# Polish OSM convention: motorway = A, trunk = S, primary = national, secondary =
# voivodeship, tertiary = county, unclassified/residential = municipal roads.
RULES = {
    "road:motorway": Rule("road", 50 + 12, "Public Roads Act art. 43: 50 m (motorway) + half carriageway"),
    "road:trunk": Rule("road", 40 + 12, "Public Roads Act art. 43: 40 m (expressway) + half carriageway"),
    "road:primary": Rule("road", 25 + 3, "Public Roads Act art. 43: 25 m (national road) + half carriageway"),
    "road:secondary": Rule("road", 20 + 3, "Public Roads Act art. 43: 20 m (voivodeship road) + half"),
    "road:tertiary": Rule("road", 20 + 3, "Public Roads Act art. 43: 20 m (county road) + half carriageway"),
    "road:unclassified": Rule("road", 15 + 3, "Public Roads Act art. 43: 15 m (municipal road) + half"),
    "road:residential": Rule("road", 15 + 3, "Public Roads Act art. 43: 15 m (municipal road) + half"),
    "railway": Rule("railway", 20, "Railway Transport Act art. 53: at least 20 m from the outer track axis"),
    "building": Rule(
        "building", 50, "Screening assumption: 50 m from any building (no national rule for PV)"
    ),
    "built_up": Rule("built_up", 0, "Screening assumption: residential, commercial, cemetery land excluded"),
    "water": Rule("water", 0, "Water bodies excluded"),
    "waterway:river": Rule("waterway", 10, "Screening assumption: 10 m strip along rivers and canals"),
    "waterway:canal": Rule("waterway", 10, "Screening assumption: 10 m strip along rivers and canals"),
    "waterway:stream": Rule("waterway", 5, "Screening assumption: 5 m strip along streams"),
    "forest": Rule("forest", 15, "Screening assumption: forest plus 15 m against tree-edge shading"),
}


def build_query(bounds_wgs84: tuple[float, float, float, float]) -> str:
    west, south, east, north = bounds_wgs84
    bbox = f"{south:.5f},{west:.5f},{north:.5f},{east:.5f}"
    body = "\n".join(f" {s}({bbox});" for s in STATEMENTS)
    # Plain "out geom" (body verbosity): "out geom tags" would print relations
    # without their member ways, so multipolygons would have no geometry.
    return f"[out:json][timeout:180];\n(\n{body}\n);\nout geom;"


def _post_with_retry(session, query: str, waits=(30, 90)) -> requests.Response:
    """Overpass answers 429/504 when busy; wait and retry a couple of times."""
    for wait in (*waits, None):
        resp = session.post(
            OVERPASS_URL, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=300
        )
        if resp.status_code not in (429, 504) or wait is None:
            return resp
        time.sleep(wait)
    raise AssertionError("unreachable")


def fetch_elements(
    gmina: gpd.GeoDataFrame,
    cache_dir: Path = DEFAULT_CACHE,
    session: requests.Session | None = None,
    waits: tuple[float, ...] = (30, 90),
) -> list[dict]:
    teryt = gmina.iloc[0]["teryt"]
    path = Path(cache_dir) / f"osm_{teryt}.json"
    if not path.exists():
        bounds = tuple(gmina.to_crs(WORK_CRS).buffer(MARGIN_M).to_crs(4326).total_bounds)
        resp = _post_with_retry(session or requests.Session(), build_query(bounds), waits)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("remark"):  # Overpass reports timeouts and errors here, with HTTP 200
            raise RuntimeError(f"Overpass: {payload['remark']}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))["elements"]


def classify(tags: dict) -> str | None:
    """Rule key (or grid feature) for an element's tags; None if it is not used."""
    if "building" in tags:
        return "building"
    if (hw := tags.get("highway")) is not None:
        return f"road:{hw.removesuffix('_link')}"
    if "railway" in tags:
        return "railway"
    if tags.get("natural") == "water" or tags.get("landuse") == "reservoir":
        return "water"
    if (ww := tags.get("waterway")) is not None:
        return f"waterway:{ww}"
    if tags.get("landuse") == "forest" or tags.get("natural") == "wood":
        return "forest"
    if tags.get("landuse") in {"residential", "cemetery", "allotments", "commercial", "retail"}:
        return "built_up"
    if tags.get("power") in {"line", "minor_line", "cable"}:
        return "grid:line"
    if tags.get("power") == "substation":
        return "grid:substation"
    return None


def _coords(geometry: list[dict]) -> list[tuple[float, float]]:
    return [(p["lon"], p["lat"]) for p in geometry]


def _way_geometry(el: dict):
    pts = _coords(el.get("geometry", []))
    if len(pts) < 2:
        return None
    closed = len(pts) >= 4 and pts[0] == pts[-1]
    is_area = any(k in el.get("tags", {}) for k in AREA_KEYS) and "waterway" not in el["tags"]
    if closed and is_area and el["tags"].get("power") not in {"line", "minor_line", "cable"}:
        return Polygon(pts).buffer(0)
    return LineString(pts)


def _relation_geometry(el: dict):
    """Multipolygon from member ways: outer rings minus inner rings."""
    rings = {"outer": [], "inner": []}
    for m in el.get("members", []):
        if m.get("type") == "way" and len(m.get("geometry", [])) >= 2:
            rings["inner" if m.get("role") == "inner" else "outer"].append(LineString(_coords(m["geometry"])))
    outer = unary_union(list(polygonize(unary_union(rings["outer"])))) if rings["outer"] else None
    if outer is None or outer.is_empty:
        return None
    if rings["inner"]:
        outer = outer.difference(unary_union(list(polygonize(unary_union(rings["inner"])))))
    return outer


def elements_to_gdf(elements: list[dict]) -> gpd.GeoDataFrame:
    rows = []
    for el in elements:
        key = classify(el.get("tags", {}))
        if key is None:
            continue
        if el["type"] == "node":
            geom = Point(el["lon"], el["lat"])
        elif el["type"] == "way":
            geom = _way_geometry(el)
        else:
            geom = _relation_geometry(el)
        if geom is None or geom.is_empty:
            continue
        tags = el.get("tags", {})
        rows.append(
            {
                "key": key,
                "osm_id": f"{el['type']}/{el['id']}",
                "voltage": tags.get("voltage"),
                "geometry": geom,
            }
        )
    if not rows:
        cols = ["key", "osm_id", "voltage", "geometry"]
        return gpd.GeoDataFrame(columns=cols, geometry="geometry", crs=WORK_CRS)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(WORK_CRS)


def exclusions(features: gpd.GeoDataFrame, gmina: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Buffered exclusion zones, one dissolved polygon per rule, clipped to the gmina."""
    boundary = gmina.to_crs(WORK_CRS)
    rows = []
    for key, rule in RULES.items():
        sub = features[features["key"] == key]
        if sub.empty:
            continue
        zone = unary_union(list(sub.geometry.buffer(rule.buffer_m) if rule.buffer_m else sub.geometry))
        zone = zone.intersection(boundary.geometry.iloc[0])
        if zone.is_empty or zone.area == 0:
            continue
        rows.append(
            {
                "rule": key,
                "feature": rule.feature,
                "buffer_m": rule.buffer_m,
                "basis": rule.basis,
                "n_features": len(sub),
                "area_ha": zone.area / 10_000,
                "geometry": zone,
            }
        )
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=WORK_CRS)


def grid(features: gpd.GeoDataFrame, gmina: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Power lines and substations within reach of the gmina (not clipped: the nearest
    connection point may lie just across the border)."""
    sub = features[features["key"].str.startswith("grid:")].copy()
    sub["kind"] = sub["key"].str.removeprefix("grid:")
    return sub[["kind", "osm_id", "voltage", "geometry"]].reset_index(drop=True)


def summarize(zones: gpd.GeoDataFrame, gmina: gpd.GeoDataFrame) -> pd.DataFrame:
    total_ha = gmina.to_crs(WORK_CRS).geometry.area.sum() / 10_000
    table = zones[["rule", "buffer_m", "n_features", "area_ha"]].copy()
    union_ha = unary_union(list(zones.geometry)).area / 10_000 if not zones.empty else 0.0
    table.loc[len(table)] = ["all (union)", None, int(zones["n_features"].sum()), union_ha]
    table["share_of_gmina"] = table["area_ha"] / total_ha
    return table


if __name__ == "__main__":
    import sys

    from pvscreen.boundary import fetch_gmina

    gm = fetch_gmina(sys.argv[1] if len(sys.argv) > 1 else "3027062")
    feats = elements_to_gdf(fetch_elements(gm))
    zones = exclusions(feats, gm)
    out = Path("outputs") / f"osm_{gm.iloc[0]['teryt']}.gpkg"
    out.parent.mkdir(parents=True, exist_ok=True)
    zones.to_file(out, layer="exclusions", driver="GPKG")
    grid(feats, gm).to_file(out, layer="grid", driver="GPKG")
    pd.set_option("display.width", 160)
    print(summarize(zones, gm).to_string(index=False))
    print(f"written {out}")
