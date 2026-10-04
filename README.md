# PV Site Screening — Poland

**Status: work in progress.** The pipeline below is the plan; each step is tracked as an [issue](https://github.com/wyplerszymon0-lab/pv-site-screening-pl/issues). No results are published until the code that produces them is in the repository.

Where in a Polish municipality (*gmina*) could a ground-mounted solar farm go? This project answers that first-pass question with **open public data only**, the way a GIS analyst would before any site visit:

1. take the municipality boundary from the national register of boundaries,
2. derive slope and aspect from the national terrain model,
3. exclude protected nature areas, buildings, roads, water and forest,
4. score what is left by terrain, sun exposure and distance to the power grid,
5. return candidate areas as polygons with an estimated energy yield,
6. check the result against solar farms that already exist.

It follows on from [Solar Site Intelligence](https://github.com/wyplerszymon0-lab/Solar-site-intelligence), which assesses one surveyed site; this one searches a whole municipality.

## Demo municipality: Przykona (TERYT 3027062)

A rural gmina in Turek County, Greater Poland. Its land includes the former *Adamów* lignite opencast mine, and OpenStreetMap maps 26 solar farms there, 25 of them of at least 1 ha, about 413 ha in total (Overpass query, 3 Oct 2026). That makes it a good test: the screening can later be checked against where farms were actually built (issue #6). Any other gmina works by passing its TERYT code.

## Usage

```bash
pip install -e ".[dev]"
pytest
```

```python
from pvscreen.boundary import fetch_gmina, area_difference

gmina = fetch_gmina("3027062")    # PRG WFS, cached under data/prg/
gmina.crs                          # EPSG:2180
area_difference(gmina)             # 0.0014: 11 079 ha measured vs 11 095 ha official
```

```bash
python -m pvscreen.terrain 3027062   # writes outputs/dem_, slope_, aspect_3027062_5m.tif
```

## Results so far

### Terrain (issue #2)

The NMT is requested in 2 × 2 km tiles with `SCALEFACTOR=0.2`, so GUGiK's server returns a 5 m grid directly (about 640 kB per tile instead of 16 MB at 1 m; 56 tiles cover Przykona). Slope and aspect use Horn's 3 × 3 method.

| Przykona (inside the boundary, 5 m cells) | |
|---|---:|
| Elevation | 58.7 – 138.9 m |
| Median slope | 0.68° |
| 95th percentile slope | 3.8° |
| Area with slope under 5° | 96.1 % |

Two things the checks caught:

- **Voids.** The service declares no nodata value and returns gaps as exactly 0 m (1 256 cells here, where the ground is 60–140 m). They are treated as missing, which would be wrong only for land at sea level (Żuławy, the coast).
- **Missing cells keep no slope.** Horn's kernel does not read the centre cell, so a gap would still get a slope from its neighbours; it is masked explicitly. Before the void fix, rows along two tile boundaries showed twice the average slope; after it, tile-boundary rows are no different from the rest (permutation test, p = 0.20).

## Data sources

| Data | Source | Access |
|---|---|---|
| Municipality boundaries (PRG) | [GUGiK](https://www.geoportal.gov.pl/) | WFS `PZGIK/PRG/WFS/AdministrativeBoundaries` |
| Digital terrain model (NMT, 1 m grid) | GUGiK | WCS `PZGIK/NMT/GRID1/WCS/DigitalTerrainModelFormatTIFF` |
| Natura 2000, national and landscape parks, reserves | [GDOŚ](https://sdi.gdos.gov.pl/) | WFS `sdi.gdos.gov.pl/wfs` |
| Buildings, roads, water, forest, power lines, existing solar farms | © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors | Overpass API |
| Solar irradiance and temperature | [NASA POWER](https://power.larc.nasa.gov/) | REST API |

All processing happens in **PL-1992 (EPSG:2180)**; final outputs are also offered in the matching **PL-2000** zone (EPSG:2176–2179).

## Planned stack

Python · GeoPandas · Shapely · rasterio · pyproj · PostGIS · QGIS

## Licence

Code: MIT. Data stays under its providers' terms (OpenStreetMap: ODbL).
