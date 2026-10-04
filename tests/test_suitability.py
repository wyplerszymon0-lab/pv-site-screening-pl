import dataclasses

import geopandas as gpd
import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, box

from pvscreen import suitability as su

CFG = su.load_config()
RES = 5.0
TRANSFORM = from_origin(0, 1000, RES, RES)  # 200 x 200 cells over a 1 km square


def test_default_config_is_valid_and_documented():
    assert CFG.weight_slope + CFG.weight_aspect + CFG.weight_grid == pytest.approx(1)
    lines = su.DEFAULT_CONFIG.read_text(encoding="utf-8").splitlines()
    for field in dataclasses.fields(su.Config):
        i = next(i for i, ln in enumerate(lines) if ln.startswith(field.name))
        block = []
        while i > 0 and lines[i - 1].strip():  # the lines above, up to a blank line
            i -= 1
            block.append(lines[i])
        assert any(ln.startswith("#") for ln in block), f"{field.name} has no comment in its block"


def test_weights_that_do_not_add_up_are_rejected(tmp_path):
    bad = su.DEFAULT_CONFIG.read_text(encoding="utf-8").replace("weight_grid = 0.5", "weight_grid = 0.6")
    path = tmp_path / "bad.toml"
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError, match="add up to 1"):
        su.load_config(path)


def test_mask_drops_outside_excluded_steep_and_missing_cells():
    slope = np.full((200, 200), 1.0)
    slope[:, 150:] = 15.0  # too steep
    slope[0, 0] = np.nan  # no terrain data
    inside = np.ones_like(slope, dtype=bool)
    inside[190:, :] = False  # outside the gmina
    road = LineString([(0, 500), (1000, 500)]).buffer(20)  # a 40 m wide exclusion zone
    mask = su.suitable_mask(slope, inside, [road], TRANSFORM, CFG)

    assert not mask[0, 0] and not mask[:, 150:].any() and not mask[190:, :].any()
    assert not mask[100, 50]  # on the road (row 100 = y 497.5)
    assert mask[50, 50] and mask[150, 50]


def test_scores():
    assert su.slope_score(np.array([0.0, 5.0, 10.0, 20.0]), CFG).tolist() == [1.0, 0.5, 0.0, 0.0]
    slope = np.full(5, 6.0)
    aspect = np.array([180.0, 0.0, 90.0, 270.0, 135.0])
    s = su.aspect_score(slope, aspect, CFG)
    assert s[:4] == pytest.approx([1.0, 0.0, 0.5, 0.5])
    assert s[4] == pytest.approx((1 + np.cos(np.radians(45))) / 2)
    assert su.aspect_score(np.array([1.0]), np.array([0.0]), CFG)[0] == 1.0  # flat: aspect irrelevant
    assert su.grid_score(0, CFG) == 1.0
    assert su.grid_score(CFG.grid_zero_at_m / 2, CFG) == pytest.approx(0.5)
    assert su.grid_score(2 * CFG.grid_zero_at_m, CFG) == 0.0


def test_candidates_lose_thin_strips_and_small_pieces():
    mask = np.zeros((200, 200), dtype=bool)
    mask[20:60, 20:60] = True  # 200 m x 200 m = 4 ha
    mask[38:42, 60:180] = True  # 20 m wide strip attached to it: thinner than 2 x 15 m
    mask[120:130, 120:130] = True  # 50 m x 50 m = 0.25 ha, below min_area_ha
    polys = su.candidate_polygons(mask, TRANSFORM, CFG)
    assert len(polys) == 1
    assert polys[0].area / 10_000 == pytest.approx(4.0, rel=0.01)


def test_candidates_are_ranked_by_score_and_report_their_parts():
    slope = np.full((200, 200), 1.0)
    aspect = np.full((200, 200), 180.0)
    near = box(50, 600, 250, 800)  # 4 ha, 50 m from the substation
    far = box(600, 100, 800, 300)  # 4 ha, ~800 m away
    grid = gpd.GeoDataFrame(
        {"kind": ["substation", "line"], "osm_id": ["n", "w"], "voltage": [None, "110000"]},
        geometry=[Point(0, 700), LineString([(700, 0), (700, 1000)])],
        crs="EPSG:2180",
    )
    constraints = gpd.GeoDataFrame(geometry=[box(600, 100, 700, 300)], crs="EPSG:2180")  # half of `far`

    out = su.score_candidates([far, near], slope, aspect, TRANSFORM, grid, constraints, CFG)

    assert list(out["rank"]) == [1, 2]
    best, worst = out.iloc[0], out.iloc[1]
    assert best.geometry.equals(near)
    assert best.substation_distance_m == pytest.approx(50)
    assert worst.line_distance_m == 0  # the line crosses it, but that is not where it connects
    assert worst.constraint_share == pytest.approx(0.5)
    expected = (
        CFG.weight_slope * 0.9 + CFG.weight_aspect * 1.0 + CFG.weight_grid * (1 - 50 / CFG.grid_zero_at_m)
    )
    assert best.score == pytest.approx(expected)
    assert best.area_ha == pytest.approx(4.0)


def test_no_substation_means_no_grid_score():
    slope, aspect = np.full((200, 200), 1.0), np.full((200, 200), 180.0)
    empty = gpd.GeoDataFrame({"kind": [], "osm_id": [], "voltage": []}, geometry=[], crs="EPSG:2180")
    out = su.score_candidates([box(0, 0, 200, 200)], slope, aspect, TRANSFORM, empty, None, CFG)
    assert out.iloc[0].score_grid == 0.0 and out.iloc[0].constraint_share == 0.0
