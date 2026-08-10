# Data sources

Every path below was verified live on **2026-08-09** with
`aws s3 ls --no-sign-request` or an HTTP HEAD. Anything that could not be
verified is marked as such rather than quietly included.

All inputs are open and cloud-native. Nothing in this pipeline requires a
manual download except FCC BDC (noted below), and nothing is bundled into the
repository.

## Terrain

| | |
|---|---|
| Dataset | Copernicus GLO-30 Digital Surface Model |
| URI | `s3://copernicus-dem-30m/Copernicus_DSM_COG_10_{N/S}{lat}_00_{E/W}{lon}_00_DEM/` |
| Format | Cloud-Optimized GeoTIFF, EPSG:4326, 1 degree tiles |
| Size | ~39 MB per tile; ~24 tiles cover West Virginia (~0.9 GB) |
| Licence | Copernicus DEM free licence |
| Access | Anonymous, no requester-pays |
| STAC | `s3://copernicus-dem-30m-stac` (verified) |

Chosen over USGS 3DEP 10 m (`s3://prd-tnm/StagedProducts/Elevation/13/`, also
verified and also free) because the model's diffraction geometry is driven by
ridgelines, and the first Fresnel radius at 700 MHz over 20 km is about 65 m —
30 m posting is already finer than the physics resolves. 3DEP remains the
upgrade path for a sensitivity run.

Note that GLO-30 is a **surface** model: its returns include forest canopy and
buildings. For a diffraction model that is arguably the more useful surface,
but it means the terrain and the clutter layer overlap slightly in what they
represent. Stated here rather than hidden; quantifying the double-count is a
task in `docs/validation.md`.

## Land cover / clutter

| | |
|---|---|
| Dataset | ESA WorldCover 10 m, v200 (2021) |
| URI | `s3://esa-worldcover/v200/2021/map/` |
| Format | COG, EPSG:4326, 3x3 degree tiles, 11 classes |
| Licence | CC BY 4.0 |
| Access | Anonymous |

A grid index (`esa_worldcover_grid.fgb`) ships in the same bucket, which is
what `02_terrain.py` joins against to find intersecting tiles instead of
guessing filenames.

Annual NLCD (`s3://usgs-landcover/annual-nlcd/`) covers 1985–2024 at 30 m and
would give a US-specific classification, but the bucket is **requester-pays**
and anonymous listing is refused. WorldCover is free, global, and finer; NLCD
is the alternative if class semantics ever matter more than resolution.

## Transmitters

| | |
|---|---|
| Dataset | FCC Antenna Structure Registration |
| URL | `https://data.fcc.gov/download/pub/uls/complete/r_tower.zip` (38 MB) |
| Format | Pipe-delimited text in a zip |
| Cadence | **Daily** (verified: files re-dated same day) |
| Licence | US public domain |

Two things to know before using it:

1. **Coordinates are stored as separate degree / minute / second columns**, not
   decimal degrees. `01_towers.py` converts and validates against a known
   structure rather than trusting the conversion.
2. **ASR structures are not cell sites.** The registry covers structures over
   200 ft, includes AM/FM broadcast masts, and misses rooftop and small-cell
   installations entirely. This is the single largest source of error in the
   whole model. It is not fixable from open data, so it is treated as a
   finding instead: reconciling ASR against FCC BDC coverage polygons infers
   which structures plausibly carry cellular, and that reconciliation is
   published as its own output.

## Ground truth for validation

**FCC Broadband Data Collection (mobile).** Per-carrier, per-technology
coverage polygons plus H3 resolution-9 aggregations, from
`broadbandmap.fcc.gov/data-download`. Shapefile / GeoPackage, not S3, and the
download is interactive enough that it is the one manual step in the pipeline
— which is why the build plan starts it on day one. Scoped to two carriers,
4G LTE, West Virginia only.

**Ookla Open Data.** `s3://ookla-open-data/parquet/performance/type=mobile/year=2026/quarter=2/2026-04-01_performance_mobile_tiles.parquet`
— read live 2026-08-10: one file, 186,254,189 bytes, **3,382,642 rows**
globally, columns `quadkey, tile, tile_x, tile_y, avg_d_kbps, avg_u_kbps,
avg_lat_ms, avg_lat_down_ms, avg_lat_up_ms, tests, devices, quarter, type,
year`. Rows are zoom-16 web-mercator tiles — **479 m across at 38.4 N**, not
the ~600 m quoted at the equator, so roughly three land in each H3 r8 hex.
`tile_x`/`tile_y` are Ookla's own precomputed tile centroids, which is why
`src/10_ookla.py` never parses the `tile` WKT. Published quarterly about 5–6
weeks after quarter close.

