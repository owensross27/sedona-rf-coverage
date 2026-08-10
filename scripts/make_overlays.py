"""Surface COGs -> web overlay PNGs + corner metadata.

MapLibre's image source drapes a PNG between four lng/lat corners in mercator
space, so each surface is warped from EPSG:5070 onto a north-up EPSG:3857
grid first -- draping the unwarped Albers raster would smear every ridge by
the projection difference. Colours are applied here (same ramp and range as
the map's RSRP legend) because a PNG carries no colormap of its own.

Usage:
    SCOPE=demo LOCAL_OUT=1 python scripts/make_overlays.py
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import REPO_ROOT  # noqa: E402
from session import out_path  # noqa: E402

OUT_DIR = REPO_ROOT / "web" / "data"
# Must match MODES.rsrp in web/index.html.
RAMP = [(-125, "#2c105c"), (-115, "#711f81"), (-105, "#b63679"),
        (-95, "#ee605e"), (-85, "#fdae78"), (-70, "#fcfdbf")]


def colormap():
    from matplotlib.colors import LinearSegmentedColormap, Normalize

    lo, hi = RAMP[0][0], RAMP[-1][0]
    norm = Normalize(lo, hi)
    return LinearSegmentedColormap.from_list(
        "rsrp", [(norm(v), c) for v, c in RAMP]), norm


def warp_3857(src_path: str):
    import rasterio
    from rasterio.warp import (Resampling, calculate_default_transform,
                               reproject)

    with rasterio.open(src_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
        dst = np.full((height, width), src.nodata, dtype="float32")
        reproject(rasterio.band(src, 1), dst, dst_transform=transform,
                  dst_crs="EPSG:3857", dst_nodata=src.nodata,
                  resampling=Resampling.bilinear)
        bounds = rasterio.transform.array_bounds(height, width, transform)
        return dst, src.nodata, bounds


def to_lnglat(x: float, y: float):
    from pyproj import Transformer

    t = Transformer.from_crs(3857, 4326, always_xy=True)
    lng, lat = t.transform(x, y)
    return [round(lng, 6), round(lat, 6)]


def main() -> int:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    cmap, norm = colormap()
    meta = {}
    for name in ("rsrp_surface_current", "rsrp_surface_upgraded"):
        arr, nodata, (minx, miny, maxx, maxy) = warp_3857(
            out_path("cog", f"{name}_5070_90m.tif"))
        rgba = cmap(norm(arr))
        rgba[..., 3] = np.where(arr == nodata, 0.0, 0.78)
        dest = OUT_DIR / f"{name}.png"
        plt.imsave(dest, rgba)
        # image-source corner order: top-left, top-right, bottom-right,
        # bottom-left.
        meta[name] = [to_lnglat(minx, maxy), to_lnglat(maxx, maxy),
                      to_lnglat(maxx, miny), to_lnglat(minx, miny)]
        print(f"wrote {dest} {arr.shape}")
    (OUT_DIR / "surface_meta.json").write_text(json.dumps(meta))
    print(f"wrote {OUT_DIR / 'surface_meta.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
