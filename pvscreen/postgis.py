"""The exclusion overlay in PostGIS, and a comparison with the GeoPandas version.

The Python pipeline (suitability.py) rasterises the exclusion zones onto the 5 m
terrain grid. Here the same inputs go into a spatial database as vectors, and
the overlay is done in SQL: terrain that is not too steep, minus every exclusion
zone, opened to remove thin strips, split into parts, small parts dropped.
Both versions should agree up to the rasterisation along zone edges.

    python -m pvscreen.postgis 3027062   # starts PostGIS (docker compose), loads, runs, compares
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import geopandas as gpd
import numpy as np
from rasterio.features import shapes
from shapely.geometry import shape
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from pvscreen import WORK_CRS
from pvscreen.suitability import Config, Inputs

DEFAULT_URL = "postgresql+psycopg://pvscreen:pvscreen@127.0.0.1:5433/pvscreen"  # docker-compose.yml
REPO = Path(__file__).resolve().parent.parent

# The main query. Parameters: :half_width (m) and :min_area (m²).
CANDIDATES_SQL = """
WITH ok AS (                                   -- gentle terrain inside the gmina
    SELECT ST_Union(geom) AS g FROM terrain_ok
), excluded AS (                               -- every exclusion zone that touches it
    SELECT ST_Union(e.geom) AS g
    FROM exclusions e, ok
    WHERE ST_Intersects(e.geom, ok.g)          -- uses the GiST index on exclusions
), free AS (
    -- no zone at all: subtract an empty polygon in the same SRID (SRID 0 would be refused)
    SELECT ST_Difference(
        ok.g, COALESCE(excluded.g, ST_SetSRID('POLYGON EMPTY'::geometry, ST_SRID(ok.g)))
    ) AS g
    FROM ok, excluded
), opened AS (                                 -- remove strips narrower than 2 x half_width
    SELECT ST_Intersection(
        ST_Buffer(ST_Buffer(g, -:half_width, 'join=mitre'), :half_width, 'join=mitre'), g
    ) AS g
    FROM free
), parts AS (
    SELECT (ST_Dump(g)).geom AS g FROM opened
)
SELECT ST_Area(g) / 10000.0 AS area_ha, g AS geom
FROM parts
WHERE ST_GeometryType(g) = 'ST_Polygon' AND ST_Area(g) >= :min_area
ORDER BY area_ha DESC
"""


def database_url() -> str:
    return os.environ.get("PVSCREEN_PG_URL", DEFAULT_URL)


def start_database() -> None:
    """docker compose up -d --wait, from the repository root."""
    subprocess.run(["docker", "compose", "up", "-d", "--wait"], cwd=REPO, check=True)


def terrain_ok_polygons(inputs: Inputs, cfg: Config) -> gpd.GeoDataFrame:
    """Cells inside the gmina, with terrain data and slope <= max_slope, as polygons."""
    with np.errstate(invalid="ignore"):
        ok = inputs.inside & (inputs.slope <= cfg.max_slope_deg)
    geoms = [shape(g) for g, v in shapes(ok.astype("uint8"), mask=ok, transform=inputs.transform) if v]
    return gpd.GeoDataFrame(geometry=geoms, crs=WORK_CRS)


def load_layers(engine: Engine, terrain_ok: gpd.GeoDataFrame, exclusions: gpd.GeoDataFrame) -> None:
    """(Re)create the two input tables with GiST indexes."""
    for name, gdf in (("terrain_ok", terrain_ok), ("exclusions", exclusions)):
        gdf.rename_geometry("geom").to_postgis(name, engine, if_exists="replace", index=False)
        with engine.begin() as conn:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name}_geom_idx ON {name} USING GIST (geom)"))
            conn.execute(text(f"ANALYZE {name}"))


def run_overlay(engine: Engine, cfg: Config) -> gpd.GeoDataFrame:
    params = {"half_width": cfg.min_half_width_m, "min_area": cfg.min_area_ha * 10_000}
    with engine.connect() as conn:
        return gpd.read_postgis(text(CANDIDATES_SQL), conn, geom_col="geom", params=params, crs=WORK_CRS)


def compare(sql: gpd.GeoDataFrame, python: gpd.GeoDataFrame) -> dict[str, float]:
    """How far apart the two candidate sets are, by area."""
    a, b = sql.geometry.union_all(), python.geometry.union_all()
    union = a.union(b).area
    return {
        "candidates_sql": len(sql),
        "candidates_python": len(python),
        "area_ha_sql": a.area / 10_000,
        "area_ha_python": b.area / 10_000,
        "area_difference": abs(a.area - b.area) / b.area,
        "symmetric_difference_share": a.symmetric_difference(b).area / union if union else 0.0,
    }


if __name__ == "__main__":
    import sys
    import time

    from pvscreen.boundary import fetch_gmina
    from pvscreen.suitability import load_config, load_inputs, screen

    gm = fetch_gmina(sys.argv[1] if len(sys.argv) > 1 else "3027062")
    cfg = load_config()
    start_database()
    inputs = load_inputs(gm)
    engine = create_engine(database_url())

    t0 = time.time()
    ok = terrain_ok_polygons(inputs, cfg)
    excl = gpd.GeoDataFrame(geometry=inputs.exclusions, crs=WORK_CRS)
    load_layers(engine, ok, excl)
    t1 = time.time()
    sql = run_overlay(engine, cfg)
    t2 = time.time()
    python = screen(gm, cfg, inputs=inputs)
    t3 = time.time()

    print(f"loaded {len(ok)} terrain polygons and {len(excl)} exclusion zones in {t1 - t0:.1f} s")
    print(f"SQL overlay {t2 - t1:.1f} s, GeoPandas screening {t3 - t2:.1f} s")
    for k, v in compare(sql, python).items():
        print(f"{k:28s} {v:.4f}" if isinstance(v, float) else f"{k:28s} {v}")
