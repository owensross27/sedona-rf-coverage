"""Per-PIXEL propagation surfaces: every 90 m cell, not every hexagon.

The hex grid is the analysis unit (population lives there), but a hexagon is
~930 m across and averages away exactly what makes a propagation map read as
one: terrain shadows. This script runs the same kernel at every 90 m pixel of
the exact scope -- roughly 150x the receiver density -- twice over:

    rsrp_surface_current   best server among today's ASR structures
    rsrp_surface_upgraded  best server after adding the optimizer's 20 sites

plus their difference as a "newly covered" mask. Same physics, same Spark
plan as stage 05 (ST_DWithin pair generation, mapInPandas kernel, one pass --
the upgraded surface is a second aggregation over the same pairs, not a
second run).

Not part of `make pipeline`: this is a visualization product, ~40-90M links
at demo scope, and nothing downstream consumes it. `make surface` builds it.

Deliberately NOT in `make pipeline`: it is a visualization product, nothing
downstream consumes it, and at state scope it is ~30x the work of the whole
hex-level coverage pass. It is a stage rather than a script because that fan-out
is cluster-sized: `make job STAGE=11 SCOPE=state` is the only way it runs
statewide, and `make surface` is the same code on one county on a laptop.

Usage:
    SCOPE=demo LOCAL_OUT=1 python src/11_surface.py     # or: make surface
    make job STAGE=11 SCOPE=state EXECUTORS=3           # statewide, on EKS
"""
import importlib
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import RF, scope  # noqa: E402
from session import assert_versions, get_sedona, out_path  # noqa: E402
from sources import aoi, gdal_path, grid_spec  # noqa: E402

links05 = importlib.import_module("05_links")

THRESHOLD = float(RF["rsrp_threshold_dbm"])
RADIUS_M = float(RF["max_link_km"]) * 1000.0
NODATA = -9999.0
# Receivers per chunk. Sized so one chunk is about the demo-scope job that has
# always run on a laptop (~290k pixels x ~340 towers in range = ~100M pairs);
# statewide that is ~116 towers in range, so 1.5M pixels lands in the same
# place. Raise it on a machine with room, lower it if a chunk spills.
CHUNK_PX = 1_500_000
# Pixels below this go out as the floor value rather than accumulating
# billions of hopeless rows through the shuffle.
FLOOR = -125.0


def transmitters(sedona) -> pd.DataFrame:
    """Current structures plus the recommended builds, tagged apart."""
    cur = sedona.read.format("geoparquet").load(
        out_path("bronze", "towers")).selectExpr(
            "asr_id", "x", "y", "height_agl_m").toPandas()
    cur["tx_height_m"] = cur["height_agl_m"].fillna(
        float(RF["default_tx_height_m"]))
    cur = cur[["asr_id", "x", "y", "tx_height_m"]]

    sit = pd.concat(map(pd.read_parquet, sorted(
        Path(out_path("gold", "siting")).glob("*.parquet"))), ignore_index=True)
    sit = sit[sit["selected_greedy"]]
    new = pd.DataFrame({
        "asr_id": "NEW:" + sit["greedy_rank"].astype(int).astype(str),
        "x": sit["x"], "y": sit["y"], "tx_height_m": sit["tx_height_m"],
    })
    print(f"transmitters: {len(cur)} current + {len(new)} recommended")
    return pd.concat([cur, new], ignore_index=True)


def pixel_centres(spec: dict, mask: np.ndarray) -> pd.DataFrame:
    """One row per in-scope pixel; the id encodes (row, col) for the burn."""
    rows, cols = np.nonzero(mask)
    minx, _, _, maxy = spec["bounds"]
    cell = spec["cell_m"]
    return pd.DataFrame({
        "pix": (rows.astype(np.int64) * spec["width"] + cols),
        "x": minx + (cols + 0.5) * cell,
        "y": maxy - (rows + 0.5) * cell,
    })


