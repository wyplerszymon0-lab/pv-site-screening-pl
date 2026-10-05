import json
from pathlib import Path

import geopandas as gpd
import pytest
from pyproj import Transformer
from shapely.geometry import box

from pvscreen import energy as E

FIXTURES = Path(__file__).parent / "fixtures" / "nasa_power"  # same files as Solar Site Intelligence


def clim(name):
    return E.parse_power_climatology(json.loads((FIXTURES / f"nasa-power-{name}.json").read_text()))


# Annual yield from the JavaScript model (Solar Site Intelligence, solar-model.js,
# 2026-10-05) for the same inputs. The port must reproduce it.
JS_MODEL = [
    ("lisbon", 38.72, 35, 180, {"system_loss": 0.14, "iam_b0": 0.05}, 1527.832688418208),
    ("warsaw", 52.23, 47, 180, {"system_loss": 0.14, "iam_b0": 0.05}, 973.7534665957894),
    ("oslo", 59.91, 54, 180, {"system_loss": 0.14, "iam_b0": 0.05}, 943.4923909204763),
    ("cairo", 30.04, 27, 180, {"system_loss": 0.14, "iam_b0": 0.05}, 1708.6091912331017),
    ("sydney", -33.87, 30, 0, {"system_loss": 0.14, "iam_b0": 0.05}, 1463.4292618095806),
    ("lisbon", 38.72, 35, 270, {"system_loss": 0.14, "iam_b0": 0.05}, 1316.9308397361362),
    ("lisbon", 38.72, 15, 0, {"system_loss": 0.14, "iam_b0": 0.05}, 1183.7973200677936),
    ("lisbon", 38.72, 0, 180, {"system_loss": 0.14, "iam_b0": 0.05}, 1363.0404476439546),
    ("warsaw", 52.23, 25, 135, {"system_loss": 0.08, "iam_b0": 0.05}, 1010.1082237447594),
    ("warsaw", 52.23, 35, 180, {"system_loss": 0.14, "iam_b0": 0}, 1022.2586215626917),
]

# PVGIS 5.3 annual yield, kWh/kWp, 14 % system loss (retrieved 2026-09-26, as in
# Solar Site Intelligence). The model is not fitted to it.
PVGIS = [
    ("lisbon", 38.72, 35, 180, 1578.69),
    ("warsaw", 52.23, 47, 180, 1045.02),
    ("oslo", 59.91, 54, 180, 948.39),
    ("cairo", 30.04, 27, 180, 1818.74),
    ("sydney", -33.87, 30, 0, 1556.69),
]


@pytest.mark.parametrize("name, lat, tilt, az, opts, expected", JS_MODEL)
def test_port_matches_the_javascript_model(name, lat, tilt, az, opts, expected):
    got = E.monthly_yield(clim(name), lat, tilt, az, E.ModelOptions(**opts))["annual"]
    assert got == pytest.approx(expected, rel=1e-9)


@pytest.mark.parametrize("name, lat, tilt, az, pvgis", PVGIS)
def test_annual_yield_is_within_7_percent_of_pvgis(name, lat, tilt, az, pvgis):
    got = E.monthly_yield(clim(name), lat, tilt, az)["annual"]
    assert abs(got - pvgis) / pvgis < 0.07


def test_beam_factor_geometry():
    for n in (17, 162, 344):
        assert E.beam_tilt_factor(45, 0, 180, n) == pytest.approx(1, abs=1e-9)  # horizontal
    assert E.beam_tilt_factor(40, 30, 90, 105) == pytest.approx(
        E.beam_tilt_factor(40, 30, 270, 105), abs=1e-6
    )
    north, south = E.beam_tilt_factor(35, 30, 180, 17), E.beam_tilt_factor(-35, 30, 0, 198)
    assert abs(north - south) / north < 0.01  # hemispheres mirror six months apart
    assert E.beam_tilt_factor(50, 40, 180, 344) > 2 > 0.2 > E.beam_tilt_factor(50, 40, 0, 344)


def test_monthly_values_add_up_and_peak_in_summer():
    y = E.monthly_yield(clim("warsaw"), 52.23, 35, 180)
    assert sum(y["monthly"]) == pytest.approx(y["annual"])
    assert max(range(12), key=lambda m: y["monthly"][m]) in (4, 5, 6, 7)  # May-August
    assert y["poa_annual"] > y["annual"]


def test_optimal_tilt_for_warsaw():
    tilt, kwh = E.optimal_tilt(clim("warsaw"), 52.23)
    assert tilt == 36  # the JavaScript tilt curve gives the same
    assert kwh == pytest.approx(986.4113872830163, rel=1e-9)


def test_incomplete_climatology_is_rejected():
    payload = json.loads((FIXTURES / "nasa-power-warsaw.json").read_text())
    payload["properties"]["parameter"]["T2M"]["MAR"] = -999.0  # NASA's fill value
    with pytest.raises(ValueError, match="incomplete"):
        E.parse_power_climatology(payload)


class FakeSession:
    def __init__(self, text):
        self.text, self.calls = text, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(params)
        session = self

        class Resp:
            text = session.text

            def raise_for_status(self):
                pass

            def json(self):
                return json.loads(session.text)

        return Resp()


def test_climatology_is_cached_per_tenth_of_a_degree(tmp_path):
    session = FakeSession((FIXTURES / "nasa-power-warsaw.json").read_text())
    a = E.fetch_climatology(52.2312, 21.0061, tmp_path, session)
    b = E.fetch_climatology(52.2449, 20.9733, tmp_path, session)  # the same 0.1° cell
    assert a == b and len(session.calls) == 1
    assert session.calls[0]["latitude"] == "52.2000" and session.calls[0]["longitude"] == "21.0000"
    assert (tmp_path / "power_52.2_21.0.json").exists()


def test_add_energy_turns_hectares_into_megawatt_hours(tmp_path):
    (tmp_path / "power_52.2_21.0.json").write_text((FIXTURES / "nasa-power-warsaw.json").read_text())
    x, y = Transformer.from_crs(4326, 2180, always_xy=True).transform(21.0, 52.2)
    cands = gpd.GeoDataFrame(
        {"area_ha": [4.0]}, geometry=[box(x - 100, y - 100, x + 100, y + 100)], crs="EPSG:2180"
    )
    out = E.add_energy(cands, mwp_per_ha=1.0, cache_dir=tmp_path, session=FakeSession("{}"))
    row = out.iloc[0]
    assert row.tilt_deg == 36
    assert row.capacity_mwp == 4.0
    assert row.energy_mwh_year == pytest.approx(4.0 * row.yield_kwh_per_kwp)
    assert row.yield_kwh_per_kwp == pytest.approx(E.optimal_tilt(clim("warsaw"), 52.2)[1], rel=1e-6)
