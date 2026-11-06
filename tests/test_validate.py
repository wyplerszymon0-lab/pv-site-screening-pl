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


def test_pooled_table_weights_the_chance_baseline_by_farm_area():
    rows = [
        {
            "teryt": "1",
            "name": "A",
            "gmina_ha": 100.0,
            "candidate_ha": 50.0,
            "farms": 2,
            "farm_ha": 10.0,
            "farm_ha_in": 9.0,
        },
        {
            "teryt": "2",
            "name": "B",
            "gmina_ha": 100.0,
            "candidate_ha": 20.0,
            "farms": 1,
            "farm_ha": 30.0,
            "farm_ha_in": 12.0,
        },
        {
            "teryt": "3",
            "name": "C",
            "gmina_ha": 100.0,
            "candidate_ha": 40.0,
            "farms": 0,
            "farm_ha": 0.0,
            "farm_ha_in": 0.0,
        },
    ]
    table = validate.pooled_table(rows).set_index("name")
    assert table.loc["A", "in_candidates"] == pytest.approx(0.9)
    assert table.loc["A", "by_chance"] == pytest.approx(0.5)
    assert table.loc["B", "lift"] == pytest.approx(0.4 / 0.2)
    assert pd.isna(table.loc["C", "in_candidates"])  # no farms, nothing to check
    pooled = table.loc["all"]
    assert pooled.farms == 3 and pooled.farm_ha == 40
    assert pooled.in_candidates == pytest.approx(21 / 40)
    assert pooled.by_chance == pytest.approx((10 * 0.5 + 30 * 0.2) / 40)  # weighted by farm area


def test_a_farm_mapped_twice_counts_once():
    # A power=plant relation for the whole site plus a way for one section of it.
    gmina = gpd.GeoDataFrame(
        {"teryt": ["0000002"]}, geometry=[box(400_000, 400_000, 401_000, 401_000)], crs="EPSG:2180"
    )
    tags = {"power": "plant", "plant:source": "solar"}
    elements = [
        osm_way(1, {**tags, "name": "Whole site"}, (100, 100, 500, 300)),  # 8 ha
        osm_way(2, tags, (100, 100, 300, 300)),  # 4 ha inside it
        osm_way(3, tags, (700, 700, 900, 800)),  # 2 ha elsewhere
    ]
    farms = validate.farms_in_gmina(elements, gmina)
    assert sorted(farms["area_ha"].round(3)) == [2.0, 8.0]
    site = farms[farms["area_ha"].round(3) == 8.0].iloc[0]
    assert site["name"] == "Whole site" and set(site["osm_id"].split(";")) == {"way/1", "way/2"}


def test_farms_only_outside_the_gmina_give_an_empty_table():
    # The Overpass query covers the gmina's bounding box, so it can return farms
    # that all lie in a neighbouring gmina (Kawęczyn, Tuliszków, Władysławów).
    gmina = gpd.GeoDataFrame(
        {"teryt": ["0000002"]}, geometry=[box(400_000, 400_000, 401_000, 401_000)], crs="EPSG:2180"
    )
    tags = {"power": "plant", "plant:source": "solar"}
    farms = validate.farms_in_gmina([osm_way(1, tags, (1500, 1500, 1700, 1700))], gmina)
    assert farms.empty and list(farms.columns) == ["osm_id", "name", "area_ha", "geometry"]


# ── farm land added since an older OpenStreetMap snapshot (issue #35) ──


def farms_gdf(*boxes):
    return gpd.GeoDataFrame(
        {"osm_id": [f"way/{i}" for i in range(len(boxes))], "name": None, "area_ha": 0.0},
        geometry=list(boxes),
        crs="EPSG:2180",
    )


def test_new_farms_keep_only_land_that_was_not_a_farm_then():
    now = farms_gdf(
        box(0, 0, 200, 100),  # 2 ha, already there: dropped
        box(300, 0, 700, 100),  # 4 ha, half of it there then: the 2 ha extension stays
        box(800, 0, 1000, 100),  # 2 ha, new
        box(0, 500, 150, 600),  # 1.5 ha, 0.75 there then: the 0.75 ha rest is too small
    )
    then = farms_gdf(box(0, 0, 200, 100), box(300, 0, 500, 100), box(0, 500, 75, 600))
    new = validate.new_farms(now, then)
    assert list(new["osm_id"]) == ["way/1", "way/2"]
    assert list(new["area_ha"].round(6)) == [2.0, 2.0]
    assert new.iloc[0].geometry.equals(box(500, 0, 700, 100))


