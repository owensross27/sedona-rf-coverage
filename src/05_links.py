"""Stage 05 -- predicted RSRP for every plausible tower/receiver pair.

This is where the two halves of the project meet:

  Sedona does the distributed spatial work. Generating candidate pairs is a
  genuinely hard join -- roughly 5k registered structures against ~85k H3
  receiver cells is 425M combinations if done naively, and ST_DWithin with a
  40 km predicate turns that into a few million with a spatial index behind
  it. That is the job Sedona is actually best at.

  numpy does the per-link physics. The DEM and clutter rasters are broadcast
  once as two small arrays and each link's terrain profile becomes a fancy
  index into RAM. See src/propagation.py for why this beats calling RS_Value
  per sample, and docs/benchmarks.md for the measured margin.

Usage:
    SCOPE=demo LOCAL_OUT=1 python src/05_links.py
    SCOPE=state python src/05_links.py          # writes to s3://$RF_BUCKET
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import BUILDINGS, GRID, RF, clutter_loss_lut, scope  # noqa: E402
from propagation import TerrainGrid, link_rsrp  # noqa: E402
from session import assert_versions, get_sedona, out_path  # noqa: E402
from sources import gdal_path  # noqa: E402

# Columns handed back to Spark. Declared once so the schema string below and
# the kernel's output dict cannot drift apart.
OUT_COLS = [
    ("asr_id", "string"),
    ("h3_r8", "long"),
    ("rsrp_dbm", "double"),
    ("path_loss_db", "double"),
    ("diffraction_db", "double"),
    ("clutter_db", "double"),
    ("distance_m", "double"),
    ("is_los", "boolean"),
]
OUT_SCHEMA = ", ".join(f"{n} {t}" for n, t in OUT_COLS)


def load_terrain(dem_path: str, clutter_path: str,
                 bldg_path: str | None = None) -> TerrainGrid:
    """Read the co-registered COGs into memory on the driver.

    02_terrain.py has already put all layers on the same EPSG:5070 grid, which
    is what makes a single set of pixel indices valid for every one of them.
    That is asserted here rather than trusted: a silent half-pixel offset
    between layers would be invisible in the output map.

    The building layer is optional two ways: pass None to run the
    pre-registered class-only baseline deliberately (BUILDING_CLUTTER=0 in
    main), or point at a file that does not exist yet and get a loud error --
    a missing layer must never silently degrade into the baseline.
    """
    import rasterio

    # gdal_path: out_path speaks s3a:// for Spark's hadoop-aws, and GDAL has
    # never heard of that scheme -- it fails as "not a supported file format",
    # which reads like a corrupt COG rather than a URI problem.
    dem_path, clutter_path = gdal_path(dem_path), gdal_path(clutter_path)
    with rasterio.open(dem_path) as src:
        dem = src.read(1)
        transform, crs = src.transform, src.crs
    with rasterio.open(clutter_path) as src:
        clutter = src.read(1).astype(np.uint8)
        if src.transform != transform:
            raise RuntimeError(
                f"clutter transform {src.transform} != dem transform {transform}; "
                "re-run 02_terrain.py, which reprojects both onto one grid"
            )
    bldg = None
    if bldg_path is not None:
        with rasterio.open(gdal_path(bldg_path)) as src:
            bldg = src.read(1).astype(np.uint8)
            if src.transform != transform:
                raise RuntimeError(
                    f"buildings transform {src.transform} != dem {transform}; "
                    "re-run 02_terrain.py, which writes all three together"
                )
    if crs.to_epsg() != GRID["crs"]:
        raise RuntimeError(f"terrain is EPSG:{crs.to_epsg()}, expected {GRID['crs']}")

    # rasterio's transform gives the outer corner of pixel [0,0]; TerrainGrid
    # wants its centre.
    cell = GRID["cell_m"]
    return TerrainGrid(
        dem=dem.astype(np.int16),
        clutter=clutter,
        x0=transform.c + cell / 2.0,
        y0=transform.f - cell / 2.0,
        cell_m=float(cell),
        clutter_lut=np.asarray(clutter_loss_lut(), dtype=np.float64),
        bldg=bldg,
    )


def make_kernel(bcast):
    """Build the mapInPandas function that runs on the executors.

    `bcast` is the broadcast TerrainGrid. Spark ships it to each executor once
    and every task on that executor shares the same read-only arrays -- there
    is no per-core copy, which is the whole reason a 61 MB payload is fine.
    """
    rf = dict(RF)
    # The building knife-edge parameters ride in the same closure dict so the
    # kernel serialises one plain dict, not a config module reference.
    rf["bldg_setback_m"] = float(BUILDINGS["setback_m"])
    rf["bldg_max_loss_db"] = float(BUILDINGS["max_loss_db"])

    def kernel(batches):
        grid = bcast.value
        for df in batches:
            if df.empty:
                continue
            out = link_rsrp(
                grid,
                tx_x=df["tx_x"].to_numpy(),
                tx_y=df["tx_y"].to_numpy(),
                tx_height_m=df["tx_height_m"].to_numpy(),
                rx_x=df["rx_x"].to_numpy(),
                rx_y=df["rx_y"].to_numpy(),
                rx_height_m=rf["rx_height_m"],
                freq_mhz=rf["frequency_mhz"],
                eirp_dbm=rf["eirp_dbm"],
                subcarriers=rf["subcarriers"],
                shadow_margin_db=rf["shadow_margin_db"],
                n_samples=rf["profile_samples"],
                k_factor=rf["k_factor"],
                bldg_setback_m=rf["bldg_setback_m"],
                bldg_max_loss_db=rf["bldg_max_loss_db"],
            )
            yield pd.DataFrame({
                "asr_id": df["asr_id"].to_numpy(),
                "h3_r8": df["h3_r8"].to_numpy(),
                "rsrp_dbm": out["rsrp_dbm"],
                "path_loss_db": out["path_loss_db"],
                "diffraction_db": out["diffraction_db"],
                "clutter_db": out["clutter_db"],
                "distance_m": out["distance_m"],
                "is_los": out["is_los"],
            })

    return kernel


def main() -> int:
    sc = scope()
    sedona = get_sedona(f"rf-links-{sc['name']}")
    assert_versions(sedona)

    # The mapInPandas kernel closes over link_rsrp, which cloudpickle
    # serialises BY REFERENCE to its module. The python workers therefore have
    # to be able to `import propagation` themselves, and src/ is on the
    # driver's sys.path only -- so without this the executors die with
    # ModuleNotFoundError before a single link is computed. propagation.py
    # imports nothing but numpy, so shipping the one file is enough.
    sedona.sparkContext.addPyFile(
        str(Path(__file__).resolve().parent / "propagation.py"))

    # format("geoparquet"), not .parquet(): the plain reader hands back the
    # geometry column as BinaryType and every ST_* call below fails to resolve
    # against it. GeometryUDT only comes back through Sedona's own reader.
    towers = sedona.read.format("geoparquet").load(out_path("bronze", "towers"))
    hexes = sedona.read.format("geoparquet").load(out_path("silver", "hex_grid"))
    towers.createOrReplaceTempView("towers")
    hexes.createOrReplaceTempView("hexes")

    # ---- the Sedona half -------------------------------------------------
    # ST_DWithin over EPSG:5070 metres. Sedona's optimizer turns this into a
    # broadcast/partitioned spatial join rather than the 425M-row cross
    # product the same query would be in plain SQL.
    #
    # tx_height_m is resolved here rather than in the kernel so the fallback
    # for structures with no registered height is visible in the lineage.
    radius_m = RF["max_link_km"] * 1000.0
    pairs = sedona.sql(f"""
        SELECT
            t.asr_id,
            h.h3_r8,
            ST_X(t.geom)  AS tx_x,
            ST_Y(t.geom)  AS tx_y,
            COALESCE(t.height_agl_m, {RF['default_tx_height_m']}) AS tx_height_m,
            ST_X(h.center) AS rx_x,
            ST_Y(h.center) AS rx_y
        FROM towers t
        JOIN hexes h ON ST_DWithin(t.geom, h.center, {radius_m})
    """)
    # Repartition so each task gets work proportional to cores, not to the
    # accident of how many hexes a single tower happens to reach.
    n_parts = int(os.environ.get("LINK_PARTITIONS", "64"))
    pairs = pairs.repartition(n_parts)

    # ---- the numpy half --------------------------------------------------
    # BUILDING_CLUTTER=0 runs the pre-registered class-only baseline; it is a
    # deliberate sensitivity switch, not a fallback -- with it unset, a missing
    # buildings COG is an error, never a silent model downgrade.
    use_bldg = os.environ.get("BUILDING_CLUTTER", "1") != "0"
    grid = load_terrain(
        out_path("cog", "dem_5070_90m.tif"),
        out_path("cog", "clutter_5070_90m.tif"),
        out_path("cog", "buildings_5070_90m.tif") if use_bldg else None,
    )
    nbytes = grid.dem.nbytes + grid.clutter.nbytes + (
        grid.bldg.nbytes if grid.bldg is not None else 0)
    print(f"clutter model: {'class LUT + building heights' if use_bldg else 'class LUT only (baseline)'}")
    print(f"broadcasting terrain {grid.dem.shape} ({nbytes / 1e6:.0f} MB)")
    bcast = sedona.sparkContext.broadcast(grid)

    links = pairs.mapInPandas(make_kernel(bcast), schema=OUT_SCHEMA)

    dest = out_path("silver", "links")
    # Only links that could plausibly serve are kept. Writing all few-million
    # rows would multiply the output for no analytical gain: a link 40 dB below
    # threshold is not a near miss, it is noise.
    keep_floor = RF["rsrp_threshold_dbm"] - 20.0
    links.filter(f"rsrp_dbm >= {keep_floor}").write.mode("overwrite").parquet(dest)
    print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
