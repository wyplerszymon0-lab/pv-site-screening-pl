"""OpenStreetMap as of a past date, from Geofabrik's dated regional extracts.

Overpass can answer "as of a date", but not for this much data: asking for the
layers around Przykona ran out of its memory (2 GB), and way["natural"="water"]
did so even for 1/64 of the area. Geofabrik keeps plain files of each voivodeship
(wielkopolskie-210101.osm.pbf is the map of 1 January 2021), which GDAL reads in
seconds and which give the same result every time.

Elements are returned in the shape Overpass gives them, so everything after the
download (osm.elements_to_gdf, validate.farms_in_gmina) works unchanged.

Data © OpenStreetMap contributors, ODbL.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pandas as pd
import pyogrio
import requests

from pvscreen.boundary import USER_AGENT

GEOFABRIK_URL = "https://download.geofabrik.de/europe/poland"
DEFAULT_CACHE = Path("data/osm/extracts")
# The study area lies on the border of these two voivodeships.
REGIONS = ("wielkopolskie", "lodzkie")
LAYERS = ("points", "lines", "multipolygons")
NOT_TAGS = {"osm_id", "osm_way_id", "other_tags", "z_order", "geometry"}
OTHER_TAG = re.compile(r'"((?:[^"\\]|\\.)*)"=>"((?:[^"\\]|\\.)*)"')


def extract_name(region: str, as_of: str) -> str:
    return f"{region}-{date.fromisoformat(as_of):%y%m%d}.osm.pbf"


def fetch_extract(
    region: str, as_of: str, cache_dir: Path = DEFAULT_CACHE, session: requests.Session | None = None
) -> Path:
    """The region's extract of that date, downloaded once. Geofabrik does not keep
    every day: 1 January of each year and the first of recent months are there."""
    path = Path(cache_dir) / extract_name(region, as_of)
    if not path.exists():
        url = f"{GEOFABRIK_URL}/{path.name}"
        with (session or requests.Session()).get(
            url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=(60, 300)
        ) as resp:
            if resp.status_code == 404:
                raise ValueError(f"Geofabrik has no extract for {as_of}: {url}")
            resp.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_suffix(".part")  # a broken download must not look like a finished one
            with open(part, "wb") as f:
                for chunk in resp.iter_content(1 << 20):
                    f.write(chunk)
            part.replace(path)
    return path


def parse_other_tags(text) -> dict[str, str]:
    """GDAL's other_tags column: "power"=>"line","voltage"=>"110000". Empty cells are NaN."""
    if not isinstance(text, str):
        return {}
    return {k.replace('\\"', '"'): v.replace('\\"', '"') for k, v in OTHER_TAG.findall(text)}


def _ring(coords) -> list[dict]:
    return [{"lon": c[0], "lat": c[1]} for c in coords]


def to_element(tags: dict, osm_id, way_id, geom) -> dict | None:
    """One GDAL feature as an Overpass element (node, way or relation with members)."""
    if geom.geom_type == "Point":
        return {"type": "node", "id": int(osm_id), "lon": geom.x, "lat": geom.y, "tags": tags}
    if geom.geom_type == "LineString":
        return {"type": "way", "id": int(osm_id), "tags": tags, "geometry": _ring(geom.coords)}
    if geom.geom_type != "MultiPolygon":
        return None
    if pd.notna(way_id):  # a closed way: GDAL lists it among the multipolygons
        ring = _ring(geom.geoms[0].exterior.coords)
        return {"type": "way", "id": int(way_id), "tags": tags, "geometry": ring}
    members = []
    for polygon in geom.geoms:
        members.append({"type": "way", "role": "outer", "geometry": _ring(polygon.exterior.coords)})
        for hole in polygon.interiors:
            members.append({"type": "way", "role": "inner", "geometry": _ring(hole.coords)})
    return {"type": "relation", "id": int(osm_id), "tags": tags, "members": members}


def read_elements(
    paths: list[Path], bounds_wgs84: tuple[float, float, float, float], keep: Callable[[dict], bool]
) -> list[dict]:
    """Elements in the bounding box whose tags pass keep. A way on the regions' border
    is in both files and counts once."""
    elements, seen = [], set()
    for path in paths:
        for layer in LAYERS:
            frame = pyogrio.read_dataframe(path, layer=layer, bbox=bounds_wgs84)
            columns = [c for c in frame.columns if c not in NOT_TAGS]
            for row in frame.to_dict("records"):
                if row["geometry"] is None:
                    continue
                tags = {c: row[c] for c in columns if pd.notna(row[c])}
                tags.update(parse_other_tags(row.get("other_tags")))
                if not keep(tags):
                    continue
                el = to_element(tags, row.get("osm_id"), row.get("osm_way_id"), row["geometry"])
                if el is not None and (el["type"], el["id"]) not in seen:
                    seen.add((el["type"], el["id"]))
                    elements.append(el)
    return elements


def elements(
    bounds_wgs84: tuple[float, float, float, float], as_of: str, keep: Callable[[dict], bool]
) -> list[dict]:
    return read_elements([fetch_extract(r, as_of) for r in REGIONS], bounds_wgs84, keep)
