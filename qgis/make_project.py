"""Build a styled QGIS project and a print-layout map for one gmina's screening.

Run with the Python that ships with QGIS, after `python -m pvscreen --teryt <code>`:

    Windows:      "C:\\Program Files\\QGIS 3.44.14\\bin\\python-qgis-ltr.bat" qgis/make_project.py 3027062
    Linux/macOS:  QT_QPA_PLATFORM=offscreen python3 qgis/make_project.py 3027062

Writes qgis/<teryt>.qgz (layer paths relative to it, so the project opens wherever
the repository is cloned once the pipeline has run) and docs/map_<teryt>.png.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

# No window is ever shown. Without a display (Linux CI) Qt needs the offscreen platform;
# on Windows that platform finds no system fonts and draws every label as boxes.
if sys.platform != "win32" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (  # noqa: E402  (QGIS must be importable first)
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsCentroidFillSymbolLayer,
    QgsColorRampLegendNodeSettings,
    QgsColorRampShader,
    QgsCoordinateReferenceSystem,
    QgsFillSymbol,
    QgsGraduatedSymbolRenderer,
    QgsHueSaturationFilter,
    QgsLayoutExporter,
    QgsLayoutItemLabel,
    QgsLayoutItemLegend,
    QgsLayoutItemMap,
    QgsLayoutItemPage,
    QgsLayoutItemPicture,
    QgsLayoutItemScaleBar,
    QgsLayoutPoint,
    QgsLayoutSize,
    QgsLineSymbol,
    QgsMapLayerLegendUtils,
    QgsMarkerSymbol,
    QgsPrintLayout,
    QgsProject,
    QgsRasterLayer,
    QgsRasterShader,
    QgsRendererCategory,
    QgsRendererRange,
    QgsSimpleFillSymbolLayer,
    QgsSingleBandPseudoColorRenderer,
    QgsSingleSymbolRenderer,
    QgsTextFormat,
    QgsUnitTypes,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import Qt  # noqa: E402
from qgis.PyQt.QtGui import QColor, QFont  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WORK_CRS = "EPSG:2180"

# Exclusion rules grouped into what a reader of the map cares about.
EXCLUSION_GROUPS = """CASE
  WHEN "rule" IN ('building', 'built_up') THEN 'Buildings and built-up land'
  WHEN "rule" LIKE 'road:%' THEN 'Roads'
  WHEN "rule" = 'water' OR "rule" LIKE 'waterway:%' THEN 'Water'
  WHEN "rule" = 'forest' THEN 'Forest'
  WHEN "rule" IN ('protected_landscape_area', 'landscape_park') THEN 'Protected landscape (constraint only)'
  ELSE 'Protected nature (excluded)'
