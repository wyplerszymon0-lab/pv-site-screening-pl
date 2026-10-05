"""Check the candidates against solar farms that already exist.

Existing ground-mounted farms from OpenStreetMap (power=plant with
plant:source=solar) are compared with the candidate areas: if the screening
captures what makes a site buildable, built farms should fall inside candidates
more often than chance (the candidates' share of the gmina) would predict.
Farms that fall outside are attributed to the exclusion that covers them.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely.ops import unary_union

from pvscreen import WORK_CRS, osm

MIN_FARM_HA = 1.0  # smaller "plants" in OSM are rooftop or yard installations
DEFAULT_CACHE = Path("data/osm")


def build_farm_query(bounds_wgs84: tuple[float, float, float, float]) -> str:
    west, south, east, north = bounds_wgs84
    bbox = f"{south:.5f},{west:.5f},{north:.5f},{east:.5f}"
    return f'[out:json][timeout:180];\nnwr["power"="plant"]["plant:source"="solar"]({bbox});\nout geom;'


def fetch_farm_elements(
    gmina: gpd.GeoDataFrame,
    cache_dir: Path = DEFAULT_CACHE,
    session: requests.Session | None = None,
    waits: tuple[float, ...] = (30, 90),
) -> list[dict]:
    teryt = gmina.iloc[0]["teryt"]
    path = Path(cache_dir) / f"solar_farms_{teryt}.json"
    if not path.exists():
        bounds = tuple(gmina.to_crs(4326).total_bounds)
        resp = osm._post_with_retry(session or requests.Session(), build_farm_query(bounds), waits)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("remark"):
            raise RuntimeError(f"Overpass: {payload['remark']}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))["elements"]


def farms_in_gmina(elements: list[dict], gmina: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Ground-mounted solar farms (polygons of at least MIN_FARM_HA) clipped to the gmina."""
    rows = []
    for el in elements:
        tags = el.get("tags", {})
        if el["type"] == "way":
            geom = osm._way_geometry(el)
        elif el["type"] == "relation":
            geom = osm._relation_geometry(el)
        else:
            continue  # a node has no area
        if geom is None or geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        rows.append({"osm_id": f"{el['type']}/{el['id']}", "name": tags.get("name"), "geometry": geom})
    if not rows:
        return gpd.GeoDataFrame(
            columns=["osm_id", "name", "area_ha", "geometry"], geometry="geometry", crs=WORK_CRS
        )
    farms = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(WORK_CRS)
    farms = gpd.clip(farms, gmina.to_crs(WORK_CRS))
    farms["area_ha"] = farms.geometry.area / 10_000
    # clip() may reorder rows depending on the geopandas version; keep the input order.
    return farms[farms["area_ha"] >= MIN_FARM_HA].sort_index().reset_index(drop=True)


