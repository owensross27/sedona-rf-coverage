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

from config import REPO_ROOT, scope  # noqa: E402
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


# A MapLibre image source becomes one WebGL texture, and 4096 px is the floor
# across GPUs that still matters (it is the cap on a lot of mobile hardware).
# Past it the layer does not error, it just never draws -- a blank overlay on a
# working map, which is the same silent failure as a dropped hexagon.
#
# This binds at state scope and not at demo scope: Kanawha warps to 870x783,
# West Virginia to roughly 4800x4200. Only the PNG is reduced; the COG keeps
# every 90 m pixel, because that is the data product. At 4096 px across ~430 km
# the overlay is ~105 m per pixel against a 90 m source, so what is lost is
# nearly nothing and what is gained is that it renders at all.
MAX_TEXTURE_PX = 4096


def warp_3857(src_path: str):
    import rasterio
    from rasterio.warp import (Resampling, calculate_default_transform,
                               reproject)

    with rasterio.open(src_path) as src:
        transform, width, height = calculate_default_transform(
            src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
        if max(width, height) > MAX_TEXTURE_PX:
            s = MAX_TEXTURE_PX / max(width, height)
            full_w, full_h = width, height
            width, height = max(1, int(width * s)), max(1, int(height * s))
            # Scale the pixel size by the SAME factor the raster shrank by, so
            # the georeferencing still covers the identical ground extent.
            transform = transform * rasterio.Affine.scale(full_w / width,
                                                          full_h / height)
            print(f"  {full_w}x{full_h} -> {width}x{height} "
                  f"(WebGL texture cap {MAX_TEXTURE_PX})")
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
    # Stamp the scope these surfaces were computed at. The client refuses to
    # offer a surface mode when this disagrees with the tileset's scope: a
    # demo-scope surface on a statewide map is a county-sized patch of colour
    # floating in the middle of the state, captioned as the state's signal.
    # `make surface` is ~30x more work statewide than at demo scope, so the
    # two really can be out of step, and only this file knows which is which.
    meta["scope"] = scope()["name"]
    (OUT_DIR / "surface_meta.json").write_text(json.dumps(meta))
    print(f"wrote {OUT_DIR / 'surface_meta.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
