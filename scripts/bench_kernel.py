"""Single-core throughput benchmark for the propagation kernel.

The plan's go/no-go gate before any statewide run: >= 100k tx/rx pairs per
minute per core. Below that, the fallbacks in order are a 180 m DEM, 64
profile samples, and a 30 km link radius -- each roughly a 2x saving.

    python scripts/bench_kernel.py [n_pairs]

Reports pairs/min/core for a synthetic West-Virginia-sized grid, so it can be
run before any real data exists.
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from propagation import TerrainGrid, link_rsrp  # noqa: E402

CELL_M = 90.0
# West Virginia in EPSG:5070 is roughly 430 x 380 km.
ROWS, COLS = int(380_000 / CELL_M), int(430_000 / CELL_M)


def main(n_pairs: int = 200_000, n_samples: int = 128) -> int:
    rng = np.random.default_rng(0)
    print(f"grid {ROWS} x {COLS} @ {CELL_M:.0f} m "
          f"= {ROWS * COLS * 3 / 1e6:.0f} MB broadcast (int16 dem + uint8 clutter)")

    # Correlated relief rather than white noise: a random DEM would make every
    # path maximally obstructed and is not representative of ridge-and-valley
    # terrain. This is a cheap smooth field, not a landscape model.
    coarse = rng.uniform(200, 1400, size=(ROWS // 32 + 2, COLS // 32 + 2))
    dem = np.repeat(np.repeat(coarse, 32, axis=0), 32, axis=1)[:ROWS, :COLS]
    dem = dem.astype(np.int16)
    clutter = rng.choice([10, 30, 40, 50], size=(ROWS, COLS)).astype(np.uint8)
    lut = np.zeros(256)
    lut[10], lut[50] = 12.0, 15.0
    grid = TerrainGrid(dem=dem, clutter=clutter, x0=0.0, y0=0.0,
                       cell_m=CELL_M, clutter_lut=lut)

    # Pairs distributed like real ones: a tower with receivers scattered
    # inside the 40 km cap, not uniformly across the state.
    tx_x = rng.uniform(0, COLS * CELL_M, n_pairs)
    tx_y = rng.uniform(-ROWS * CELL_M, 0, n_pairs)
    bearing = rng.uniform(0, 2 * np.pi, n_pairs)
    reach = np.sqrt(rng.uniform(0, 1, n_pairs)) * 40_000.0
    rx_x = tx_x + reach * np.cos(bearing)
    rx_y = tx_y + reach * np.sin(bearing)
    tx_h = rng.uniform(30, 120, n_pairs)

    batch = 10_000  # matches spark.sql.execution.arrow.maxRecordsPerBatch
    t0 = time.perf_counter()
    covered = 0
    for i in range(0, n_pairs, batch):
        s = slice(i, i + batch)
        out = link_rsrp(
            grid, tx_x[s], tx_y[s], tx_h[s], rx_x[s], rx_y[s],
            rx_height_m=1.5, freq_mhz=700.0, eirp_dbm=60.0, subcarriers=600,
            shadow_margin_db=8.0, n_samples=n_samples, k_factor=1.333,
        )
        covered += int(np.count_nonzero(out["rsrp_dbm"] >= -105.0))
    elapsed = time.perf_counter() - t0

    rate = n_pairs / elapsed * 60.0
    print(f"{n_pairs:,} pairs x {n_samples} samples in {elapsed:.2f}s "
          f"({n_pairs / elapsed:,.0f} pairs/s)")
    print(f"  -> {rate:,.0f} pairs/min/core   gate = 100,000")
    print(f"  {covered / n_pairs:.1%} of synthetic links above threshold")
    ok = rate >= 100_000
    print("PASS" if ok else "FAIL: fall back to 180 m DEM / 64 samples / 30 km")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 200_000))
