"""Protected nature areas from the GDOŚ (General Directorate for Environmental Protection) WFS.

Each layer is either a hard exclusion or a constraint to report. The legal basis is
the Nature Conservation Act (ustawa z dnia 16 kwietnia 2004 r. o ochronie przyrody);
treating Natura 2000 sites as exclusions is a screening choice, explained below.

Responses are requested as GML, not GeoJSON: the service writes GeoJSON coordinates
northing first (EPSG:2180's official axis order), whereas GeoJSON must be x, y. GDAL
reads the GML's urn:ogc:def:crs:EPSG::2180 axis order correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import box

from pvscreen import WORK_CRS
from pvscreen.boundary import USER_AGENT

GDOS_WFS_URL = "https://sdi.gdos.gov.pl/wfs"
DEFAULT_CACHE = Path("data/gdos")

# Boundaries from GDOŚ and PRG do not match exactly; pieces smaller than this
# along the gmina edge are artefacts of that, not protected land in the gmina.
MIN_AREA_HA = 0.1

EXCLUDE = "exclude"
CONSTRAINT = "constraint"


@dataclass(frozen=True)
class Layer:
    name: str  # WFS type name without the GDOS: prefix
    category: str
    kind: str  # EXCLUDE or CONSTRAINT
    reason: str


LAYERS = (
    Layer("ParkiNarodowe", "national_park", EXCLUDE, "Art. 15(1)(1): building is banned in national parks."),
    Layer("Rezerwaty", "nature_reserve", EXCLUDE, "Art. 15(1)(1): building is banned in nature reserves."),
    Layer(
        "SpecjalneObszaryOchrony",
        "natura2000_habitats",
        EXCLUDE,
        "Art. 33: projects that may significantly harm a Natura 2000 site are banned. Whether a PV farm "
        "would needs an appropriate assessment, which a screening cannot predict, so the site is excluded.",
    ),
    Layer(
        "ObszarySpecjalnejOchrony",
        "natura2000_birds",
        EXCLUDE,
        "Art. 33, as for habitat sites; open farmland in bird areas is often feeding habitat.",
    ),
    Layer(
        "UzytkiEkologiczne", "ecological_site", EXCLUDE, "Art. 45: transforming an ecological site is banned."
    ),
    Layer(
        "ZespolyPrzyrodniczoKrajobrazowe",
        "nature_landscape_complex",
        EXCLUDE,
        "Art. 45: transforming a nature and landscape complex is banned.",
    ),
    Layer(
        "ParkiKrajobrazowe",
        "landscape_park",
        CONSTRAINT,
        "Art. 17: bans are set for each park (often incl. projects that may significantly affect the "
        "environment); check the park's regulation.",
    ),
    Layer(
        "ObszaryChronionegoKrajobrazu",
        "protected_landscape_area",
        CONSTRAINT,
        "Art. 24: bans are set by the voivodeship assembly; PV is often allowed with conditions.",
    ),
)

COLUMNS = ["category", "kind", "name", "reason", "area_ha", "geometry"]


def layer_request_params(layer: str, bounds: tuple[float, float, float, float]) -> dict[str, str]:
    xmin, ymin, xmax, ymax = bounds
    return {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": f"GDOS:{layer}",
        # With the plain EPSG:2180 code the server reads the box easting first
        # (checked against a reserve whose location is known); the urn form
        # would expect northing first.
        "BBOX": f"{xmin:.0f},{ymin:.0f},{xmax:.0f},{ymax:.0f},EPSG:2180",
    }


def _empty() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(columns=COLUMNS, geometry="geometry", crs=WORK_CRS)


def read_layer(path: Path, bounds: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    """Parse a cached GDOŚ GML response; check the features really fall inside bounds."""
    content = path.read_bytes()
    if b"<wfs:member>" not in content:
        return gpd.GeoDataFrame(geometry=[], crs=WORK_CRS)
    head = content[:600].decode("utf-8", "replace")
    if 'numberMatched="' in head:
        matched = head.split('numberMatched="')[1].split('"')[0]
        returned = head.split('numberReturned="')[1].split('"')[0]
        if matched != returned:
            raise ValueError(f"{path.name}: server returned {returned} of {matched} features")
    gdf = gpd.read_file(path).to_crs(WORK_CRS)
    if not gdf.intersects(box(*bounds)).any():
        raise ValueError(f"{path.name}: no feature intersects the requested box; axis order changed?")
    return gdf


def fetch_layer(
    layer: str, bounds: tuple[float, float, float, float], cache_dir: Path, session: requests.Session
) -> gpd.GeoDataFrame:
    path = Path(cache_dir) / f"{layer}_{bounds[0]:.0f}_{bounds[1]:.0f}_{bounds[2]:.0f}_{bounds[3]:.0f}.gml"
    if not path.exists():
        resp = session.get(
            GDOS_WFS_URL,
            params=layer_request_params(layer, bounds),
            headers={"User-Agent": USER_AGENT},
            timeout=180,
        )
        resp.raise_for_status()
        if b"FeatureCollection" not in resp.content[:2000]:
            raise RuntimeError(f"GDOŚ WFS did not return features for {layer}: {resp.content[:200]!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    return read_layer(path, bounds)


def protected_areas(
    gmina: gpd.GeoDataFrame,
    cache_dir: Path = DEFAULT_CACHE,
    session: requests.Session | None = None,
    layers: tuple[Layer, ...] = LAYERS,
) -> gpd.GeoDataFrame:
    """Protected areas clipped to the gmina, one row per area, with kind and legal reason."""
    http = session or requests.Session()
    boundary = gmina.to_crs(WORK_CRS)
    bounds = tuple(boundary.total_bounds)
    parts = []
    for layer in layers:
        raw = fetch_layer(layer.name, bounds, cache_dir, http)
        if raw.empty:
            continue
        clipped = gpd.clip(raw, boundary).sort_index()  # clip() order varies between versions
        clipped = clipped[clipped.geometry.area >= MIN_AREA_HA * 10_000]
        if clipped.empty:
            continue
        parts.append(
            gpd.GeoDataFrame(
                {
                    "category": layer.category,
                    "kind": layer.kind,
                    "name": clipped["nazwa"].astype(str) if "nazwa" in clipped else "",
                    "reason": layer.reason,
                    "area_ha": clipped.geometry.area / 10_000,
                },
                geometry=clipped.geometry.values,
                crs=WORK_CRS,
            )
        )
    if not parts:
        return _empty()
    return gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs=WORK_CRS)


def summarize(areas: gpd.GeoDataFrame, gmina: gpd.GeoDataFrame) -> pd.DataFrame:
    """Area per category, plus the union per kind (categories can overlap)."""
    total_ha = gmina.to_crs(WORK_CRS).geometry.area.sum() / 10_000
    rows = [
        {
            "group": f"{cat} ({kind})",
            "names": ", ".join(sorted(set(g["name"]))),
            "area_ha": g["area_ha"].sum(),
        }
        for (cat, kind), g in areas.groupby(["category", "kind"], sort=False)
    ]
    for kind in (EXCLUDE, CONSTRAINT):
        sub = areas[areas["kind"] == kind]
        union_ha = sub.geometry.union_all().area / 10_000 if not sub.empty else 0.0
        rows.append({"group": f"all {kind}", "names": "", "area_ha": union_ha})
    out = pd.DataFrame(rows)
    out["share_of_gmina"] = out["area_ha"] / total_ha
    return out


if __name__ == "__main__":
    import sys

    from pvscreen.boundary import fetch_gmina

    gm = fetch_gmina(sys.argv[1] if len(sys.argv) > 1 else "3027062")
    found = protected_areas(gm)
    out = Path("outputs") / f"protected_{gm.iloc[0]['teryt']}.gpkg"
    out.parent.mkdir(parents=True, exist_ok=True)
    found.to_file(out, layer="protected_areas", driver="GPKG")
    print(summarize(found, gm).to_string(index=False))
    print(f"written {out}")
