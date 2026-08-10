"""Stage 10 -- Ookla mobile speedtest tiles, and the one claim they support.

Deliberately NOT part of `make pipeline`. Nothing in stages 01-09 reads this;
it is a validation input, not a pipeline input. It is also the only
CC BY-NC-SA (non-commercial) source in an otherwise permissive project, so a
commercial reuser drops one Makefile target instead of editing the pipeline.

THE ASYMMETRY, which governs everything below. Ookla proves that service was
PRESENT somewhere in a tile at some point in the quarter. It can never prove
absence: a tile with no speedtests may hold no people, no road, or no service,
and the three are indistinguishable. Worse, the selection effect runs the wrong
way -- someone with no signal cannot complete a speedtest, so the population
whose missing coverage we most want to confirm is exactly the one that cannot
appear in the numerator.

So this data gets exactly one number: the false-negative rate on gap calls. Of
the hexes the model calls uncovered, what fraction demonstrably had service?
That is a LOWER BOUND on the model's error. Its complement is not a claim.
Thresholds are pre-registered in config.yml under `validation:`.

Usage:
    SCOPE=demo LOCAL_OUT=1 python src/10_ookla.py
"""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import GRID, SOURCES, VALIDATION  # noqa: E402
from session import assert_versions, get_sedona, out_path  # noqa: E402
from sources import block_groups  # noqa: E402

RES = int(GRID["receiver_res"])
# Ookla's rows are zoom-16 tiles. Zoom 8 is the prefix we filter on: 16 of them
# cover West Virginia, against 2 at zoom 6 (too coarse to prune much) and 195
# at zoom 10 (a needlessly long IN list for the same result).
PREFIX_ZOOM = 8


def _quadkey(x: int, y: int, z: int) -> str:
    """Bing/OSM quadkey from slippy tile x/y.

    Digit d = (x bit) + 2*(y bit), most significant zoom level first. Tile
    (3, 5) at zoom 3 is "213" in Microsoft's own worked example, which is what
    tests/test_bronze.py pins: an interleaving error here selects a different
    continent's tiles and every number downstream still computes happily.
    """
    return "".join(
        str((1 if x & (1 << (i - 1)) else 0) + (2 if y & (1 << (i - 1)) else 0))
        for i in range(z, 0, -1)
    )


def _tile_xy(lon: float, lat: float, z: int) -> "tuple[int, int]":
    """Web-mercator slippy tile indices containing a lon/lat."""
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return min(n - 1, max(0, x)), min(n - 1, max(0, y))


def quadkey_at(lon: float, lat: float, z: int) -> str:
    return _quadkey(*_tile_xy(lon, lat, z), z)


def prefixes_for(bounds_4326) -> "list[str]":
    """Zoom-PREFIX_ZOOM quadkeys covering a lon/lat box.

    y increases southward in web mercator, so the box's NORTH edge gives the
    smallest y. Getting that backwards yields an empty range and a silent zero
    rows, which is why the test pins the count.
    """
    minx, miny, maxx, maxy = bounds_4326
    x0, y0 = _tile_xy(minx, maxy, PREFIX_ZOOM)
    x1, y1 = _tile_xy(maxx, miny, PREFIX_ZOOM)
    return sorted(
        _quadkey(x, y, PREFIX_ZOOM)
        for x in range(x0, x1 + 1)
        for y in range(y0, y1 + 1)
    )


def fetch_tiles(prefixes: "list[str]") -> pd.DataFrame:
    """One quarter of Ookla mobile tiles over the state.

    duckdb rather than Spark, for the same reason 04_grid reads Overture that
    way: this is one 186 MB file, a string filter and a groupby, and Spark's
    only contribution would be the write path.

    Note `substr(...) IN (...)` is a post-decompression filter, not a row-group
    prune. Deliberate: the whole file is 186 MB so the worst case is bounded
    and known. If a future quarter grows enough for that to matter, rewrite as
    OR'd `quadkey BETWEEN` ranges so parquet statistics can prune -- after
    measuring, not before.

    tile_x / tile_y are Ookla's own precomputed tile centroids, so the WKT in
    `tile` is never parsed. One fewer dependency on a geometry library and one
    fewer place for a lon/lat transposition to hide.
    """
    import duckdb

    ok = SOURCES["ookla"]
    path = (f"s3://{ok['bucket']}/{ok['prefix']}"
            f"/year={ok['year']}/quarter={ok['quarter']}/*.parquet")
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    print(f"scanning {path}")
    return con.execute(
        f"""
        SELECT quadkey, tile_x, tile_y, tests, devices, avg_d_kbps
        FROM read_parquet('{path}')
        WHERE substr(quadkey, 1, {PREFIX_ZOOM}) IN
              ({','.join('?' * len(prefixes))})
        """,
        prefixes,
    ).df()


