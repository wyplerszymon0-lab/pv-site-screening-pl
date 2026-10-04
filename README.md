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
python -m pvscreen.protected 3027062 # writes outputs/protected_3027062.gpkg and prints areas
python -m pvscreen.osm 3027062       # writes outputs/osm_3027062.gpkg (exclusions, grid)
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

### Protected nature areas (issue #3)

Layers from the GDOŚ WFS, clipped to the gmina. Each is a hard exclusion or a constraint to report, with its legal basis in the Nature Conservation Act of 16 April 2004 stored next to every polygon:

| Layer | Treated as | Basis |
|---|---|---|
| National parks, nature reserves | exclude | art. 15: building banned |
| Natura 2000 (habitats and birds) | exclude | art. 33: projects that may significantly harm a site are banned; whether a PV farm would needs an appropriate assessment, which a screening cannot predict |
| Ecological sites, nature and landscape complexes | exclude | art. 45: transforming them banned |
| Landscape parks | constraint | art. 17: bans set per park |
| Protected landscape areas | constraint | art. 24: bans set by the voivodeship; PV often allowed with conditions |

| Przykona | Area | Share of gmina |
|---|---:|---:|
| Natura 2000 bird area *Dolina Środkowej Warty* (exclude) | 60.1 ha | 0.5 % |
| Protected landscape area *Uniejowski* (constraint) | 2 745.2 ha | 24.8 % |

No national or landscape park, reserve, habitat site or ecological site lies in the gmina. A 0.02 ha strip of the *Nadwarciański* landscape area along the border is dropped: it is where the GDOŚ and PRG boundaries disagree (pieces under 0.1 ha are ignored).

GDOŚ writes GeoJSON coordinates northing first (EPSG:2180's official axis order) although GeoJSON must be x, y, so responses are read as GML, whose axis order GDAL handles. Every layer is also checked to intersect the requested box, which would fail if the axis order were read wrongly.

### Buildings, roads, water, forest and the grid (issue #4)

One Overpass query per gmina (bounding box plus 100 m, so buffers around features just across the border still count), cached as JSON. Each feature type is buffered and dissolved; the distance and its basis are stored with every zone. Values from the law are marked as such; the rest are screening assumptions and live in one table in [`pvscreen/osm.py`](pvscreen/osm.py).

| Feature (OSM) | Buffer | Basis |
|---|---:|---|
| Motorway / expressway (`motorway`, `trunk`) | 62 / 52 m | Public Roads Act art. 43: 50 / 40 m from the carriageway outside built-up areas, + 12 m from the centreline |
| National / voivodeship / county road | 28 / 23 / 23 m | art. 43: 25 / 20 / 20 m, + 3 m |
| Municipal road (`unclassified`, `residential`) | 18 m | art. 43: 15 m, + 3 m |
| Railway | 20 m | Railway Transport Act art. 53: at least 20 m from the outer track axis |
| Any building | 50 m | assumption: no national rule for PV |
| Residential, commercial, cemetery, allotment land | 0 m | assumption |
| Water bodies | 0 m | — |
| Rivers, canals / streams | 10 / 5 m | assumption |
| Forest, wood | 15 m | assumption: tree-edge shading |

Power lines and substations are not exclusions but a separate **grid** layer for scoring (issue #5). It is not clipped, since the nearest connection may lie just across the border.

| Przykona | Zone | Share of gmina |
|---|---:|---:|
| Forest + 15 m (219 areas) | 3 330 ha | 30.1 % |
| Buildings + 50 m (6 896) | 1 219 ha | 11.0 % |
| Roads (242 ways) | 519 ha | 4.7 % |
| Water bodies, rivers, streams | 386 ha | 3.5 % |
| Built-up land | 58 ha | 0.5 % |
| **All OSM exclusions (union)** | **5 000 ha** | **45.1 %** |

Grid within reach: 67 power lines (51 at 110 kV, 16 at 220 kV, a few medium-voltage) and 9 substations.

One bug was caught by checking the result rather than trusting it: the first version printed relations without their member ways (`out geom tags` instead of `out geom`), so all multipolygons were silently dropped, among them a 2 734 ha forest and two reservoirs of 139 and 106 ha (flooded mine pits). The exclusion share went from 26 % to 45 % once they were in. A test now checks that a relation without members is skipped and that the query asks for member geometry.

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
