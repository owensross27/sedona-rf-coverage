"""Static map of the coverage surface and the recommended sites.

Not a pipeline stage -- a figure generator. It reads the gold artifacts with
pandas rather than Spark because a PNG does not need a cluster, and because
this has to keep working after the cluster is deleted.

Two panels, because one of them alone misleads:

    left    predicted RSRP everywhere, which shows the terrain doing the work
            and is the picture people expect.
    right   where the gaps actually are and which twenty sites the optimizer
            chose, which is the picture that answers the question.

Usage:
    SCOPE=demo LOCAL_OUT=1 python scripts/make_map.py
"""
import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import RF, scope  # noqa: E402
from session import out_path  # noqa: E402

THRESHOLD = float(RF["rsrp_threshold_dbm"])


def read_parquet_dir(path: str) -> pd.DataFrame:
    """Spark writes a directory of part files; pandas wants the files."""
    files = sorted(glob.glob(f"{path}/*.parquet"))
    if not files:
        raise FileNotFoundError(
            f"no parquet parts under {path} -- run `make demo` first")
    return pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)


def main() -> int:
    import matplotlib
    matplotlib.use("Agg")            # no display on a build box or in CI
    import matplotlib.pyplot as plt
    import rasterio
    from matplotlib.lines import Line2D

    sc = scope()
    cov = read_parquet_dir(out_path("gold", "coverage"))
    sit = read_parquet_dir(out_path("gold", "siting"))
    towers = read_parquet_dir(out_path("bronze", "towers"))
    chosen = sit[sit["selected_greedy"]].sort_values("greedy_rank")

    with rasterio.open(out_path("cog", "coverage_rsrp_5070_90m.tif")) as src:
        arr = src.read(1)
        extent = (src.bounds.left, src.bounds.right,
                  src.bounds.bottom, src.bounds.top)
    surface = np.ma.masked_equal(arr, arr.min() if src.nodata is None
                                 else src.nodata)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8), constrained_layout=True)

    # --- left: the predicted surface ------------------------------------
    im = ax1.imshow(surface, extent=extent, origin="upper", cmap="viridis",
                    vmin=-125, vmax=-60, interpolation="nearest")
    # Only towers inside the frame; the tower set is padded by max_link_km and
    # would otherwise scatter dots far outside the county.
    inside = towers[(towers.x.between(extent[0], extent[1]))
                    & (towers.y.between(extent[2], extent[3]))]
    ax1.scatter(inside.x, inside.y, s=6, c="white", edgecolors="black",
                linewidths=0.3, label=f"ASR structures ({len(inside)})")
    cb = fig.colorbar(im, ax=ax1, shrink=0.75)
    cb.set_label("predicted RSRP (dBm)")
    cb.ax.axhline(THRESHOLD, color="red", linewidth=1.5)
    ax1.set_title(f"Predicted best-server RSRP, {sc['name']} scope\n"
                  f"700 MHz LTE, Deygout diffraction over 30 m DSM, "
                  f"90 m EPSG:5070 grid")
    ax1.legend(loc="lower left", fontsize=8, framealpha=0.9)

    # --- right: gaps and the plan ---------------------------------------
    gaps = cov[~cov["is_covered"]]
    served = cov[cov["is_covered"]]
    ax2.scatter(served.x, served.y, s=4, c="#c8dcc8", label=
                f"covered ({len(served):,} cells)")
    ax2.scatter(gaps.x, gaps.y, s=4, c="#d1495b", label=
                f"gap ({len(gaps):,} cells, {gaps['pop'].sum():,.0f} people)")
    # Marker area tracks marginal demand, so the ranking is legible without
    # reading twenty labels -- site 1 is worth far more than site 20.
    size = 60 + 340 * (chosen["marginal_demand"]
                       / chosen["marginal_demand"].max())
    ax2.scatter(chosen.x, chosen.y, s=size, marker="*", c="#ffd166",
                edgecolors="black", linewidths=0.6, zorder=5,
                label=f"recommended sites ({len(chosen)})")
    for _, r in chosen.iterrows():
        ax2.annotate(int(r.greedy_rank), (r.x, r.y), fontsize=7,
                     xytext=(4, 4), textcoords="offset points", zorder=6)
    ax2.set_xlim(extent[0], extent[1])
    ax2.set_ylim(extent[2], extent[3])
    ax2.set_aspect("equal")
    ax2.set_title("Coverage gaps and the recommended build\n"
                  "greedy submodular, verified against an exact MILP")
    ax2.legend(loc="lower left", fontsize=8, framealpha=0.9)

    for ax in (ax1, ax2):
        ax.set_xticks([])
        ax.set_yticks([])

    pop_cov = (cov.loc[cov.is_covered, "pop"].sum() / cov["pop"].sum())
    fig.suptitle(
        f"{pop_cov:.1%} of population covered at {THRESHOLD:.0f} dBm; "
        f"{gaps['pop'].sum():,.0f} people in gap cells. "
        f"Sources: FCC ASR, Copernicus GLO-30, ESA WorldCover, ACS, Overture.",
        fontsize=10)

    from config import REPO_ROOT
    dest = REPO_ROOT / "docs" / "img" / "coverage_map.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(dest, dpi=140)
    print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
