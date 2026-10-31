import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from pvscreen import energy as E
from pvscreen import variability as V

FIXTURES = Path(__file__).parent / "fixtures" / "nasa_power"


def cache(tmp_path):
    (tmp_path / "power_52.2_21.0.json").write_text((FIXTURES / "nasa-power-warsaw.json").read_text())
    (tmp_path / "power_monthly_52.2_21.0_2001-2025.json").write_text(
        (FIXTURES / "nasa-power-monthly-warsaw.json").read_text()
    )
    return tmp_path


def candidates(areas):
    # Small squares near 52.2 N 21.0 E, in the web map's EPSG:4326, all in one cache cell.
    geoms = [box(21.0 + i * 0.002, 52.2, 21.001 + i * 0.002, 52.201) for i in range(len(areas))]
    return gpd.GeoDataFrame({"area_ha": areas}, geometry=geoms, crs="EPSG:4326")


def test_yearly_energy_is_area_times_yearly_yield_summed(tmp_path):
    opts = E.ModelOptions()
    seen: set = set()
    years = V.yearly_energy(
        candidates([10.0, 30.0]), 1.0, 2025, opts, cache(tmp_path), session=object(), seen=seen
    )
    clim = E.parse_power_climatology(json.loads((FIXTURES / "nasa-power-warsaw.json").read_text()))
    tilt, _ = E.optimal_tilt(clim, 52.2005, opts=opts)
    one = E.yearly_yields(
        E.parse_power_monthly(json.loads((FIXTURES / "nasa-power-monthly-warsaw.json").read_text())),
        52.2005,
        tilt,
        180,
        opts,
    )
    assert list(years.index) == list(range(2001, 2026))
    assert years[2010] == pytest.approx(40.0 * one[2010], rel=1e-6)  # 40 MWp × kWh/kWp = MWh
    assert len(seen) == 1


def test_perfectly_correlated_gminas_pool_to_the_sum_of_their_p90s():
    a = pd.Series([100.0, 90.0, 110.0, 95.0, 105.0], index=range(2001, 2006))
    pooled = V.summary_row("all", "", a + 2 * a, 0.0)["p90_gwh"]
    parts = V.summary_row("a", "1", a, 0.0)["p90_gwh"] + V.summary_row("b", "2", 2 * a, 0.0)["p90_gwh"]
    assert pooled == pytest.approx(parts)
    # Opposite poor years: pooling removes the spread, the pooled P90 is the mean.
    b = 200.0 - a
    assert V.summary_row("all", "", a + b, 0.0)["p90_gwh"] == pytest.approx(0.2)


def test_summary_row_and_names():
    years = pd.Series([1000.0, 1200.0, 1100.0], index=[2001, 2002, 2003])
    row = V.summary_row("Przykona", "3027062", years, 1050.0)
    assert row["worst"] == (2001, 1.0) and row["best"] == (2002, 1.2)
    assert row["average_gwh"] == 1.05 and row["p50_gwh"] == pytest.approx(1.1)
    assert V.gmina_name("3027011", {"name": "Turek"}, ["Turek", "Turek", "Dobra"]) == "Turek (town)"
    assert V.gmina_name("3027033", {"name": "Dobra"}, ["Turek", "Turek", "Dobra"]) == "Dobra"


def test_report_has_a_row_per_gmina_the_total_and_the_caveats():
    years = pd.Series([1000.0, 1200.0, 1100.0], index=[2001, 2002, 2003])
    rows = [V.summary_row("Dobra", "3027033", years, 1100.0), V.summary_row("All gminas", "", years, 1100.0)]
    md = V.render_markdown("3027", rows, years, 1.0, 1)
    assert "| Dobra | 3027033 |" in md and "| **All gminas** |" in md
    assert "correlated by construction" in md and "leaves out" in md
    assert "| 2001 | 2002 | 2003 |" in md
