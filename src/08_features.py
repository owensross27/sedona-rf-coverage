"""Stage 08 -- per-hex terrain, land-cover and building features, in Sedona.

This is the stage where the raster work is genuinely Sedona's to do, and it is
deliberately built the opposite way round from the propagation kernel:

    05_links samples 380M pixel lookups along terrain profiles. That is a
    hot inner loop over two broadcast arrays, and pushing every lookup through
    a JVM raster call was measured (docs/benchmarks.md) and rejected.

    This stage asks ~3k-85k zonal questions of whole rasters: what is the
    relief inside this hexagon, how much of it is forest, how tall are its
    buildings. That is tiled raster analytics -- RS_TileExplode the COGs once,
    spatial-join hexagons to tiles with RS_Intersects, RS_ZonalStatsAll per
    intersection, and aggregate the combinable pieces (sum, count, min, max)
    per hexagon. The same plan runs unchanged on a laptop and on the cluster.

Right tool at each scale, with the benchmark that justifies each choice --
that is the architecture claim of the whole repository, and this stage is the
half of it that the propagation kernel cannot show.

Everything here is derived data for analysis and the writeup ("what drives
the gaps"); nothing feeds back into the propagation physics, so features can
be recomputed freely without touching pre-registered results.

Outputs:
    silver/hex_features             one row per receiver cell
    cog/coverage_gap_mask_...tif    RS_MapAlgebra gap mask, written via RS_AsCOG
    stdout                          covered-vs-gap feature comparison

Usage:
    SCOPE=demo LOCAL_OUT=1 python src/08_features.py
"""
import importlib
import sys
import time
from pathlib import Path


import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import RF, scope  # noqa: E402
from session import assert_versions, get_sedona, out_path  # noqa: E402
# GDAL does not understand the s3a:// scheme that out_path returns, so every
# rasterio call in this file must route through gdal_path (-> /vsis3/). Hoisted
# to module scope because two separate places need it and one of them did not
# have it, which only surfaced at SCOPE=state -- see the self-check below.
from sources import gdal_path  # noqa: E402

coverage06 = importlib.import_module("06_coverage")

THRESHOLD = float(RF["rsrp_threshold_dbm"])
TILE = 512


def register_tiles(sedona, name: str, path: str) -> None:
    """One COG -> a view of georeferenced tiles named `<name>_tiles`.

    The tile column is aliased `rast` so RS_MapAlgebra scripts can refer to it
    by the name the jiffle binding expects.
    """
    sedona.read.format("binaryFile").load(path) \
        .createOrReplaceTempView(f"{name}_bin")
    sedona.sql(f"""
        SELECT RS_TileExplode(RS_FromGeoTiff(content), {TILE}, {TILE})
               AS (tx, ty, rast)
        FROM {name}_bin
    """).createOrReplaceTempView(f"{name}_tiles")


def hex_view(sedona) -> pd.DataFrame:
    """Receiver hexagons as EPSG:5070 polygons, from gold/coverage."""
    cov = sedona.read.parquet(out_path("gold", "coverage")) \
        .select("h3_r8", "h3_str", "pop", "demand", "is_covered").toPandas()
    geoms = coverage06.hex_polygons(cov["h3_str"])
    hx = pd.DataFrame({"h3_r8": cov["h3_r8"], "wkt": geoms.to_wkt()})
    sedona.createDataFrame(hx).createOrReplaceTempView("hx_raw")
    sedona.sql("""
        SELECT h3_r8, ST_SetSRID(ST_GeomFromWKT(wkt), 5070) AS geom
        FROM hx_raw
    """).createOrReplaceTempView("hx")
    return cov


