"""Correctness gate for the propagation kernel.

Runs under pytest, or standalone with `python tests/test_propagation.py` so
that `make demo` has no framework dependency.

The diffraction anchors here are published values, not self-generated: J(0) is
the textbook 6 dB grazing-incidence knife-edge loss and J(1) is ~13.9 dB. If
those two move, the model changed.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from propagation import (  # noqa: E402
    TerrainGrid,
    diffraction_loss_db,
    fspl_db,
    knife_edge_loss_db,
    link_rsrp,
)

FREQ = 700.0
K = 1.333


def _grid(dem: np.ndarray, clutter: np.ndarray | None = None) -> TerrainGrid:
    if clutter is None:
        clutter = np.full(dem.shape, 30, dtype=np.uint8)  # grassland, 0 dB
    lut = np.zeros(256)
    lut[10] = 12.0   # tree cover
    lut[50] = 15.0   # built-up
    return TerrainGrid(dem=dem, clutter=clutter, x0=0.0, y0=0.0,
                       cell_m=90.0, clutter_lut=lut)


def test_fspl_matches_hand_computation():
    # 20*log10(10 km) + 20*log10(700 MHz) + 32.44
    #   = 20 + 56.9020 + 32.44 = 109.342 dB
    got = fspl_db(np.array([10_000.0]), FREQ)[0]
    assert abs(got - 109.342) < 0.01, got


def test_fspl_doubles_distance_for_six_db():
    """Free space is inverse-square: 2x distance is exactly 6.02 dB."""
    a = fspl_db(np.array([5_000.0]), FREQ)[0]
    b = fspl_db(np.array([10_000.0]), FREQ)[0]
    assert abs((b - a) - 6.0206) < 0.001, b - a


def test_knife_edge_published_anchors():
    """J(0) = 6.02 dB (grazing) and J(1) = 13.9 dB, per ITU-R P.526."""
    assert abs(knife_edge_loss_db(np.array([0.0]))[0] - 6.03) < 0.01
    assert abs(knife_edge_loss_db(np.array([1.0]))[0] - 13.93) < 0.02


def test_knife_edge_is_zero_when_clear():
    """Below v = -0.78 the obstruction contributes nothing."""
    v = np.array([-0.79, -1.0, -5.0, -np.inf])
    assert np.all(knife_edge_loss_db(v) == 0.0)


def test_knife_edge_monotonic_in_v():
    v = np.linspace(-0.78, 5.0, 50)
    loss = knife_edge_loss_db(v)
    assert np.all(np.diff(loss) >= -1e-9), "J(v) must be non-decreasing"


def test_flat_terrain_is_line_of_sight_and_lossless():
    dem = np.zeros((100, 100), dtype=np.int16)
    out = link_rsrp(
        _grid(dem),
        tx_x=np.array([0.0]), tx_y=np.array([0.0]), tx_height_m=np.array([50.0]),
        rx_x=np.array([5000.0]), rx_y=np.array([0.0]), rx_height_m=1.5,
        freq_mhz=FREQ, eirp_dbm=60.0, subcarriers=600,
        shadow_margin_db=8.0, n_samples=128, k_factor=K,
    )
    assert bool(out["is_los"][0]), "flat ground with a 50 m mast must be LOS"
    assert out["diffraction_db"][0] == 0.0


def test_no_phantom_diffraction_over_flat_ground_at_range():
    """Regression: a smooth ground plane is not a knife edge.

    An earlier revision searched for the maximum Fresnel parameter over every
    sample. Because a 1.5 m receiver never clears its first Fresnel zone at
    range, the ground beside it registered as an obstruction and Deygout
    charged for it three times -- 11.4 dB of diffraction loss across dead-flat
    terrain, on every link in the state. Diffraction is now restricted to
    terrain that rises above the geometric ray.
    """
    # Kept inside the ~34 km radio horizon on purpose: past it the earth's own
    # bulge is a real obstruction, which
    # test_earth_curvature_blocks_beyond_the_radio_horizon covers separately.
    dem = np.zeros((600, 600), dtype=np.int16)
    n = 6
    out = link_rsrp(
        _grid(dem),
        tx_x=np.zeros(n), tx_y=np.zeros(n), tx_height_m=np.full(n, 50.0),
        rx_x=np.linspace(2_000.0, 30_000.0, n), rx_y=np.zeros(n), rx_height_m=1.5,
        freq_mhz=FREQ, eirp_dbm=60.0, subcarriers=600,
        shadow_margin_db=8.0, n_samples=128, k_factor=K,
    )
    assert np.all(out["diffraction_db"] == 0.0), out["diffraction_db"]
    assert np.all(out["is_los"])
    # And the surviving loss must be free space alone, to the dB.
    assert np.allclose(out["path_loss_db"], fspl_db(out["distance_m"], FREQ))


def test_ridge_blocks_and_costs_more_than_grazing():
    """A 200 m ridge mid-path is well past grazing, so loss must exceed 6 dB."""
    dem = np.zeros((100, 100), dtype=np.int16)
    dem[0, 25:32] = 200
    out = link_rsrp(
        _grid(dem),
        tx_x=np.array([0.0]), tx_y=np.array([0.0]), tx_height_m=np.array([50.0]),
        rx_x=np.array([5000.0]), rx_y=np.array([0.0]), rx_height_m=1.5,
        freq_mhz=FREQ, eirp_dbm=60.0, subcarriers=600,
        shadow_margin_db=8.0, n_samples=128, k_factor=K,
    )
    assert not bool(out["is_los"][0])
    assert out["diffraction_db"][0] > 6.0, out["diffraction_db"][0]


def test_taller_ridge_costs_more_loss():
    dem_low = np.zeros((100, 100), dtype=np.int16)
    dem_low[0, 25:32] = 120
    dem_high = dem_low.copy()
    dem_high[0, 25:32] = 400
    args = dict(
        tx_x=np.array([0.0]), tx_y=np.array([0.0]), tx_height_m=np.array([50.0]),
        rx_x=np.array([5000.0]), rx_y=np.array([0.0]), rx_height_m=1.5,
        freq_mhz=FREQ, eirp_dbm=60.0, subcarriers=600,
        shadow_margin_db=8.0, n_samples=128, k_factor=K,
    )
    lo = link_rsrp(_grid(dem_low), **args)["diffraction_db"][0]
    hi = link_rsrp(_grid(dem_high), **args)["diffraction_db"][0]
    assert hi > lo, (lo, hi)


def test_deygout_counts_a_second_ridge():
    """Two ridges must cost more than the taller one alone -- that is the
    entire reason for using Deygout rather than a single knife edge."""
    one = np.zeros((100, 100), dtype=np.int16)
    one[0, 20:24] = 300
    two = one.copy()
    two[0, 38:42] = 260
    args = dict(
        tx_x=np.array([0.0]), tx_y=np.array([0.0]), tx_height_m=np.array([50.0]),
        rx_x=np.array([5000.0]), rx_y=np.array([0.0]), rx_height_m=1.5,
        freq_mhz=FREQ, eirp_dbm=60.0, subcarriers=600,
        shadow_margin_db=8.0, n_samples=128, k_factor=K,
    )
    single = link_rsrp(_grid(one), **args)["diffraction_db"][0]
    double = link_rsrp(_grid(two), **args)["diffraction_db"][0]
    assert double > single, (single, double)


def test_clutter_applies_receiver_class_only():
    """Tree cover at the receiver costs its configured 12 dB, and the tx-side
    land cover is irrelevant (path-integrated clutter would double-count)."""
    dem = np.zeros((100, 100), dtype=np.int16)
    grass = np.full(dem.shape, 30, dtype=np.uint8)
    trees_at_rx = grass.copy()
    trees_at_rx[0, 50:60] = 10
    args = dict(
        tx_x=np.array([0.0]), tx_y=np.array([0.0]), tx_height_m=np.array([50.0]),
        rx_x=np.array([5000.0]), rx_y=np.array([0.0]), rx_height_m=1.5,
        freq_mhz=FREQ, eirp_dbm=60.0, subcarriers=600,
        shadow_margin_db=8.0, n_samples=128, k_factor=K,
    )
    open_rx = link_rsrp(_grid(dem, grass), **args)
    treed_rx = link_rsrp(_grid(dem, trees_at_rx), **args)
    assert treed_rx["clutter_db"][0] == 12.0
    assert abs((open_rx["rsrp_dbm"][0] - treed_rx["rsrp_dbm"][0]) - 12.0) < 1e-9


def test_rsrp_decreases_with_distance():
    dem = np.zeros((200, 200), dtype=np.int16)
    n = 8
    out = link_rsrp(
        _grid(dem),
        tx_x=np.zeros(n), tx_y=np.zeros(n), tx_height_m=np.full(n, 50.0),
        rx_x=np.linspace(1000.0, 15000.0, n), rx_y=np.zeros(n), rx_height_m=1.5,
        freq_mhz=FREQ, eirp_dbm=60.0, subcarriers=600,
        shadow_margin_db=8.0, n_samples=128, k_factor=K,
    )
    assert np.all(np.diff(out["rsrp_dbm"]) < 0)


def _radio_horizon_m(height_m: float) -> float:
    """sqrt(2 * k * R * h) -- the effective-earth radio horizon."""
    from propagation import EARTH_RADIUS_M
    return float(np.sqrt(2.0 * K * EARTH_RADIUS_M * height_m))


def test_earth_curvature_blocks_beyond_the_radio_horizon():
    """Over flat ground the ONLY obstruction is the earth itself, so the
    line-of-sight flag must flip at the combined radio horizon of the two
    antennas -- about 34 km for a 50 m mast and a 1.5 m handset.

    This is what makes the 40 km pair-generation cap in config.yml safe rather
    than merely generous: links past the horizon are computed, found to be
    obstructed, and fall below threshold on their own.
    """
    horizon = _radio_horizon_m(50.0) + _radio_horizon_m(1.5)
    assert 33_000 < horizon < 35_000, horizon
    dem = np.zeros((700, 700), dtype=np.int16)
    args = dict(
        tx_x=np.array([0.0]), tx_y=np.array([0.0]), tx_height_m=np.array([50.0]),
        rx_y=np.array([0.0]), rx_height_m=1.5, freq_mhz=FREQ, eirp_dbm=60.0,
        subcarriers=600, shadow_margin_db=8.0, n_samples=128, k_factor=K,
    )
    inside = link_rsrp(_grid(dem), rx_x=np.array([horizon * 0.8]), **args)
    outside = link_rsrp(_grid(dem), rx_x=np.array([horizon * 1.2]), **args)
    assert bool(inside["is_los"][0]), "inside the horizon must be clear"
    assert inside["diffraction_db"][0] == 0.0
    assert not bool(outside["is_los"][0]), "past the horizon must be obstructed"
    assert outside["diffraction_db"][0] > 6.0, outside["diffraction_db"][0]


def test_link_budget_closes_within_the_horizon():
    """A clear 25 km link must land above the -105 dBm coverage threshold.

    This ties the kernel to config.yml: if eirp_dbm, subcarriers,
    shadow_margin_db or rsrp_threshold_dbm are edited so that even an
    unobstructed mid-range link cannot close, the model would report a state
    with essentially no coverage and this test says so first.
    """
    dem = np.zeros((500, 500), dtype=np.int16)
    out = link_rsrp(
        _grid(dem),
        tx_x=np.array([0.0]), tx_y=np.array([0.0]), tx_height_m=np.array([50.0]),
        rx_x=np.array([25_000.0]), rx_y=np.array([0.0]), rx_height_m=1.5,
        freq_mhz=FREQ, eirp_dbm=60.0, subcarriers=600,
        shadow_margin_db=8.0, n_samples=128, k_factor=K,
    )
    assert bool(out["is_los"][0])
    rsrp = out["rsrp_dbm"][0]
    assert rsrp > -105.0, f"clear 25 km link at {rsrp:.1f} dBm fails threshold"
    assert abs(rsrp - -93.1) < 0.5, rsrp


def test_grid_shape_mismatch_is_rejected():
    try:
        TerrainGrid(
            dem=np.zeros((10, 10)), clutter=np.zeros((10, 11), dtype=np.uint8),
            x0=0.0, y0=0.0, cell_m=90.0, clutter_lut=np.zeros(256),
        )
    except ValueError:
        return
    raise AssertionError("mismatched dem/clutter grids must raise")


def test_sampling_clamps_out_of_bounds():
    """A path clipping the state edge must clamp, not crash or wrap."""
    dem = np.arange(100, dtype=np.int16).reshape(10, 10)
    g = _grid(dem, np.full((10, 10), 30, dtype=np.uint8))
    elev, _ = g.sample(np.array([-1e6, 1e6]), np.array([1e6, -1e6]))
    assert elev[0] == dem[0, 0] and elev[1] == dem[-1, -1]


def test_batch_matches_individual_links():
    """Vectorization must not change any single answer -- the whole kernel
    rests on batching being transparent."""
    rng = np.random.default_rng(0)
    dem = rng.integers(0, 600, size=(300, 300)).astype(np.int16)
    g = _grid(dem)
    n = 25
    tx_x, tx_y = rng.uniform(0, 5000, n), rng.uniform(-5000, 0, n)
    rx_x, rx_y = rng.uniform(5000, 20000, n), rng.uniform(-20000, -5000, n)
    h = rng.uniform(20, 90, n)
    common = dict(rx_height_m=1.5, freq_mhz=FREQ, eirp_dbm=60.0, subcarriers=600,
                  shadow_margin_db=8.0, n_samples=128, k_factor=K)
    batch = link_rsrp(g, tx_x, tx_y, h, rx_x, rx_y, **common)["rsrp_dbm"]
    for i in range(n):
        one = link_rsrp(g, tx_x[i:i+1], tx_y[i:i+1], h[i:i+1],
                        rx_x[i:i+1], rx_y[i:i+1], **common)["rsrp_dbm"][0]
        assert abs(one - batch[i]) < 1e-9, (i, one, batch[i])


def test_diffraction_loss_is_never_negative():
    """Diffraction can only remove signal. A negative loss would mean the
    geometry produced gain out of nowhere."""
    rng = np.random.default_rng(1)
    elev = rng.uniform(0, 1200, size=(200, 128))
    path = rng.uniform(1000, 40000, size=200)
    z_tx = elev[:, 0] + 50.0
    z_rx = elev[:, -1] + 1.5
    loss, _ = diffraction_loss_db(elev, path, z_tx, z_rx, FREQ, K)
    assert np.all(loss >= 0.0)
    assert np.all(np.isfinite(loss))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} propagation checks passed")
