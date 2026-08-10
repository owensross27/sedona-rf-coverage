"""Cached remote fetch and the one definition of "the area we are processing".

Stages 01-04 each need a file pulled from the open internet and each need to
know which polygon SCOPE refers to. Both live here so the four stages cannot
quietly disagree about the extent they are working over -- a disagreement that
would not raise anything, it would just produce a coverage map with a stripe
of missing receivers down one edge.

Two extents, and the distinction matters:

  aoi()          the exact scope polygon. Receivers and population live here.
  aoi(buffer_m)  padded by max_link_km. TRANSMITTERS live here, because a
                 tower 20 km outside the county line still serves the county.
                 Filtering towers to the scope polygon would put a fake
                 uncovered ring around every boundary, which at demo scope
                 (one county) is most of the map.

The terrain raster is cut to the padded extent for the same reason: a straight
tx->rx profile between two points inside a bounding box stays inside that box,
so a padded box is guaranteed to contain every profile the kernel samples.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

import requests

from config import DATA_DIR, GRID, RF, SITING, SOURCES, STATE, scope

RAW_DIR = DATA_DIR / "raw"


def cached(url: str, name: str | None = None) -> Path:
    """Download `url` into data/raw once and return the local path.

    Deliberately not a real cache: no etag, no expiry. These are versioned
    archives (TIGER 2024, an ASR daily dump) and re-downloading 38 MB on every
    stage run is the only cost being avoided. `rm data/raw/<file>` to refresh.
    """
    dest = RAW_DIR / (name or url.rsplit("/", 1)[-1])
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"fetching {url}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    # Rename only after a complete body, so an interrupted download can never
    # be picked up as a valid cache entry on the next run.
    tmp.rename(dest)
    print(f"  -> {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
    return dest


def block_groups():
    """TIGER block groups for the state, as a GeoDataFrame in EPSG:5070.

    Used by 03_census.py for population and by aoi() for the scope polygon.
    Block groups nest exactly inside counties, so dissolving the ones whose
    GEOID starts with a scope county FIPS reproduces the county boundary
    without needing the (national, ~120 MB) county file as well.
    """
    import geopandas as gpd

    path = cached(SOURCES["tiger"]["url_fmt"].format(fips=STATE["fips"]))
    bg = gpd.read_file(f"/vsizip/{path}")
    return bg.to_crs(GRID["crs"])


def aoi(buffer_m: float = 0.0):
    """The scope polygon in EPSG:5070, optionally padded.

    SCOPE=state has `counties: null` in config.yml, meaning all of them.
    """
    counties = scope()["counties"]
    bg = block_groups()
    if counties:
        bg = bg[bg["GEOID"].str[:5].isin(counties)]
        if bg.empty:
            raise RuntimeError(
                f"no TIGER block groups matched counties {counties}; check "
                "config.yml scopes against FIPS 5-digit codes"
            )
    geom = bg.geometry.union_all()
    return geom.buffer(buffer_m) if buffer_m else geom


def link_pad_m() -> float:
    """The transmitter/terrain pad: one maximum link length."""
    return float(RF["max_link_km"]) * 1000.0


def grid_spec(geom) -> dict:
    """Snap a geometry's bounds outward onto the shared 90 m EPSG:5070 grid.

    Snapping (rather than using the raw bounds) is what makes the DEM and the
    clutter raster land on identical pixel edges -- 05_links.load_terrain
    asserts their transforms are equal, and a half-pixel offset between
    terrain and clutter is invisible in the output map but wrong everywhere.
    """
    cell = float(GRID["cell_m"])
    minx, miny, maxx, maxy = geom.bounds
    minx = math.floor(minx / cell) * cell
    miny = math.floor(miny / cell) * cell
    maxx = math.ceil(maxx / cell) * cell
    maxy = math.ceil(maxy / cell) * cell
    return {
        "bounds": (minx, miny, maxx, maxy),
        "width": int(round((maxx - minx) / cell)),
        "height": int(round((maxy - miny) / cell)),
        "cell_m": cell,
        "crs": int(GRID["crs"]),
    }


def nrqz():
    """The National Radio Quiet Zone as a polygon in EPSG:5070.

    Constructed, not fetched: 47 CFR 1.924 defines the zone as a lat/lon
    rectangle and NRAO publishes it only as a KMZ, so the four corners live in
    config.yml where a reader can check them against the regulation.

    Segmentized BEFORE the transform, because a rectangle in EPSG:4269 is not
    one in Albers. The meridians stay straight, but joining the north and south
    corners with straight lines in EPSG:5070 cuts 461 m out of the middle of
    each -- five times the 90 m analysis grid, and 105 km2 of the zone wrongly
    left outside it. 0.05 degree segments bring that to 0.27 m. Same reason
    02_terrain.py passes densify_pts to transform_bounds.
    """
    import geopandas as gpd
    import shapely

    box = shapely.segmentize(shapely.box(*SITING["nrqz_bounds_4269"]), 0.05)
    return gpd.GeoSeries([box], crs=4269).to_crs(GRID["crs"]).iloc[0]


def gdal_path(uri: str) -> str:
    """Rewrite an s3a:// URI into something GDAL can open.

    out_path() speaks s3a:// because that is what Spark's hadoop-aws needs.
    rasterio goes through GDAL, which has never heard of the s3a scheme and
    fails with an unhelpful "not recognized as a supported file format".
    """
    return "/vsis3/" + uri[len("s3a://"):] if uri.startswith("s3a://") else uri


def anon_gdal_env():
    """A rasterio Env for reading the open buckets without credentials.

    The Spark side of this is the per-bucket provider table in session.py.
    GDAL has no per-bucket equivalent, so stages that read open data with
    rasterio scope the anonymous flag to that read and nothing else.
    """
    import rasterio

    return rasterio.Env(
        AWS_NO_SIGN_REQUEST="YES",
        AWS_S3_ENDPOINT="s3.amazonaws.com",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        AWS_REGION=os.environ.get("AWS_REGION", "us-west-2"),
    )
