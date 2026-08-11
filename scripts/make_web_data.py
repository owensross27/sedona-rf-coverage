"""Gold outputs -> GeoJSONL for the web map's vector tiles.

Six layers, one file each, consumed by scripts/make_tiles.sh:

    hexes    every receiver cell at H3 r8, carrying everything the click panel
             needs to answer "why is signal weak HERE": predicted RSRP,
             population, the serving tower (id, height, distance, line of
             sight), and the environment (tree cover, relief, building
             heights) from the Sedona zonal-stats stage.
    hex7     the same cells rolled up to r7, r6 and r5 for lower zooms. Each
    hex6     level renders over its own disjoint zoom range, so the number of
    hex5     features in view stays roughly constant while panning instead of
             growing sevenfold per zoom-out.
    towers   ASR structures with their registered heights.
    sites    the optimizer's recommended builds, ranked.

Plain pandas, no Spark: this runs after the pipeline, must work with the
cluster gone, and 3k-85k rows is not a distributed problem.

Usage:
    SCOPE=demo LOCAL_OUT=1 python scripts/make_web_data.py
"""
import glob
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import REPO_ROOT, RF, scope  # noqa: E402
from session import out_path  # noqa: E402

OUT_DIR = REPO_ROOT / "web" / "data"


def read_dir(path: str) -> pd.DataFrame:
    files = sorted(glob.glob(f"{path}/*.parquet"))
    if not files:
        raise FileNotFoundError(f"no parquet under {path}; run `make demo` first")
    return pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)


def feature(geom: dict, props: dict) -> str:
    # Nulls are dropped rather than serialised: tippecanoe stores them as the
    # string "null" otherwise, and the panel JS treats absence as "no data".
    clean = {k: v for k, v in props.items() if v is not None and v == v}
    return json.dumps({"type": "Feature", "geometry": geom,
                       "properties": clean}, separators=(",", ":"))


def hex_layer() -> pd.DataFrame:
    """Writes the r8 layer and returns the merged frame for lod_layers()."""
    import h3

    cov = read_dir(out_path("gold", "coverage"))
    feats = read_dir(out_path("silver", "hex_features"))[
        ["h3_r8", "elev_mean_m", "relief_m", "tree_frac", "built_frac",
         "bldg_mean_m", "bldg_max_m"]]
    towers = read_dir(out_path("bronze", "towers"))[["asr_id", "height_agl_m"]]

    df = cov.merge(feats, on="h3_r8", how="left").merge(
        towers.rename(columns={"asr_id": "best_asr_id",
                               "height_agl_m": "srv_height_m"}),
        on="best_asr_id", how="left")

    # Accumulated while the rings are already in hand, so the client can open
    # on the data instead of on a hardcoded county. A statewide tileset behind
    # a viewport centred on Kanawha looks like a broken map, not a wide one.
    bounds = [180.0, 90.0, -180.0, -90.0]

    with open(OUT_DIR / "hexes.geojsonl", "w") as fh:
        for r in df.itertuples(index=False):
            # cell_to_boundary returns (lat, lng); GeoJSON wants (lng, lat).
            ring = [[lng, lat] for lat, lng in h3.cell_to_boundary(r.h3_str)]
            ring.append(ring[0])
            for lng, lat in ring:
                bounds = [min(bounds[0], lng), min(bounds[1], lat),
                          max(bounds[2], lng), max(bounds[3], lat)]
            fh.write(feature(
                {"type": "Polygon", "coordinates": [ring]},
                {
                    "h3": r.h3_str,
                    "rsrp": round(r.best_rsrp_dbm, 1)
                            if pd.notna(r.best_rsrp_dbm) else None,
                    "covered": bool(r.is_covered),
                    "pop": round(r.pop, 1),
                    "demand": round(r.demand),
                    "n_srv": int(r.n_servers),
                    "srv_asr": r.best_asr_id,
                    "srv_km": round(r.best_distance_m / 1000.0, 1)
                              if pd.notna(r.best_distance_m) else None,
                    "srv_los": bool(r.best_is_los)
                               if pd.notna(r.best_is_los) else None,
                    "srv_h": round(r.srv_height_m)
                             if pd.notna(r.srv_height_m) else None,
                    "tree": round(r.tree_frac * 100.0),
                    "built": round(r.built_frac * 100.0),
                    "relief": round(r.relief_m),
                    "bldg_mean": round(r.bldg_mean_m, 1),
                    "bldg_max": round(r.bldg_max_m),
                }) + "\n")
    df.attrs["bounds"] = [round(v, 4) for v in bounds]
    return df


# Which H3 resolution renders at which zoom. Each level is ~7x fewer cells
# than the one below it, so features-per-viewport stays roughly constant while
# panning instead of exploding at low zoom.
LOD = ((5, "hex5"), (6, "hex6"), (7, "hex7"))


