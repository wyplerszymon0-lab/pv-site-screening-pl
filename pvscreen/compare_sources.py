"""Compare OpenStreetMap and BDOT10k as the source of exclusions for one gmina.

    python -m pvscreen.compare_sources 3027062

Prints Markdown tables: exclusion area per feature type, buildings each source lacks (and
how many of those fall outside the other source's building zone, i.e. change the
result), and the candidates and validation each source leads to.
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

from pvscreen import WORK_CRS, osm


def buildings_in(features: gpd.GeoDataFrame, gmina: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    boundary = gmina.to_crs(WORK_CRS).geometry.iloc[0]
    b = features[features["key"] == "building"]
    b = b[b.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    return b[b.representative_point().within(boundary)]


def missing_buildings(a: gpd.GeoDataFrame, b: gpd.GeoDataFrame, zone_m: float) -> dict[str, int]:
    """Buildings of `a` that `b` does not have (no b building contains a's representative
    point), and how many of those also lie outside b's buffered building zone, so
    that b's screening would put a candidate on top of them."""
    pts = gpd.GeoDataFrame(geometry=a.representative_point(), crs=a.crs)
    absent = pts[~pts.index.isin(gpd.sjoin(pts, b[["geometry"]], predicate="within").index)]
    zone = unary_union(list(b.geometry.buffer(zone_m))) if len(b) else None
    outside = int((~absent.within(zone)).sum()) if zone is not None else len(absent)
    return {"buildings": len(a), "absent_from_other": len(absent), "outside_other_zone": outside}


def feature_table(zones: dict[str, gpd.GeoDataFrame]) -> pd.DataFrame:
    """Exclusion area (ha) per feature type (all road classes together, ...) and source.

    Rules do not line up one to one (OSM residential streets are BDOT10k municipal
    roads), so areas are compared per feature, as the union of its rules' zones.
    """
    areas: dict[str, dict[str, float]] = {}
    for name, z in zones.items():
        for feature, group in z.groupby("feature"):
            areas.setdefault(str(feature), {})[name] = unary_union(list(group.geometry)).area / 10_000
        areas.setdefault("all (union)", {})[name] = unary_union(list(z.geometry)).area / 10_000
    order = sorted(k for k in areas if k != "all (union)") + ["all (union)"]
    rows = [{"feature": k, **{name: areas[k].get(name, 0.0) for name in zones}} for k in order]
    return pd.DataFrame(rows)


def _markdown(df: pd.DataFrame, floatfmt: str = ",.1f") -> str:
    cols = list(df.columns)
    lines = [
        "| " + " | ".join(cols) + " |",
        "|" + "|".join(":---" if i == 0 else "---:" for i in range(len(cols))) + "|",
    ]
    for row in df.itertuples(index=False):
        cells = [format(v, floatfmt) if isinstance(v, float) else str(v) for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(teryt: str) -> None:
    from pvscreen import pipeline
    from pvscreen.suitability import exclusion_features

    results = {src: pipeline.run(teryt, source=src) for src in ("osm", "bdot10k")}
    gmina = results["osm"].gmina
    feats = {src: exclusion_features(gmina, src) for src in results}

    print(f"## Exclusions per feature type (ha), gmina {gmina.iloc[0]['name']}\n")
    print(_markdown(feature_table({src: r.osm_zones for src, r in results.items()})))

    zone_m = osm.RULES["building"].buffer_m
    b_osm, b_bdot = buildings_in(feats["osm"], gmina), buildings_in(feats["bdot10k"], gmina)
    rows = [
        {"source": "OSM", **missing_buildings(b_osm, b_bdot, zone_m)},
        {"source": "BDOT10k", **missing_buildings(b_bdot, b_osm, zone_m)},
    ]
    print(f"\n## Buildings (other source's zone: {zone_m:g} m)\n")
    print(_markdown(pd.DataFrame(rows)))

    cand = {src: unary_union(list(r.candidates.geometry)) for src, r in results.items()}
    both = cand["osm"].intersection(cand["bdot10k"]).area / 10_000
    rows = []
    for src, r in results.items():
        v = r.validation or {}
        rows.append(
            {
                "source": src,
                "candidates": len(r.candidates),
                "area_ha": float(r.candidates["area_ha"].sum()),
                "only_this_source_ha": cand[src].area / 10_000 - both,
                "farm_area_in_candidates_pct": 100 * v.get("farm_area_in_candidates", float("nan")),
                "by_chance_pct": 100 * v.get("expected_by_chance", float("nan")),
                "lift": v.get("lift", float("nan")),
            }
        )
    print(f"\n## Candidates and validation (candidate area in both: {both:,.0f} ha)\n")
    print(_markdown(pd.DataFrame(rows)))


if __name__ == "__main__":
    import sys
    import warnings

    warnings.filterwarnings("ignore", message="One or several characters")  # see pvscreen.bdot10k
    main(sys.argv[1] if len(sys.argv) > 1 else "3027062")
