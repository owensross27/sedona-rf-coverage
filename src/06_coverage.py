"""Stage 06 -- best server per receiver, coverage gaps, and a raster surface.

05_links.py produces every plausible tower/receiver pair. A handset does not
average them: it camps on the strongest cell it can hear. So coverage is a
per-receiver MAX over links, not a sum, and the gap set is what that max fails
to reach.

The join direction is the part that matters. This stage starts from the hex
grid and LEFT JOINs the links onto it, never the other way round. A receiver
with no surviving link is not missing data -- it is the most uncovered cell in
the state, and an inner join would silently drop exactly the rows the whole
project exists to find. 05_links drops links below `threshold - 20 dB`, so
"no row" already means "nothing within 20 dB of usable".

Three outputs:

    gold/coverage       one row per receiver, always. Feeds 09_siting,
                        validation against BDC, and the vector tiles.
    cog/coverage_rsrp   the same surface on the shared 90 m EPSG:5070 lattice,
                        so it is pixel-aligned with the DEM and clutter and can
                        be differenced against them directly.
    stdout              the headline numbers, population-weighted.

Population weighting is not a presentation choice. 62% of cells covered and
62% of people covered are different claims, and in a state where the terrain
puts most of the population in valleys, they differ a lot. Every number this
stage prints says which one it is.

Usage:
    SCOPE=demo LOCAL_OUT=1 python src/06_coverage.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import GRID, RF, scope  # noqa: E402
from session import assert_versions, get_sedona, out_path  # noqa: E402
from sources import aoi, gdal_path, grid_spec  # noqa: E402

THRESHOLD = float(RF["rsrp_threshold_dbm"])

# 05_links keeps links down to threshold - 20 dB and discards the rest, so this
# is the strongest signal a receiver with no surviving row could have had. It
# is a CEILING on those cells, not an estimate of them -- written into the
# raster only so gaps render as "very weak" rather than as holes in the map.
FLOOR_DBM = THRESHOLD - 20.0

# Distinct from FLOOR_DBM: this marks pixels outside the receiver grid
# altogether, which is a different statement from "modelled and found weak".
RASTER_NODATA = -9999.0


def best_server(sedona):
    """One row per receiver cell, carrying its strongest link.

    MAX_BY pulls the winning link's attributes alongside the max itself, so the
    serving tower, its distance and whether that particular path was line of
    sight all describe the SAME link rather than three independent maxima.
    """
    links = sedona.read.parquet(out_path("silver", "links"))
    hexes = sedona.read.format("geoparquet").load(out_path("silver", "hex_grid"))
    links.createOrReplaceTempView("links")
    hexes.createOrReplaceTempView("hexes")
    return sedona.sql(f"""
        WITH best AS (
            SELECT h3_r8,
                   MAX(rsrp_dbm)                  AS best_rsrp_dbm,
                   MAX_BY(asr_id, rsrp_dbm)       AS best_asr_id,
                   MAX_BY(is_los, rsrp_dbm)       AS best_is_los,
                   MAX_BY(distance_m, rsrp_dbm)   AS best_distance_m,
                   COUNT(*)                       AS n_links,
                   SUM(CASE WHEN rsrp_dbm >= {THRESHOLD} THEN 1 ELSE 0 END)
                                                  AS n_servers
            FROM links
            GROUP BY h3_r8
        )
        SELECT h.h3_r8, h.h3_str, h.pop, h.pop_grown, h.poi_score, h.demand,
               h.x, h.y,
               b.best_rsrp_dbm, b.best_asr_id, b.best_is_los, b.best_distance_m,
               COALESCE(b.n_links, 0)   AS n_links,
               COALESCE(b.n_servers, 0) AS n_servers,
               COALESCE(b.best_rsrp_dbm >= {THRESHOLD}, false) AS is_covered
        FROM hexes h
        LEFT JOIN best b ON h.h3_r8 = b.h3_r8
    """)


def summarise(df: pd.DataFrame, threshold: float = THRESHOLD) -> dict:
    """Coverage headline numbers. Pure, so tests/test_coverage.py can pin it.

    `demand` already folds growth and tourism together (see 04_grid), which
    makes the demand-weighted figure the one that answers "is the uncovered
    part worth building for" rather than merely "how big is it".
    """
    cov = df["is_covered"].to_numpy(dtype=bool)
    pop = df["pop"].to_numpy(dtype=float)
    dem = df["demand"].to_numpy(dtype=float)

    def frac(weights):
        total = weights.sum()
        return float(weights[cov].sum() / total) if total > 0 else 0.0

    served = df.loc[cov, "best_rsrp_dbm"].to_numpy(dtype=float)
    return {
        "threshold_dbm": float(threshold),
        "cells": int(len(df)),
        "cells_covered": int(cov.sum()),
        "cells_covered_frac": frac(np.ones_like(pop)),
        "population": float(pop.sum()),
        "population_covered_frac": frac(pop),
        "population_uncovered": float(pop[~cov].sum()),
        "demand_covered_frac": frac(dem),
        "demand_uncovered": float(dem[~cov].sum()),
        "cells_no_link": int((df["n_links"].to_numpy() == 0).sum()),
        "median_served_rsrp_dbm": float(np.median(served)) if served.size else float("nan"),
        # Redundant capacity: a cell served by exactly one tower loses service
        # entirely when that tower does. Cheap to carry, and it is the question
        # an operator asks second.
        "cells_single_server": int(((df["n_servers"].to_numpy() == 1)).sum()),
    }


def hex_polygons(h3_str: pd.Series):
    """Rebuild hexagon boundaries from their H3 indices, in EPSG:5070.

    silver/hex_grid stores centres only -- that is all 05_links needs, and
    storing 3.4k (demo) to 85k (state) polygons to serve one rasterisation
    would be paying for them on every read of every downstream stage.
    """
    import geopandas as gpd
    import h3
    import shapely

    poly = [shapely.Polygon([(x, y) for y, x in h3.cell_to_boundary(c)])
            for c in h3_str]
    return gpd.GeoSeries(poly, crs=4326).to_crs(GRID["crs"])


def write_raster(pdf: pd.DataFrame, path: str) -> dict:
    """Burn best-server RSRP onto the shared 90 m lattice as a COG.

    The extent is the EXACT scope polygon, not the padded one 02_terrain uses:
    transmitters need the pad, receivers do not exist outside the scope. Both
    come from grid_spec(), which snaps to the same lattice, so the two rasters
    have different windows onto identical pixel edges and can be read together
    without resampling either.

    Rasterised with rasterio rather than Sedona's RS_* functions on purpose.
    The array is 2.6 M pixels and already on the driver; moving it into a
    RasterUDT to call RS_MapAlgebra and moving it straight back would be
    ceremony, not distribution. The raster work that genuinely needs Sedona is
    the statewide BDC comparison in 08.
    """
    import rasterio
    from rasterio.features import rasterize
    from rasterio.transform import from_origin

    spec = grid_spec(aoi())
    minx, _, _, maxy = spec["bounds"]
    transform = from_origin(minx, maxy, spec["cell_m"], spec["cell_m"])

    values = pdf["best_rsrp_dbm"].fillna(FLOOR_DBM).to_numpy(dtype="float32")
    geoms = hex_polygons(pdf["h3_str"])
    arr = rasterize(
        zip(geoms, values),
        out_shape=(spec["height"], spec["width"]),
        transform=transform,
        fill=RASTER_NODATA,
        dtype="float32",
        # Centre containment matches how 04_grid chose the cells in the first
        # place, and all_touched=True would let neighbouring hexes overwrite
        # each other along every shared edge.
        all_touched=False,
    )
    with rasterio.open(
        path, "w", driver="COG", dtype="float32", count=1,
        height=spec["height"], width=spec["width"],
        crs=rasterio.crs.CRS.from_epsg(spec["crs"]),
        transform=transform, nodata=RASTER_NODATA, compress="deflate",
    ) as dst:
        dst.write(arr, 1)

    burned = int((arr != RASTER_NODATA).sum())
    print(f"wrote {path} {arr.shape} float32 "
          f"({burned / arr.size:.1%} of pixels inside the receiver grid)")
    return spec


def main() -> int:
    sc = scope()
    sedona = get_sedona(f"rf-coverage-{sc['name']}")
    assert_versions(sedona)

    cov = best_server(sedona)
    dest = out_path("gold", "coverage")
    cov.write.mode("overwrite").parquet(dest)
    print(f"wrote {dest}")

    # Read back rather than reusing `cov`: without this the whole links
    # aggregation is recomputed for the summary, and it also proves the file
    # that downstream stages will actually open is the one just described.
    pdf = sedona.read.parquet(dest).toPandas()

    s = summarise(pdf)
    print(f"\ncoverage at {s['threshold_dbm']:.0f} dBm RSRP, scope={sc['name']}")
    print(f"  cells       {s['cells_covered']:,} / {s['cells']:,} "
          f"({s['cells_covered_frac']:.1%})")
    print(f"  population  {s['population_covered_frac']:.1%} covered, "
          f"{s['population_uncovered']:,.0f} of {s['population']:,.0f} people "
          f"in gap cells")
    print(f"  demand      {s['demand_covered_frac']:.1%} covered, "
          f"{s['demand_uncovered']:,.0f} unserved demand")
    print(f"  median served RSRP {s['median_served_rsrp_dbm']:.1f} dBm")
    print(f"  {s['cells_no_link']:,} cells had no link at all; "
          f"{s['cells_single_server']:,} covered cells have exactly one server")

    write_raster(pdf, gdal_path(out_path("cog", "coverage_rsrp_5070_90m.tif")))

    # Not a DQ gate -- that is 07's job, with the pre-registered thresholds.
    # This is the one invariant local to this stage: a LEFT JOIN that changed
    # the row count means duplicate h3_r8 keys upstream, and every weighted
    # fraction above would then be quietly wrong.
    n_hexes = sedona.read.format("geoparquet").load(
        out_path("silver", "hex_grid")).count()
    if len(pdf) != n_hexes:
        raise RuntimeError(
            f"coverage has {len(pdf)} rows but hex_grid has {n_hexes}; the "
            "best-server join duplicated or dropped receivers"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
