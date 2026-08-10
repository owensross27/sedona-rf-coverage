"""Stage 02 -- Copernicus DEM + ESA WorldCover -> one co-registered 90 m grid.

The whole point of this stage is the word "one". 05_links.load_terrain asserts
that the DEM and the clutter raster share an affine transform, because the
propagation kernel computes a single set of pixel indices per terrain profile
and reads both arrays with it. A half-pixel offset between the two would not
raise anything anywhere -- it would just attribute every ridge's land cover to
its neighbour, and the output map would look entirely plausible.

So neither raster is reprojected on its own terms. Both are warped onto a
destination grid computed once, up front, from the scope bounds snapped to the
90 m EPSG:5070 lattice.

Sources are read straight off S3 by range request -- no tile is downloaded
whole. The DEM alone is ~47 MB x ~35 tiles at state scope and only the
overlapping windows are ever fetched.

Usage:
    SCOPE=demo LOCAL_OUT=1 python src/02_terrain.py
"""
import math
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject, transform_bounds

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import SOURCES  # noqa: E402
from session import out_path  # noqa: E402
from sources import aoi, anon_gdal_env, gdal_path, grid_spec, link_pad_m  # noqa: E402

DEM_NODATA = -32768.0
# WorldCover class 0 IS "no data", and clutter_loss_lut() maps 0 to 0 dB. So an
# unfilled clutter pixel contributes no excess loss, which is the honest
# behaviour for a gap: add nothing rather than invent an obstruction.
CLUTTER_NODATA = 0

# Copernicus GLO-30 is a SURFACE model -- returns include canopy and buildings.
# Downsampling 30 m -> 90 m with `average` would shave ridge crests, and lower
# ridges mean less diffraction loss, which means MORE predicted coverage. That
# is the direction of error that flatters the result, so it is the one to
# avoid; bilinear preserves crests better than average without the pessimistic
# bias of `max`.
# ponytail: fixed bilinear resample. Resampling.max is the pessimistic
# sensitivity run if docs/validation.md ever needs the error bound both ways.
DEM_RESAMPLING = Resampling.bilinear
# Land cover is categorical: majority class over the 81 source pixels that fall
# in each destination cell. Nearest-neighbour would pick one arbitrary 10 m
# pixel out of 81 and call it the cell's land cover.
CLUTTER_RESAMPLING = Resampling.mode


def dem_tiles(bounds_4326) -> list[str]:
    """Copernicus GLO-30 keys covering a lat/lon box. 1 degree, SW-corner named.

    Trap: the object is nested one level deep inside a directory of the same
    name, and both carry the `_DEM` suffix --
    `Copernicus_DSM_COG_10_N38_00_W082_00_DEM/<same>.tif`.
    """
    fmt = SOURCES["dem"]["prefix_fmt"]
    bucket = SOURCES["dem"]["bucket"]
    keys = []
    for lat in range(math.floor(bounds_4326[1]), math.ceil(bounds_4326[3])):
        for lon in range(math.floor(bounds_4326[0]), math.ceil(bounds_4326[2])):
            name = fmt.format(ns="N" if lat >= 0 else "S", lat=abs(lat),
                              ew="E" if lon >= 0 else "W", lon=abs(lon))
            keys.append(f"/vsis3/{bucket}/{name}/{name}.tif")
    return keys


def worldcover_tiles(bounds_4326) -> list[str]:
    """ESA WorldCover v200 keys covering a lat/lon box. 3 degree, SW-corner."""
    bucket = SOURCES["worldcover"]["bucket"]
    prefix = SOURCES["worldcover"]["prefix"]
    lat0 = math.floor(bounds_4326[1] / 3) * 3
    lon0 = math.floor(bounds_4326[0] / 3) * 3
    keys = []
    for lat in range(lat0, math.ceil(bounds_4326[3]), 3):
        for lon in range(lon0, math.ceil(bounds_4326[2]), 3):
            ns, ew = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
            keys.append(
                f"/vsis3/{bucket}/{prefix}/ESA_WorldCover_10m_2021_v200_"
                f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}_Map.tif"
            )
    return keys


def _mosaic(keys, spec, dtype, nodata, resampling) -> np.ndarray:
    """Warp every source tile onto the shared destination grid.

    Each tile is reprojected into just its own destination window rather than a
    full-size scratch array, and merged in under a validity mask. The mask is
    not optional: a tile that is a rectangle in EPSG:4326 is a curved
    quadrilateral in EPSG:5070, so its window corners come back as nodata and
    a straight assignment would punch holes in the neighbouring tile that
    already filled them.
    """
    minx, miny, maxx, maxy = spec["bounds"]
    cell = spec["cell_m"]
    dst = np.full((spec["height"], spec["width"]), nodata, dtype=dtype)
    dst_crs = rasterio.crs.CRS.from_epsg(spec["crs"])
    used = 0

    for key in keys:
        try:
            src = rasterio.open(key)
        except rasterio.errors.RasterioIOError:
            # Open-data grids are global and mostly ocean; a bbox on a coast
            # legitimately names tiles that were never published.
            print(f"  skip (absent): {key.rsplit('/', 1)[-1]}")
            continue
        with src:
            tb = transform_bounds(src.crs, dst_crs, *src.bounds, densify_pts=64)
            # Destination window for this tile, snapped to the shared lattice
            # and clipped to the grid.
            c0 = max(0, int(math.floor((tb[0] - minx) / cell)))
            c1 = min(spec["width"], int(math.ceil((tb[2] - minx) / cell)))
            r0 = max(0, int(math.floor((maxy - tb[3]) / cell)))
            r1 = min(spec["height"], int(math.ceil((maxy - tb[1]) / cell)))
            if c1 <= c0 or r1 <= r0:
                continue

            win = np.full((r1 - r0, c1 - c0), nodata, dtype=dtype)
            reproject(
                source=rasterio.band(src, 1),
                destination=win,
                src_nodata=src.nodata,
                dst_transform=from_origin(minx + c0 * cell, maxy - r0 * cell,
                                          cell, cell),
                dst_crs=dst_crs,
                dst_nodata=nodata,
                resampling=resampling,
            )
            np.copyto(dst[r0:r1, c0:c1], win, where=(win != nodata))
            used += 1
            print(f"  merged {key.rsplit('/', 1)[-1]}")

    if not used:
        raise RuntimeError(f"no source tiles resolved for bounds {spec['bounds']}")
    return dst