def evaluate(
    farms: gpd.GeoDataFrame,
    candidates: gpd.GeoDataFrame,
    gmina: gpd.GeoDataFrame,
    reasons: dict[str, object] | None = None,
) -> tuple[gpd.GeoDataFrame, dict[str, float]]:
    """Per-farm overlap with candidates and, for the part outside, the reason.

    reasons maps a label (e.g. 'osm:building') to a geometry; the uncovered part of
    each farm is attributed to the label covering most of it.
    """
    gmina_area = gmina.to_crs(WORK_CRS).geometry.area.sum()
    cand_union = unary_union(list(candidates.geometry)) if not candidates.empty else None
    per_farm = farms.copy()
    inside, scores, main_reason = [], [], []
    for farm in farms.geometry:
        covered = farm.intersection(cand_union) if cand_union is not None else None
        share = covered.area / farm.area if covered is not None else 0.0
        inside.append(share)
        hits = candidates[candidates.intersects(farm)] if cand_union is not None else candidates.iloc[:0]
        if len(hits):
            weights = np.array([g.intersection(farm).area for g in hits.geometry])
            scores.append(float(np.average(hits["score"], weights=weights)) if weights.sum() else np.nan)
        else:
            scores.append(np.nan)
        missing = farm.difference(cand_union) if cand_union is not None else farm
        best, best_area = None, 0.0
        for label, geom in (reasons or {}).items():
            a = missing.intersection(geom).area if geom is not None else 0.0
            if a > best_area:
                best, best_area = label, a
        main_reason.append(best if share < 0.5 else None)
    per_farm["share_in_candidates"] = inside
    per_farm["candidate_score"] = scores
    per_farm["main_reason_outside"] = main_reason

    farm_area = farms.geometry.area.sum()
    in_area = sum(s * a for s, a in zip(inside, farms.geometry.area, strict=True))
    cand_score_all = (
        float(np.average(candidates["score"], weights=candidates.geometry.area))
        if not candidates.empty
        else np.nan
    )
    summary = {
        "farms": len(farms),
        "farm_area_ha": farm_area / 10_000,
        "farm_area_in_candidates": in_area / farm_area if farm_area else np.nan,
        "expected_by_chance": (cand_union.area / gmina_area) if cand_union is not None else 0.0,
        "farms_mostly_inside": int(sum(s >= 0.5 for s in inside)),
        "score_under_farms": float(np.nanmean(scores)) if len(scores) else np.nan,
        "score_of_all_candidate_area": cand_score_all,
    }
    summary["lift"] = summary["farm_area_in_candidates"] / summary["expected_by_chance"]
    return per_farm, summary


def reason_layers(gmina: gpd.GeoDataFrame) -> dict[str, object]:
    """The exclusion layers of the screening, by label, for attributing misses."""
    from pvscreen import protected, suitability, terrain

    feats = osm.elements_to_gdf(osm.fetch_elements(gmina))
    layers = {f"osm:{r.rule}": r.geometry for r in osm.exclusions(feats, gmina).itertuples()}
    areas = protected.protected_areas(gmina)
    for r in areas[areas["kind"] == protected.EXCLUDE].itertuples():
        layers[f"protected:{r.category}"] = r.geometry
    cfg = suitability.load_config()
    dem, transform = terrain.fetch_dem(gmina)
    slope, _ = terrain.slope_aspect(dem, terrain.DEFAULT_RES_M)
    with np.errstate(invalid="ignore"):
        steep = (slope > cfg.max_slope_deg).astype("uint8")
    from rasterio.features import shapes
    from shapely.geometry import shape

    layers["slope"] = unary_union(
        [shape(g) for g, v in shapes(steep, mask=steep == 1, transform=transform) if v]
    )
    return layers


if __name__ == "__main__":
    import sys

    from pvscreen.boundary import fetch_gmina
    from pvscreen.suitability import screen

    gm = fetch_gmina(sys.argv[1] if len(sys.argv) > 1 else "3027062")
    farms = farms_in_gmina(fetch_farm_elements(gm), gm)
    per_farm, summary = evaluate(farms, screen(gm), gm, reason_layers(gm))
    # Sensitivity: farms often bring their own substation, which then makes their
    # surroundings score well. Re-score without substations near any farm.
    farm_union = unary_union(list(farms.geometry))
    _, without_own = evaluate(farms, screen(gm, ignore_substations_near=farm_union), gm)
    summary["score_under_farms_without_their_substations"] = without_own["score_under_farms"]
    summary["score_of_all_candidate_area_without_them"] = without_own["score_of_all_candidate_area"]
    pd.set_option("display.width", 180)
    cols = ["osm_id", "name", "area_ha", "share_in_candidates", "candidate_score", "main_reason_outside"]
    print(per_farm[cols].sort_values("area_ha", ascending=False).round(2).to_string(index=False))
    for k, v in summary.items():
        print(f"{k:45s} {v:.3f}" if isinstance(v, float) else f"{k:45s} {v}")
    out = Path("outputs") / f"validation_{gm.iloc[0]['teryt']}.gpkg"
    per_farm.to_file(out, layer="farms", driver="GPKG")
    print(f"written {out}")
