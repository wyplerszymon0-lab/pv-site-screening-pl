import shutil
from pathlib import Path

import geopandas as gpd
import pytest
from pyproj import Transformer
from shapely.geometry import box

from pvscreen import boundary, protected

FIXTURES = Path(__file__).parent / "fixtures"
PRZYKONA_BOUNDS = (468565, 452823, 482254, 464951)


class NoNetwork:
    def get(self, *args, **kwargs):
        raise AssertionError("layers should come from the cache")


@pytest.fixture
def gmina():
    return boundary.read_gmina(FIXTURES / "prg_gmina_3027062.gml")


@pytest.fixture
def cache(tmp_path):
    for f in (FIXTURES / "gdos").glob("*.gml"):  # GDOŚ responses recorded 2026-10-04
        shutil.copy(f, tmp_path / f.name)
    return tmp_path


def test_bbox_is_sent_easting_first_with_the_plain_epsg_code():
    params = protected.layer_request_params("Rezerwaty", (468565.3, 452823.9, 482254.1, 464951.0))
    assert params["TYPENAMES"] == "GDOS:Rezerwaty"
    assert params["BBOX"] == "468565,452824,482254,464951,EPSG:2180"


def test_przykona_protected_areas(gmina, cache):
    areas = protected.protected_areas(gmina, cache_dir=cache, session=NoNetwork())
    by_cat = {row.category: row for row in areas.itertuples()}

    assert set(by_cat) == {"natura2000_birds", "protected_landscape_area"}
    birds = by_cat["natura2000_birds"]
    assert (birds.kind, birds.name) == ("exclude", "Dolina Środkowej Warty")
    assert birds.area_ha == pytest.approx(60.07, abs=0.05)
    landscape = by_cat["protected_landscape_area"]
    assert (landscape.kind, landscape.name) == ("constraint", "Uniejowski")
    assert landscape.area_ha == pytest.approx(2745.2, abs=0.5)
    assert all(areas["reason"].str.startswith("Art."))


def test_slivers_along_the_boundary_are_dropped(gmina, cache):
    # Nadwarciański touches Przykona only in a 0.02 ha strip where the
    # GDOŚ and PRG boundaries disagree.
    areas = protected.protected_areas(gmina, cache_dir=cache, session=NoNetwork())
    assert "Nadwarciański" not in set(areas["name"])
    assert (areas["area_ha"] >= protected.MIN_AREA_HA).all()


def test_clipped_areas_stay_inside_the_gmina_and_where_they_belong(gmina, cache):
    areas = protected.protected_areas(gmina, cache_dir=cache, session=NoNetwork())
    assert areas.within(gmina.geometry.iloc[0].buffer(1)).all()
    # The Warta valley bird area is in the gmina's north-east, near 52.04° N, 18.7° E.
    point = areas[areas["category"] == "natura2000_birds"].geometry.iloc[0].representative_point()
    lon, lat = Transformer.from_crs(2180, 4326, always_xy=True).transform(point.x, point.y)
    assert 51.95 < lat < 52.1 and 18.55 < lon < 18.8


def test_summary_counts_overlapping_categories_once_in_the_union(gmina):
    a = box(470000, 455000, 471000, 456000)  # 100 ha
    b = box(470500, 455000, 471500, 456000)  # 100 ha, half of it overlapping a
    areas = gpd.GeoDataFrame(
        {
            "category": ["x", "y"],
            "kind": ["exclude", "exclude"],
            "name": ["A", "B"],
            "reason": ["", ""],
            "area_ha": [100.0, 100.0],
        },
        geometry=[a, b],
        crs="EPSG:2180",
    )
    summary = protected.summarize(areas, gmina).set_index("group")
    assert summary.loc["x (exclude)", "area_ha"] == 100
    assert summary.loc["all exclude", "area_ha"] == pytest.approx(150)
    assert summary.loc["all constraint", "area_ha"] == 0


def test_empty_response_gives_an_empty_layer(tmp_path):
    path = tmp_path / "empty.gml"
    shutil.copy(FIXTURES / "gdos" / "Rezerwaty_468565_452823_482254_464951.gml", path)
    assert protected.read_layer(path, PRZYKONA_BOUNDS).empty


def test_features_outside_the_requested_box_are_rejected(tmp_path):
    # The same response read against a box 100 km away: what a swapped axis order looks like.
    path = tmp_path / "birds.gml"
    shutil.copy(FIXTURES / "gdos" / "ObszarySpecjalnejOchrony_468565_452823_482254_464951.gml", path)
    with pytest.raises(ValueError, match="axis order"):
        protected.read_layer(path, (568565, 552823, 582254, 564951))


def test_truncated_response_is_rejected(tmp_path):
    path = tmp_path / "partial.gml"
    path.write_text(
        '<?xml version="1.0"?><wfs:FeatureCollection numberMatched="5" numberReturned="2">'
        "<wfs:member></wfs:member></wfs:FeatureCollection>",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="2 of 5"):
        protected.read_layer(path, PRZYKONA_BOUNDS)
