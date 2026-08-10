"""Vectorized RF path-loss kernel: free space + terrain diffraction + clutter.

This is the performance-critical file. It is deliberately pure numpy with no
Spark import so it can be unit-tested (and profiled) on its own; 05_links.py
wraps it in a pandas_udf.

WHY NOT SEDONA RASTER FUNCTIONS FOR THE INNER LOOP
--------------------------------------------------
The obvious implementation explodes each transmitter/receiver pair into its
terrain samples and calls RS_Value per sample. At state scope that is roughly
3M pairs x 128 samples = ~380M raster lookups, each crossing the JVM boundary.
Instead the whole state's DEM and clutter are broadcast once as two small
numpy arrays (int16 + uint8 at 90 m: ~55 MB for West Virginia) and every
sample becomes a fancy-index into RAM.

Sedona still does the work it is genuinely best at -- the ST_DWithin spatial
join that generates the candidate pairs in the first place, the block-group
areal interpolation, and zonal statistics. See docs/benchmarks.md for the
measured comparison; the point of publishing both numbers is that "I profiled
the obvious approach" is a stronger claim than "I used the fast one".

MODEL
-----
    RSRP = EIRP - 10*log10(subcarriers) - FSPL - L_diffraction - L_clutter - shadow

FSPL          Friis free-space loss.
L_diffraction Deygout three-edge knife-edge construction over the terrain
              profile, with an effective-earth-radius correction for
              atmospheric refraction (ITU-R P.526 J(v)).
L_clutter     Excess loss for the RECEIVER's land cover class. Applied at the
              receiver only, not integrated along the path: path-integrated
              clutter would double-count the obstruction that the diffraction
              term already models.

Known ceilings, stated rather than hidden (see README "Limitations"):
  - Deygout over-predicts loss when several edges are of similar prominence.
    It is used anyway because the alternative (single knife-edge) systematically
    UNDER-predicts loss in multi-ridge terrain, which would inflate the
    headline coverage number -- the direction of error that flatters the
    result is the one to avoid.
  - Isotropic transmitters: no antenna pattern, downtilt, or sectorization.
  - No interference, no capacity, no building penetration.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_M = 6371000.0
C_M_PER_S = 299792458.0

# Below this Fresnel-Kirchhoff parameter the obstruction is clear enough that
# ITU-R P.526's J(v) is ~0 dB; it is also the conventional threshold for
# calling a path line-of-sight.
V_CLEAR = -0.78


def fspl_db(distance_m: np.ndarray, freq_mhz: float) -> np.ndarray:
    """Free-space path loss in dB. 32.44 is the constant for km and MHz.

    Distances below 1 m are clamped: a receiver co-located with its tower is
    not physically meaningful and log10(0) would poison the whole batch.
    """
    d_km = np.maximum(distance_m, 1.0) / 1000.0
    return 20.0 * np.log10(d_km) + 20.0 * np.log10(freq_mhz) + 32.44


def knife_edge_loss_db(v: np.ndarray) -> np.ndarray:
    """ITU-R P.526 single knife-edge diffraction loss J(v), in dB.

        J(v) = 6.9 + 20*log10( sqrt((v-0.1)^2 + 1) + v - 0.1 )   for v > -0.78

    Sanity anchors this reproduces (see test_propagation.py): J(0) = 6.02 dB,
    the classic grazing-incidence result, and J(1) = 13.9 dB.
    """
    v = np.asarray(v, dtype=np.float64)
    out = np.zeros_like(v)
    lit = v > V_CLEAR
    if np.any(lit):
        t = v[lit] - 0.1
        out[lit] = 6.9 + 20.0 * np.log10(np.sqrt(t * t + 1.0) + t)
    return np.maximum(out, 0.0)


@dataclass(frozen=True)
class TerrainGrid:
    """A DEM and a co-registered clutter raster sharing one affine transform.

    Both arrays MUST be on the same grid -- that shared registration is the
    reason 02_terrain.py reprojects them onto a single EPSG:5070 90 m grid, and
    it is what lets one set of pixel indices read both.

    x0, y0 is the centre of pixel [0, 0] (north-west corner pixel); y
    decreases as the row index increases, per normal raster convention.
    """
    dem: np.ndarray            # (rows, cols) int16 or float32, metres
    clutter: np.ndarray        # (rows, cols) uint8, WorldCover class codes
    x0: float
    y0: float
    cell_m: float
    clutter_lut: np.ndarray    # (256,) float, class code -> excess loss dB

    def __post_init__(self) -> None:
        if self.dem.shape != self.clutter.shape:
            raise ValueError(
                f"dem {self.dem.shape} and clutter {self.clutter.shape} must "
                "share a grid; reproject them together in 02_terrain.py"
            )

    def sample(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Nearest-neighbour sample of (dem, clutter) at projected coords.

        Out-of-bounds coordinates clamp to the edge rather than raising: a
        path can legitimately clip the state boundary, and the alternative is
        dropping the whole link.
        """
        rows, cols = self.dem.shape
        c = np.clip(np.rint((x - self.x0) / self.cell_m).astype(np.int64), 0, cols - 1)
        r = np.clip(np.rint((self.y0 - y) / self.cell_m).astype(np.int64), 0, rows - 1)
        return self.dem[r, c], self.clutter[r, c]


