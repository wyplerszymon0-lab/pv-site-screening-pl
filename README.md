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

A rural gmina in Turek County, Greater Poland. Its land includes the former *Adamów* lignite opencast mine, and OpenStreetMap maps 26 solar farms of at least 1 ha there, 709 ha in total (counted in issue #6; a first count of 413 ha on 3 Oct had missed the largest farm, mapped as a 297 ha multipolygon relation). That makes it a good test: the screening can later be checked against where farms were actually built (issue #6). Any other gmina works by passing its TERYT code.

## Usage

```bash
pip install -e ".[dev]"
pytest

# The whole screening for any gmina, by TERYT code (downloads are cached in data/)
python -m pvscreen --teryt 3027062               # writes outputs/3027062/screening.gpkg and reports/3027062.md
python -m pvscreen --teryt 3027022 --crs pl2000  # output in the gmina's PL-2000 zone
```

Reports so far: **[Przykona](reports/3027062.md)** (the demo gmina) and **[Brudzew](reports/3027022.md)**, its neighbour, run end to end without any code change.

The individual steps can also be run on their own:

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
python -m pvscreen.suitability 3027062  # writes outputs/candidates_3027062.gpkg, ranked, with energy
python -m pvscreen.suitability 3027062 --crs pl2000  # the same in the gmina's PL-2000 zone
python -m pvscreen.postgis 3027062      # starts PostGIS (Docker), runs the overlay in SQL, compares
python -m pvscreen.validate 3027062     # compares the candidates with existing solar farms
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

### Candidate areas (issue #5)

On the 5 m terrain grid a cell is suitable if it is inside the gmina, outside every exclusion zone (OSM and Natura 2000) and its slope is at most 10°. Suitable cells become polygons; strips narrower than 30 m are removed (shrink by 15 m, grow back) and pieces under 2 ha are dropped. Each candidate is scored from 0 to 1:

| Criterion | Weight | Score |
|---|---:|---|
| Slope | 0.3 | 1 on flat ground, 0 at 10° (mean over the candidate) |
| Aspect | 0.2 | 1 facing south, 0 facing north; ground under 2° counts as flat |
| Grid | 0.5 | 1 at a substation, 0 at 10 km |

All thresholds and weights are in [`pvscreen/screening.toml`](pvscreen/screening.toml), each with a comment saying why.

The grid criterion uses the distance to the nearest **substation**, not to the nearest power line. The first version used lines, and every large candidate scored 1 because a 110 or 220 kV line crossed it. A farm of several MWp is usually connected at a substation or to the medium-voltage network, which OpenStreetMap barely maps, and a high-voltage line overhead cannot simply be tapped. The distance to the nearest line is still reported.

| Przykona | |
|---|---:|
| Candidates | 107 |
| Total area | 5 634 ha (51 % of the gmina) |
| Area per candidate | 2 – 965 ha (median 17 ha) |
| Score | 0.53 – 0.98 (median 0.84) |
| Distance to a substation | 0 – 9.0 km (median 2.3 km) |
| Candidates scoring ≥ 0.8 | 64, covering 3 562 ha |
| Mostly in the *Uniejowski* protected landscape area (constraint) | 11, covering 741 ha |

**What this does not see.** Half of the gmina passing a screening is mostly a statement about its flat, open farmland, not about where farms can actually be built. Two things decide a lot of that and are not in open data for a whole gmina: the **agricultural soil class** of each plot (the best classes are protected from development) and the **local zoning plan** (MPZP) or its absence. Existing solar farms are deliberately not excluded, so that issue #6 can check whether they fall inside the candidates.

### Validation against existing solar farms (issue #6)

Ground-mounted solar farms in OpenStreetMap (`power=plant` + `plant:source=solar`, polygons of at least 1 ha, clipped to the gmina) are compared with the candidates. If built farms fell on random land, the share of their area inside candidates would equal the candidates' share of the gmina.

| Przykona | |
|---|---:|
| Solar farms (≥ 1 ha) | 26, covering 709 ha |
| Farm area inside candidates | **97.7 %** |
| Expected by chance (candidates' share of the gmina) | 50.9 % |
| Ratio (lift) | 1.92 (the maximum possible here is 1 / 0.509 = 1.96) |
| Farms mostly (≥ 50 %) inside candidates | 26 of 26 |
| Mean score of candidate land under farms / of all candidate land | 0.958 / 0.860 |
| … without substations within 500 m of a farm | 0.856 / 0.761 |

**How to read this.** The screening removes almost no land where farms were actually built: not one farm was cut out by a building buffer, forest, slope or Natura 2000. Within the candidates, farm land scores about 0.1 higher than the average.

Three things make this weaker than it looks:

1. **The data comes after the fact.** OpenStreetMap shows the land as it is now. A farm replaced whatever stood there before, so the farm's own area is no longer mapped as forest or buildings. Most farms in Przykona sit on the reclaimed *Adamów* mine.
2. **Farms bring their own substations.** 5 of the 9 substations lie within 31 m of a farm, which makes the farms' surroundings score well on the grid criterion. Leaving those substations out lowers every score but keeps the gap between farm land and other candidate land (0.856 vs 0.761), so the farms are also closer to the grid that was there anyway.
3. **High recall is easy when half the gmina is a candidate.** This shows that the exclusions do not throw away buildable land, not that the screening pinpoints where farms go.

A sharper test would screen the land as it was before the farms were built (OpenStreetMap history) or use farms in neighbouring gminas. The second follows once the pipeline runs for any gmina in one command (issue #11).

### Energy yield (issue #7)

[`pvscreen/energy.py`](pvscreen/energy.py) is a Python port of the yield model from Solar Site Intelligence: NASA POWER monthly climatology (global and diffuse irradiance, air temperature) transposed to the panel plane with the isotropic-sky model, with glass reflection losses, temperature derating and 14 % system losses. Each candidate gets panels facing south at the tilt that maximises the annual yield in its climate, an installed capacity of 1 MWp per hectare (an assumption in `screening.toml`) and from that its annual energy.

The port reproduces the JavaScript model to floating-point precision (largest relative difference 5 × 10⁻¹⁶ over ten cases, including the optimum tilt for Warsaw, 36°). It also inherits its validation: annual yield within 7 % of PVGIS for five climates on both hemispheres, 0.5 – 6.8 % low for well-oriented arrays.

NASA POWER is queried once per 0.1° cell; four requests cover Przykona.

| Przykona | |
|---|---:|
| Yield | 984 – 1 014 kWh/kWp per year at 36 – 37° tilt |
| All 107 candidates | 5 634 MWp, about 5 550 GWh per year |
| The 64 candidates scoring ≥ 0.8 | 3 562 MWp, about 3 510 GWh per year |

These are theoretical upper bounds: they assume every hectare that passes the screening is covered with panels, with no check on soil class, zoning, ownership or grid capacity.

### PL-2000 output (issue #10)

Processing stays in PL-1992, but surveyors, the cadastre and local authorities work in **PL-2000**: four 3° zones (central meridians 15°, 18°, 21° and 24° E, EPSG:2176 – 2179) with a scale of 0.999923. `--crs pl2000` writes the candidates in the zone of the gmina:

- **Zone choice.** The zone is picked from the longitude of the gmina's centroid. A boundary meridian (16.5°, 19.5°, 22.5°) belongs to the zone east of it.
- **Gminas across a zone boundary.** All of the gmina's outputs still go to one zone, with a note in the output. The part across the boundary is then slightly outside its own zone, which is acceptable that close to it.
- **Przykona** (18.54° – 18.74° E) lies inside zone 6, so its output is EPSG:2177. Eastings start with 6, as every zone-6 coordinate does.
- **Areas.** The `area_ha` attributes are computed in PL-1992. The same polygons measured in PL-2000 are 0.13 % larger, because PL-1992 shrinks distances here (scale about 0.9993 near its 19° meridian) and PL-2000 hardly does.

The tests do not check PROJ against itself. They derive control points from the definitions of the two systems: on a central meridian, the northing is the scale factor times the GRS80 meridian arc (Helmert's series), and the easting is the false easting. PROJ matches them to within 1 mm in all four PL-2000 zones and in PL-1992, at 49°, 52° and 54.8° N. A round trip WGS 84 → PL-1992 → PL-2000 → WGS 84 loses less than a millimetre.

### The overlay in PostGIS (issue #8)

`python -m pvscreen.postgis 3027062` starts PostGIS with `docker compose` ([`docker-compose.yml`](docker-compose.yml), port 5433 on localhost), loads the same inputs as the Python pipeline as two tables with GiST indexes, `terrain_ok` (cells inside the gmina with slope ≤ 10°, as polygons) and `exclusions` (the OSM zones and Natura 2000), and runs the overlay in SQL:

```sql
WITH ok AS (                                   -- gentle terrain inside the gmina
    SELECT ST_Union(geom) AS g FROM terrain_ok
), excluded AS (                               -- every exclusion zone that touches it
    SELECT ST_Union(e.geom) AS g
    FROM exclusions e, ok
    WHERE ST_Intersects(e.geom, ok.g)          -- uses the GiST index on exclusions
), free AS (
    SELECT ST_Difference(
        ok.g, COALESCE(excluded.g, ST_SetSRID('POLYGON EMPTY'::geometry, ST_SRID(ok.g)))
    ) AS g
    FROM ok, excluded
), opened AS (                                 -- remove strips narrower than 2 x half_width
    SELECT ST_Intersection(
        ST_Buffer(ST_Buffer(g, -:half_width, 'join=mitre'), :half_width, 'join=mitre'), g
    ) AS g
    FROM free
), parts AS (
    SELECT (ST_Dump(g)).geom AS g FROM opened
)
SELECT ST_Area(g) / 10000.0 AS area_ha, g AS geom
FROM parts
WHERE ST_GeometryType(g) = 'ST_Polygon' AND ST_Area(g) >= :min_area
ORDER BY area_ha DESC
```

| Przykona | SQL (vector) | GeoPandas (5 m raster) |
|---|---:|---:|
| Candidates | 102 | 107 |
| Area | 5 683.6 ha | 5 634.3 ha |
| Time (overlay / screening) | 12.6 s | 11.7 s |

The two agree to 0.9 % in area; the symmetric difference is 1.5 % of their union. No candidate exists in one version only. The count differs because the raster version splits three areas into eight: its stair-stepped edges make a few narrow necks slightly narrower, and the 30 m strip filter then cuts them.

The tests run the SQL on a real PostGIS (a service container in CI, `docker compose` locally): a road across a square leaves two 7.2 ha halves, a 20 m strip and a 1 ha piece are dropped, both tables get GiST indexes. One of them caught a bug the demo gmina never hit: with no exclusion zone touching the terrain, the empty fallback polygon had SRID 0 and PostGIS refused the difference.

### One command for any gmina (issue #11)

`python -m pvscreen --teryt <code>` runs every step above: boundary, terrain, protected areas, OpenStreetMap, candidates, energy and the check against existing farms. It writes a GeoPackage (layers `boundary`, `exclusions`, `candidates`) and a Markdown report with the inputs, thresholds, exclusions, the ten best candidates, the farm check and the data attribution.

| | [Przykona](reports/3027062.md) | [Brudzew](reports/3027022.md) |
|---|---:|---:|
| Area | 11 079 ha | 11 251 ha |
| Excluded by OpenStreetMap layers | 45.1 % | 41.5 % |
| Natura 2000 excluded | 60 ha | 1 535 ha |
| Candidates | 107, 5 634 ha | 103, 5 117 ha |
| Existing solar farms (≥ 1 ha) | 26, 709 ha | 5, 240 ha |
| Farm area inside candidates / by chance | 97.7 % / 50.9 % | 72.3 % / 45.5 % |

Brudzew needed no code change. Its first run did expose a weakness: GUGiK's terrain service dropped connections partway (an SSL error, then a closed connection), so tile downloads now retry on connection errors and 5xx answers. Downloads that already succeeded stay in the cache, so a rerun continues where the last one stopped.

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
