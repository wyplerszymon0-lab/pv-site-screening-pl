import shutil
from pathlib import Path

import pytest
from pyproj import Transformer

from pvscreen import boundary

FIXTURE = Path(__file__).parent / "fixtures" / "prg_gmina_3027062.gml"  # Przykona, recorded 2026-10-03


class FakeSession:
    """Stands in for requests.Session: records calls, never touches the network."""

    def __init__(self, content=b""):
        self.content = content
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        session = self

        class Response:
            content = session.content

            def raise_for_status(self):
                pass

        return Response()


@pytest.mark.parametrize("code", ["3027062", " 0608022 ", "1465011", "0212033"])
def test_valid_teryt_codes(code):
    assert boundary.validate_teryt(code) == code.strip()


@pytest.mark.parametrize("code", ["302706", "30270621", "302706X", "3027064", "3027065", ""])
def test_invalid_teryt_codes(code):
    with pytest.raises(ValueError):
        boundary.validate_teryt(code)


def test_request_filters_the_gmina_layer_by_teryt():
    params = boundary.gmina_request_params("3027062")
    assert params["TYPENAMES"] == "ms:A03_Granice_gmin"
    assert params["REQUEST"] == "GetFeature"
    assert "<fes:ValueReference>JPT_KOD_JE</fes:ValueReference>" in params["FILTER"]
    assert "<fes:Literal>3027062</fes:Literal>" in params["FILTER"]


def test_read_recorded_response():
    gmina = boundary.read_gmina(FIXTURE)
    row = gmina.iloc[0]
    assert len(gmina) == 1
    assert gmina.crs.to_epsg() == 2180
    assert (row.teryt, row["name"], row.official_area_ha) == ("3027062", "Przykona", 11095.0)
    assert boundary.area_difference(gmina) < 0.01


def test_axis_order_puts_the_gmina_where_it_is():
    # urn:ogc:def:crs:EPSG::2180 lists northing first; a swapped read would land
    # hundreds of kilometres away. Przykona lies near 52.0° N, 18.6° E.
    centroid = boundary.read_gmina(FIXTURE).geometry.centroid.iloc[0]
    lon, lat = Transformer.from_crs(2180, 4326, always_xy=True).transform(centroid.x, centroid.y)
    assert 51.9 < lat < 52.1
    assert 18.5 < lon < 18.8


def test_cached_boundary_is_read_without_network(tmp_path):
    shutil.copy(FIXTURE, tmp_path / "gmina_3027062.gml")
    session = FakeSession()
    gmina = boundary.fetch_gmina("3027062", cache_dir=tmp_path, session=session)
    assert session.calls == []
    assert gmina.iloc[0]["name"] == "Przykona"


def test_missing_cache_downloads_once_and_saves(tmp_path):
    session = FakeSession(FIXTURE.read_bytes())
    boundary.fetch_gmina("3027062", cache_dir=tmp_path, session=session)
    boundary.fetch_gmina("3027062", cache_dir=tmp_path, session=session)
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == boundary.PRG_WFS_URL
    assert "pv-site-screening-pl" in session.calls[0]["headers"]["User-Agent"]
    assert (tmp_path / "gmina_3027062.gml").exists()


def test_unknown_teryt_raises_and_caches_nothing(tmp_path):
    empty = b'<wfs:FeatureCollection numberMatched="0" numberReturned="0"></wfs:FeatureCollection>'
    with pytest.raises(LookupError, match="3027992"):
        boundary.fetch_gmina("3027992", cache_dir=tmp_path, session=FakeSession(empty))
    assert not list(tmp_path.iterdir())
