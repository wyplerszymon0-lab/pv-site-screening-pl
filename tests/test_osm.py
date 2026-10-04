import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, box

from pvscreen import osm

FIXTURE = Path(__file__).parent / "fixtures" / "osm_sample.json"  # cut from the Przykona response


def way(id_, tags, coords):
    return {"type": "way", "id": id_, "tags": tags, "geometry": [{"lon": x, "lat": y} for x, y in coords]}


SQUARE = [(18.60, 52.00), (18.61, 52.00), (18.61, 52.01), (18.60, 52.01), (18.60, 52.00)]


@pytest.mark.parametrize(
    "tags, key",
    [
        ({"building": "house"}, "building"),
        ({"highway": "primary_link"}, "road:primary"),
        ({"highway": "residential"}, "road:residential"),
        ({"railway": "rail"}, "railway"),
        ({"landuse": "reservoir"}, "water"),
        ({"natural": "wood"}, "forest"),
        ({"waterway": "stream"}, "waterway:stream"),
        ({"landuse": "cemetery"}, "built_up"),
        ({"power": "line", "voltage": "110000"}, "grid:line"),
        ({"power": "substation"}, "grid:substation"),
        ({"landuse": "farmland"}, None),
    ],
)
def test_classify(tags, key):
    assert osm.classify(tags) == key


def test_closed_area_ways_become_polygons_but_lines_stay_lines():
    gdf = osm.elements_to_gdf(
        [
            way(1, {"building": "yes"}, SQUARE),
            way(2, {"highway": "tertiary"}, SQUARE),  # a closed road loop is still a road
            way(3, {"power": "line"}, SQUARE),
            way(4, {"waterway": "river"}, SQUARE[:3]),
            {"type": "node", "id": 5, "lat": 52.0, "lon": 18.6, "tags": {"power": "substation"}},
            way(6, {"landuse": "farmland"}, SQUARE),
        ]
    ).set_index("osm_id")
    assert gdf.crs.to_epsg() == 2180
    assert list(gdf.index) == ["way/1", "way/2", "way/3", "way/4", "node/5"]
    assert gdf.geom_type.to_dict() == {
        "way/1": "Polygon",
        "way/2": "LineString",
        "way/3": "LineString",
        "way/4": "LineString",
        "node/5": "Point",
    }


def test_multipolygon_relation_is_assembled_with_its_hole():
    # Outer ring split over two member ways (as in OSM), one inner ring.
    outer_a = [(18.60, 52.00), (18.62, 52.00), (18.62, 52.02)]
    outer_b = [(18.62, 52.02), (18.60, 52.02), (18.60, 52.00)]
    inner = [(18.605, 52.005), (18.615, 52.005), (18.615, 52.015), (18.605, 52.015), (18.605, 52.005)]
    rel = {
        "type": "relation",
        "id": 9,
        "tags": {"type": "multipolygon", "landuse": "forest"},
        "members": [
            {"type": "way", "role": "outer", "geometry": [{"lon": x, "lat": y} for x, y in outer_a]},
            {"type": "way", "role": "outer", "geometry": [{"lon": x, "lat": y} for x, y in outer_b]},
            {"type": "way", "role": "inner", "geometry": [{"lon": x, "lat": y} for x, y in inner]},
        ],
    }
    forest = osm.elements_to_gdf([rel]).geometry.iloc[0]
    full = osm.elements_to_gdf([way(1, {"landuse": "forest"}, outer_a + outer_b[1:])]).geometry.iloc[0]
    hole = osm.elements_to_gdf([way(2, {"landuse": "forest"}, inner)]).geometry.iloc[0]
    assert forest.area == pytest.approx(full.area - hole.area, rel=1e-6)


def test_relation_without_members_is_skipped():
    # What "out geom tags" returns: tags and bounds only.
    rel = {"type": "relation", "id": 9, "bounds": {}, "tags": {"natural": "water"}}
    assert osm.elements_to_gdf([rel]).empty


def test_query_asks_for_member_geometry_and_bounds_each_statement():
    q = osm.build_query((18.54, 51.94, 18.74, 52.05))
    assert q.rstrip().endswith("out geom;")
    assert "[bbox:" not in q
    assert q.count("(51.94000,18.54000,52.05000,18.74000);") == len(osm.STATEMENTS)


