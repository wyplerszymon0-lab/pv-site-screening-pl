"""Terrain from the national digital terrain model (NMT): elevation, slope, aspect.

GUGiK serves the 1 m NMT as a WCS coverage in EPSG:2180. A whole gmina at 1 m is
hundreds of millions of cells, so tiles are requested with SCALEFACTOR and the
server returns them already resampled to the working resolution (5 m by default),
which is plenty for slope criteria on parcels of several hectares.
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import requests
from rasterio.features import geometry_mask
from rasterio.merge import merge

from pvscreen import WORK_CRS
from pvscreen.boundary import USER_AGENT

NMT_WCS_URL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/NMT/GRID1/WCS/DigitalTerrainModelFormatTIFF"
NMT_COVERAGE = "DTM_PL-KRON86-NH_TIFF"
NATIVE_RES_M = 1.0
TILE_M = 2000
DEFAULT_RES_M = 5.0
DEFAULT_CACHE = Path("data/nmt")
NODATA = -9999.0

Tile = tuple[int, int, int, int]  # xmin, ymin, xmax, ymax in EPSG:2180 metres


def tile_grid(bounds: tuple[float, float, float, float], tile_m: int = TILE_M) -> list[Tile]:
    """Tiles aligned to multiples of tile_m that together cover bounds."""
    xmin, ymin, xmax, ymax = bounds
    x0, y0 = math.floor(xmin / tile_m) * tile_m, math.floor(ymin / tile_m) * tile_m
    x1, y1 = math.ceil(xmax / tile_m) * tile_m, math.ceil(ymax / tile_m) * tile_m
    return [(x, y, x + tile_m, y + tile_m) for y in range(y0, y1, tile_m) for x in range(x0, x1, tile_m)]


def coverage_params(tile: Tile, res_m: float = DEFAULT_RES_M) -> dict[str, str | list[str]]:
    xmin, ymin, xmax, ymax = tile
    return {
        "SERVICE": "WCS",
        "VERSION": "2.0.1",
        "REQUEST": "GetCoverage",
        "COVERAGEID": NMT_COVERAGE,
        "FORMAT": "image/tiff",
        "SUBSET": [f"x({xmin},{xmax})", f"y({ymin},{ymax})"],  # requests repeats the key
        "SCALEFACTOR": f"{NATIVE_RES_M / res_m:g}",
    }


# Waits before each retry of a tile. The service sometimes drops a connection
# (seen: an SSL "wrong version number" error mid-run) or answers 5xx.
RETRY_WAITS_S = (10, 60, 180)


def _get_with_retry(session: requests.Session, params: dict, waits=RETRY_WAITS_S) -> requests.Response:
    for wait in (*waits, None):
        try:
            resp = session.get(NMT_WCS_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=180)
            if resp.status_code < 500 or wait is None:
                return resp
        except (requests.ConnectionError, requests.Timeout):
            if wait is None:
                raise
        time.sleep(wait)
    raise AssertionError("unreachable")


def _fetch_tile(
    tile: Tile, res_m: float, cache_dir: Path, session: requests.Session, waits=RETRY_WAITS_S
) -> Path:
    path = cache_dir / f"nmt_{tile[0]}_{tile[1]}_{res_m:g}m.tif"
    if not path.exists():
        resp = _get_with_retry(session, coverage_params(tile, res_m), waits)
        resp.raise_for_status()
        if not resp.content.startswith((b"II*\x00", b"MM\x00*")):
            raise RuntimeError(f"NMT WCS did not return a GeoTIFF for tile {tile}: {resp.content[:200]!r}")
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return path


def fetch_dem(
    gmina: gpd.GeoDataFrame,
    res_m: float = DEFAULT_RES_M,
    cache_dir: Path = DEFAULT_CACHE,
    session: requests.Session | None = None,
) -> tuple[np.ndarray, rasterio.Affine]:
    """Elevation over the gmina's bounding box (NaN where the service has no data)."""
    http = session or requests.Session()
    tiles = tile_grid(tuple(gmina.to_crs(WORK_CRS).total_bounds))
    paths = [_fetch_tile(t, res_m, Path(cache_dir), http) for t in tiles]
    sources = [rasterio.open(p) for p in paths]
    try:
        mosaic, transform = merge(sources, nodata=NODATA)
    finally:
        for src in sources:
            src.close()
    dem = mosaic[0].astype("float64")
    # The service declares no nodata value and returns voids as exactly 0 m
    # (1 256 cells in Przykona, where the ground is 90-140 m). Treating 0 as
    # missing is wrong only for land at sea level (Żuławy, the coast).
    dem[(dem <= NODATA) | (dem == 0.0)] = np.nan
    return dem, transform


