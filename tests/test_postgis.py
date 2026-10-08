import geopandas as gpd
import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import box

from pvscreen import postgis
from pvscreen.suitability import Inputs, load_config

CFG = load_config()


def gdf(*geoms):
    return gpd.GeoDataFrame(geometry=list(geoms), crs="EPSG:2180")


def test_terrain_ok_keeps_gentle_cells_inside_the_gmina():
    slope = np.full((40, 40), 2.0)
    slope[:, 30:] = 20.0  # too steep
    slope[0, 0] = np.nan  # no data
    inside = np.ones_like(slope, dtype=bool)
    inside[35:, :] = False
    inputs = Inputs(slope, slope, from_origin(0, 200, 5, 5), inside, [], gdf(), gdf())
    ok = postgis.terrain_ok_polygons(inputs, CFG).union_all()
    # 30 x 35 cells of 25 m², minus the missing one
    assert ok.area == pytest.approx((30 * 35 - 1) * 25)
    assert ok.bounds == (0, 25, 150, 200)


def test_compare_reports_area_and_symmetric_difference():
    sql = gdf(box(0, 0, 100, 100))
    py = gdf(box(0, 0, 100, 90), box(0, 95, 100, 100))
    out = postgis.compare(sql, py)
    assert (out["candidates_sql"], out["candidates_python"]) == (1, 2)
    assert out["area_difference"] == pytest.approx(500 / 9500)
    assert out["symmetric_difference_share"] == pytest.approx(500 / 10_000)


def test_main_query_names_the_postgis_steps():
    sql = postgis.CANDIDATES_SQL
    for step in ("ST_Union", "ST_Difference", "ST_Buffer", "ST_Dump", "ST_Area", "ST_Intersects"):
        assert step in sql


# ── against a real PostGIS (docker compose locally, a service container in CI) ──


@pytest.fixture(scope="module")
def engine():
    sqlalchemy = pytest.importorskip("sqlalchemy")
    eng = sqlalchemy.create_engine(postgis.database_url())
    try:
        with eng.connect() as conn:
            conn.execute(sqlalchemy.text("SELECT PostGIS_Version()"))
    except Exception as exc:  # noqa: BLE001 - any connection problem means: no database here
        pytest.skip(f"no PostGIS at {postgis.database_url()}: {type(exc).__name__}")
    return eng


def test_overlay_subtracts_opens_and_filters(engine):
    terrain = gdf(box(0, 0, 400, 400), box(400, 190, 600, 210), box(1000, 0, 1100, 100))
    exclusions = gdf(box(-10, 180, 410, 220))  # a 40 m wide road across the big square
    postgis.load_layers(engine, terrain, exclusions)
    out = postgis.run_overlay(engine, CFG)

    # Two 400 m x 180 m halves remain. The 20 m strip east of the road is
    # thinner than 2 x 15 m and the 1 ha square is below 2 ha; both are dropped.
    assert sorted(out["area_ha"].round(4)) == [7.2, 7.2]
    assert out.crs.to_epsg() == 2180
    assert all(g.within(box(0, 0, 400, 400)) for g in out.geometry)


def test_overlay_without_exclusions_keeps_the_terrain(engine):
    postgis.load_layers(engine, gdf(box(0, 0, 300, 300)), gdf(box(5000, 5000, 5100, 5100)))
    out = postgis.run_overlay(engine, CFG)
    assert list(out["area_ha"].round(4)) == [9.0]


def test_tables_have_spatial_indexes(engine):
    from sqlalchemy import text

    postgis.load_layers(engine, gdf(box(0, 0, 300, 300)), gdf(box(0, 0, 10, 10)))
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT tablename, indexdef FROM pg_indexes WHERE tablename IN ('terrain_ok', 'exclusions')")
        ).all()
    gist = {t for t, d in rows if "USING gist" in d}
    assert gist == {"terrain_ok", "exclusions"}