def features_2180(rows):
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:2180")


def test_exclusions_buffer_by_rule_and_clip_to_the_gmina():
    gmina = gpd.GeoDataFrame({"teryt": ["0000002"]}, geometry=[box(0, 0, 1000, 1000)], crs="EPSG:2180")
    feats = features_2180(
        [
            {
                "key": "road:primary",
                "osm_id": "way/1",
                "voltage": None,
                "geometry": LineString([(0, 500), (1000, 500)]),
            },
            {"key": "building", "osm_id": "way/2", "voltage": None, "geometry": Point(990, 990).buffer(5)},
            {
                "key": "grid:line",
                "osm_id": "way/3",
                "voltage": "110000",
                "geometry": LineString([(0, 0), (5000, 0)]),
            },
        ]
    )
    zones = osm.exclusions(feats, gmina).set_index("rule")
    assert set(zones.index) == {"road:primary", "building"}  # the grid is not an exclusion
    assert zones.loc["road:primary", "area_ha"] == pytest.approx(2 * 28 * 1000 / 10_000, rel=1e-3)
    # the building sits 10 m from the corner: its 55 m buffer is cut at the gmina edge
    assert zones.loc["building", "geometry"].within(gmina.geometry.iloc[0].buffer(1e-6))

    lines = osm.grid(feats, gmina)
    assert list(lines["kind"]) == ["line"] and lines.geometry.iloc[0].length == 5000  # not clipped


def test_recorded_sample_end_to_end():
    elements = json.loads(FIXTURE.read_text(encoding="utf-8"))["elements"]
    feats = osm.elements_to_gdf(elements)
    counts = feats["key"].value_counts()
    assert counts["building"] == 165
    assert counts["water"] >= 1 and counts["grid:line"] >= 1

    reservoir = feats[feats["osm_id"] == "relation/3206876"].geometry.iloc[0]
    assert reservoir.area / 10_000 == pytest.approx(139.0, abs=0.5)

    # Power lines and rivers run far beyond the cut-out; screen 300 m around the reservoir.
    area = gpd.GeoDataFrame(
        {"teryt": ["0000002"]}, geometry=[reservoir.buffer(300).envelope], crs="EPSG:2180"
    )
    zones = osm.exclusions(feats, area).set_index("rule")
    raw_buildings = feats[feats["key"] == "building"].union_all().area / 10_000
    assert zones.loc["building", "area_ha"] > 5 * raw_buildings  # 50 m buffers dwarf the footprints
    assert zones.loc["water", "buffer_m"] == 0
    assert zones.loc["water", "area_ha"] == pytest.approx(
        feats[feats["key"] == "water"].union_all().intersection(area.geometry.iloc[0]).area / 10_000
    )
    summary = osm.summarize(zones.reset_index(), area)
    assert summary.iloc[-1]["rule"] == "all (union)"
    assert summary.iloc[-1]["area_ha"] <= zones["area_ha"].sum()


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls += 1
        status, body = self.responses.pop(0)

        class Resp:
            status_code = status

            def raise_for_status(self):
                if status >= 400:
                    raise RuntimeError(status)

            def json(self):
                return body

        return Resp()


@pytest.fixture
def gmina_fixture():
    from pvscreen.boundary import read_gmina

    return read_gmina(Path(__file__).parent / "fixtures" / "prg_gmina_3027062.gml")


def test_busy_server_is_retried_then_cached(tmp_path, gmina_fixture):
    session = FakeSession([(504, None), (429, None), (200, {"elements": [{"type": "node", "id": 1}]})])
    els = osm.fetch_elements(gmina_fixture, cache_dir=tmp_path, session=session, waits=(0, 0))
    assert session.calls == 3 and els == [{"type": "node", "id": 1}]
    assert osm.fetch_elements(gmina_fixture, cache_dir=tmp_path, session=FakeSession([]), waits=(0, 0)) == els


def test_overpass_error_remark_is_raised_and_not_cached(tmp_path, gmina_fixture):
    session = FakeSession([(200, {"elements": [], "remark": "runtime error: Query timed out"})])
    with pytest.raises(RuntimeError, match="timed out"):
        osm.fetch_elements(gmina_fixture, cache_dir=tmp_path, session=session, waits=())
    assert not list(tmp_path.iterdir())
