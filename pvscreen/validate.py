"""Check the candidates against solar farms that already exist.

Existing ground-mounted farms from OpenStreetMap (power=plant with
plant:source=solar) are compared with the candidate areas: if the screening
captures what makes a site buildable, built farms should fall inside candidates
more often than chance (the candidates' share of the gmina) would predict.
Farms that fall outside are attributed to the exclusion that covers them.

With --as-of the check is out of sample in time: the gmina is screened on
OpenStreetMap as it was on that date, and only the farm land added between then
and --until is tested (issue #35).
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely.ops import unary_union

from pvscreen import WORK_CRS, osm, osm_extract

MIN_FARM_HA = 1.0  # smaller "plants" in OSM are rooftop or yard installations
UNTIL = "2026-10-01"  # the "after" snapshot of --as-of: a dated extract, so results can be repeated
DEFAULT_CACHE = Path("data/osm")


def build_farm_query(bounds_wgs84: tuple[float, float, float, float]) -> str:
    west, south, east, north = bounds_wgs84
    bbox = f"{south:.5f},{west:.5f},{north:.5f},{east:.5f}"
    return f'[out:json][timeout:180];\nnwr["power"="plant"]["plant:source"="solar"]({bbox});\nout geom;'


def _is_solar_plant(tags: dict) -> bool:
    return tags.get("power") == "plant" and tags.get("plant:source") == "solar"


def fetch_farm_elements(
    gmina: gpd.GeoDataFrame,
    cache_dir: Path = DEFAULT_CACHE,
    session: requests.Session | None = None,
    waits: tuple[float, ...] = (30, 90, 180),
    as_of: str | None = None,
) -> list[dict]:
    teryt = gmina.iloc[0]["teryt"]
    path = Path(cache_dir) / osm.cache_name(f"solar_farms_{teryt}", as_of)
    if not path.exists():
        bounds = tuple(gmina.to_crs(4326).total_bounds)
        if as_of is not None:
            payload = {"elements": osm_extract.elements(bounds, as_of, _is_solar_plant)}
        else:
            resp = osm._post_with_retry(session or requests.Session(), build_farm_query(bounds), waits)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("remark"):
                raise RuntimeError(f"Overpass: {payload['remark']}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))["elements"]


def farms_in_gmina(elements: list[dict], gmina: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Ground-mounted solar farms (polygons of at least MIN_FARM_HA) clipped to the gmina.

    OpenStreetMap often maps one farm twice: a power=plant relation for the whole
    site and power=plant ways for its sections. Overlapping polygons are merged,
    so each piece of land counts once; osm_id lists every element merged into it.
    """
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
    empty = gpd.GeoDataFrame(
        columns=["osm_id", "name", "area_ha", "geometry"], geometry="geometry", crs=WORK_CRS
    )
    if not rows:
        return empty
    raw = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326").to_crs(WORK_CRS)
    raw = gpd.clip(raw, gmina.to_crs(WORK_CRS)).sort_index()  # clip() order varies between versions
    if raw.empty:  # farms in the query's bounding box, but all of them outside the gmina
        return empty
    merged = []
    for part in getattr(unary_union(list(raw.geometry)), "geoms", [unary_union(list(raw.geometry))]):
        members = raw[raw.intersects(part) & (raw.intersection(part).area > 0)]
        biggest = members.loc[members.geometry.area.idxmax()]
        merged.append(
            {
                "osm_id": ";".join(members["osm_id"]),
                "name": biggest["name"],
                "geometry": part,
                "first": members.index.min(),
            }
        )
    # unary_union's part order depends on the GEOS version; keep the order of the input.
    farms = (
        gpd.GeoDataFrame(merged, geometry="geometry", crs=WORK_CRS).sort_values("first").drop(columns="first")
    )
    farms["area_ha"] = farms.geometry.area / 10_000
    return farms[farms["area_ha"] >= MIN_FARM_HA].reset_index(drop=True)


