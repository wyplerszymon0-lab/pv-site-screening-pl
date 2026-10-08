import math

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from pvscreen import terrain

RES = 5.0


def plane(slope_deg, facing_deg, n=20, width=None):
    """Elevation of a plane that slopes down towards compass direction facing_deg."""
    rows, cols = np.mgrid[0:n, 0 : width or n]
    east, north = cols * RES, -rows * RES  # row 0 is the northern edge
    rise = math.tan(math.radians(slope_deg))
    down_e, down_n = math.sin(math.radians(facing_deg)), math.cos(math.radians(facing_deg))
    return 100 - rise * (east * down_e + north * down_n)


def interior(a):
    return a[1:-1, 1:-1]


def test_flat_ground_has_no_slope_and_no_aspect():
    slope, aspect = terrain.slope_aspect(np.full((10, 10), 115.0), RES)
    assert np.allclose(interior(slope), 0)
    assert np.isnan(interior(aspect)).all()


@pytest.mark.parametrize(
    "slope_deg, facing_deg",
    [(10, 180), (45, 90), (20, 270), (5, 135), (30, 0.0)],  # south, east, west, south-east, north
)
def test_planes_give_their_slope_and_aspect(slope_deg, facing_deg):
    slope, aspect = terrain.slope_aspect(plane(slope_deg, facing_deg), RES)
    assert np.allclose(interior(slope), slope_deg)
    # compare angles on the circle so that north (0 / 360) works
    diff = (interior(aspect) - facing_deg + 180) % 360 - 180
    assert np.allclose(diff, 0, atol=1e-6)


def test_edges_and_neighbours_of_missing_cells_are_nan():
    dem = plane(10, 180)
    dem[10, 10] = np.nan
    slope, _ = terrain.slope_aspect(dem, RES)
    assert np.isnan(slope[0]).all() and np.isnan(slope[:, -1]).all()
    assert np.isnan(slope[9:12, 9:12]).all()  # incl. the missing cell, which Horn does not read
    assert not np.isnan(slope[5, 5])


def test_tile_grid_is_aligned_and_covers_the_bounds():
    tiles = terrain.tile_grid((468565.3, 452823.9, 482254.1, 464951.0))
    assert all(t[0] % 2000 == 0 and t[1] % 2000 == 0 and t[2] - t[0] == 2000 for t in tiles)
    xs, ys = {t[0] for t in tiles}, {t[1] for t in tiles}
    assert min(xs) == 468000 and max(xs) + 2000 == 484000
    assert min(ys) == 452000 and max(ys) + 2000 == 466000
    assert len(tiles) == len(xs) * len(ys) == 8 * 7


def test_coverage_request_asks_the_server_to_resample():
    params = terrain.coverage_params((470000, 454000, 472000, 456000), res_m=5)
    assert params["COVERAGEID"] == "DTM_PL-KRON86-NH_TIFF"
    assert params["SUBSET"] == ["x(470000,472000)", "y(454000,456000)"]
    assert params["SCALEFACTOR"] == "0.2"


class NoNetwork:
    def get(self, *args, **kwargs):
        raise AssertionError("tiles should come from the cache")


def write_tile(cache, x0, y0, values):
    path = cache / f"nmt_{x0}_{y0}_5m.tif"
    profile = {
        "driver": "GTiff",
        "height": 400,
        "width": 400,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:2180",
        "transform": from_origin(x0, y0 + 2000, RES, RES),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values.astype("float32"), 1)


def test_cached_tiles_mosaic_and_zero_voids_become_nan(tmp_path):
    # Two tiles stacked north-south, one continuous south-facing plane across both.
    full = plane(2, 180, n=800, width=400)
    north, south = full[:400].copy(), full[400:].copy()
    south[100, 100] = 0.0  # a void as the service returns it
    write_tile(tmp_path, 470000, 456000, north)
    write_tile(tmp_path, 470000, 454000, south)
    gmina = gpd.GeoDataFrame(
        {"teryt": ["0000002"]}, geometry=[box(470100, 455100, 471900, 456900)], crs="EPSG:2180"
    )

    dem, transform = terrain.fetch_dem(gmina, cache_dir=tmp_path, session=NoNetwork())

    assert dem.shape == (800, 400)
    assert (transform.c, transform.f) == (470000, 458000)
    assert np.isnan(dem[500, 100]) and np.isnan(dem).sum() == 1
    slope, aspect = terrain.slope_aspect(dem, RES)
    seam = slope[398:402, 1:-1]  # across the tile boundary
    assert np.allclose(seam, 2)
    assert np.allclose(aspect[398:402, 1:-1], 180)


def test_mask_and_summary_count_only_cells_inside_the_gmina():
    transform = from_origin(0, 100, 10, 10)  # 10 x 10 cells of 10 m
    gmina = gpd.GeoDataFrame(geometry=[box(0, 0, 50, 100)], crs="EPSG:2180")  # western half
    inside = terrain.gmina_mask(gmina, (10, 10), transform)
    assert inside.sum() == 50 and inside[:, :5].all()
    slope = np.where(np.arange(10) < 3, 2.0, 8.0)[None, :].repeat(10, axis=0)
    summary = terrain.summarize(slope, inside)
    assert summary["cells"] == 50
    assert summary["share_under_5_deg"] == pytest.approx(0.6)
    assert summary["median_slope_deg"] == 2.0


class FlakySession:
    """Fails like the real service did, then answers."""

    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def get(self, url, params=None, headers=None, timeout=None):
        import requests

        self.calls += 1
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure

            class Busy:
                status_code = failure

                def raise_for_status(self):
                    raise requests.HTTPError(str(failure))

            return Busy()

        class Ok:
            status_code = 200
            content = b"II*\x00" + b"\x00" * 16

            def raise_for_status(self):
                pass

        return Ok()


def test_tile_download_retries_dropped_connections_and_5xx(tmp_path):
    import requests

    session = FlakySession([requests.exceptions.SSLError("wrong version number"), 503])
    path = terrain._fetch_tile((470000, 454000, 472000, 456000), 5.0, tmp_path, session, waits=(0, 0))
    assert session.calls == 3 and path.read_bytes().startswith(b"II*\x00")


def test_tile_download_gives_up_after_the_last_retry(tmp_path):
    import requests

    session = FlakySession([requests.ConnectionError("down")] * 3)
    with pytest.raises(requests.ConnectionError):
        terrain._fetch_tile((470000, 454000, 472000, 456000), 5.0, tmp_path, session, waits=(0, 0))
    assert session.calls == 3 and not list(tmp_path.iterdir())
