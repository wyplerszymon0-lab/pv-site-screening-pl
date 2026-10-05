import geopandas as gpd
import pandas as pd
import pytest
from pyproj import Transformer
from shapely.geometry import LineString, Point, box

from pvscreen import suitability, validate

TO_WGS84 = Transformer.from_crs(2180, 4326, always_xy=True)
GMINA = gpd.GeoDataFrame({"teryt": ["0000002"]}, geometry=[box(0, 0, 1000, 1000)], crs="EPSG:2180")


def osm_way(id_, tags, rect):
    """An OSM way (lon/lat geometry) for a rectangle given in EPSG:2180 metres."""
    x0, y0, x1, y1 = rect
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    pts = [TO_WGS84.transform(x + 400_000, y + 400_000) for x, y in ring]
    return {"type": "way", "id": id_, "tags": tags, "geometry": [{"lon": lo, "lat": la} for lo, la in pts]}


def test_query_asks_for_solar_plants_with_geometry():
    q = validate.build_farm_query((18.54, 51.94, 18.74, 52.05))
    assert '["power"="plant"]["plant:source"="solar"](51.94000,18.54000,52.05000,18.74000)' in q
    assert q.rstrip().endswith("out geom;")


def test_farms_are_ground_mounted_polygons_clipped_to_the_gmina():
    gmina = gpd.GeoDataFrame(
        {"teryt": ["0000002"]}, geometry=[box(400_000, 400_000, 401_000, 401_000)], crs="EPSG:2180"
    )
    tags = {"power": "plant", "plant:source": "solar"}
    elements = [
        osm_way(1, {**tags, "name": "Big"}, (100, 100, 300, 200)),  # 2 ha
        osm_way(2, tags, (500, 500, 550, 550)),  # 0.25 ha: a rooftop-size "plant"
        osm_way(3, tags, (900, 100, 1100, 200)),  # 2 ha, half outside the gmina
        {"type": "node", "id": 4, "lat": 52.0, "lon": 18.6, "tags": tags},
    ]
    farms = validate.farms_in_gmina(elements, gmina).set_index("osm_id")
    assert list(farms.index) == ["way/1", "way/3"]
    assert farms.loc["way/1", "name"] == "Big"
    assert farms.loc["way/1", "area_ha"] == pytest.approx(2.0, rel=1e-3)
    assert farms.loc["way/3", "area_ha"] == pytest.approx(1.0, rel=1e-3)


def test_evaluate_overlap_chance_baseline_scores_and_reasons():
    candidates = gpd.GeoDataFrame(
        {"score": [0.9, 0.5]}, geometry=[box(0, 0, 500, 500), box(500, 500, 1000, 1000)], crs="EPSG:2180"
    )  # half of the gmina
    farms = gpd.GeoDataFrame(
        {"osm_id": ["a", "b", "c"], "area_ha": [4.0, 2.0, 4.0]},
        geometry=[box(100, 100, 300, 300), box(400, 400, 600, 500), box(600, 0, 800, 200)],
        crs="EPSG:2180",
    )
    forest = box(550, 0, 1000, 450)
    per_farm, summary = validate.evaluate(farms, candidates, GMINA, {"osm:forest": forest, "slope": None})

    share = dict(zip(per_farm["osm_id"], per_farm["share_in_candidates"], strict=True))
    assert share == pytest.approx({"a": 1.0, "b": 0.5, "c": 0.0})
    assert per_farm.set_index("osm_id").loc["c", "main_reason_outside"] == "osm:forest"
    assert pd.isna(per_farm.set_index("osm_id").loc["a", "main_reason_outside"])
    assert per_farm.set_index("osm_id").loc["a", "candidate_score"] == pytest.approx(0.9)

    assert summary["farms"] == 3 and summary["farms_mostly_inside"] == 2
    assert summary["expected_by_chance"] == pytest.approx(0.5)
    assert summary["farm_area_in_candidates"] == pytest.approx((4 + 1) / 10)
    assert summary["lift"] == pytest.approx(1.0)
    assert summary["score_of_all_candidate_area"] == pytest.approx(0.7)


def test_substations_near_farms_can_be_left_out_of_the_grid():
    grid = gpd.GeoDataFrame(
        {"kind": ["substation", "substation", "line"], "osm_id": ["s1", "s2", "l1"], "voltage": [None] * 3},
        geometry=[Point(0, 0), Point(5000, 0), LineString([(0, 0), (100, 0)])],
        crs="EPSG:2180",
    )
    farm = box(10, 10, 200, 200)
    kept = suitability.drop_substations_near(grid, farm, 500)
    assert list(kept["osm_id"]) == ["s2", "l1"]