def slope_aspect(dem: np.ndarray, res_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Slope (degrees) and aspect (compass degrees the slope faces) by Horn's method.

    Row 0 is the northern edge, as in a north-up raster. Aspect is the downhill
    direction: 180 = facing south. Flat cells have aspect NaN; edge cells and any
    cell next to NaN are NaN in both outputs.
    """
    z = np.pad(dem.astype("float64"), 1, constant_values=np.nan)
    a, b, c = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
    d, f = z[1:-1, :-2], z[1:-1, 2:]
    g, h, i = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]
    dz_east = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * res_m)
    dz_north = ((a + 2 * b + c) - (g + 2 * h + i)) / (8 * res_m)
    slope = np.degrees(np.arctan(np.hypot(dz_east, dz_north)))
    aspect = np.degrees(np.arctan2(-dz_east, -dz_north)) % 360
    aspect[slope < 1e-9] = np.nan
    # Horn's kernel ignores the centre cell, so a missing cell would still get
    # a slope from its neighbours; it must stay missing.
    missing = np.isnan(dem)
    slope[missing] = np.nan
    aspect[missing] = np.nan
    return slope, aspect


def gmina_mask(gmina: gpd.GeoDataFrame, shape: tuple[int, int], transform: rasterio.Affine) -> np.ndarray:
    """True for cells whose centre lies inside the gmina."""
    return geometry_mask(gmina.to_crs(WORK_CRS).geometry, out_shape=shape, transform=transform, invert=True)


def write_raster(path: Path, array: np.ndarray, transform: rasterio.Affine) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.where(np.isnan(array), NODATA, array).astype("float32")
    profile = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": WORK_CRS,
        "transform": transform,
        "nodata": NODATA,
        "compress": "deflate",
        "tiled": True,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def summarize(slope: np.ndarray, inside: np.ndarray) -> dict[str, float]:
    values = slope[inside & ~np.isnan(slope)]
    return {
        "median_slope_deg": float(np.median(values)),
        "p95_slope_deg": float(np.percentile(values, 95)),
        "share_under_5_deg": float(np.mean(values < 5)),
        "cells": int(values.size),
    }


def build_terrain(
    gmina: gpd.GeoDataFrame,
    out_dir: Path = Path("outputs"),
    res_m: float = DEFAULT_RES_M,
    cache_dir: Path = DEFAULT_CACHE,
) -> dict[str, float]:
    """Write dem/slope/aspect GeoTIFFs for the gmina (cells outside it are nodata)."""
    teryt = gmina.iloc[0]["teryt"]
    dem, transform = fetch_dem(gmina, res_m, cache_dir)
    slope, aspect = slope_aspect(dem, res_m)
    inside = gmina_mask(gmina, dem.shape, transform)
    for name, arr in (("dem", dem), ("slope", slope), ("aspect", aspect)):
        path = Path(out_dir) / f"{name}_{teryt}_{res_m:g}m.tif"
        write_raster(path, np.where(inside, arr, np.nan), transform)
    return summarize(slope, inside)


if __name__ == "__main__":
    from pvscreen.boundary import fetch_gmina

    print(build_terrain(fetch_gmina(sys.argv[1] if len(sys.argv) > 1 else "3027062")))
