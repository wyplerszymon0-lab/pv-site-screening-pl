import zipfile

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, box

from pvscreen import bdot10k, compare_sources, osm

CRS = "EPSG:2180"


@pytest.mark.parametrize(
    "cls,attrs,key",
    [
        ("BUBD_A", {"KAT_ISTNIE": "eksploatowany"}, "building"),
        ("BUBD_A", {"KAT_ISTNIE": "zniszczony"}, None),
        ("PTZB_A", {"RODZAJ": "jednorodzinna"}, "built_up"),
        ("PTZB_A", {"RODZAJ": "przemysłowo-składowa"}, None),  # OSM industrial land is not excluded either
        ("KUSC_A", {"RODZAJ": "cmentarz komunalny"}, "built_up"),
        ("KUSC_A", {"RODZAJ": "zespół sakralny lub klasztorny"}, None),
        ("PTUT_A", {"RODZAJ": "ogródki działkowe"}, "built_up"),
        ("PTUT_A", {"RODZAJ": "sad"}, None),
        ("SKDR_L", {"KAT_ZARZAD": "krajowa", "KLASA_DROG": "droga główna"}, "road:primary"),
        ("SKDR_L", {"KAT_ZARZAD": "krajowa", "KLASA_DROG": "autostrada"}, "road:motorway"),
        ("SKDR_L", {"KAT_ZARZAD": "gminna", "KLASA_DROG": "droga lokalna"}, "road:unclassified"),
        ("SKDR_L", {"KAT_ZARZAD": "wewnętrzna", "KLASA_DROG": "droga wewnętrzna"}, None),
        ("SWRS_L", {"RODZAJ": "rzeka"}, "waterway:river"),
        ("SWRS_L", {"RODZAJ": "strumień, potok lub struga"}, "waterway:stream"),
        ("PTLZ_A", {"RODZAJ": "zagajnik"}, "forest"),
        ("SULN_L", {"RODZAJ": "linia elektroenergetyczna"}, None),
    ],
)
def test_classes_map_to_the_osm_rule_keys(cls, attrs, key):
    assert bdot10k.classify(cls, pd.Series(attrs)) == key
    assert key is None or key in osm.RULES


def test_package_layers_are_read_as_pl1992_from_windows_1250(tmp_path):
    # GUGiK's packages: no EPSG in the .prj and attribute text in Windows-1250.
    shp_dir = tmp_path / "shp"
    shp_dir.mkdir()
    gdf = gpd.GeoDataFrame(
        {"LOKALNYID": ["a", "b"], "RODZAJ": ["handlowo-usługowa", "jednorodzinna"]},
        geometry=[box(450_000, 460_000, 450_100, 460_100), box(470_000, 470_000, 470_100, 470_100)],
    )
    name = "PL.PZGiK.308.BDOT10k.3027__OT_PTZB_A"
    gdf.to_file(shp_dir / f"{name}.shp", encoding="cp1250")
    package = tmp_path / "3027_SHP.zip"
    with zipfile.ZipFile(package, "w") as zf:
        for f in shp_dir.iterdir():
            if f.suffix != ".prj":
                zf.write(f, f.name)

    layer = bdot10k.read_class(package, "PTZB_A", bbox=(449_000, 459_000, 451_000, 461_000))
    assert layer.crs.to_epsg() == 2180
    assert list(layer["RODZAJ"]) == ["handlowo-usługowa"]  # the box keeps the first one only
    assert bdot10k.read_class(package, "BUBD_A").empty  # a class missing from the package


def test_missing_buildings_count_only_those_outside_the_other_zone():
    other = gpd.GeoDataFrame(geometry=[box(0, 0, 10, 10)], crs=CRS)
    mine = gpd.GeoDataFrame(
        geometry=[box(2, 2, 8, 8), box(30, 0, 36, 6), box(200, 200, 206, 206)],  # same, 20 m away, far
        crs=CRS,
    )
    assert compare_sources.missing_buildings(mine, other, zone_m=50) == {
        "buildings": 3,
        "absent_from_other": 2,
        "outside_other_zone": 1,
    }


def test_feature_table_takes_the_union_of_overlapping_rules():
    def zones(*rows):
        return gpd.GeoDataFrame(
            [{"rule": r, "feature": f, "geometry": g} for r, f, g in rows], geometry="geometry", crs=CRS
        )

    a = zones(
        ("road:primary", "road", box(0, 0, 100, 100)),  # 1 ha
        ("road:residential", "road", box(50, 0, 150, 100)),  # overlaps half of it
    )
    b = zones(
        ("road:unclassified", "road", box(0, 0, 100, 100)),
        ("forest", "forest", LineString([(0, 0), (1, 1)]).buffer(100)),
    )
    t = compare_sources.feature_table({"osm": a, "bdot10k": b}).set_index("feature")
    assert t.loc["road", "osm"] == pytest.approx(1.5)  # not 2.0
    assert t.loc["road", "bdot10k"] == pytest.approx(1.0)
    assert t.loc["forest", "osm"] == 0.0
    assert list(t.index)[-1] == "all (union)"