def zonal(sedona, tiles: str, agg: str) -> pd.DataFrame:
    """Per-hex aggregation of RS_ZonalStatsAll over a tiled raster.

    Only COMBINABLE statistics survive the tile boundary: sum, count, min and
    max of per-tile partials are exact, while a mean of per-tile means would
    weight a sliver of hexagon in one tile equally with its bulk in another.
    Means are therefore reconstructed as sum/count after aggregation.
    """
    df = sedona.sql(f"""
        WITH parts AS (
            SELECT h.h3_r8, RS_ZonalStatsAll(t.rast, h.geom, 1) AS s
            FROM {tiles} t JOIN hx h ON RS_Intersects(t.rast, h.geom)
        )
        SELECT h3_r8,
               SUM(s.sum)   AS {agg}_sum,
               SUM(s.count) AS {agg}_n,
               MIN(s.min)   AS {agg}_min,
               MAX(s.max)   AS {agg}_max
        FROM parts GROUP BY h3_r8
    """).toPandas()
    return df.set_index("h3_r8")


def gap_mask_cog(sedona) -> None:
    """The uncovered-area mask, computed and encoded inside Sedona.

    RS_MapAlgebra runs the jiffle script against the coverage surface and
    RS_AsCOG hands back finished COG bytes -- the raster never becomes a numpy
    array on the way. -9000 stands clear of both the -9999 nodata and every
    real RSRP value (floor -125), so the two conditions split nodata from
    "modelled and weak".
    """
    import rasterio
    from rasterio.io import MemoryFile

    row = sedona.sql(f"""
        SELECT RS_AsCOG(RS_MapAlgebra(RS_FromGeoTiff(content), 'us',
            'out = (rast[0] > -9000.0 && rast[0] < {THRESHOLD}) ? 1 : 0;'
        )) AS cog
        FROM cov_bin
    """).first()
    dest = out_path("cog", "coverage_gap_mask_5070_90m.tif")
    with MemoryFile(bytes(row.cog)) as mem, mem.open() as src:
        profile = src.profile
        with rasterio.open(gdal_path(dest), "w", **profile) as dst:
            dst.write(src.read())
    print(f"wrote {dest} (RS_MapAlgebra -> RS_AsCOG)")


def self_check(sedona, feats: pd.DataFrame) -> None:
    """Recompute three hexes' mean elevation with rasterio and compare.

    The zonal plan above involves a tiling, a spatial join, a rasterised zone
    and an aggregation -- four places for a silent half-pixel disagreement.
    Independently recomputing a sample with a different library bounds all
    four at once. Tolerance is one part in fifty: the two rasterisations may
    legitimately disagree about a boundary pixel row, which moves a hexagon
    mean by centimetres, not metres.
    """
    import rasterio
    from rasterio.features import geometry_mask

    rows = feats.sample(3, random_state=7)
    hx = sedona.sql("SELECT h3_r8, ST_AsText(geom) AS wkt FROM hx").toPandas() \
        .set_index("h3_r8")
    import shapely.wkt as swkt
    # gdal_path, NOT the bare out_path. This is the one rasterio call in the
    # file that lacked it, and LOCAL_OUT=1 hid that for every demo-scope run:
    # out_path returns a local filesystem path there, so GDAL was happy. At
    # SCOPE=state it returns s3a://, which GDAL has never heard of, and the
    # failure reads as a missing file rather than an unsupported scheme:
    #   RasterioIOError: s3a://.../cog/dem_5070_90m.tif: No such file or directory
    with rasterio.open(gdal_path(out_path("cog", "dem_5070_90m.tif"))) as src:
        dem = src.read(1)
        for h3_id, row in rows.iterrows():
            geom = swkt.loads(hx.loc[h3_id, "wkt"])
            mask = geometry_mask([geom], out_shape=dem.shape,
                                 transform=src.transform, invert=True)
            want = float(dem[mask].mean())
            got = row["elev_mean_m"]
            if abs(got - want) > abs(want) / 50.0:
                raise RuntimeError(
                    f"hex {h3_id}: Sedona zonal mean {got:.1f} m vs rasterio "
                    f"{want:.1f} m -- the tiling or the zone rasterisation "
                    "is off"
                )
    print(f"self-check: 3 hexes recomputed with rasterio, "
          f"max |delta| within 2% (Sedona zonal plan agrees)")


