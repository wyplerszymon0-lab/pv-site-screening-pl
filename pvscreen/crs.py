"""PL-1992 and PL-2000: the national projected coordinate systems.

All processing uses PL-1992 (EPSG:2180, one transverse Mercator zone for the
whole country, central meridian 19° E, scale 0.9993). Surveyors, cadastre and
local authorities work in PL-2000, four 3°-wide zones with a scale of 0.999923,
which keeps distortion within a zone under a few centimetres per kilometre:

| Zone | Central meridian | Longitudes      | EPSG |
|------|------------------|-----------------|------|
| 5    | 15° E            | 13.5° – 16.5° E | 2176 |
| 6    | 18° E            | 16.5° – 19.5° E | 2177 |
| 7    | 21° E            | 19.5° – 22.5° E | 2178 |
| 8    | 24° E            | 22.5° – 25.5° E | 2179 |

Eastings start with the zone number (e.g. 6 500 000 m on 18° E), so a
coordinate shows which zone it belongs to.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import geopandas as gpd

PL1992 = "EPSG:2180"
PL2000_EPSG = {5: 2176, 6: 2177, 7: 2178, 8: 2179}


def pl2000_zone(lon: float) -> int:
    """Zone number for a longitude. A boundary meridian (16.5°, 19.5°, 22.5°)
    belongs to the zone east of it."""
    zone = math.floor((lon + 1.5) / 3)
    if zone not in PL2000_EPSG:
        raise ValueError(f"longitude {lon}° E is outside the PL-2000 zones (13.5° - 25.5° E)")
    return zone


@dataclass(frozen=True)
class ZoneChoice:
    zone: int
    epsg: int
    straddles: bool  # the area crosses a zone boundary
    note: str


def choose_zone(area: gpd.GeoDataFrame) -> ZoneChoice:
    """One PL-2000 zone for a whole area (a gmina), chosen by its centroid.

    An area crossing a zone boundary still gets a single zone, so that all its
    outputs share one coordinate system; the part across the boundary is then
    slightly outside its own zone, which PL-2000 tolerates near the boundary.
    """
    projected = area.to_crs(PL1992)
    centroid = projected.geometry.union_all().centroid
    lon = gpd.GeoSeries([centroid], crs=PL1992).to_crs(4326).iloc[0].x
    west, _, east, _ = area.to_crs(4326).total_bounds
    zone = pl2000_zone(lon)
    straddles = pl2000_zone(west) != pl2000_zone(east)
    span = f"{west:.3f}° - {east:.3f}° E"
    note = (
        f"crosses a zone boundary ({span}); zone {zone} chosen by the centroid at {lon:.3f}° E"
        if straddles
        else f"inside zone {zone} ({span})"
    )
    return ZoneChoice(zone, PL2000_EPSG[zone], straddles, note)


def to_output_crs(gdf: gpd.GeoDataFrame, system: str, area: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, str]:
    """Reproject an output layer to 'pl1992' or to the PL-2000 zone of area."""
    if system == "pl1992":
        return gdf.to_crs(PL1992), f"{PL1992} (PL-1992)"
    if system == "pl2000":
        choice = choose_zone(area)
        return gdf.to_crs(choice.epsg), f"EPSG:{choice.epsg} (PL-2000 zone {choice.zone}); {choice.note}"
    raise ValueError(f"unknown coordinate system {system!r}: use 'pl1992' or 'pl2000'")
