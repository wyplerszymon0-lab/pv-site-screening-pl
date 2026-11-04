import geopandas as gpd
import pytest
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box

from pvscreen import osm, osm_extract, validate


def test_extract_name_is_the_region_and_the_date():
    assert osm_extract.extract_name("lodzkie", "2021-01-01") == "lodzkie-210101.osm.pbf"


def test_other_tags_are_parsed_with_escaped_quotes():
    text = '"power"=>"plant","plant:source"=>"solar","name"=>"Farma \\"Adam\\""'
    assert osm_extract.parse_other_tags(text) == {
        "power": "plant",
        "plant:source": "solar",
        "name": 'Farma "Adam"',
    }
    assert osm_extract.parse_other_tags(None) == {}


def test_point_and_line_become_a_node_and_a_way():
    node = osm_extract.to_element({"power": "substation"}, "5", None, Point(18.5, 52.0))
    assert node == {"type": "node", "id": 5, "lon": 18.5, "lat": 52.0, "tags": {"power": "substation"}}
    way = osm_extract.to_element({"highway": "primary"}, "6", None, LineString([(0, 0), (1, 1)]))
    assert way["type"] == "way" and way["id"] == 6
    assert way["geometry"] == [{"lon": 0, "lat": 0}, {"lon": 1, "lat": 1}]


def test_a_closed_way_keeps_its_way_id_and_makes_the_same_polygon():
    geom = MultiPolygon([box(0, 0, 1, 1)])
    el = osm_extract.to_element({"building": "yes"}, None, "42", geom)
    assert (el["type"], el["id"]) == ("way", 42)
    assert osm._way_geometry(el).equals(box(0, 0, 1, 1))


def test_a_relation_keeps_its_holes():
    hole = [(2, 2), (3, 2), (3, 3), (2, 3), (2, 2)]
    geom = MultiPolygon([Polygon([(0, 0), (5, 0), (5, 5), (0, 5), (0, 0)], [hole]), box(10, 0, 11, 1)])
    el = osm_extract.to_element({"landuse": "forest"}, "9", None, geom)
    assert (el["type"], el["id"]) == ("relation", 9)
    assert [m["role"] for m in el["members"]] == ["outer", "inner", "outer"]
    assert osm._relation_geometry(el).area == pytest.approx(25 - 1 + 1)


def test_a_closed_solar_plant_way_from_the_lines_layer_is_a_polygon():
    # GDAL does not count power=* among closed ways that are areas, so a farm arrives as a line.
    ring = LineString([(18.5, 52.0), (18.51, 52.0), (18.51, 52.01), (18.5, 52.01), (18.5, 52.0)])
    el = osm_extract.to_element({"power": "plant", "plant:source": "solar"}, "3", None, ring)
    gmina = gpd.GeoDataFrame({"teryt": ["1"]}, geometry=[box(18, 51, 19, 53)], crs=4326).to_crs(2180)
    farms = validate.farms_in_gmina([el], gmina)
    assert len(farms) == 1 and farms.iloc[0]["osm_id"] == "way/3"


def test_read_elements_filters_merges_tags_and_counts_a_border_way_once(tmp_path, monkeypatch):
    lines = gpd.GeoDataFrame(
        {
            "osm_id": ["1", "2"],
            "highway": ["primary", None],
            "other_tags": [None, '"power"=>"line"'],
            "z_order": [0, 0],
        },
        geometry=[LineString([(0, 0), (1, 1)]), LineString([(0, 1), (1, 0)])],
        crs=4326,
    )
    empty = lines.iloc[:0]

    def fake(path, layer, bbox):
        return lines if layer == "lines" else empty

    monkeypatch.setattr(osm_extract.pyogrio, "read_dataframe", fake)
    els = osm_extract.read_elements(["a.pbf", "b.pbf"], (0, 0, 1, 1), lambda tags: "power" in tags)
    assert [e["id"] for e in els] == [2]
    assert els[0]["tags"] == {"power": "line"}


def test_a_date_geofabrik_does_not_keep_is_a_clear_error(tmp_path):
    class Resp:
        status_code = 404

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Session:
        def get(self, *args, **kwargs):
            return Resp()

    with pytest.raises(ValueError, match="no extract for 2021-03-07"):
        osm_extract.fetch_extract("lodzkie", "2021-03-07", tmp_path, Session())
    assert not list(tmp_path.iterdir())


def test_download_goes_through_a_part_file(tmp_path):
    class Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            return [b"pbf", b"data"]

    class Session:
        calls = 0

        def get(self, *args, **kwargs):
            Session.calls += 1
            return Resp()

    path = osm_extract.fetch_extract("lodzkie", "2021-01-01", tmp_path, Session())
    assert path.read_bytes() == b"pbfdata" and [p.name for p in tmp_path.iterdir()] == [path.name]
    osm_extract.fetch_extract("lodzkie", "2021-01-01", tmp_path, Session())
    assert Session.calls == 1  # the second time it comes from the cache
