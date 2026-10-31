"""The whole screening for one gmina, from TERYT code to candidates and a report.

    python -m pvscreen --teryt 3027022 [--crs pl2000]

Every download is cached (data/), so a second run for the same gmina only
recomputes. Outputs: outputs/<teryt>/screening.gpkg (layers boundary,
exclusions, candidates, grid, farms), outputs/<teryt>/slope.tif and
reports/<teryt>.md. qgis/make_project.py turns them into a styled QGIS project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio

from pvscreen import WORK_CRS, crs, osm, protected, terrain, validate
from pvscreen.boundary import fetch_gmina
from pvscreen.energy import add_energy
from pvscreen.suitability import Config, exclusion_features, load_config, load_inputs, screen


@dataclass
class Result:
    gmina: gpd.GeoDataFrame
    cfg: Config
    osm_zones: gpd.GeoDataFrame
    protected_areas: gpd.GeoDataFrame
    candidates: gpd.GeoDataFrame
    farms: gpd.GeoDataFrame
    validation: dict | None  # None when OSM maps no solar farm of at least 1 ha in the gmina
    # For the map: power lines and substations, and the slope raster (NaN outside the gmina).
    grid: gpd.GeoDataFrame | None = None
    slope: np.ndarray | None = field(default=None, repr=False)
    transform: rasterio.Affine | None = None
    source: str = "osm"  # where buildings, roads, water and forest came from


def run(teryt: str, cfg: Config | None = None, source: str = "osm") -> Result:
    cfg = cfg or load_config()
    gmina = fetch_gmina(teryt)
    inputs = load_inputs(gmina, source)
    candidates = add_energy(
        screen(gmina, cfg, inputs=inputs), cfg.mwp_per_ha, cfg.system_loss_pct / 100,
        last_year=date.today().year - 1,  # P90 from every complete year since 2001
    )

    feats = exclusion_features(gmina, source)
    farms = validate.farms_in_gmina(validate.fetch_farm_elements(gmina), gmina)
    summary = validate.evaluate(farms, candidates, gmina)[1] if len(farms) else None
    return Result(
        gmina=gmina,
        cfg=cfg,
        osm_zones=osm.exclusions(feats, gmina),
        protected_areas=protected.protected_areas(gmina),
        candidates=candidates,
        farms=farms,
        validation=summary,
        grid=inputs.grid,
        slope=np.where(inputs.inside, inputs.slope, np.nan),
        transform=inputs.transform,
        source=source,
    )


def write_outputs(
    result: Result, system: str = "pl1992", out_root: Path = Path("outputs")
) -> tuple[Path, str]:
    """GeoPackage with boundary, exclusions, candidates, grid and farms, in PL-1992 or the
    PL-2000 zone, plus the slope raster (always PL-1992; GIS software reprojects it on the fly)."""
    teryt = result.gmina.iloc[0]["teryt"]
    suffix = "" if result.source == "osm" else f"_{result.source}"
    path = out_root / teryt / f"screening{suffix}.gpkg"
    path.parent.mkdir(parents=True, exist_ok=True)
    note = ""
    layers = {
        "boundary": result.gmina,
        "exclusions": pd.concat(
            [
                result.osm_zones[["rule", "basis", "geometry"]],
                result.protected_areas.rename(columns={"category": "rule", "reason": "basis"})[
                    ["rule", "basis", "geometry"]
                ],
            ],
            ignore_index=True,
        ),
        "candidates": result.candidates,
    }
    if result.grid is not None:
        layers["grid"] = result.grid[["kind", "osm_id", "voltage", "geometry"]]
    if len(result.farms):
        layers["farms"] = result.farms[["osm_id", "name", "area_ha", "geometry"]]
    for name, layer in layers.items():
        out, note = crs.to_output_crs(
            gpd.GeoDataFrame(layer, geometry="geometry", crs=WORK_CRS), system, result.gmina
        )
        out.to_file(path, layer=name, driver="GPKG")
    if result.slope is not None:
        terrain.write_raster(path.parent / "slope.tif", result.slope, result.transform)
    return path, note


def render_report(
    result: Result, gpkg: Path, crs_note: str, today: date | None = None, lang: str = "en"
) -> str:
    """The Markdown report, in English or Polish (see pvscreen.report)."""
    from pvscreen import report

    return report.render(result, gpkg, crs_note, today, lang)
