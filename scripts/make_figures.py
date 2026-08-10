"""The model's input layers, as one figure.

Four panels, one shared extent, because the claim they support is joint: every
layer below is on the same 90 m EPSG:5070 lattice, so the propagation kernel
reads all of them with a single set of pixel indices. A reader should be able
to line a ridge up across all four panels by eye.

    terrain     hillshaded DSM -- the diffraction geometry
    land cover  ESA WorldCover -- the class-based clutter term
    buildings   Overture heights -- the knife-edge clutter term
    relief      DSM minus its own 450 m mean -- what a SURFACE model carries
                that a bare-earth DEM would not: canopy and structure texture

Writes docs/img/inputs.png (tracked; the README embeds it).

Usage:
    SCOPE=demo LOCAL_OUT=1 python scripts/make_figures.py
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import CLUTTER, REPO_ROOT, scope  # noqa: E402
from session import out_path  # noqa: E402
from sources import aoi  # noqa: E402

# Official ESA WorldCover v200 class colours.
WC_PALETTE = {
    10: ("#006400", "tree cover"),
    20: ("#ffbb22", "shrubland"),
    30: ("#ffff4c", "grassland"),
    40: ("#f096ff", "cropland"),
    50: ("#fa0000", "built-up"),
    60: ("#b4b4b4", "bare / sparse"),
    70: ("#f0f0f0", "snow / ice"),
    80: ("#0064c8", "water"),
    90: ("#0096a0", "wetland"),
    95: ("#00cf75", "mangrove"),
    100: ("#fae6a0", "moss / lichen"),
}


def main() -> int:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import rasterio
    from matplotlib.colors import LightSource, ListedColormap, BoundaryNorm
    from matplotlib.patches import Patch
    from scipy.ndimage import uniform_filter

    with rasterio.open(out_path("cog", "dem_5070_90m.tif")) as src:
        dem = src.read(1).astype(float)
        extent = (src.bounds.left, src.bounds.right,
                  src.bounds.bottom, src.bounds.top)
    with rasterio.open(out_path("cog", "clutter_5070_90m.tif")) as src:
        clutter = src.read(1)
    with rasterio.open(out_path("cog", "buildings_5070_90m.tif")) as src:
        bldg = src.read(1).astype(float)

    boundary = aoi()
    def draw_boundary(ax):
        geoms = getattr(boundary, "geoms", [boundary])
        for g in geoms:
            x, y = g.exterior.xy
            ax.plot(x, y, color="black", linewidth=0.8, alpha=0.8)

    fig, axes = plt.subplots(2, 2, figsize=(15, 16), constrained_layout=True)
    (ax_t, ax_lc), (ax_b, ax_r) = axes

    # terrain: hillshade under a translucent elevation ramp
    ls = LightSource(azdeg=315, altdeg=45)
    shade = ls.hillshade(dem, vert_exag=2.0, dx=90.0, dy=90.0)
    ax_t.imshow(shade, extent=extent, origin="upper", cmap="gray")
    im = ax_t.imshow(dem, extent=extent, origin="upper", cmap="terrain",
                     alpha=0.45)
    fig.colorbar(im, ax=ax_t, shrink=0.7, label="elevation (m, GLO-30 DSM)")
    ax_t.set_title("Terrain: Copernicus GLO-30 surface model\n"
                   "drives Deygout knife-edge diffraction")

    # land cover with the official palette, legend limited to classes present
    present = sorted(int(c) for c in np.unique(clutter) if c in WC_PALETTE)
    cmap = ListedColormap([WC_PALETTE[c][0] for c in present])
    norm = BoundaryNorm([c - 0.5 for c in present] + [present[-1] + 0.5],
                        cmap.N)
    ax_lc.imshow(clutter, extent=extent, origin="upper", cmap=cmap, norm=norm,
                 interpolation="nearest")
    ax_lc.legend(handles=[
        Patch(color=WC_PALETTE[c][0],
              label=f"{WC_PALETTE[c][1]} ({CLUTTER.get(c, 0.0):.0f} dB)")
        for c in present], loc="lower left", fontsize=8, framealpha=0.9,
        title="class (excess loss)", title_fontsize=8)
    ax_lc.set_title("Land cover: ESA WorldCover 10 m -> 90 m majority\n"
                    "class-based clutter loss at the receiver")

    # building heights; log scale because the distribution is 4 m houses with
    # an 89 m tail, and a linear ramp would render the whole state invisible
    masked = np.ma.masked_equal(bldg, 0.0)
    im = ax_b.imshow(masked, extent=extent, origin="upper", cmap="magma",
                     norm=matplotlib.colors.LogNorm(vmin=1, vmax=100))
    fig.colorbar(im, ax=ax_b, shrink=0.7, label="max building height (m, log)")
    ax_b.set_title("Buildings: Overture Maps heights, max per 90 m pixel\n"
                   "rooftop knife-edge clutter at the receiver")

    # local relief: what the surface model sees that bare earth would not
    relief = dem - uniform_filter(dem, size=5)
    im = ax_r.imshow(relief, extent=extent, origin="upper", cmap="RdBu_r",
                     vmin=-25, vmax=25)
    fig.colorbar(im, ax=ax_r, shrink=0.7, label="DSM - 450 m local mean (m)")
    ax_r.set_title("Surface texture: DSM minus its 450 m mean\n"
                   "canopy and structures the DSM carries implicitly")

    for ax in axes.flat:
        draw_boundary(ax)
        ax.set_xticks([])
        ax.set_yticks([])

    sc = scope()
    fig.suptitle(
        f"Input layers, one shared 90 m EPSG:5070 grid ({sc['name']} scope: "
        "grid extent is the scope padded by one 40 km link; "
        "outline is the exact scope)", fontsize=11)

    dest = REPO_ROOT / "docs" / "img" / "inputs.png"
    dest.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(dest, dpi=110)
    print(f"wrote {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