def main() -> int:
    sc = scope()
    sedona = get_sedona(f"rf-features-{sc['name']}")
    assert_versions(sedona)

    cov = hex_view(sedona)
    print(f"{len(cov):,} receiver cells")

    register_tiles(sedona, "dem", out_path("cog", "dem_5070_90m.tif"))
    register_tiles(sedona, "bldg", out_path("cog", "buildings_5070_90m.tif"))
    # Class masks are themselves map algebra over the clutter tiles, so the
    # land-cover fractions come out of the same tiled plan as everything else.
    # The building presence mask is what turns "sum of heights over the whole
    # zone" into "mean height over the pixels that hold a building".
    sedona.read.format("binaryFile") \
        .load(out_path("cog", "clutter_5070_90m.tif")) \
        .createOrReplaceTempView("clut_bin")
    for name, src, expr in (("tree", "clut_bin", "rast[0] == 10"),
                            ("built", "clut_bin", "rast[0] == 50"),
                            ("bpres", "bldg_bin", "rast[0] > 0")):
        sedona.sql(f"""
            SELECT tx, ty, RS_MapAlgebra(rast, 'us', 'out = ({expr}) ? 1 : 0;')
                   AS rast
            FROM (SELECT RS_TileExplode(RS_FromGeoTiff(content),
                                        {TILE}, {TILE}) AS (tx, ty, rast)
                  FROM {src})
        """).createOrReplaceTempView(f"{name}_tiles")
    sedona.read.format("binaryFile") \
        .load(out_path("cog", "coverage_rsrp_5070_90m.tif")) \
        .createOrReplaceTempView("cov_bin")

    t0 = time.perf_counter()
    dem = zonal(sedona, "dem_tiles", "elev")
    tree = zonal(sedona, "tree_tiles", "tree")
    built = zonal(sedona, "built_tiles", "built")
    bldg = zonal(sedona, "bldg_tiles", "bldg")
    bpres = zonal(sedona, "bpres_tiles", "bpres")
    elapsed = time.perf_counter() - t0

    # Denominators come from the DEM's pixel count, not each mask's own: S12
    # proved the DEM has zero nodata cells in scope, so its count is the true
    # zone size, while a mask's count depends on how the engine treats an
    # inherited nodata value. Sums of ones are immune to that either way.
    feats = pd.DataFrame(index=dem.index)
    n_px = dem["elev_n"]
    feats["elev_mean_m"] = dem["elev_sum"] / n_px
    feats["relief_m"] = dem["elev_max"] - dem["elev_min"]
    feats["tree_frac"] = tree["tree_sum"] / n_px
    feats["built_frac"] = built["built_sum"] / n_px
    feats["bldg_max_m"] = bldg["bldg_max"]
    feats["bldg_px"] = bpres["bpres_sum"]
    feats["bldg_mean_m"] = (bldg["bldg_sum"]
                            / bpres["bpres_sum"].clip(lower=1.0)).fillna(0.0)
    print(f"zonal stats: 5 tiled rasters x {len(feats):,} hexes in "
          f"{elapsed:.1f}s (RS_TileExplode + RS_Intersects + RS_ZonalStatsAll)")

    self_check(sedona, feats)

    out = cov.merge(feats, on="h3_r8", how="left")
    missing = int(out["elev_mean_m"].isna().sum())
    if missing:
        raise RuntimeError(
            f"{missing} hexes got no zonal stats; the tile join dropped them")

    dest = out_path("silver", "hex_features")
    sedona.createDataFrame(out.drop(columns=["is_covered"])
                           .merge(cov[["h3_r8", "is_covered"]], on="h3_r8")) \
        .write.mode("overwrite").parquet(dest)
    print(f"wrote {dest}")

    gap_mask_cog(sedona)

    # The writeup numbers: what actually distinguishes a gap cell.
    g, c = out[~out["is_covered"]], out[out["is_covered"]]
    print(f"\nwhat drives the gaps (gap vs covered cell means):")
    for col, label in (("relief_m", "terrain relief within the cell (m)"),
                       ("tree_frac", "tree-cover fraction"),
                       ("built_frac", "built-up fraction"),
                       ("elev_mean_m", "mean elevation (m)")):
        print(f"  {label:38s} {g[col].mean():8.2f} vs {c[col].mean():8.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