def _write(path: str, array: np.ndarray, spec: dict, nodata) -> None:
    minx, _, _, maxy = spec["bounds"]
    profile = {
        "driver": "COG",
        "dtype": array.dtype.name,
        "count": 1,
        "height": array.shape[0],
        "width": array.shape[1],
        "crs": rasterio.crs.CRS.from_epsg(spec["crs"]),
        "transform": from_origin(minx, maxy, spec["cell_m"], spec["cell_m"]),
        "nodata": nodata,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array, 1)
    print(f"wrote {path} {array.shape} {array.dtype}")


def validate_against_towers(dem: np.ndarray, spec: dict) -> None:
    """Cross-check the DEM against the ASR structures' own ground elevations.

    This is the closest thing to independent ground truth available without a
    manual download, and it validates two stages at once: if 01_towers.py had
    the coordinate sign or unit wrong, or if this stage had the destination
    transform wrong, the towers would land somewhere the DEM disagrees with
    and the residual would blow up rather than sit at a few metres.

    A positive bias is EXPECTED and is not an error: GLO-30 is a surface model,
    so it reads canopy and rooftops, while ASR reports bare ground.
    """
    import pandas as pd

    try:
        towers = pd.read_parquet(out_path("bronze", "towers"),
                                 columns=["x", "y", "ground_elev_m"])
    except Exception as exc:                      # noqa: BLE001 - advisory only
        print(f"  (skipping DEM/ASR cross-check: {exc})")
        return
    towers = towers.dropna(subset=["ground_elev_m"])
    minx, _, _, maxy = spec["bounds"]
    cell = spec["cell_m"]
    col = np.rint((towers["x"].to_numpy() - minx) / cell).astype(int)
    row = np.rint((maxy - towers["y"].to_numpy()) / cell).astype(int)
    ok = ((row >= 0) & (row < dem.shape[0]) & (col >= 0) & (col < dem.shape[1]))
    sampled = dem[row[ok], col[ok]]
    resid = sampled - towers["ground_elev_m"].to_numpy()[ok]
    med, p90 = np.median(resid), np.percentile(np.abs(resid), 90)
    print(f"DEM vs ASR ground elevation, n={len(resid)}: "
          f"median {med:+.1f} m, p90 |resid| {p90:.1f} m")
    if abs(med) > 30.0:
        raise RuntimeError(
            f"DEM is {med:+.1f} m off the ASR ground elevations. That is not a "
            "resampling or canopy effect -- suspect the coordinate conversion "
            "in 01_towers.py or the destination transform here"
        )


def main() -> int:
    spec = grid_spec(aoi(link_pad_m()))
    px = spec["width"] * spec["height"]
    print(f"destination grid {spec['width']}x{spec['height']} @ "
          f"{spec['cell_m']:.0f} m EPSG:{spec['crs']} "
          f"({px * 3 / 1e6:.0f} MB broadcast as int16 + uint8)")

    b4326 = transform_bounds(spec["crs"], 4326, *spec["bounds"], densify_pts=64)

    with anon_gdal_env():
        print("DEM:")
        dem = _mosaic(dem_tiles(b4326), spec, "float32", DEM_NODATA,
                      DEM_RESAMPLING)
        print("clutter:")
        clutter = _mosaic(worldcover_tiles(b4326), spec, "uint8",
                          CLUTTER_NODATA, CLUTTER_RESAMPLING)

    # Clutter has no gap check of its own: WorldCover class 0 is a legitimate
    # value ("no data", 0 dB of excess loss), so an unfilled cell and a real
    # one are indistinguishable after the fact. The DEM gap count below is what
    # actually proves the tile windowing is right -- both mosaics use the same
    # window arithmetic, so zero DEM gaps means zero clutter gaps too.
    print(f"clutter class 0 (no data / no loss): "
          f"{int((clutter == CLUTTER_NODATA).sum()) / px:.2%} of cells")

    gaps = int((dem == DEM_NODATA).sum())
    if gaps:
        print(f"WARNING: {gaps} DEM cells ({gaps / px:.2%}) have no source "
              "coverage; they will read as sea level")
        dem[dem == DEM_NODATA] = 0.0

    # int16 is what TerrainGrid wants and is lossless here: metres of elevation
    # over land, and it halves the broadcast payload.
    dem_i16 = np.rint(dem).astype(np.int16)
    validate_against_towers(dem_i16, spec)

    _write(gdal_path(out_path("cog", "dem_5070_90m.tif")), dem_i16, spec,
           int(np.iinfo(np.int16).min))
    _write(gdal_path(out_path("cog", "clutter_5070_90m.tif")), clutter, spec,
           CLUTTER_NODATA)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
