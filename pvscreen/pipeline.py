"""The whole screening for one gmina, from TERYT code to candidates and a report.

    python -m pvscreen --teryt 3027022 [--crs pl2000]

Every download is cached (data/), so a second run for the same gmina only
recomputes. Outputs: outputs/<teryt>/screening.gpkg (layers boundary,
exclusions, candidates) and reports/<teryt>.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd

from pvscreen import WORK_CRS, __version__, crs, osm, protected, validate
from pvscreen.boundary import fetch_gmina
from pvscreen.energy import add_energy
from pvscreen.suitability import Config, load_config, load_inputs, screen


@dataclass
class Result:
    gmina: gpd.GeoDataFrame
    cfg: Config
    osm_zones: gpd.GeoDataFrame
    protected_areas: gpd.GeoDataFrame
    candidates: gpd.GeoDataFrame
    farms: gpd.GeoDataFrame
    validation: dict | None  # None when OSM maps no solar farm of at least 1 ha in the gmina


def run(teryt: str, cfg: Config | None = None) -> Result:
    cfg = cfg or load_config()
    gmina = fetch_gmina(teryt)
    inputs = load_inputs(gmina)
    candidates = add_energy(screen(gmina, cfg, inputs=inputs), cfg.mwp_per_ha, cfg.system_loss_pct / 100)

    feats = osm.elements_to_gdf(osm.fetch_elements(gmina))
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
    )


def write_outputs(
    result: Result, system: str = "pl1992", out_root: Path = Path("outputs")
) -> tuple[Path, str]:
    """GeoPackage with boundary, exclusions and candidates, in PL-1992 or the PL-2000 zone."""
    teryt = result.gmina.iloc[0]["teryt"]
    path = out_root / teryt / "screening.gpkg"
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
    for name, layer in layers.items():
        out, note = crs.to_output_crs(
            gpd.GeoDataFrame(layer, geometry="geometry", crs=WORK_CRS), system, result.gmina
        )
        out.to_file(path, layer=name, driver="GPKG")
    return path, note


def _pct(x: float) -> str:
    return f"{100 * x:.1f} %"


def render_report(result: Result, gpkg: Path, crs_note: str, today: date | None = None) -> str:
    g = result.gmina.iloc[0]
    area_ha = result.gmina.to_crs(WORK_CRS).geometry.area.sum() / 10_000
    c, cfg = result.candidates, result.cfg
    osm_union = osm.summarize(result.osm_zones, result.gmina).iloc[-1] if not result.osm_zones.empty else None
    excl = result.protected_areas[result.protected_areas["kind"] == protected.EXCLUDE]
    cons = result.protected_areas[result.protected_areas["kind"] == protected.CONSTRAINT]

    lines = [
        f"# Solar-farm screening: gmina {g['name']} (TERYT {g['teryt']})",
        "",
        f"_Generated {(today or date.today()).isoformat()} by `python -m pvscreen` (pvscreen {__version__}). "
        "A first-pass screening from open data, not a site assessment._",
        "",
        "## Gmina",
        "",
        f"- Area: {area_ha:,.0f} ha (official {g['official_area_ha']:,.0f} ha)",
        f"- Output: `{gpkg.as_posix()}`, {crs_note}",
        "",
        "## Thresholds",
        "",
        "| Setting | Value |",
        "|---|---:|",
        f"| Maximum slope | {cfg.max_slope_deg:g}° |",
        f"| Minimum candidate area | {cfg.min_area_ha:g} ha |",
        f"| Narrowest strip kept | {2 * cfg.min_half_width_m:g} m |",
        f"| Weights: slope / aspect / grid | {cfg.weight_slope:g} / {cfg.weight_aspect:g} / {cfg.weight_grid:g} |",
        f"| Grid score reaches 0 at | {cfg.grid_zero_at_m / 1000:g} km from a substation |",
        f"| Capacity / system losses | {cfg.mwp_per_ha:g} MWp per ha / {cfg.system_loss_pct:g} % |",
        "",
        "Sources and reasons for every value: `pvscreen/screening.toml`.",
        "",
        "## Exclusions",
        "",
        "- OpenStreetMap (buildings, roads, water, forest, built-up land, with buffers): "
        + (
            f"{osm_union['area_ha']:,.0f} ha, {_pct(osm_union['share_of_gmina'])} of the gmina"
            if osm_union is not None
            else "none"
        ),
        "- Protected nature areas excluded: "
        + (", ".join(f"{r.name} ({r.category}, {r.area_ha:,.0f} ha)" for r in excl.itertuples()) or "none"),
        "- Protected areas kept as constraints: "
        + (", ".join(f"{r.name} ({r.category}, {r.area_ha:,.0f} ha)" for r in cons.itertuples()) or "none"),
        "",
        "## Candidates",
        "",
    ]
    if c.empty:
        lines += ["No area passes the screening.", ""]
    else:
        lines += [
            f"{len(c)} candidates, {c['area_ha'].sum():,.0f} ha ({_pct(c['area_ha'].sum() / area_ha)} of the gmina), "
            f"{c['capacity_mwp'].sum():,.0f} MWp, about {c['energy_mwh_year'].sum() / 1000:,.0f} GWh per year "
            "if every hectare were built (a theoretical upper bound). "
            f"Yield {c['yield_kwh_per_kwp'].min():,.0f} – {c['yield_kwh_per_kwp'].max():,.0f} kWh/kWp.",
            "",
            "| Rank | Area (ha) | Score | Mean slope | Substation | In protected landscape | Energy (MWh/yr) |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for r in c.head(10).itertuples():
            lines.append(
                f"| {r.rank} | {r.area_ha:,.1f} | {r.score:.2f} | {r.mean_slope_deg:.1f}° | "
                f"{r.substation_distance_m / 1000:.1f} km | {_pct(r.constraint_share)} | {r.energy_mwh_year:,.0f} |"
            )
        lines.append("")
    lines += ["## Existing solar farms", ""]
    if result.validation is None:
        lines += [
            "OpenStreetMap maps no solar farm of at least 1 ha in this gmina, so there is nothing to check.",
            "",
        ]
    else:
        v = result.validation
        lines += [
            f"{v['farms']} farms ({v['farm_area_ha']:,.0f} ha). {_pct(v['farm_area_in_candidates'])} of their area "
            f"lies in candidates, against {_pct(v['expected_by_chance'])} expected by chance "
            f"(lift {v['lift']:.2f}); {v['farms_mostly_inside']} of {v['farms']} are mostly inside. "
            "See the README for why this is weaker evidence than it looks.",
            "",
        ]
    lines += [
        "## Data",
        "",
        "Boundary: PRG (GUGiK). Terrain: NMT (GUGiK). Protected areas: GDOŚ. Buildings, roads, water, forest, "
        "power grid and solar farms: © OpenStreetMap contributors, ODbL. Climate: NASA POWER.",
        "",
    ]
    return "\n".join(lines)
