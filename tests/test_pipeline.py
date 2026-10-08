from datetime import date
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

from pvscreen import __main__ as cli
from pvscreen import pipeline
from pvscreen.suitability import load_config

CRS = "EPSG:2180"


def B(x0, y0, x1, y1):
    """A box near Brudzew (EPSG:2180), so that a PL-2000 zone can be chosen."""
    return box(x0 + 470_000, y0 + 455_000, x1 + 470_000, y1 + 455_000)


def result(candidates=True, validation=True):
    gmina = gpd.GeoDataFrame(
        {"teryt": ["3027022"], "name": ["Brudzew"], "official_area_ha": [100.0]},
        geometry=[B(0, 0, 1000, 1000)],
        crs=CRS,
    )
    osm_zones = gpd.GeoDataFrame(
        {
            "rule": ["building"],
            "buffer_m": [50],
            "basis": ["assumption"],
            "n_features": [3],
            "area_ha": [20.0],
        },
        geometry=[B(0, 0, 1000, 200)],
        crs=CRS,
    )
    protected = gpd.GeoDataFrame(
        {
            "category": ["natura2000_birds", "protected_landscape_area"],
            "kind": ["exclude", "constraint"],
            "name": ["Dolina", "Nadwarciański"],
            "reason": ["Art. 33", "Art. 24"],
            "area_ha": [5.0, 30.0],
        },
        geometry=[B(0, 200, 250, 400), B(500, 500, 1000, 1000)],
        crs=CRS,
    )
    cands = gpd.GeoDataFrame(
        {
            "rank": [1, 2],
            "area_ha": [12.0, 3.5],
            "score": [0.91, 0.62],
            "mean_slope_deg": [0.8, 2.4],
            "substation_distance_m": [450.0, 7200.0],
            "constraint_share": [0.0, 1.0],
            "yield_kwh_per_kwp": [990.0, 1001.0],
            "capacity_mwp": [12.0, 3.5],
            "energy_mwh_year": [11880.0, 3503.5],
        },
        geometry=[B(300, 400, 700, 700), B(600, 800, 800, 975)],
        crs=CRS,
    )
    if not candidates:
        cands = cands.iloc[:0]
    farms = gpd.GeoDataFrame({"osm_id": [], "name": [], "area_ha": []}, geometry=[], crs=CRS)
    summary = (
        {
            "farms": 2,
            "farm_area_ha": 8.0,
            "farm_area_in_candidates": 0.75,
            "expected_by_chance": 0.155,
            "farms_mostly_inside": 1,
            "lift": 4.84,
        }
        if validation
        else None
    )
    return pipeline.Result(gmina, load_config(), osm_zones, protected, cands, farms, summary)


def test_report_has_every_section_and_the_numbers():
    md = pipeline.render_report(
        result(), Path("outputs/3027022/screening.gpkg"), "EPSG:2180 (PL-1992)", today=date(2026, 10, 8)
    )
    for heading in (
        "# Solar-farm screening: gmina Brudzew (TERYT 3027022)",
        "## Thresholds",
        "## Exclusions",
        "## Candidates",
        "## Existing solar farms",
        "## Data",
    ):
        assert heading in md
    assert "_Generated 2026-10-08" in md
    assert "| Maximum slope | 10° |" in md
    assert "Dolina (natura2000_birds, 5 ha)" in md and "Nadwarciański (protected_landscape_area, 30 ha)" in md
    assert "2 candidates, 16 ha (15.5 % of the gmina), 16 MWp, about 15 GWh per year" in md
    assert "| 1 | 12.0 | 0.91 | 0.8° | 0.5 km | 0.0 % | 11,880 |" in md
    assert "| 2 | 3.5 | 0.62 | 2.4° | 7.2 km | 100.0 % | 3,504 |" in md
    assert "75.0 % of their area lies in candidates, against 15.5 % expected by chance (lift 4.84)" in md
    assert "© OpenStreetMap contributors, ODbL" in md


def test_report_without_candidates_or_farms():
    md = pipeline.render_report(result(candidates=False, validation=False), Path("x.gpkg"), "EPSG:2180")
    assert "No area passes the screening." in md
    assert "maps no solar farm of at least 1 ha in this gmina" in md


def test_outputs_go_to_one_geopackage_per_gmina(tmp_path):
    path, note = pipeline.write_outputs(result(), "pl2000", out_root=tmp_path)
    assert path == tmp_path / "3027022" / "screening.gpkg"
    layers = {name: gpd.read_file(path, layer=name) for name in ("boundary", "exclusions", "candidates")}
    assert all(layer.crs.to_epsg() == 2177 for layer in layers.values()) and "zone 6" in note
    assert set(layers["exclusions"]["rule"]) == {"building", "natura2000_birds", "protected_landscape_area"}
    assert len(layers["candidates"]) == 2


def test_cli_requires_a_teryt(capsys):
    with pytest.raises(SystemExit):
        cli.main([])
    assert "--teryt" in capsys.readouterr().err
