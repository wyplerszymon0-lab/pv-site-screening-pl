"""Combine terrain, exclusions and the grid into scored candidate areas.

Works on the terrain grid (5 m cells, EPSG:2180): a cell is suitable if it lies
inside the gmina, outside every exclusion zone and is not too steep. Suitable
cells are turned into polygons, thin strips and small pieces are dropped, and
each remaining candidate gets a score from slope, aspect and distance to the grid.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import rasterize, shapes
from shapely.geometry import shape
from shapely.ops import unary_union

from pvscreen import WORK_CRS

DEFAULT_CONFIG = Path(__file__).with_name("screening.toml")


@dataclass(frozen=True)
class Config:
    max_slope_deg: float
    min_area_ha: float
    min_half_width_m: float
    weight_slope: float
    weight_aspect: float
    weight_grid: float
    flat_below_deg: float
    grid_zero_at_m: float
    mwp_per_ha: float
    system_loss_pct: float


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    cfg = Config(**raw["mask"], **raw["score"], **raw["energy"])
    total = cfg.weight_slope + cfg.weight_aspect + cfg.weight_grid
    if abs(total - 1) > 1e-9:
        raise ValueError(f"score weights must add up to 1, got {total}")
    return cfg


def suitable_mask(
    slope: np.ndarray,
    inside: np.ndarray,
    exclusion_zones: list,
    transform: rasterio.Affine,
    cfg: Config,
) -> np.ndarray:
    """True where a cell is inside the gmina, not excluded and not too steep."""
    excluded = np.zeros(slope.shape, dtype=bool)
    geoms = [g for g in exclusion_zones if g is not None and not g.is_empty]
    if geoms:
        excluded = rasterize(
            ((g, 1) for g in geoms), out_shape=slope.shape, transform=transform, fill=0, dtype="uint8"
        ).astype(bool)
    with np.errstate(invalid="ignore"):
        gentle = slope <= cfg.max_slope_deg  # NaN (no terrain data) is never suitable
    return inside & ~excluded & gentle


def slope_score(slope: np.ndarray, cfg: Config) -> np.ndarray:
    return np.clip(1 - slope / cfg.max_slope_deg, 0, 1)


def aspect_score(slope: np.ndarray, aspect: np.ndarray, cfg: Config) -> np.ndarray:
    tilted = (1 + np.cos(np.radians(aspect - 180))) / 2
    return np.where(slope < cfg.flat_below_deg, 1.0, tilted)


def grid_score(distance_m: float, cfg: Config) -> float:
    return float(np.clip(1 - distance_m / cfg.grid_zero_at_m, 0, 1))


def candidate_polygons(mask: np.ndarray, transform: rasterio.Affine, cfg: Config) -> list:
    """Polygons of suitable cells, without strips thinner than 2 x min_half_width_m
    and without pieces smaller than min_area_ha."""
    pieces = [
        shape(geom) for geom, value in shapes(mask.astype("uint8"), mask=mask, transform=transform) if value
    ]
    if not pieces:
        return []
    merged = unary_union(pieces)
    w = cfg.min_half_width_m
    opened = merged.buffer(-w, join_style="mitre").buffer(w, join_style="mitre").intersection(merged)
    parts = getattr(opened, "geoms", [opened])
    return [p for p in parts if p.geom_type == "Polygon" and p.area >= cfg.min_area_ha * 10_000]


def score_candidates(
    polygons: list,
    slope: np.ndarray,
    aspect: np.ndarray,
    transform: rasterio.Affine,
    grid: gpd.GeoDataFrame,
    constraints: gpd.GeoDataFrame | None,
    cfg: Config,
) -> gpd.GeoDataFrame:
    s_slope, s_aspect = slope_score(slope, cfg), aspect_score(slope, aspect, cfg)

    def union_of(kind):
        sub = grid[grid["kind"] == kind] if not grid.empty else grid
        return unary_union(list(sub.geometry)) if not sub.empty else None

    substations, lines = union_of("substation"), union_of("line")
    constraint_union = (
        unary_union(list(constraints.geometry)) if constraints is not None and not constraints.empty else None
    )
    rows = []
    for poly in polygons:
        cells = rasterize([(poly, 1)], out_shape=slope.shape, transform=transform, fill=0, dtype="uint8") == 1
        cells &= ~np.isnan(slope)
        dist = poly.distance(substations) if substations is not None else float("inf")
        parts = {
            "score_slope": float(np.mean(s_slope[cells])),
            "score_aspect": float(np.mean(s_aspect[cells])),
            "score_grid": grid_score(dist, cfg),
        }
        rows.append(
            {
                "area_ha": poly.area / 10_000,
                "mean_slope_deg": float(np.mean(slope[cells])),
                "substation_distance_m": dist,
                "line_distance_m": poly.distance(lines) if lines is not None else float("inf"),
                **parts,
                "score": cfg.weight_slope * parts["score_slope"]
                + cfg.weight_aspect * parts["score_aspect"]
                + cfg.weight_grid * parts["score_grid"],
                "constraint_share": poly.intersection(constraint_union).area / poly.area
                if constraint_union
                else 0.0,
                "geometry": poly,
            }
        )
    cols = [
        "area_ha",
        "mean_slope_deg",
        "substation_distance_m",
        "line_distance_m",
        "score_slope",
        "score_aspect",
        "score_grid",
        "score",
        "constraint_share",
        "geometry",
    ]
    gdf = gpd.GeoDataFrame(rows, columns=cols, geometry="geometry", crs=WORK_CRS)
    gdf = gdf.sort_values("score", ascending=False, ignore_index=True)
    gdf.insert(0, "rank", range(1, len(gdf) + 1))
    return gdf


def drop_substations_near(grid: gpd.GeoDataFrame, geom, within_m: float) -> gpd.GeoDataFrame:
    """Grid layer without the substations closer than within_m to geom (lines are kept)."""
    near = (grid["kind"] == "substation") & (grid.distance(geom) < within_m)
    return grid[~near]


def screen(
    gmina: gpd.GeoDataFrame,
    cfg: Config | None = None,
    ignore_substations_near=None,
    ignore_within_m: float = 500.0,
) -> gpd.GeoDataFrame:
    """Run the whole screening for one gmina from the cached input layers.

    ignore_substations_near: optional geometry; substations within ignore_within_m
    of it are left out of the grid score (used by validation, because a solar farm
    often brings its own substation).
    """
    from pvscreen import osm, protected, terrain

    cfg = cfg or load_config()
    dem, transform = terrain.fetch_dem(gmina)
    slope, aspect = terrain.slope_aspect(dem, terrain.DEFAULT_RES_M)
    inside = terrain.gmina_mask(gmina, dem.shape, transform)

    feats = osm.elements_to_gdf(osm.fetch_elements(gmina))
    areas = protected.protected_areas(gmina)
    zones = list(osm.exclusions(feats, gmina).geometry)
    zones += list(areas[areas["kind"] == protected.EXCLUDE].geometry)

    mask = suitable_mask(slope, inside, zones, transform, cfg)
    polygons = candidate_polygons(mask, transform, cfg)
    constraints = areas[areas["kind"] == protected.CONSTRAINT]
    grid = osm.grid(feats, gmina)
    if ignore_substations_near is not None:
        grid = drop_substations_near(grid, ignore_substations_near, ignore_within_m)
    return score_candidates(polygons, slope, aspect, transform, grid, constraints, cfg)


if __name__ == "__main__":
    import sys

    import pandas as pd

    from pvscreen.boundary import fetch_gmina
    from pvscreen.energy import add_energy

    gm = fetch_gmina(sys.argv[1] if len(sys.argv) > 1 else "3027062")
    cfg = load_config()
    cands = add_energy(screen(gm, cfg), cfg.mwp_per_ha, cfg.system_loss_pct / 100)
    out = Path("outputs") / f"candidates_{gm.iloc[0]['teryt']}.gpkg"
    out.parent.mkdir(parents=True, exist_ok=True)
    cands.to_file(out, layer="candidates", driver="GPKG")
    pd.set_option("display.width", 160)
    print(cands.drop(columns="geometry").head(15).round(2).to_string(index=False))
    print(
        f"{len(cands)} candidates, {cands['area_ha'].sum():.0f} ha, {cands['capacity_mwp'].sum():.0f} MWp, "
        f"{cands['energy_mwh_year'].sum() / 1000:.0f} GWh/year; written {out}"
    )