def main() -> int:
    sc = scope()
    sedona = get_sedona(f"rf-surface-{sc['name']}")
    assert_versions(sedona)
    sedona.sparkContext.addPyFile(
        str(Path(__file__).resolve().parent / "propagation.py"))

    from rasterio.features import rasterize
    from rasterio.transform import from_origin

    geom = aoi()
    spec = grid_spec(geom)
    minx, _, _, maxy = spec["bounds"]
    transform = from_origin(minx, maxy, spec["cell_m"], spec["cell_m"])
    mask = rasterize([geom], out_shape=(spec["height"], spec["width"]),
                     transform=transform, fill=0, default_value=1,
                     dtype="uint8").astype(bool)

    px = pixel_centres(spec, mask)
    print(f"{len(px):,} in-scope pixels of {mask.size:,} "
          f"({spec['width']}x{spec['height']})")

    tx = transmitters(sedona)
    sedona.createDataFrame(tx).selectExpr(
        "asr_id", "tx_height_m", "ST_Point(x, y) AS geom"
    ).createOrReplaceTempView("tx")
    grid = links05.load_terrain(
        out_path("cog", "dem_5070_90m.tif"),
        out_path("cog", "clutter_5070_90m.tif"),
        out_path("cog", "buildings_5070_90m.tif"),
    )
    bcast = sedona.sparkContext.broadcast(grid)

    # RUN THE RECEIVERS IN CHUNKS. The whole state in one job is 0.93 billion
    # pairs, and it died twice on this laptop: first as "SparkOutOfMemoryError:
    # ... No space left on device" (a DISK error wearing a memory error's name,
    # from a `.repartition(96)` that redistributed 50 GB of fan-out), then as a
    # dead SparkContext once the machine ran out of headroom entirely.
    #
    # A pixel's RSRP depends only on that pixel and the transmitters near it,
    # so the receiver set splits with no interaction at all: the `tx` view is
    # never chunked, so every transmitter within max_link_km is still joined to
    # every pixel, and there is no such thing as a seam. Each chunk is about
    # the size of the demo-scope job that has always run here.
    #
    # This is not a cluster's alternative, it is what makes the stage portable:
    # `make job STAGE=11 SCOPE=state` runs the same code, and bounded chunks
    # keep a spot node's ephemeral disk out of the failure modes above.
    parts = []
    t0 = time.perf_counter()
    n_chunks = (len(px) + CHUNK_PX - 1) // CHUNK_PX
    for i in range(n_chunks):
        chunk = px.iloc[i * CHUNK_PX:(i + 1) * CHUNK_PX]
        sedona.createDataFrame(chunk).selectExpr(
            "pix", "ST_Point(x, y) AS center"
        ).repartition(96).createOrReplaceTempView("px")

        # Identical shape to 05: ST_DWithin fan-out, kernel, floor filter. The
        # pixel id rides in the h3_r8 slot -- the kernel treats it as an opaque
        # long either way.
        pairs = sedona.sql(f"""
            SELECT t.asr_id, p.pix AS h3_r8,
                   ST_X(t.geom) AS tx_x, ST_Y(t.geom) AS tx_y, t.tx_height_m,
                   ST_X(p.center) AS rx_x, ST_Y(p.center) AS rx_y
            FROM tx t JOIN px p ON ST_DWithin(t.geom, p.center, {RADIUS_M})
        """)
        out = pairs.mapInPandas(links05.make_kernel(bcast),
                                schema=links05.OUT_SCHEMA)
        out.filter(f"rsrp_dbm >= {FLOOR}").createOrReplaceTempView("links")
        # One pass, two surfaces: the current one simply ignores NEW: rows.
        part = sedona.sql("""
            SELECT h3_r8 AS pix,
                   MAX(rsrp_dbm) AS rsrp_all,
                   MAX(CASE WHEN asr_id NOT LIKE 'NEW:%' THEN rsrp_dbm END)
                       AS rsrp_cur
            FROM links GROUP BY h3_r8
        """).toPandas()
        parts.append(part)
        print(f"  chunk {i + 1}/{n_chunks}: {len(chunk):,} pixels in, "
              f"{len(part):,} served, {time.perf_counter() - t0:.0f}s elapsed")

    agg = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
    # Chunks partition the pixels, so a pixel cannot appear twice. Asserted
    # rather than assumed: a duplicate here would silently take whichever row
    # the burn wrote last instead of the true best server.
    assert agg["pix"].is_unique, "chunks overlapped; a pixel was served twice"
    print(f"kernel + aggregation: {len(agg):,} pixels served in "
          f"{time.perf_counter() - t0:.0f}s")

    def burn(values: pd.Series) -> np.ndarray:
        arr = np.full((spec["height"], spec["width"]), NODATA, dtype="float32")
        got = values.notna().to_numpy()
        idx = agg["pix"].to_numpy()[got]
        arr[idx // spec["width"], idx % spec["width"]] = \
            values.to_numpy(dtype="float64")[got]
        # In-scope pixels no transmitter reaches at all are the floor, not
        # nodata: "very weak" and "outside the study area" are different facts.
        arr[mask & (arr == NODATA)] = FLOOR
        arr[~mask] = NODATA
        return arr

    import rasterio
    cur, upg = burn(agg["rsrp_cur"]), burn(agg["rsrp_all"])
    for name, arr in (("rsrp_surface_current", cur),
                      ("rsrp_surface_upgraded", upg)):
        dest = gdal_path(out_path("cog", f"{name}_5070_90m.tif"))
        with rasterio.open(
            dest, "w", driver="COG", dtype="float32", count=1,
            height=spec["height"], width=spec["width"],
            crs=rasterio.crs.CRS.from_epsg(spec["crs"]),
            transform=transform, nodata=NODATA, compress="deflate",
        ) as dst:
            dst.write(arr, 1)
        print(f"wrote {dest}")

    live = mask.sum()
    for label, arr in (("current", cur), ("upgraded", upg)):
        cov = ((arr >= THRESHOLD) & mask).sum()
        print(f"  {label}: {cov / live:.1%} of in-scope pixels above "
              f"{THRESHOLD:.0f} dBm")
    newly = (mask & (cur < THRESHOLD) & (upg >= THRESHOLD)).sum()
    print(f"  newly covered by the 20 sites: {newly:,} pixels "
          f"({newly / live:.1%} of the county)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