def new_farms(now: gpd.GeoDataFrame, then: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Farm land mapped now that was not yet farm land in the older snapshot.

    Each of today's farms loses the part already mapped as a farm then; what is
    left is kept if it is still at least MIN_FARM_HA. A farm extended since the
    snapshot thus counts with its extension only.
    """
    if then.empty or now.empty:
        return now.copy()
    old = unary_union(list(then.geometry))
    out = now.copy()
    out["geometry"] = [g.difference(old) for g in now.geometry]
    out = gpd.GeoDataFrame(out, geometry="geometry", crs=now.crs)
    out["area_ha"] = out.geometry.area / 10_000
    return out[out["area_ha"] >= MIN_FARM_HA].reset_index(drop=True)


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


def reason_layers(gmina: gpd.GeoDataFrame, as_of: str | None = None) -> dict[str, object]:
    """The exclusion layers of the screening, by label, for attributing misses."""
    from pvscreen import protected, suitability, terrain

    feats = osm.elements_to_gdf(osm.fetch_elements(gmina, as_of=as_of))
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


def pooled_table(rows: list[dict]) -> pd.DataFrame:
    """One row per gmina plus a pooled row.

    Each input row: teryt, name, gmina_ha, candidate_ha, farms, farm_ha, farm_ha_in.
    The pooled chance baseline weights each gmina's candidate share by its farm
    area, because that is where the pooled farm area could have fallen.
    """
    table = pd.DataFrame(rows)
    table["in_candidates"] = table["farm_ha_in"] / table["farm_ha"]
    table["by_chance"] = table["candidate_ha"] / table["gmina_ha"]
    farm_ha = table["farm_ha"].sum()
    pooled = {
        "teryt": "",
        "name": "all",
        "gmina_ha": table["gmina_ha"].sum(),
        "candidate_ha": table["candidate_ha"].sum(),
        "farms": int(table["farms"].sum()),
        "farm_ha": farm_ha,
        "farm_ha_in": table["farm_ha_in"].sum(),
        "in_candidates": table["farm_ha_in"].sum() / farm_ha if farm_ha else float("nan"),
        "by_chance": (table["farm_ha"] * table["by_chance"]).sum() / farm_ha if farm_ha else float("nan"),
    }
    table = pd.concat([table, pd.DataFrame([pooled])], ignore_index=True)
    table["lift"] = table["in_candidates"] / table["by_chance"]
    return table


def score_rank(farms: gpd.GeoDataFrame, candidates: gpd.GeoDataFrame) -> pd.DataFrame:
    """For each candidate holding farm land: how much, and how it ranks in its gmina.

    below is the share of the gmina's candidate land scoring lower, ties counted as
    half. A candidate has one score, so it is the unit here, not the hectare.
    """
    area = candidates.geometry.area.to_numpy()
    scores = candidates["score"].to_numpy()
    land = unary_union(list(farms.geometry))
    farm_ha = np.array([g.intersection(land).area / 10_000 for g in candidates.geometry])
    below = [(area[scores < s].sum() + area[scores == s].sum() / 2) / area.sum() for s in scores]
    ranks = pd.DataFrame({"farm_ha": farm_ha, "below": below})
    return ranks[ranks["farm_ha"] > 0].reset_index(drop=True)


def pooled_rank(ranks: pd.DataFrame, runs: int = 10_000, seed: int = 1) -> dict[str, float]:
    """AUC: the chance that a hectare of farm land outscores a hectare of candidate land
    in its gmina, 0.5 for no skill. The 95 % interval resamples candidates, because
    the hectares inside one candidate share a score and are not separate evidence."""
    if ranks.empty:
        return {"candidates": 0, "auc": np.nan, "low": np.nan, "high": np.nan}
    ha, below = ranks["farm_ha"].to_numpy(), ranks["below"].to_numpy()
    picks = np.random.default_rng(seed).integers(len(ranks), size=(runs, len(ranks)))
    resampled = (ha[picks] * below[picks]).sum(axis=1) / ha[picks].sum(axis=1)
    low, high = np.percentile(resampled, [2.5, 97.5])
    return {
        "candidates": len(ranks),
        "auc": float((ha * below).sum() / ha.sum()),
        "low": float(low),
        "high": float(high),
    }


def rank_table(names: list[str], ranks: list[pd.DataFrame]) -> pd.DataFrame:
    """One row per gmina plus the pooled row."""
    rows = [{"name": n, **pooled_rank(r)} for n, r in zip(names, ranks, strict=True)]
    rows.append({"name": "all", **pooled_rank(pd.concat(ranks, ignore_index=True))})
    return pd.DataFrame(rows)


def _summary_row(teryt: str) -> dict:
    from pvscreen import pipeline

    r = pipeline.run(teryt)
    gmina_ha = r.gmina.to_crs(WORK_CRS).geometry.area.sum() / 10_000
    v = r.validation or {"farms": 0, "farm_area_ha": 0.0, "farm_area_in_candidates": float("nan")}
    return {
        "teryt": teryt,
        "name": r.gmina.iloc[0]["name"],
        "gmina_ha": gmina_ha,
        "candidate_ha": r.candidates["area_ha"].sum(),
        "farms": v["farms"],
        "farm_ha": v["farm_area_ha"],
        "farm_ha_in": v["farm_area_ha"] * v["farm_area_in_candidates"] if v["farms"] else 0.0,
    }


def _row(teryt: str, name: str, gmina_ha: float, candidates: gpd.GeoDataFrame, summary: dict) -> dict:
    return {
        "teryt": teryt,
        "name": name,
        "gmina_ha": gmina_ha,
        "candidate_ha": candidates.geometry.area.sum() / 10_000,
        "farms": summary["farms"],
        "farm_ha": summary["farm_area_ha"],
        "farm_ha_in": (
            summary["farm_area_ha"] * summary["farm_area_in_candidates"] if summary["farms"] else 0.0
        ),
    }


def before_and_after(
    teryt: str, as_of: str, until: str = UNTIL
) -> tuple[gpd.GeoDataFrame, dict, dict, pd.DataFrame, pd.DataFrame]:
    """The farm land added between as_of and until, checked against candidates screened
    on the OpenStreetMap of each of the two dates.

    Returns the per-farm table (against the old screening, with the old exclusion
    that covers each miss), one pooled_table row for each screening, and one
    score_rank table for each screening.
    """
    from pvscreen.boundary import fetch_gmina
    from pvscreen.suitability import load_inputs, screen

    gm = fetch_gmina(teryt)
    farms = new_farms(
        farms_in_gmina(fetch_farm_elements(gm, as_of=until), gm),
        farms_in_gmina(fetch_farm_elements(gm, as_of=as_of), gm),
    )
    then = screen(gm, inputs=load_inputs(gm, as_of=as_of))
    now = screen(gm, inputs=load_inputs(gm, as_of=until))
    name, gmina_ha = gm.iloc[0]["name"], gm.to_crs(WORK_CRS).geometry.area.sum() / 10_000
    if farms.empty:
        empty = {"farms": 0, "farm_area_ha": 0.0, "farm_area_in_candidates": float("nan")}
        no_ranks = pd.DataFrame({"farm_ha": [], "below": []})
        row_then, row_now = _row(teryt, name, gmina_ha, then, empty), _row(teryt, name, gmina_ha, now, empty)
        return farms, row_then, row_now, no_ranks, no_ranks
    per_farm, s_then = evaluate(farms, then, gm, reason_layers(gm, as_of))
    _, s_now = evaluate(farms, now, gm)
    row_then, row_now = _row(teryt, name, gmina_ha, then, s_then), _row(teryt, name, gmina_ha, now, s_now)
    return per_farm, row_then, row_now, score_rank(farms, then), score_rank(farms, now)


if __name__ == "__main__":
    import argparse

    from pvscreen.boundary import fetch_gmina
    from pvscreen.suitability import screen

    parser = argparse.ArgumentParser(description="Check the candidates against existing solar farms.")
    parser.add_argument("teryt", nargs="*", default=["3027062"], help="7-digit TERYT codes of gminas")
    parser.add_argument(
        "--as-of",
        type=osm.check_as_of,
        help="YYYY-MM-DD: screen on OpenStreetMap as of this date, test only farms added since",
    )
    parser.add_argument(
        "--until", type=osm.check_as_of, default=UNTIL, help="YYYY-MM-DD: the later snapshot for --as-of"
    )
    args = parser.parse_args()
    pd.set_option("display.width", 180)

    if args.as_of:
        runs = [before_and_after(t, args.as_of, args.until) for t in args.teryt]
        cols = ["osm_id", "name", "area_ha", "share_in_candidates", "main_reason_outside"]
        for t, (per_farm, *_) in zip(args.teryt, runs, strict=True):
            if len(per_farm):
                print(f"{t}: farm land added since {args.as_of}, against the old screening")
                print(per_farm[cols].sort_values("area_ha", ascending=False).round(2).to_string(index=False))
        for day, rows, ranks in (
            (args.as_of, [r[1] for r in runs], [r[3] for r in runs]),
            (args.until, [r[2] for r in runs], [r[4] for r in runs]),
        ):
            print(f"\nscreened on OSM as of {day}")
            print(pooled_table(rows).round(3).to_string(index=False))
            print("does the score rank farm land above other candidate land? (0.5 = no)")
            print(rank_table([r["name"] for r in rows], ranks).round(3).to_string(index=False))
        raise SystemExit(0)

    if len(args.teryt) > 1:  # several gminas: one pooled table
        print(pooled_table([_summary_row(t) for t in args.teryt]).round(3).to_string(index=False))
        raise SystemExit(0)

    gm = fetch_gmina(args.teryt[0])
    farms = farms_in_gmina(fetch_farm_elements(gm), gm)
    per_farm, summary = evaluate(farms, screen(gm), gm, reason_layers(gm))
    # Sensitivity: farms often bring their own substation, which then makes their
    # surroundings score well. Re-score without substations near any farm.
    farm_union = unary_union(list(farms.geometry))
    _, without_own = evaluate(farms, screen(gm, ignore_substations_near=farm_union), gm)
    summary["score_under_farms_without_their_substations"] = without_own["score_under_farms"]
    summary["score_of_all_candidate_area_without_them"] = without_own["score_of_all_candidate_area"]
    cols = ["osm_id", "name", "area_ha", "share_in_candidates", "candidate_score", "main_reason_outside"]
    print(per_farm[cols].sort_values("area_ha", ascending=False).round(2).to_string(index=False))
    for k, v in summary.items():
        print(f"{k:45s} {v:.3f}" if isinstance(v, float) else f"{k:45s} {v}")
    out = Path("outputs") / f"validation_{gm.iloc[0]['teryt']}.gpkg"
    per_farm.to_file(out, layer="farms", driver="GPKG")
    print(f"written {out}")
