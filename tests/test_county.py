import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from pvscreen import boundary, county, pipeline
from pvscreen.suitability import load_config

CRS = "EPSG:2180"


def result(teryt, name, x0, cands, farms_ha=0.0, farm_in=0.0):
    """A screening result for a 1000 x 1000 m gmina (100 ha) at x0."""
    gmina = gpd.GeoDataFrame(
        {"teryt": [teryt], "name": [name], "official_area_ha": [100.0]},
        geometry=[box(x0, 0, x0 + 1000, 1000)],
        crs=CRS,
    )
    c = gpd.GeoDataFrame(
        {
            "rank": list(range(1, len(cands) + 1)),
            "area_ha": [a for a, _ in cands],
            "score": [s for _, s in cands],
            "energy_mwh_year": [a * 1000 for a, _ in cands],
        },
        geometry=[box(x0 + 10 * i, 0, x0 + 10 * i + 5, 5) for i in range(len(cands))],
        crs=CRS,
    )
    farms = gpd.GeoDataFrame(
        {
            "osm_id": ["way/1"] if farms_ha else [],
            "name": ["F"] if farms_ha else [],
            "area_ha": [farms_ha] if farms_ha else [],
        },
        geometry=[box(x0, 0, x0 + 100, 100)] if farms_ha else [],
        crs=CRS,
    )
    validation = (
        {"farms": 1, "farm_area_ha": farms_ha, "farm_area_in_candidates": farm_in / farms_ha}
        if farms_ha
        else None
    )
    empty = gpd.GeoDataFrame(geometry=[], crs=CRS)
    return pipeline.Result(gmina, load_config(), empty, empty, c, farms, validation)


def test_powiat_filter_selects_gminas_by_teryt_prefix():
    params = boundary.powiat_request_params("3027")
    assert "<fes:Literal>3027*</fes:Literal>" in params["FILTER"] and "PropertyIsLike" in params["FILTER"]
    with pytest.raises(ValueError):
        boundary.powiat_request_params("30271")


def test_county_table_rows_and_pooled_values():
    results = [
        result("3027011", "Miasto", 0, [(10.0, 0.95), (5.0, 0.7)], farms_ha=4.0, farm_in=4.0),
        result("3027022", "Wieś", 2000, [(30.0, 0.92)]),  # no farms: nothing to validate
    ]
    table = county.county_table([county.gmina_row(r) for r in results]).set_index("name")
    assert table.loc["Miasto", "candidates"] == 2 and table.loc["Miasto", "high_score_ha"] == 10.0
    assert table.loc["Miasto", "candidate_share"] == pytest.approx(0.15)
    assert table.loc["Miasto", "lift"] == pytest.approx(1 / 0.15)
    assert pd.isna(table.loc["Wieś", "in_candidates"])
    pooled = table.loc["all"]
    assert pooled.candidates == 3 and pooled.candidate_ha == 45.0 and pooled.high_score_ha == 40.0
    assert pooled.largest_ha == 30.0 and pooled.energy_gwh == pytest.approx(45.0)
    assert pooled.by_chance == pytest.approx(0.15)  # weighted by farm area: only Miasto has farms

    md = county.render_markdown("3027", table.reset_index())
    assert (
        "| Miasto | 3027011 | 100 | 2 | 15 ha | 15.0 % | 10 | 10 | 15 | 1 (4) | 100.0 % | 15.0 % | 6.67 |"
        in md
    )
    assert "| **All gminas** |" in md and "| Wieś | 3027022 |" in md


def test_overview_map_is_written(tmp_path):
    pytest.importorskip("matplotlib")
    results = [
        result("3027011", "Miasto", 0, [(10.0, 0.95)], farms_ha=4.0, farm_in=4.0),
        result("3027022", "Wieś", 2000, [(30.0, 0.5)]),
    ]
    png = county.overview_map(results, tmp_path / "map.png", "Test")
    assert png.exists() and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
