"""Export one gmina's screening to small GeoJSON files for the web map in site/.

    python -m pvscreen.webmap 3027062

Reads outputs/<teryt>/screening.gpkg (run `python -m pvscreen --teryt <code>` first)
and writes site/data/<teryt>/*.geojson plus meta.json. Browsers want WGS84, so the
layers are simplified in PL-1992 first (by a tolerance in metres) and their
coordinates rounded to about a metre, which keeps the page quick to load.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
from shapely import set_precision

from pvscreen import WORK_CRS, __version__

SITE_DATA = Path("site/data")
SIMPLIFY_M = 3.0  # well under the 5 m terrain cells the screening works on
DECIMALS = 5  # ~1.1 m in latitude

# Exclusion rules grouped as on the QGIS map (qgis/make_project.py).
GROUPS = {
    "building": "Buildings and built-up land",
    "built_up": "Buildings and built-up land",
    "water": "Water",
    "forest": "Forest",
    "protected_landscape_area": "Protected landscape (constraint only)",
    "landscape_park": "Protected landscape (constraint only)",
}

CANDIDATE_FIELDS = {
    "rank": int,
    "area_ha": 1,
    "score": 2,
    "score_slope": 2,
    "score_aspect": 2,
    "score_grid": 2,
    "mean_slope_deg": 1,
    "substation_distance_m": 0,
    "constraint_share": 2,
    "yield_kwh_per_kwp": 0,
    "energy_mwh_year": 0,
}


def exclusion_group(rule: str) -> str:
    if rule.startswith("road:") or rule == "railway":
        return "Roads and railways"
    if rule.startswith("waterway:"):
        return "Water"
    return GROUPS.get(rule, "Protected nature (excluded)")


def _to_web(gdf: gpd.GeoDataFrame, simplify_m: float = SIMPLIFY_M) -> gpd.GeoDataFrame:
    out = gdf.to_crs(WORK_CRS)
    if simplify_m:
        out = out.set_geometry(out.geometry.simplify(simplify_m, preserve_topology=True))
    out = out.to_crs(4326)
    out = out.set_geometry(set_precision(out.geometry.values, 10**-DECIMALS))
    return out[~out.geometry.is_empty & out.geometry.notna()]


def candidates_layer(cands: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    cols = {}
    for name, digits in CANDIDATE_FIELDS.items():
        if name not in cands:
            continue
        cols[name] = cands[name].astype(int) if digits is int else cands[name].round(digits)
    return gpd.GeoDataFrame(cols, geometry=cands.geometry, crs=cands.crs)


def exclusions_layer(excl: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """One (multi)polygon per group: the browser draws 6 features instead of dozens."""
    excl = excl.to_crs(WORK_CRS).assign(group=excl["rule"].map(exclusion_group))
    dissolved = excl.dissolve(by="group", as_index=False)[["group", "geometry"]]
    return dissolved.sort_values("group").reset_index(drop=True)


def grid_layers(grid: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    lines = grid[grid["kind"] == "line"][["voltage", "geometry"]]
    subs = grid[grid["kind"] == "substation"]
    points = gpd.GeoDataFrame(
        {"voltage": subs["voltage"]}, geometry=subs.geometry.representative_point(), crs=grid.crs
    )
    return lines, points


def export(teryt: str, out_root: Path = Path("outputs"), site_data: Path = SITE_DATA) -> dict:
    gpkg = out_root / teryt / "screening.gpkg"
    if not gpkg.exists():
        raise SystemExit(f"{gpkg} not found; run `python -m pvscreen --teryt {teryt}` first")
    read = lambda layer: gpd.read_file(gpkg, layer=layer)  # noqa: E731
    present = {name for name, _ in pyogrio.list_layers(gpkg)}
    boundary = read("boundary")
    layers = {
        "boundary": boundary[["teryt", "name", "geometry"]],
        "candidates": candidates_layer(read("candidates")),
        "exclusions": exclusions_layer(read("exclusions")),
    }
    if "grid" in present:  # GeoPackages written before the map layers were added lack it
        layers["power_lines"], layers["substations"] = grid_layers(read("grid"))
    if "farms" in present:  # only when OSM maps a solar farm in the gmina
        farms = read("farms")
        layers["farms"] = farms[["name", "area_ha", "geometry"]].assign(area_ha=farms["area_ha"].round(1))

    folder = site_data / teryt
    folder.mkdir(parents=True, exist_ok=True)
    sizes = {}
    for name, layer in layers.items():
        path = folder / f"{name}.geojson"
        web = _to_web(layer, 0 if name in ("substations",) else SIMPLIFY_M)
        path.write_text(web.to_json(drop_id=True, separators=(",", ":")), encoding="utf-8")
        sizes[name] = path.stat().st_size

    cands = layers["candidates"]
    meta = {
        "teryt": teryt,
        "name": str(boundary.iloc[0]["name"]),
        "generated": date.today().isoformat(),
        "pvscreen": __version__,
        "layers": sorted(layers),
        "candidates": int(len(cands)),
        "candidate_area_ha": round(float(cands["area_ha"].sum()), 1) if len(cands) else 0.0,
        "gmina_area_ha": round(float(boundary.to_crs(WORK_CRS).geometry.area.sum() / 10_000), 1),
    }
    (folder / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    index = site_data / "index.json"
    known = json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
    known = sorted({*known, teryt})
    index.write_text(json.dumps(known) + "\n", encoding="utf-8")
    return {"meta": meta, "bytes": sizes}


if __name__ == "__main__":
    import sys

    result = export(sys.argv[1] if len(sys.argv) > 1 else "3027062")
    total = sum(result["bytes"].values())
    print(
        pd.Series(result["bytes"]).div(1024).round(0).astype(int).to_string(),
        f"\ntotal {total / 1024:.0f} kB",
    )