END"""
GROUP_COLOURS = {
    "Buildings and built-up land": "#9e9e9e",
    "Roads": "#616161",
    "Water": "#64b5f6",
    "Forest": "#81c784",
    "Protected nature (excluded)": "#ba68c8",
}
SCORE_CLASSES = [  # candidate score, colour (YlOrBr: warm, so it never reads as forest)
    (0.0, 0.6, "#ffffd4"),
    (0.6, 0.7, "#fee391"),
    (0.7, 0.8, "#fec44f"),
    (0.8, 0.9, "#fe9929"),
    (0.9, 1.0, "#d95f0e"),
]


def vector(gpkg: Path, layer: str, name: str, geometry_type: str | None = None) -> QgsVectorLayer:
    uri = f"{gpkg}|layername={layer}" + (f"|geometrytype={geometry_type}" if geometry_type else "")
    lyr = QgsVectorLayer(uri, name, "ogr")
    if not lyr.isValid():
        raise SystemExit(f"cannot read layer {layer!r} from {gpkg}; run `python -m pvscreen` first")
    return lyr


def fill(colour: str, outline: str = "#00000000", alpha: int = 255, width: float = 0.1) -> QgsFillSymbol:
    c = QColor(colour)
    c.setAlpha(alpha)
    return QgsFillSymbol.createSimple(
        {"color": c.name(QColor.HexArgb), "outline_color": outline, "outline_width": str(width)}
    )


def style_exclusions(lyr: QgsVectorLayer) -> None:
    cats = [QgsRendererCategory(g, fill(c, alpha=150), g) for g, c in GROUP_COLOURS.items()]
    hatch = QgsFillSymbol()
    layer = QgsSimpleFillSymbolLayer(QColor("#6a1b9a"), Qt.BDiagPattern, QColor("#6a1b9a"))
    layer.setStrokeWidth(0.3)
    hatch.changeSymbolLayer(0, layer)
    cats.append(
        QgsRendererCategory(
            "Protected landscape (constraint only)", hatch, "Protected landscape (constraint only)"
        )
    )
    lyr.setRenderer(QgsCategorizedSymbolRenderer(EXCLUSION_GROUPS, cats))


def style_candidates(lyr: QgsVectorLayer) -> None:
    ranges = [
        QgsRendererRange(lo, hi, fill(c, outline="#8c2d04", alpha=210, width=0.15), f"{lo:.1f} – {hi:.1f}")
        for lo, hi, c in SCORE_CLASSES
    ]
    lyr.setRenderer(QgsGraduatedSymbolRenderer("score", ranges))


def style_slope(lyr: QgsRasterLayer, limit: float = 10.0) -> None:
    # Flat ground stays transparent so the basemap shows; slopes near and above the
    # screening limit turn red.
    ramp = QgsColorRampShader(0, 2 * limit)
    ramp.setColorRampType(QgsColorRampShader.Interpolated)
    legend = QgsColorRampLegendNodeSettings()
    legend.setUseContinuousLegend(False)  # list the labelled classes, not a gradient bar
    ramp.setLegendSettings(legend)
    ramp.setColorRampItemList(
        [
            QgsColorRampShader.ColorRampItem(0, QColor(255, 255, 255, 0), "0°"),
            QgsColorRampShader.ColorRampItem(limit / 2, QColor("#fdd49e"), f"{limit / 2:g}°"),
            QgsColorRampShader.ColorRampItem(limit, QColor("#ef6548"), f"{limit:g}° (screening limit)"),
            QgsColorRampShader.ColorRampItem(2 * limit, QColor("#7f0000"), f"≥ {2 * limit:g}°"),
        ]
    )
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)
    renderer = QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, shader)
    renderer.setClassificationMin(0)
    renderer.setClassificationMax(2 * limit)
    lyr.setRenderer(renderer)
    lyr.setOpacity(0.8)


def basemap() -> QgsRasterLayer:
    url = "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmin=0&zmax=19"
    lyr = QgsRasterLayer(url, "OpenStreetMap", "wms")
    lyr.hueSaturationFilter().setGrayscaleMode(QgsHueSaturationFilter.GrayscaleLuminosity)
    lyr.setOpacity(0.5)
    return lyr


def label(
    layout: QgsPrintLayout, text: str, x: float, y: float, size: float, bold: bool = False, width: float = 287
):
    item = QgsLayoutItemLabel(layout)
    item.setText(text)
    fmt = QgsTextFormat()
    font = QFont("Arial")
    font.setBold(bold)
    fmt.setFont(font)
    fmt.setSize(size)
    item.setTextFormat(fmt)
    item.attemptMove(QgsLayoutPoint(x, y))
    item.attemptResize(QgsLayoutSize(width, size * 0.6))
    layout.addLayoutItem(item)
    return item


def north_arrow_svg() -> str:
    for folder in QgsApplication.svgPaths():
        path = Path(folder) / "arrows" / "NorthArrow_02.svg"
        if path.exists():
            return str(path)
    raise SystemExit("QGIS north-arrow SVG not found")


def build(teryt: str) -> tuple[Path, Path]:
    gpkg = ROOT / "outputs" / teryt / "screening.gpkg"
    slope_tif = ROOT / "outputs" / teryt / "slope.tif"
    qgz = ROOT / "qgis" / f"{teryt}.qgz"
    png = ROOT / "docs" / f"map_{teryt}.png"

    project = QgsProject.instance()
    project.clear()
    project.setFileName(str(qgz))  # paths are stored relative to this file
    project.setCrs(QgsCoordinateReferenceSystem(WORK_CRS))

    boundary = vector(gpkg, "boundary", "Gmina boundary")
    boundary.setRenderer(QgsSingleSymbolRenderer(fill("#ffffff", outline="#212121", alpha=0, width=0.8)))
    name = next(boundary.getFeatures())["name"]

    lines = vector(gpkg, "grid", "Power lines (OSM)", "LineString")
    lines.setRenderer(
        QgsSingleSymbolRenderer(QgsLineSymbol.createSimple({"color": "#d32f2f", "width": "0.4"}))
    )

    substations = vector(gpkg, "grid", "Substations (OSM)", "Polygon")
    marker = QgsCentroidFillSymbolLayer()
    marker.setSubSymbol(
        QgsMarkerSymbol.createSimple(
            {"name": "square", "color": "#d32f2f", "outline_color": "#ffffff", "size": "2.6"}
        )
    )
    sub_symbol = QgsFillSymbol()
    sub_symbol.changeSymbolLayer(0, marker)
    substations.setRenderer(QgsSingleSymbolRenderer(sub_symbol))

    layers = [boundary, lines, substations]
    try:
        farms = vector(gpkg, "farms", "Existing solar farms (OSM)")
        farms.setRenderer(QgsSingleSymbolRenderer(fill("#ffffff", outline="#000000", alpha=0, width=0.5)))
        layers.append(farms)
    except SystemExit:
        pass  # no farms mapped in this gmina

    candidates = vector(gpkg, "candidates", "Candidate areas by score")
    style_candidates(candidates)
    exclusions = vector(gpkg, "exclusions", "Exclusions by reason")
    style_exclusions(exclusions)
    slope = QgsRasterLayer(str(slope_tif), "Slope")
    if not slope.isValid():
        raise SystemExit(f"cannot read {slope_tif}; run `python -m pvscreen` first")
    style_slope(slope)
    osm = basemap()
    layers += [candidates, exclusions, slope, osm]

    for lyr in reversed(layers):  # first in the list ends up on top
        project.addMapLayer(lyr)

    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(f"Map {teryt}")
    layout.pageCollection().page(0).setPageSize("A4", QgsLayoutItemPage.Landscape)

    label(layout, f"Solar-farm screening: gmina {name} (TERYT {teryt})", 10, 7, 16, bold=True)
    map_item = QgsLayoutItemMap(layout)
    map_item.attemptMove(QgsLayoutPoint(10, 20))
    map_item.attemptResize(QgsLayoutSize(195, 170))
    map_item.setCrs(QgsCoordinateReferenceSystem(WORK_CRS))
    extent = boundary.extent()
    extent.grow(0.04 * max(extent.width(), extent.height()))
    map_item.zoomToExtent(extent)
    map_item.setFrameEnabled(True)
    layout.addLayoutItem(map_item)

    legend = QgsLayoutItemLegend(layout)
    legend.setTitle("Legend")
    legend.setLinkedMap(map_item)
    legend.setAutoUpdateModel(False)
    legend.model().rootGroup().removeLayer(osm)
    # The slope legend starts with the band name and a transparent 0° swatch; keep the classes.
    slope_node = legend.model().rootGroup().findLayer(slope)
    nodes = legend.model().layerLegendNodes(slope_node)
    keep = [i for i, n in enumerate(nodes) if n.data(Qt.DisplayRole) not in ("Band 1 (Gray)", "0°")]
    QgsMapLayerLegendUtils.setLegendNodeOrder(slope_node, keep)
    legend.model().refreshLayerLegend(slope_node)
    legend.attemptMove(QgsLayoutPoint(210, 20))
    legend.setResizeToContents(True)
    layout.addLayoutItem(legend)

    scalebar = QgsLayoutItemScaleBar(layout)
    scalebar.setStyle("Single Box")
    scalebar.setLinkedMap(map_item)
    scalebar.setUnits(QgsUnitTypes.DistanceKilometers)
    scalebar.setUnitLabel("km")
    scalebar.setNumberOfSegmentsLeft(0)
    scalebar.setNumberOfSegments(4)
    scalebar.setUnitsPerSegment(1)
    scalebar.attemptMove(QgsLayoutPoint(14, 176))
    layout.addLayoutItem(scalebar)

    arrow = QgsLayoutItemPicture(layout)
    arrow.setPicturePath(north_arrow_svg())
    arrow.setLinkedMap(map_item)
    arrow.attemptMove(QgsLayoutPoint(190, 23))
    arrow.attemptResize(QgsLayoutSize(12, 12))
    layout.addLayoutItem(arrow)

    label(
        layout,
        "Data: boundary PRG (GUGiK); terrain NMT (GUGiK); protected areas GDOŚ; buildings, roads, water, "
        "forest, power grid and solar farms © OpenStreetMap contributors (ODbL); basemap © OpenStreetMap "
        f"contributors. PL-1992 (EPSG:2180). Generated by pvscreen on {date.today().isoformat()}. "
        "A first-pass screening from open data, not a site assessment.",
        10,
        193,
        7,
        width=277,
    )
    project.layoutManager().addLayout(layout)

    png.parent.mkdir(parents=True, exist_ok=True)
    settings = QgsLayoutExporter.ImageExportSettings()
    settings.dpi = 150
    settings.generateWorldFile = False
    settings.exportMetadata = False
    status = QgsLayoutExporter(layout).exportToImage(str(png), settings)
    if status != QgsLayoutExporter.Success:
        raise SystemExit(f"map export failed (status {status})")
    if not project.write():
        raise SystemExit(f"could not write {qgz}")
    return qgz, png


def main() -> None:
    teryt = sys.argv[1] if len(sys.argv) > 1 else "3027062"
    app = QgsApplication([], False)
    app.initQgis()
    try:
        qgz, png = build(teryt)
        print(f"wrote {qgz.relative_to(ROOT)} and {png.relative_to(ROOT)}")
    finally:
        app.exitQgis()


if __name__ == "__main__":
    main()
