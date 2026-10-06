import math
from pathlib import Path

import geopandas as gpd
import pytest
from pyproj import Transformer
from shapely.geometry import Point, box

from pvscreen import boundary, crs

FIXTURES = Path(__file__).parent / "fixtures"

# GRS80, the ellipsoid of both PL-1992 and PL-2000
A = 6378137.0
F = 1 / 298.257222101


def meridian_arc(lat_deg: float) -> float:
    """Distance from the equator along a meridian (Helmert's series, sub-mm accurate)."""
    n = F / (2 - F)
    p = math.radians(lat_deg)
    return (A / (1 + n)) * (
        (1 + n**2 / 4 + n**4 / 64) * p
        - 1.5 * (n - n**3 / 8) * math.sin(2 * p)
        + 15 / 16 * (n**2 - n**4 / 4) * math.sin(4 * p)
        - 35 / 48 * n**3 * math.sin(6 * p)
        + 315 / 512 * n**4 * math.sin(8 * p)
    )


@pytest.mark.parametrize(
    "lon, zone", [(14.2, 5), (16.4999, 5), (16.5, 6), (18.64, 6), (19.5, 7), (21.0, 7), (22.5, 8), (24.1, 8)]
)
def test_zone_by_longitude(lon, zone):
    assert crs.pl2000_zone(lon) == zone


@pytest.mark.parametrize("lon", [13.4, 25.6, 0.0])
def test_longitudes_outside_poland_are_rejected(lon):
    with pytest.raises(ValueError, match="outside"):
        crs.pl2000_zone(lon)


@pytest.mark.parametrize("lat", [49.0, 52.0, 54.8])
@pytest.mark.parametrize("zone, lon0", [(5, 15), (6, 18), (7, 21), (8, 24)])
def test_pl2000_control_points_on_each_central_meridian(zone, lon0, lat):
    # On the central meridian: easting = zone x 1 000 000 + 500 000, northing = 0.999923 x arc.
    e, n = Transformer.from_crs(4326, crs.PL2000_EPSG[zone], always_xy=True).transform(lon0, lat)
    assert e == pytest.approx(zone * 1_000_000 + 500_000, abs=1e-6)
    assert n == pytest.approx(0.999923 * meridian_arc(lat), abs=0.001)


@pytest.mark.parametrize("lat", [49.0, 52.0, 54.8])
def test_pl1992_control_points_on_its_central_meridian(lat):
    e, n = Transformer.from_crs(4326, 2180, always_xy=True).transform(19.0, lat)
    assert e == pytest.approx(500_000, abs=1e-6)
    assert n == pytest.approx(0.9993 * meridian_arc(lat) - 5_300_000, abs=0.001)


def test_round_trip_wgs84_pl1992_pl2000_loses_less_than_a_millimetre():
    lon, lat = 18.6345, 52.0012  # in Przykona
    x, y = Transformer.from_crs(4326, 2180, always_xy=True).transform(lon, lat)
    e, n = Transformer.from_crs(2180, 2177, always_xy=True).transform(x, y)
    lon2, lat2 = Transformer.from_crs(2177, 4326, always_xy=True).transform(e, n)
    metres_per_deg = 111_320
    assert abs(lat2 - lat) * metres_per_deg < 0.001
    assert abs(lon2 - lon) * metres_per_deg * math.cos(math.radians(lat)) < 0.001
    assert str(int(e)).startswith("6")  # the easting names the zone


def test_przykona_is_inside_zone_6():
    choice = crs.choose_zone(boundary.read_gmina(FIXTURES / "prg_gmina_3027062.gml"))
    assert (choice.zone, choice.epsg, choice.straddles) == (6, 2177, False)


def test_an_area_across_a_zone_boundary_gets_one_zone_and_a_note():
    # 19.40° - 19.70° E: crosses 19.5°, most of it east of the boundary.
    to_1992 = Transformer.from_crs(4326, 2180, always_xy=True)
    (x0, y0), (x1, y1) = to_1992.transform(19.40, 51.70), to_1992.transform(19.70, 51.75)
    area = gpd.GeoDataFrame(geometry=[box(x0, y0, x1, y1)], crs="EPSG:2180")
    choice = crs.choose_zone(area)
    assert (choice.zone, choice.epsg, choice.straddles) == (7, 2178, True)
    assert "crosses a zone boundary" in choice.note


def test_output_reprojection():
    gmina = boundary.read_gmina(FIXTURES / "prg_gmina_3027062.gml")
    layer = gpd.GeoDataFrame({"v": [1]}, geometry=[Point(475000, 459000)], crs="EPSG:2180")
    same, note = crs.to_output_crs(layer, "pl1992", gmina)
    assert same.crs.to_epsg() == 2180 and "PL-1992" in note
    zoned, note = crs.to_output_crs(layer, "pl2000", gmina)
    assert zoned.crs.to_epsg() == 2177 and "zone 6" in note
    assert 6_500_000 < zoned.geometry.iloc[0].x < 6_600_000
    with pytest.raises(ValueError, match="pl1992"):
        crs.to_output_crs(layer, "wgs84", gmina)
