"""Offline checks for the bronze stages' pure logic.

Everything here runs with no network, no Spark and no AWS account, which is
what keeps `make test` usable on a cold clone. The parts of stages 01-04 that
genuinely need data assert themselves against that data at runtime instead:
the D/M/S vs total-seconds cross-check and the height-column identity in
01_towers, the DEM-vs-ASR elevation residual in 02_terrain, and the population
conservation and POI vocabulary checks in 04_grid.

What is worth testing offline is the arithmetic that would be wrong silently:
a tile name that resolves to a real but neighbouring tile, or bounds that snap
half a pixel off the shared lattice.
"""
import importlib
import sys
from pathlib import Path

import numpy as np
import shapely

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

towers = importlib.import_module("01_towers")
terrain = importlib.import_module("02_terrain")
ookla = importlib.import_module("10_ookla")
from sources import gdal_path, grid_spec, nrqz  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
def test_dms_converts_with_hemisphere_signs():
    """A sign error here puts West Virginia in China and nothing downstream
    would notice -- every stage would still run, on the wrong continent."""
    lat = towers._dms_to_deg(np.array([38.0]), np.array([24.0]),
                             np.array([18.8]), np.array(["N"]), "S")
    lon = towers._dms_to_deg(np.array([81.0]), np.array([39.0]),
                             np.array([14.8]), np.array(["W"]), "W")
    assert abs(lat[0] - 38.405222) < 1e-5, lat
    assert abs(lon[0] + 81.654111) < 1e-5, lon
    south = towers._dms_to_deg(np.array([38.0]), np.array([0.0]),
                               np.array([0.0]), np.array(["S"]), "S")
    assert south[0] == -38.0


@check
def test_dem_tile_key_matches_the_verified_layout():
    """The object sits inside a directory of the same name and both carry the
    _DEM suffix. Verified against a live listing on 2026-08-09."""
    keys = terrain.dem_tiles((-81.6, 38.4, -81.5, 38.5))
    assert keys == [
        "/vsis3/copernicus-dem-30m/Copernicus_DSM_COG_10_N38_00_W082_00_DEM"
        "/Copernicus_DSM_COG_10_N38_00_W082_00_DEM.tif"
    ], keys


@check
def test_worldcover_tile_key_snaps_to_the_three_degree_grid():
    """WorldCover is a 3 degree grid named by its SOUTH-WEST corner, so a
    point at 38.4N 81.6W lives in the tile called N36W084, not N38W081."""
    keys = terrain.worldcover_tiles((-81.6, 38.4, -81.5, 38.5))
    assert keys == [
        "/vsis3/esa-worldcover/v200/2021/map/"
        "ESA_WorldCover_10m_2021_v200_N36W084_Map.tif"
    ], keys


@check
def test_tile_enumeration_covers_a_multi_tile_box():
    box = (-83.2, 37.8, -81.1, 39.2)
    # 1 degree tiles: lon W084/W083/W082, lat N37/N38/N39.
    assert len(terrain.dem_tiles(box)) == 3 * 3, terrain.dem_tiles(box)
    # 3 degree tiles: lat N36/N39, but a single lon column -- W084 spans
    # -84 to -81, so a box ending at -81.1 never reaches W081.
    wc = terrain.worldcover_tiles(box)
    assert len(wc) == 2, wc
    assert all("W084" in k for k in wc), wc


@check
def test_grid_spec_snaps_outward_onto_the_ninety_metre_lattice():
    """Both rasters are warped onto this grid, and 05_links.load_terrain
    asserts their transforms are identical. If the bounds were not snapped to
    a multiple of the cell size, the two would round independently."""
    spec = grid_spec(shapely.box(1_000_001.0, 1_900_001.0,
                                 1_000_179.0, 1_900_179.0))
    minx, miny, maxx, maxy = spec["bounds"]
    for v in (minx, miny, maxx, maxy):
        assert v % 90 == 0, spec["bounds"]
    # Snapping must only ever grow the extent, never clip it.
    assert minx <= 1_000_001.0 and miny <= 1_900_001.0
    assert maxx >= 1_000_179.0 and maxy >= 1_900_179.0
    assert spec["width"] == (maxx - minx) / 90
    assert spec["height"] == (maxy - miny) / 90


@check
def test_grid_spec_is_exact_on_an_already_aligned_box():
    spec = grid_spec(shapely.box(0.0, 0.0, 900.0, 450.0))
    assert spec["width"] == 10 and spec["height"] == 5, spec


@check
def test_gdal_path_rewrites_only_the_s3a_scheme():
    assert gdal_path("s3a://bucket/cog/dem.tif") == "/vsis3/bucket/cog/dem.tif"
    assert gdal_path("/local/dem.tif") == "/local/dem.tif"
    assert gdal_path("s3://bucket/dem.tif") == "s3://bucket/dem.tif"


@check
def test_nrqz_contains_green_bank_and_excludes_charleston():
    """The Quiet Zone is built from four numbers in config.yml, never fetched,
    so nothing external would catch a transposed corner or a dropped minus
    sign. The failure mode is a legal siting constraint applied silently to
    the wrong half of the state, on a map that still looks right.

    Green Bank is the observatory the zone exists to protect and must be
    inside it. Charleston is 150 km west and must not be."""
    from pyproj import Transformer

    zone = nrqz()
    to_5070 = Transformer.from_crs(4326, 5070, always_xy=True)
    assert shapely.contains_xy(zone, *to_5070.transform(-79.8398, 38.4331))
    assert not shapely.contains_xy(zone, *to_5070.transform(-81.6326, 38.3498))


@check
def test_nrqz_area_matches_the_published_figure():
    """~13,000 square miles is the figure NRAO and the FCC filings quote, and
    it is the only independent check available on a boundary transcribed by
    hand from 47 CFR 1.924 -- one wrong degree moves it by thousands."""
    sq_mi = nrqz().area / 1e6 / 2.58999
    assert 12_900 < sq_mi < 13_300, sq_mi


@check
def test_quadkey_prefix_filter_selects_west_virginia_and_nothing_else():
    """The Ookla tile filter is pure arithmetic and its failure mode is
    silent: a wrong bit interleaving returns a different continent's tiles and
    the stage runs happily on them. "213" is Microsoft's own worked example
    for tile (3,5) at zoom 3, which pins the digit order; the rest pins the
    window over West Virginia."""
    assert ookla._quadkey(3, 5, 3) == "213"
    chs = ookla.quadkey_at(-81.6326, 38.3498, 16)     # Charleston WV
    gbk = ookla.quadkey_at(-79.8398, 38.4331, 16)     # Green Bank WV
    sfo = ookla.quadkey_at(-122.4200, 37.7700, 16)    # San Francisco
    assert len(chs) == 16 and chs.startswith("0320"), chs
    pre = ookla.prefixes_for((-82.65, 37.20, -77.72, 40.64))
    z = ookla.PREFIX_ZOOM
    assert chs[:z] in pre and gbk[:z] in pre, pre
    assert sfo[:z] not in pre, (sfo, pre)


if __name__ == "__main__":
    failed = 0
    for fn in CHECKS:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} bronze checks passed")
    raise SystemExit(1 if failed else 0)