def lod_layers(df: pd.DataFrame) -> dict[str, int]:
    """H3 roll-ups for low zoom. Hierarchical aggregation, NOT feature dropping.

    tippecanoe's --drop-densest-as-needed keeps a tile small by throwing
    features away. On a choropleth that means HOLES, and a viewer cannot tell a
    dropped cell from an uncovered one -- the map gets faster by lying. H3 gives
    the honest version for free: cell_to_parent is a bit shift on the index, and
    every r8 cell has exactly one parent at each coarser level, so the union
    still tiles the state with no gaps.

    The aggregate is POPULATION-WEIGHTED COVERAGE and SUMMED population.

    Never a mean RSRP. Averaging a strong -70 dBm cell against a no-signal one
    yields a comfortable middle number for a place with a hole in it, and the
    hole is the entire point of the map. RSRP stays at r8, where it is a real
    per-cell value rather than an artifact of the aggregation.
    """
    import h3

    counts = {}
    for res, layer in LOD:
        # NOT named with a leading underscore: itertuples silently renames any
        # such column to a positional `_1`, and the rename is invisible until
        # the attribute access fails at row zero.
        g = df.assign(
            parent=[h3.cell_to_parent(c, res) for c in df["h3_str"]],
            covpop=df["pop"].where(df["is_covered"], 0.0),
        )
        agg = g.groupby("parent", as_index=False).agg(
            pop=("pop", "sum"),
            cov_pop=("covpop", "sum"),
            demand=("demand", "sum"),
            cells=("h3_str", "size"),
            cov_cells=("is_covered", "sum"),
        )
        # The invariant that makes this honest: nothing is lost on the way up.
        # A roll-up that drops cells produces a hole indistinguishable from an
        # uncovered area, and it would only be visible to someone who zoomed
        # out over exactly the missing patch.
        assert int(agg["cells"].sum()) == len(df), \
            f"{layer}: {agg['cells'].sum()} cells rolled up from {len(df)}"
        assert abs(agg["pop"].sum() - df["pop"].sum()) < 1.0, \
            f"{layer}: population not conserved"
        assert abs(agg["cov_pop"].sum()
                   - df["pop"].where(df["is_covered"], 0.0).sum()) < 1.0, \
            f"{layer}: covered population not conserved"

        with open(OUT_DIR / f"{layer}.geojsonl", "w") as fh:
            for r in agg.itertuples(index=False):
                ring = [[lng, lat] for lat, lng in h3.cell_to_boundary(r.parent)]
                ring.append(ring[0])
                fh.write(feature(
                    {"type": "Polygon", "coordinates": [ring]},
                    {
                        "h3": r.parent,
                        "pop": round(r.pop),
                        "gap_pop": round(r.pop - r.cov_pop),
                        # Percent of PEOPLE covered. None where nobody lives:
                        # 0/0 is not 0% coverage, and colouring it as such
                        # paints empty ridgelines as the worst places in the
                        # state. The client falls back to cell_pct there.
                        "pop_pct": round(100.0 * r.cov_pop / r.pop, 1)
                                   if r.pop > 0 else None,
                        "cell_pct": round(100.0 * r.cov_cells / r.cells, 1),
                        "cells": int(r.cells),
                        "demand": round(r.demand),
                    }) + "\n")
        counts[layer] = len(agg)
    return counts


def tower_layer() -> int:
    t = read_dir(out_path("bronze", "towers"))
    with open(OUT_DIR / "towers.geojsonl", "w") as fh:
        for r in t.itertuples(index=False):
            fh.write(feature(
                {"type": "Point", "coordinates": [round(r.lon, 5),
                                                  round(r.lat, 5)]},
                {"asr": r.asr_id,
                 "h": round(r.height_agl_m) if pd.notna(r.height_agl_m) else None,
                 "type": r.structure_type or None}) + "\n")
    return len(t)


def site_layer() -> int:
    from pyproj import Transformer

    s = read_dir(out_path("gold", "siting"))
    s = s[s["selected_greedy"]].sort_values("greedy_rank")
    to_4326 = Transformer.from_crs(5070, 4326, always_xy=True)
    with open(OUT_DIR / "sites.geojsonl", "w") as fh:
        for r in s.itertuples(index=False):
            lng, lat = to_4326.transform(r.x, r.y)
            fh.write(feature(
                {"type": "Point", "coordinates": [round(lng, 5), round(lat, 5)]},
                {"rank": int(r.greedy_rank),
                 "kind": r.kind,
                 "h": round(r.tx_height_m),
                 "gain": round(r.marginal_demand)}) + "\n")
    return len(s)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sc = scope()
    df = hex_layer()
    lod = lod_layers(df)
    n_tow, n_site = tower_layer(), site_layer()
    # The client reads scope and cell count rather than hardcoding them: the
    # page said "Demo scope: Kanawha County" for as long as it took someone to
    # notice, which on a statewide build is simply false. A caption that
    # derives from the data cannot drift away from it.
    meta = {"threshold_dbm": RF["rsrp_threshold_dbm"],
            "frequency_mhz": RF["frequency_mhz"],
            "scope": sc["name"],
            # config.yml already authors a human sentence per tier, and for
            # `state` the county list is null (= all 55), so counting it would
            # publish "0 counties". Carry the description rather than re-derive
            # a fact config.yml already states correctly.
            "scope_desc": sc["description"],
            "n_hexes": len(df),
            "bounds": df.attrs["bounds"]}
    (OUT_DIR / "meta.json").write_text(json.dumps(meta))
    rollup = "  ".join(f"{k}={v}" for k, v in lod.items())
    print(f"wrote {len(df)} hexes ({rollup}), {n_tow} towers, "
          f"{n_site} sites -> {OUT_DIR}")

    # Cell and population conservation are asserted per level inside
    # lod_layers; this only catches a resolution listed out of order in LOD.
    assert lod["hex7"] >= lod["hex6"] >= lod["hex5"], "roll-up is not monotonic"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