def test_new_farms_with_no_farm_then_are_all_of_today_s():
    now = farms_gdf(box(0, 0, 200, 100))
    assert validate.new_farms(now, farms_gdf()).equals(now)


def test_farms_as_of_a_date_come_from_the_extract(tmp_path, monkeypatch):
    gmina = GMINA.assign(teryt="0000002")
    seen = {}

    def fake(bounds, as_of, keep):
        seen["as_of"] = as_of
        seen["solar"] = keep({"power": "plant", "plant:source": "solar"})
        seen["wind"] = keep({"power": "plant", "plant:source": "wind"})
        return []

    monkeypatch.setattr(validate.osm_extract, "elements", fake)
    assert validate.fetch_farm_elements(gmina, cache_dir=tmp_path, as_of="2021-01-01") == []
    assert seen == {"as_of": "2021-01-01", "solar": True, "wind": False}
    assert (tmp_path / "solar_farms_0000002_2021-01-01.json").exists()


def ranked_candidates(*scores):
    """Candidates of 100 x 100 m in a row, with these scores."""
    boxes = [box(i * 100, 0, (i + 1) * 100, 100) for i in range(len(scores))]
    return gpd.GeoDataFrame({"score": list(scores)}, geometry=boxes, crs="EPSG:2180")


def test_score_rank_gives_the_share_of_candidate_land_scoring_lower():
    ranks = validate.score_rank(farms_gdf(box(200, 0, 300, 100)), ranked_candidates(0.2, 0.4, 0.8, 0.9))
    assert ranks["farm_ha"].tolist() == pytest.approx([1.0])
    assert ranks["below"].tolist() == pytest.approx([0.625])  # 2 lower, itself half: 2.5 of 4


def test_score_rank_counts_ties_as_half():
    ranks = validate.score_rank(farms_gdf(box(0, 0, 100, 100)), ranked_candidates(0.5, 0.5, 0.5, 0.5))
    assert ranks["below"].tolist() == pytest.approx([0.5])


def test_score_rank_weights_candidates_by_area():
    wide = gpd.GeoDataFrame(
        {"score": [0.2, 0.8]}, geometry=[box(0, 0, 300, 100), box(300, 0, 400, 100)], crs="EPSG:2180"
    )
    ranks = validate.score_rank(farms_gdf(box(300, 0, 400, 100)), wide)
    assert ranks["below"].tolist() == pytest.approx([0.875])  # 3 of 4 lower, itself half of 1


def test_score_rank_ignores_farm_land_outside_candidates():
    farms = farms_gdf(box(50, 0, 150, 100), box(500, 0, 600, 100))
    ranks = validate.score_rank(farms, ranked_candidates(0.2, 0.8))
    assert ranks["farm_ha"].tolist() == pytest.approx([0.5, 0.5])


def test_pooled_rank_weights_candidates_by_their_farm_land():
    ranks = pd.DataFrame({"farm_ha": [3.0, 1.0], "below": [1.0, 0.0]})
    assert validate.pooled_rank(ranks)["auc"] == pytest.approx(0.75)


def test_pooled_rank_interval_is_seeded_and_widens_with_fewer_candidates():
    few = pd.DataFrame({"farm_ha": [1.0] * 4, "below": [0.2, 0.4, 0.6, 0.8]})
    many = pd.concat([few] * 25, ignore_index=True)
    a, b, c = validate.pooled_rank(few), validate.pooled_rank(few), validate.pooled_rank(many)
    assert a == b
    assert a["low"] < a["auc"] < a["high"]
    assert a["high"] - a["low"] > c["high"] - c["low"]
    assert (a["candidates"], c["candidates"]) == (4, 100)


def test_one_candidate_gives_no_width_to_the_interval():
    r = validate.pooled_rank(pd.DataFrame({"farm_ha": [5.0], "below": [0.9]}))
    assert r["low"] == r["high"] == pytest.approx(0.9)


def test_no_farm_land_in_candidates_gives_no_auc():
    r = validate.pooled_rank(pd.DataFrame({"farm_ha": [], "below": []}))
    assert r["candidates"] == 0
    assert pd.isna(r["auc"])


def test_rank_table_has_a_row_per_gmina_and_a_pooled_row():
    one = pd.DataFrame({"farm_ha": [3.0], "below": [1.0]})
    two = pd.DataFrame({"farm_ha": [1.0], "below": [0.0]})
    table = validate.rank_table(["A", "B"], [one, two])
    assert table["name"].tolist() == ["A", "B", "all"]
    assert table["auc"].tolist() == pytest.approx([1.0, 0.0, 0.75])