> **Licence constraint: CC BY-NC-SA 4.0, non-commercial only.** Fine for this
> portfolio project. Anyone reusing this pipeline commercially must drop the
> Ookla leg or license it separately.

The two are used asymmetrically on purpose. Ookla proves *presence* of service
and cannot prove absence — a tile with no speedtests may simply have nobody in
it. So it is used for exactly one thing: the **false-negative rate on gap
calls**. BDC gives full polygon coverage, so it supports IoU and a confusion
matrix.

## Population and demand

**ACS 5-year, table B01003 (total population), by block group**, from
`api.census.gov/data/{year}/acs/acs5`.

> Requires a free API key exported as `CENSUS_API_KEY`. Verified: keyless
> requests 302 to a "missing key" page rather than returning data or an error,
> so an unkeyed run fails in a confusing way. Get one at
> `https://api.census.gov/data/key_signup.html`.

**TIGER/Line block group geometry**, `www2.census.gov/geo/tiger/TIGER2024/BG/tl_2024_54_bg.zip`.
Shapefile only. **There is no public GeoParquet of TIGER block groups on S3 or
Source Cooperative** — searched and not found. `03_census.py` converts, and
publishing that conversion is a small genuine contribution.

**Growth** is computed at **county** level only, from two ACS vintages. Block
group boundaries were redrawn between the 2010 and 2020 censuses, so a
GEOID-to-GEOID join across vintages is silently wrong — it produces plausible
numbers for the wrong geographies. Counties are stable.

**Tourism demand**: Overture Maps `places` theme,
`s3://overturemaps-us-west-2/release/2026-07-22.0/theme=places/type=place/`
(verified; 16 parts, ~10.5 GB globally, cheap to scan with a bbox filter).
Categories weighted in `config.yml`. CDLA Permissive 2.0.

> Overture lifecycle-deletes releases after roughly 60 days, so the release
> string in `config.yml` is pinned and needs periodic refresh. A stale pin
> fails as a missing prefix, which is at least loud.

## Building heights

| | |
|---|---|
| Dataset | Overture Maps buildings theme |
| URI | `s3://overturemaps-us-west-2/release/2026-07-22.0/theme=buildings/type=building/` (~257 GB globally) |
| Format | GeoParquet with a `bbox` struct for row-group pruning |
| Licence | CDLA Permissive 2.0 / ODbL for OSM-derived rows |
| Access | Anonymous; scanned with duckdb + httpfs, no spatial extension |

Measured over West Virginia (2026-08-09): **4,550,373 buildings, `height`
present on 74.2%**, median 3.85 m statewide (3.55 m over the demo box's
426k-526k footprints, max 89 m). `num_floors` is present on 0.49% and
`min_height` / `roof_height` are effectively absent (1 and 11 rows statewide)
— only `height` is usable.

Used since 2026-08-10 as the third co-registered raster layer: heights burned
onto the shared 90 m grid at the footprint's bbox centre with a per-pixel
max, feeding the rooftop knife-edge clutter term (`config.yml: buildings`,
which records the forcing measurement per the pre-registration rule). The
demo-box scan is ~3 minutes against the open bucket and cached under
`data/raw/`. An earlier note here dismissed the theme as "sparse" without
measuring; the numbers above corrected that.

## Considered and not used

| Source | Why not |
|---|---|
| Meta / WRI canopy height (`s3://dataforgood-fb-data/forests/`) | Within the model's 8 dB shadow margin, and CC BY-NC complicates the licence story. |
| Microsoft ML building footprints | Azure-hosted, no native S3. |
| FEMA / ORNL USA Structures | No S3 or cloud-native distribution found; GDB/Shapefile via Figshare and Esri only. |
| GHSL, WorldPop | Neither found on AWS S3; JRC FTP and worldpop.org respectively. ACS block groups are the better fit for a US-only study anyway. |
| Meta HRSL (`s3://dataforgood-fb-data/hrsl-cogs/`) | Verified and usable, but redundant against ACS at block-group resolution. Retained as a cross-check option. |
| OpenCellID | The S3 mirror is a PMTiles archive last modified 2024-06; the fresh feed is rate-limited to 2 downloads/day behind a token. |
| NASADEM, ETH canopy height | No anonymously accessible S3 bucket found for either. |
