import json

import geopandas as gpd
import pytest
from shapely.geometry import LineString, box

from pvscreen import webmap

CRS = "EPSG:2180"


def write_gpkg(folder, with_grid=True):
    folder.mkdir(parents=True)
    path = folder / "screening.gpkg"
    gpd.GeoDataFrame(
        {"teryt": ["3027062"], "name": ["Testowo"]},
        geometry=[box(450_000, 450_000, 452_000, 452_000)],
        crs=CRS,
    ).to_file(path, layer="boundary")
    gpd.GeoDataFrame(
        {
            "rule": [
                "building",
                "road:primary",
                "waterway:stream",
                "forest",
                "natura2000_birds",
                "protected_landscape_area",
            ]
        },
        geometry=[box(450_000 + 100 * i, 450_000, 450_050 + 100 * i, 450_050) for i in range(6)],
        crs=CRS,
    ).to_file(path, layer="exclusions")
    gpd.GeoDataFrame(
        {
            "rank": [1],
            "area_ha": [12.3456],
            "score": [0.91234],
            "score_slope": [0.9],
            "score_aspect": [1.0],
            "score_grid": [0.8],
            "mean_slope_deg": [0.71],
            "substation_distance_m": [1234.5],
            "constraint_share": [0.0],
            "yield_kwh_per_kwp": [990.4],
            "energy_mwh_year": [12222.2],
            "line_distance_m": [55.0],  # not exported
        },
        geometry=[box(451_000, 451_000, 451_351, 451_351)],
        crs=CRS,
    ).to_file(path, layer="candidates")
    if with_grid:
        gpd.GeoDataFrame(
            {"kind": ["line", "substation"], "osm_id": ["way/1", "way/2"], "voltage": ["110000", None]},
            geometry=[
                LineString([(450_000, 451_000), (452_000, 451_000)]),
                box(451_500, 451_500, 451_520, 451_520),
            ],
            crs=CRS,
        ).to_file(path, layer="grid")
    return path


def test_exclusion_rules_are_grouped_like_the_qgis_map():
    assert webmap.exclusion_group("road:tertiary") == "Roads and railways"
    assert webmap.exclusion_group("railway") == "Roads and railways"
    assert webmap.exclusion_group("waterway:river") == "Water"
    assert webmap.exclusion_group("built_up") == "Buildings and built-up land"
    assert webmap.exclusion_group("protected_landscape_area") == "Protected landscape (constraint only)"
    assert webmap.exclusion_group("nature_reserve") == "Protected nature (excluded)"


def test_export_writes_small_wgs84_layers_and_meta(tmp_path):
    write_gpkg(tmp_path / "outputs" / "3027062")
    result = webmap.export("3027062", out_root=tmp_path / "outputs", site_data=tmp_path / "site")
    folder = tmp_path / "site" / "3027062"

    cands = json.loads((folder / "candidates.geojson").read_text(encoding="utf-8"))
    props = cands["features"][0]["properties"]
    assert props == {
        "rank": 1,
        "area_ha": 12.3,
        "score": 0.91,
        "score_slope": 0.9,
        "score_aspect": 1.0,
        "score_grid": 0.8,
        "mean_slope_deg": 0.7,
        "substation_distance_m": 1234.0,
        "constraint_share": 0.0,
        "yield_kwh_per_kwp": 990.0,
        "energy_mwh_year": 12222.0,
    }
    lon, lat = cands["features"][0]["geometry"]["coordinates"][0][0]
    assert 14 < lon < 25 and 49 < lat < 55  # WGS84, somewhere in Poland
    assert len(str(lon).split(".")[1]) <= webmap.DECIMALS

    groups = [
        f["properties"]["group"] for f in json.loads((folder / "exclusions.geojson").read_text())["features"]
    ]
    assert groups == sorted(set(groups)) and len(groups) == 6  # one dissolved feature per group
    subs = json.loads((folder / "substations.geojson").read_text())["features"]
    assert subs[0]["geometry"]["type"] == "Point"

    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["name"] == "Testowo" and meta["candidates"] == 1
    assert set(meta["layers"]) == {"boundary", "candidates", "exclusions", "power_lines", "substations"}
    assert json.loads((tmp_path / "site" / "index.json").read_text()) == ["3027062"]
    assert set(result["bytes"]) == set(meta["layers"])


def test_export_without_grid_layer_and_missing_run(tmp_path):
    write_gpkg(tmp_path / "outputs" / "3027062", with_grid=False)
    meta = webmap.export("3027062", out_root=tmp_path / "outputs", site_data=tmp_path / "site")["meta"]
    assert "power_lines" not in meta["layers"]
    with pytest.raises(SystemExit, match="run `python -m pvscreen"):
        webmap.export("1234567", out_root=tmp_path / "outputs", site_data=tmp_path / "site")