def _profile(grid: TerrainGrid, tx_x, tx_y, rx_x, rx_y, n_samples: int):
    """Terrain elevation along each tx->rx path.

    Returns (elev, frac) where elev is (N, n_samples) metres and frac is the
    (n_samples,) normalised distance along the path. Vectorized across all N
    links at once -- there is no Python loop over links anywhere in this file.
    """
    frac = np.linspace(0.0, 1.0, n_samples)
    sx = tx_x[:, None] + (rx_x - tx_x)[:, None] * frac[None, :]
    sy = tx_y[:, None] + (rx_y - tx_y)[:, None] * frac[None, :]
    elev, _ = grid.sample(sx, sy)
    return elev.astype(np.float64), frac


def _edge_metrics(elev, d_tx, d_rx, z_near, z_far, wavelength_m, k_factor):
    """Fresnel-Kirchhoff parameter v and obstruction height h per sample.

    h is how far the terrain rises ABOVE the straight ray joining the two
    endpoints, in metres, including the effective-earth bulge. d_tx/d_rx are
    (N, n) distances from each endpoint. The endpoints themselves (where d_tx
    or d_rx is 0) come back as -inf so they can never win an argmax.
    """
    total = d_tx + d_rx
    with np.errstate(divide="ignore", invalid="ignore"):
        # Effective-earth bulge: how much the curved earth rises above the
        # straight chord between the endpoints, at each sample.
        bulge = (d_tx * d_rx) / (2.0 * k_factor * EARTH_RADIUS_M)
        # Height of the straight ray above datum at each sample.
        ray = z_near[:, None] + (z_far - z_near)[:, None] * (d_tx / total)
        h = (elev + bulge) - ray
        v = h * np.sqrt(2.0 * total / (wavelength_m * d_tx * d_rx))
    ok = np.isfinite(v)
    return np.where(ok, v, -np.inf), np.where(ok, h, -np.inf)


def _principal_edge(v, h, eligible):
    """Strongest genuine obstruction within `eligible`, as (index, v).

    "Genuine" means terrain that actually protrudes above the geometric ray
    (h > 0). This guard is load-bearing, not cosmetic -- see the WHY below.
    Rows with no obstruction get v = -inf, hence zero loss.
    """
    vm = np.where(eligible & (h > 0.0), v, -np.inf)
    idx = np.argmax(vm, axis=1)
    return idx, vm[np.arange(idx.shape[0]), idx]