def scored_parts():
    """Four equal candidates: the grid part follows the score, the slope part reverses it."""
    boxes = [box(i * 100, 0, (i + 1) * 100, 100) for i in range(4)]
    return gpd.GeoDataFrame(
        {
            "score": [0.1, 0.2, 0.3, 0.4],
            "score_grid": [0.1, 0.2, 0.3, 0.4],
            "score_slope": [0.4, 0.3, 0.2, 0.1],
            "score_aspect": [1.0, 1.0, 1.0, 1.0],
            "area_ha": [1.0, 2.0, 3.0, 4.0],
        },
        geometry=boxes,
        crs="EPSG:2180",
    )


def test_part_ranks_ranks_the_same_land_by_each_part():
    cfg = suitability.load_config()
    ranks = validate.part_ranks(farms_gdf(box(300, 0, 400, 100)), scored_parts(), cfg)
    assert tuple(ranks) == validate.RANKINGS
    assert ranks["score"]["below"].tolist() == pytest.approx([0.875])
    assert ranks["grid"]["below"].tolist() == pytest.approx([0.875])
    assert ranks["slope"]["below"].tolist() == pytest.approx([0.125])
    assert ranks["aspect"]["below"].tolist() == pytest.approx([0.5])  # all tied


def test_part_ranks_weights_slope_and_aspect_as_the_score_does():
    cfg = suitability.load_config()
    parts = scored_parts()
    parts["score_aspect"] = [0.0, 0.0, 0.0, 1.0]
    ranks = validate.part_ranks(farms_gdf(box(300, 0, 400, 100)), parts, cfg)
    terrain = cfg.weight_slope * 0.1 + cfg.weight_aspect * 1.0
    assert terrain > cfg.weight_slope * 0.4
    assert ranks["slope and aspect"]["below"].tolist() == pytest.approx([0.875])


def test_paired_difference_of_a_ranking_with_itself_is_exactly_zero():
    ranks = pd.DataFrame({"farm_ha": [1.0, 2.0, 3.0], "below": [0.2, 0.5, 0.9]})
    assert validate.paired_difference(ranks, ranks) == {"difference": 0.0, "low": 0.0, "high": 0.0}


def test_paired_difference_is_narrower_than_separate_intervals():
    a = pd.DataFrame({"farm_ha": [1.0] * 6, "below": [0.1, 0.3, 0.5, 0.6, 0.8, 0.95]})
    b = a.assign(below=a["below"] - 0.05)
    d = validate.paired_difference(a, b)
    assert d["difference"] == pytest.approx(0.05)
    assert d["low"] == pytest.approx(0.05)
    assert d["high"] == pytest.approx(0.05)
    separate = validate.pooled_rank(a)
    assert separate["high"] - separate["low"] > d["high"] - d["low"]


def test_paired_difference_is_weighted_by_farm_land_and_seeded():
    a = pd.DataFrame({"farm_ha": [3.0, 1.0], "below": [1.0, 0.0]})
    b = pd.DataFrame({"farm_ha": [3.0, 1.0], "below": [0.0, 1.0]})
    assert validate.paired_difference(a, b)["difference"] == pytest.approx(0.5)
    assert validate.paired_difference(a, b) == validate.paired_difference(a, b)


def test_paired_difference_without_farm_land_is_empty():
    none = pd.DataFrame({"farm_ha": [], "below": []})
    assert pd.isna(validate.paired_difference(none, none)["difference"])


def test_part_table_pools_gminas_and_gives_the_lead_of_the_full_score():
    one = {
        "score": pd.DataFrame({"farm_ha": [3.0], "below": [1.0]}),
        "grid": pd.DataFrame({"farm_ha": [3.0], "below": [0.5]}),
    }
    two = {
        "score": pd.DataFrame({"farm_ha": [1.0], "below": [0.0]}),
        "grid": pd.DataFrame({"farm_ha": [1.0], "below": [0.5]}),
    }
    table = validate.part_table([one, two]).set_index("ranked_by")
    assert table.loc["score", "auc"] == pytest.approx(0.75)
    assert table.loc["grid", "auc"] == pytest.approx(0.5)
    assert table.loc["score", "score_lead"] == 0.0
    assert table.loc["grid", "score_lead"] == pytest.approx(0.25)
