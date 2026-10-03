"""Municipality (gmina) boundaries from the national register of boundaries (PRG).

GUGiK publishes PRG as a WFS. Gminas are layer ``ms:A03_Granice_gmin``; each
feature carries its 7-digit TERYT code (``JPT_KOD_JE``), name (``JPT_NAZWA_``)
and official area in hectares (``JPT_POWIER``). Geometries come in EPSG:2180.
"""

from __future__ import annotations

import re
from pathlib import Path

import geopandas as gpd
import requests

from pvscreen import WORK_CRS

PRG_WFS_URL = "https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries"
GMINA_LAYER = "ms:A03_Granice_gmin"
DEFAULT_CACHE = Path("data/prg")
USER_AGENT = "pv-site-screening-pl (https://github.com/wyplerszymon0-lab/pv-site-screening-pl)"

# TERYT TERC for a gmina: 2 digits voivodeship, 2 powiat, 2 gmina, 1 type
# (1 urban, 2 rural, 3 urban-rural). Types 4 and 5 are the town and rural parts
# of an urban-rural gmina and are not separate features in this layer.
_TERYT = re.compile(r"\d{6}[123]")


def validate_teryt(code: str) -> str:
    code = str(code).strip()
    if not _TERYT.fullmatch(code):
        raise ValueError(
            f"{code!r} is not a gmina TERYT code: expected 7 digits ending in 1, 2 or 3, e.g. 3027062"
        )
    return code


def gmina_request_params(teryt: str) -> dict[str, str]:
    """WFS 2.0 GetFeature parameters selecting one gmina by TERYT code."""
    teryt = validate_teryt(teryt)
    fes = (
        '<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0"><fes:PropertyIsEqualTo>'
        f"<fes:ValueReference>JPT_KOD_JE</fes:ValueReference><fes:Literal>{teryt}</fes:Literal>"
        "</fes:PropertyIsEqualTo></fes:Filter>"
    )
    return {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": GMINA_LAYER,
        "FILTER": fes,
    }


def _download(teryt: str, path: Path, session: requests.Session | None) -> None:
    http = session or requests.Session()
    resp = http.get(
        PRG_WFS_URL, params=gmina_request_params(teryt), headers={"User-Agent": USER_AGENT}, timeout=120
    )
    resp.raise_for_status()
    if b"<wfs:member>" not in resp.content:
        raise LookupError(f"PRG has no gmina with TERYT code {teryt}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)


def read_gmina(path: Path) -> gpd.GeoDataFrame:
    """Parse a PRG WFS response into one row: teryt, name, official_area_ha, geometry."""
    raw = gpd.read_file(path)
    if len(raw) != 1:
        raise ValueError(f"expected exactly one gmina in {path}, found {len(raw)}")
    if raw.crs is None:
        raise ValueError(f"{path} has no coordinate reference system")
    gdf = gpd.GeoDataFrame(
        {
            "teryt": raw["JPT_KOD_JE"].astype(str),
            "name": raw["JPT_NAZWA_"].astype(str),
            "official_area_ha": raw["JPT_POWIER"].astype(float),
        },
        geometry=raw.geometry,
        crs=raw.crs,
    )
    return gdf.to_crs(WORK_CRS)


def fetch_gmina(
    teryt: str, cache_dir: Path = DEFAULT_CACHE, session: requests.Session | None = None
) -> gpd.GeoDataFrame:
    """Boundary of one gmina in EPSG:2180, downloaded once and then read from the cache."""
    teryt = validate_teryt(teryt)
    path = Path(cache_dir) / f"gmina_{teryt}.gml"
    if not path.exists():
        _download(teryt, path, session)
    return read_gmina(path)


def area_difference(gmina: gpd.GeoDataFrame) -> float:
    """Relative difference between the polygon's area and the official area."""
    row = gmina.iloc[0]
    measured_ha = row.geometry.area / 10_000
    return abs(measured_ha - row.official_area_ha) / row.official_area_ha