def diffraction_loss_db(
    elev: np.ndarray,
    path_len_m: np.ndarray,
    z_tx: np.ndarray,
    z_rx: np.ndarray,
    freq_mhz: float,
    k_factor: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Deygout three-edge diffraction loss. Returns (loss_db, blocked).

    Deygout's construction: find the sample with the largest v (the "principal
    edge"), then repeat independently on the two sub-paths tx->principal and
    principal->rx, treating the principal edge top as the far endpoint. The
    three losses add.

    `blocked` is True where terrain rises above the geometric ray; its inverse
    is the line-of-sight flag, returned here so the caller does not recompute
    the geometry.

    WHY EDGES ARE RESTRICTED TO TERRAIN ABOVE THE RAY
    -------------------------------------------------
    Searching for the maximum v over ALL samples looks more rigorous and is
    badly wrong for this model. A receiver 1.5 m above flat ground has ~3 m of
    clearance against a first Fresnel radius of ~8 m at 5 km, so the ground
    immediately around it always registers as a partial obstruction. Deygout
    then charges for it three times over -- principal edge plus both sub-path
    edges -- and the measured result was 11.4 dB of diffraction loss across
    perfectly flat terrain. Applied to every link in the state, that phantom
    loss inflates the headline uncovered-population figure.

    The physical error is treating a smooth ground plane as a knife edge. Knife
    edges model isolated ridges; the near-antenna ground plane is a
    height-gain/two-ray effect, and in this model it is carried by the clutter
    term and the 8 dB shadow margin instead.

    The simplification this buys is bounded and stated: partial Fresnel
    obstruction by terrain that stays BELOW the ray (-0.78 < v < 0) is scored
    as 0 dB rather than up to 6 dB. That is at most a 6 dB optimism, entirely
    inside the 8 dB shadow margin already subtracted from every link.
    """
    n_links, n = elev.shape
    wavelength_m = C_M_PER_S / (freq_mhz * 1e6)
    frac = np.linspace(0.0, 1.0, n)
    d_tx = path_len_m[:, None] * frac[None, :]
    d_rx = path_len_m[:, None] - d_tx
    idx = np.arange(n)[None, :]
    rows = np.arange(n_links)
    interior = (idx > 0) & (idx < n - 1)

    v, h = _edge_metrics(elev, d_tx, d_rx, z_tx, z_rx, wavelength_m, k_factor)
    principal, v_principal = _principal_edge(v, h, interior)
    blocked = np.isfinite(v_principal)
    loss = knife_edge_loss_db(v_principal)

    d_principal = d_tx[rows, principal]
    z_principal = elev[rows, principal]

    for side in ("tx", "rx"):
        if side == "tx":
            mask = interior & (idx < principal[:, None])
            sub_len = d_principal
            sub_d_tx = d_tx
            near_z, far_z = z_tx, z_principal
        else:
            mask = interior & (idx > principal[:, None])
            sub_len = path_len_m - d_principal
            sub_d_tx = d_tx - d_principal[:, None]
            near_z, far_z = z_principal, z_rx
        # Only links with a principal edge have sub-paths to search.
        mask = mask & blocked[:, None]
        with np.errstate(divide="ignore", invalid="ignore"):
            sub_d_rx = sub_len[:, None] - sub_d_tx
            sub_v, sub_h = _edge_metrics(
                elev, sub_d_tx, sub_d_rx, near_z, far_z, wavelength_m, k_factor
            )
        _, best = _principal_edge(sub_v, sub_h, mask)
        loss = loss + knife_edge_loss_db(best)

    return loss, blocked


def link_rsrp(
    grid: TerrainGrid,
    tx_x: np.ndarray,
    tx_y: np.ndarray,
    tx_height_m: np.ndarray,
    rx_x: np.ndarray,
    rx_y: np.ndarray,
    rx_height_m: float,
    freq_mhz: float,
    eirp_dbm: float,
    subcarriers: int,
    shadow_margin_db: float,
    n_samples: int,
    k_factor: float,
) -> dict[str, np.ndarray]:
    """Predicted RSRP for a batch of transmitter/receiver pairs.

    Every argument beyond the grid is an array of length N (or a scalar that
    applies to all N). Returns a dict of length-N arrays, which is the shape
    05_links.py hands straight back to Spark as pandas Series.
    """
    tx_x, tx_y = np.asarray(tx_x, float), np.asarray(tx_y, float)
    rx_x, rx_y = np.asarray(rx_x, float), np.asarray(rx_y, float)
    tx_height_m = np.asarray(tx_height_m, float)

    path_len_m = np.hypot(rx_x - tx_x, rx_y - tx_y)
    elev, _ = _profile(grid, tx_x, tx_y, rx_x, rx_y, n_samples)

    # Ground elevation under each endpoint comes from the profile itself, so
    # the antenna heights are unambiguously above the same DEM the diffraction
    # geometry uses.
    z_tx = elev[:, 0] + tx_height_m
    z_rx = elev[:, -1] + rx_height_m

    l_free = fspl_db(path_len_m, freq_mhz)
    l_diff, blocked = diffraction_loss_db(
        elev, path_len_m, z_tx, z_rx, freq_mhz, k_factor
    )
    _, rx_class = grid.sample(rx_x, rx_y)
    l_clutter = grid.clutter_lut[rx_class]

    rsrp = (
        eirp_dbm
        - 10.0 * np.log10(subcarriers)
        - l_free
        - l_diff
        - l_clutter
        - shadow_margin_db
    )
    return {
        "rsrp_dbm": rsrp,
        "path_loss_db": l_free + l_diff + l_clutter,
        "diffraction_db": l_diff,
        "clutter_db": l_clutter,
        "distance_m": path_len_m,
        # Geometric line of sight: no terrain rises above the tx->rx ray. This
        # is the viewshed sense of the term, and the sense a reader of the map
        # will assume. It is deliberately NOT the Fresnel-clearance criterion,
        # which a ground-level receiver essentially never satisfies at range.
        "is_los": ~blocked,
        "clutter_class": rx_class,
    }