def to_hexes(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate zoom-16 tiles onto H3 r8 receivers by tile centroid.

    Centroid containment, NOT an area-weighted intersection, and the choice is
    driven by what the number is used for. The metric downstream is a presence
    test against an integer count. Area weighting splits `tests` fractionally,
    so a tile with 4 tests straddling two hexes contributes 2.4 and 1.6 and
    both fall under a threshold of 5 -- even though four real speedtests
    happened. Presence must not be diluted by geometry.

    A zoom-16 tile is ~480 m across at 38.4 N against an r8 hex of 0.74 km2,
    so roughly three tiles land in each hex and are summed. Tiles near a hex
    edge land in the neighbour; that slop is well inside a model whose own
    config calls r9 false precision against an 8 dB shadow margin.
    """
    import h3

    cells = [h3.latlng_to_cell(lat, lng, RES)
             for lat, lng in zip(df["tile_y"], df["tile_x"])]
    df = df.assign(h3_str=cells,
                   h3_r8=[h3.str_to_int(c) for c in cells])
    g = df.groupby(["h3_r8", "h3_str"], as_index=False).agg(
        tiles=("quadkey", "size"),
        tests=("tests", "sum"),
        devices=("devices", "sum"),
    )
    # Test-weighted, so a tile with one test does not swing a hex's mean the
    # way a tile with two hundred does.
    wsum = df.assign(w=df["avg_d_kbps"] * df["tests"]).groupby("h3_r8")[
        ["w", "tests"]].sum()
    g["d_kbps"] = (wsum["w"] / wsum["tests"]).reindex(g["h3_r8"]).to_numpy()
    ok = SOURCES["ookla"]
    g["quarter"] = f"{ok['year']}Q{ok['quarter']}"
    return g


def false_negative_rate(cov: pd.DataFrame, ook: pd.DataFrame) -> None:
    """The one claim this dataset supports, printed so it cannot be misread.

    The covered-hex rate beside it is a CONTROL, not a second metric: if gap
    and covered hexes carry speedtests at similar rates then the model is not
    discriminating and the first number says nothing at all. If gap hexes carry
    them at a far lower rate, the model orders the world correctly and the
    first number bounds where it is wrong.
    """
    m = cov.merge(ook[["h3_r8", "tests", "devices"]], on="h3_r8", how="left")
    m[["tests", "devices"]] = m[["tests", "devices"]].fillna(0)
    gap = ~m["is_covered"].to_numpy(dtype=bool)
    min_dev = int(VALIDATION["ookla_min_devices"])
    min_tests = int(VALIDATION["ookla_min_tests"])

    # The denominator that makes everything below interpretable, printed FIRST
    # because without it a low false-negative rate reads as a triumph when it
    # may only mean nobody ran a speedtest here. Ookla lists a tile only if it
    # saw a test that quarter, so this is the sample density, not a bug.
    any_test = int((m["tests"] > 0).sum())
    print(f"\nOokla sample density in scope: {any_test:,} of {len(m):,} hexes "
          f"({any_test / len(m):.1%}) contain any speedtest at all")

    def rate(mask, n_tests):
        tested = ((m["tests"] >= n_tests)
                  & (m["devices"] >= min_dev)).to_numpy()
        hit = int((mask & tested).sum())
        return hit, int(mask.sum()), hit / max(1, int(mask.sum()))

    print(f"Ookla false-negative check -- {ook['quarter'].iloc[0]}, "
          f">={min_tests} tests from >={min_dev} devices")
    for label, mask in (("gap hexes with speedtests    ", gap),
                        ("covered hexes with speedtests", ~gap)):
        hit, tot, frac = rate(mask, min_tests)
        tag = ("LOWER BOUND on the FN rate" if mask is gap
               else "control, not an accuracy claim")
        print(f"  {label} {hit:,} of {tot:,} ({frac:.1%})  {tag}")
    sens = " / ".join(
        f"{rate(gap, n)[2]:.1%}" for n in VALIDATION["ookla_test_sensitivity"])
    print("  at >=" + " / >=".join(
        str(n) for n in VALIDATION["ookla_test_sensitivity"])
        + f" tests: {sens}")

    fn = rate(gap, min_tests)[2]
    ctrl = rate(~gap, min_tests)[2]
    # The control is what decides whether the first number means anything. If
    # hexes the model says ARE covered barely carry speedtests either, then the
    # data cannot discriminate and a low false-negative rate is a statement
    # about Ookla's sample, not about this model. Refusing to quote the number
    # in that case is the whole reason the control is computed.
    if ctrl < 0.10:
        print(f"\n  VERDICT: NOT USABLE at this scope. Only {ctrl:.1%} of hexes"
              " the model calls\n  COVERED carry speedtests clearing the "
              "threshold, so the sample cannot\n  discriminate covered from "
              "uncovered ground and the "
              f"{fn:.1%} above is a statement\n  about Ookla's sample density, "
              "not about this model. Do not quote it as\n  validation. Ookla "
              "lists a tile only where somebody ran a test, and most\n  of this"
              " scope is forest -- re-run at SCOPE=state, where the denominator"
              "\n  is 25x larger, before drawing any conclusion.")
        return
    print(f"\n  Read: at least {fn:.1%} of the cells this model calls "
          "uncovered demonstrably had\n  mobile service. The remainder is NOT "
          "evidence of correctness -- a tile with no\n  speedtests may hold no "
          "people, no road, or no service, and someone with no\n  signal "
          "cannot run a speedtest at all. This number can only rise with more"
          "\n  data. It is not \"the model is wrong this often\".")


def main() -> int:
    sedona = get_sedona("rfc-10-ookla")
    assert_versions(sedona)

    bounds = tuple(block_groups().to_crs(4326).total_bounds)
    prefixes = prefixes_for(bounds)
    print(f"state bounds {tuple(round(b, 3) for b in bounds)} -> "
          f"{len(prefixes)} zoom-{PREFIX_ZOOM} quadkey prefixes")

    df = fetch_tiles(prefixes)
    if df.empty:
        raise RuntimeError(
            f"no Ookla tiles matched prefixes {prefixes}. The filter is pure "
            "arithmetic, so an empty result is a bug here, not an empty quarter"
        )
    # Runtime assertion, free: centroids outside the state bounds mean the
    # prefix arithmetic put us on the wrong part of the planet. A zoom-8 tile
    # is ~150 km across, so the prefix window legitimately overhangs the state.
    print(f"tiles: {len(df):,} across {df['quadkey'].str[:PREFIX_ZOOM].nunique()}"
          f" prefixes, lon {df['tile_x'].min():.2f}..{df['tile_x'].max():.2f}, "
          f"lat {df['tile_y'].min():.2f}..{df['tile_y'].max():.2f}")
    assert -84.0 < df["tile_x"].mean() < -77.0, df["tile_x"].mean()
    assert 36.0 < df["tile_y"].mean() < 41.0, df["tile_y"].mean()

    g = to_hexes(df)
    # NOT state-clipped, and the wording says so: a zoom-8 prefix is ~150 km
    # across, so this window overhangs into Pennsylvania, Ohio and Virginia
    # (the busiest tile in it is in Pittsburgh). Clipping would cost a spatial
    # join for a log line; the h3_r8 join against the coverage table is what
    # actually scopes the numbers that get quoted.
    print(f"H3 r{RES} cells with speedtests: {len(g):,} in the quadkey window "
          f"(overhangs the state; the coverage join is what scopes it) -- "
          f"{g['tests'].sum():,} tests from {g['devices'].sum():,} devices, "
          f"median {np.median(g['d_kbps']) / 1000:.1f} Mbps down")

    dest = out_path("bronze", "ookla_h3")
    sedona.createDataFrame(g).write.mode("overwrite").parquet(dest)
    print(f"wrote {len(g)} cells -> {dest}")

    # The metric, when a coverage table exists to compare against. Reading it
    # with pandas rather than Spark keeps this runnable after teardown, the
    # same reason scripts/make_map.py never starts a session.
    cov_path = Path(out_path("gold", "coverage"))
    if cov_path.exists():
        cov = pd.read_parquet(cov_path)
        false_negative_rate(cov[["h3_r8", "is_covered"]], g)
    else:
        print(f"\n(no coverage table at {cov_path} -- run 06_coverage for the "
              "false-negative rate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
