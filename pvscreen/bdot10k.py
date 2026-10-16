"""Buildings, built-up land, roads, railways, water and forest from BDOT10k.

BDOT10k is GUGiK's official topographic database (1:10 000). GUGiK publishes it as
open data in one Shapefile package per powiat, in PL-1992. Its classes are mapped
to the same rule keys as the OpenStreetMap features (pvscreen.osm.RULES), so both
sources go through the same buffers and the screening can run on either.

Two quirks of the packages, handled below: the .prj uses an Esri WKT that GDAL
does not match to EPSG:2180, and the .cpg files declare UTF-8 while the
attribute tables are in Windows-1250.

The power grid still comes from OpenStreetMap: BDOT10k has power lines (SULN) but
no substations, and the grid score is the distance to a substation.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import box

from pvscreen import WORK_CRS
from pvscreen.boundary import USER_AGENT
from pvscreen.osm import MARGIN_M

DOWNLOAD_URL = "https://opendata.geoportal.gov.pl/bdot10k/schemat2021/SHP/{woj}/{powiat}_SHP.zip"
DEFAULT_CACHE = Path("data/bdot10k")
ENCODING = "cp1250"

# Management category of a road -> the OSM highway value the Public Roads Act setbacks
# are keyed by (Polish OSM convention: primary = national road, ... unclassified =
# municipal road). Internal roads (droga wewnętrzna: farm, forest, estate roads) are
# left out, as OSM service and track ways are.
ROAD_CATEGORY = {
    "krajowa": "road:primary",
    "wojewódzka": "road:secondary",
    "powiatowa": "road:tertiary",
    "gminna": "road:unclassified",
}
ROAD_CLASS = {"autostrada": "road:motorway", "droga ekspresowa": "road:trunk"}  # override the category
BUILT_UP = {"jednorodzinna", "wielorodzinna", "handlowo-usługowa"}  # PTZB kinds, as OSM residential/retail


def classify(cls: str, row: pd.Series) -> str | None:
    """Rule key for one BDOT10k object of class `cls` (e.g. "BUBD_A"); None if unused."""
    kind = row.get("RODZAJ")
    if cls == "BUBD_A":
        return None if row.get("KAT_ISTNIE") == "zniszczony" else "building"
    if cls == "PTZB_A":
        return "built_up" if kind in BUILT_UP else None
    if cls == "KUSC_A":
        return "built_up" if str(kind).startswith("cmentarz") else None
    if cls == "PTUT_A":
        return "built_up" if kind == "ogródki działkowe" else None
    if cls == "SKDR_L":
        return ROAD_CLASS.get(str(row.get("KLASA_DROG"))) or ROAD_CATEGORY.get(str(row.get("KAT_ZARZAD")))
    if cls == "SKTR_L":
        return "railway"
    if cls == "PTWP_A":
        return "water"
    if cls == "SWRS_L":
        return "waterway:river" if kind == "rzeka" else "waterway:stream"
    if cls == "SWKN_L":
        return "waterway:canal"
    if cls == "PTLZ_A":
        return "forest"  # las, zagajnik (young forest), zadrzewienie (tree group)
    return None


CLASSES = ("BUBD_A", "PTZB_A", "KUSC_A", "PTUT_A", "SKDR_L", "SKTR_L", "PTWP_A", "SWRS_L", "SWKN_L", "PTLZ_A")


def package_path(
    teryt: str, cache_dir: Path = DEFAULT_CACHE, session: requests.Session | None = None
) -> Path:
    """The cached powiat package for a gmina's TERYT code, downloaded on first use."""
    powiat = teryt[:4]
    path = Path(cache_dir) / f"{powiat}_SHP.zip"
    if not path.exists():
        url = DOWNLOAD_URL.format(woj=teryt[:2], powiat=powiat)
        resp = (session or requests.Session()).get(url, headers={"User-Agent": USER_AGENT}, timeout=300)
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return path


def read_class(
    package: Path, cls: str, bbox: tuple[float, float, float, float] | None = None
) -> gpd.GeoDataFrame:
    """One BDOT10k class from a powiat package, in PL-1992, optionally limited to a box."""
    with zipfile.ZipFile(package) as zf:
        names = [n for n in zf.namelist() if n.endswith(f"__OT_{cls}.shp")]
    if not names:
        return gpd.GeoDataFrame(geometry=[], crs=WORK_CRS)
    gdf = gpd.read_file(
        f"/vsizip/{Path(package).resolve().as_posix()}/{names[0]}", bbox=bbox, encoding=ENCODING
    )
    return gdf.set_crs(WORK_CRS, allow_override=True)


def features(gmina: gpd.GeoDataFrame, cache_dir: Path = DEFAULT_CACHE) -> gpd.GeoDataFrame:
    """Exclusion features around the gmina, with a `key` column like osm.elements_to_gdf."""
    package = package_path(gmina.iloc[0]["teryt"], cache_dir)
    xmin, ymin, xmax, ymax = gmina.to_crs(WORK_CRS).total_bounds
    bbox = (xmin - MARGIN_M, ymin - MARGIN_M, xmax + MARGIN_M, ymax + MARGIN_M)
    parts = []
    for cls in CLASSES:
        gdf = read_class(package, cls, bbox)
        if gdf.empty:
            continue
        gdf["key"] = [classify(cls, row) for _, row in gdf.iterrows()]
        gdf["source_id"] = cls + "/" + gdf["LOKALNYID"].astype(str)
        parts.append(gdf.loc[gdf["key"].notna(), ["key", "source_id", "geometry"]])
    if not parts:
        return gpd.GeoDataFrame(columns=["key", "source_id", "geometry"], geometry="geometry", crs=WORK_CRS)
    out = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs=WORK_CRS)
    return out[out.intersects(box(*bbox))].reset_index(drop=True)
