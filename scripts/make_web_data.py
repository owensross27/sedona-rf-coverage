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

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import REPO_ROOT, RF, scope  # noqa: E402
from session import out_path  # noqa: E402

OUT_DIR = REPO_ROOT / "web" / "data"

# The analysis grid's cell area. Used to turn summed population back into a
# density so one colour ramp is valid at every roll-up level.
AREA_R8_KM2 = 0.737

# Service bands, exhaustive and mutually exclusive, in worsening-to-best order.
# These are the filter the map is actually for: "how many PEOPLE are in each",
# not "how many cells".
#
# `n_servers` counts towers whose predicted RSRP clears the threshold, so
# covered == n_servers >= 1. A NaN `best_rsrp_dbm` means no plausible link to
# any tower at all, which is a different and worse thing than a link that
# arrives too weak -- one needs a new site, the other might only need more
# height or power on a site that already exists.
#
# `single` is the sharpest band and the least obvious: those cells are covered
# today and lose service entirely if one tower goes down.
BANDS = ("no_link", "gap", "single", "multi", "over")
BAND_LABELS = {
    "no_link": "No link to any tower",
    "gap": "Below threshold (has a link)",
    "single": "Covered by exactly ONE tower",
    "multi": "Covered by 2-4 towers",
    "over": "Covered by 5+ towers",
}


def band_of(df: pd.DataFrame) -> pd.Series:
    """Vectorised band assignment. Order matters: first match wins."""
    n = df["n_servers"]
    return pd.Series(
        np.select(
            [df["best_rsrp_dbm"].isna() & (n < 1), n < 1, n == 1, n <= 4],
            ["no_link", "gap", "single", "multi"],
            default="over",
        ),
        index=df.index,
    )


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
    df["band"] = band_of(df)

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
                    # Emitted at r8 too, where they are degenerate (0 or 100,
                    # and a one-cell density), purely so ONE paint expression
                    # per mode works at every zoom. The alternative is two
                    # parallel sets of colour ramps that have to be kept in
                    # step by hand, and the first one to drift does so
                    # silently -- the map just quietly recolours.
                    "cell_pct": 100.0 if r.is_covered else 0.0,
                    "pop_pct": (100.0 if r.is_covered else 0.0)
                               if r.pop > 0 else None,
                    # Density, not a count. `pop` SUMS on the way up, so an r6
                    # parent holds ~49 r8 cells and any shared ramp built on
                    # raw counts saturates to solid black two zooms out.
                    # People per km2 means the same thing at every level.
                    "pop_km2": round(r.pop / AREA_R8_KM2, 1),
                    # A single dictionary-encoded string, rather than the five
                    # cells_* counters the roll-ups carry: at r8 a cell is in
                    # exactly one band, and 88,281 repeated short strings cost
                    # almost nothing in a vector tile. The client builds one
                    # filter expression that reads either shape.
                    "band": r.band,
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

    EVERY map mode is rolled up, not just coverage -- a roll-up that carried
    only population left the tree-cover, relief and signal modes painting the
    same coverage choropleth at every zoom below 13, which looks exactly like
    a broken mode switcher.

    Each measure gets the aggregate its own units justify, and they are NOT
    interchangeable:

    - population and demand SUM. They are counts; a parent holds the total.
    - coverage is POPULATION-WEIGHTED (`pop_pct`) and also reported as a plain
      share of cells (`cell_pct`), because those answer different questions:
      "what fraction of people here can get service" versus "what fraction of
      the ground is served". In rural WV they diverge sharply.
    - tree cover, built fraction and relief take a plain MEAN over child cells.
      Every H3 cell at a given resolution has the same area, so an unweighted
      mean over children IS the area mean -- no weighting needed.
    - RSRP takes the MEDIAN, never the mean, and only ever the median.
      dBm is logarithmic, so an arithmetic mean of dBm is not the mean of
      anything physical; worse, averaging a strong -70 cell against a
      no-signal one yields a comfortable middle number for a place with a hole
      in it. The median is the typical cell and cannot be dragged up by a few
      very strong ones: if most of a parent is in a gap, its median is below
      threshold, which is the truth the map exists to show.
    """
    import h3

    counts = {}
    for res, layer in LOD:
        # NOT named with a leading underscore: itertuples silently renames any
        # such column to a positional `_1`, and the rename is invisible until
        # the attribute access fails at row zero.
        # One column per band, holding this cell's population if it is in that
        # band and 0 otherwise, so a plain groupby-sum yields the population
        # breakdown. Same trick for the cell counts. It is more columns than a
        # pivot would need, but it keeps the whole roll-up in ONE aggregation
        # that the conservation asserts below can check end to end.
        extra = {}
        for b in BANDS:
            inb = df["band"] == b
            extra[f"pop_{b}"] = df["pop"].where(inb, 0.0)
            extra[f"cells_{b}"] = inb.astype(int)
        g = df.assign(
            parent=[h3.cell_to_parent(c, res) for c in df["h3_str"]],
            covpop=df["pop"].where(df["is_covered"], 0.0),
            **extra,
        )
        agg = g.groupby("parent", as_index=False).agg(
            pop=("pop", "sum"),
            cov_pop=("covpop", "sum"),
            demand=("demand", "sum"),
            cells=("h3_str", "size"),
            cov_cells=("is_covered", "sum"),
            # Physical properties of the ground: plain mean over equal-area
            # children. pandas skips NaN, so cells the zonal-stats stage could
            # not fill do not drag the average to zero.
            tree=("tree_frac", "mean"),
            built=("built_frac", "mean"),
            relief=("relief_m", "mean"),
            elev=("elev_mean_m", "mean"),
            n_srv=("n_servers", "mean"),
            # Median, for the reason in the docstring. Cells with no plausible
            # link at all are NaN here and pandas drops them, so this is the
            # median of cells that HAVE a link -- `pop_pct` and `cell_pct` are
            # what carry the no-link cells, and the two are meant to be read
            # together.
            rsrp=("best_rsrp_dbm", "median"),
            **{f"pop_{b}": (f"pop_{b}", "sum") for b in BANDS},
            **{f"cells_{b}": (f"cells_{b}", "sum") for b in BANDS},
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
        # The bands are exhaustive and mutually exclusive, so they must add
        # back up to the whole. This is the check that would catch a band
        # predicate that silently stops matching -- e.g. n_servers changing
        # dtype and `n == 1` never being true.
        assert int(sum(agg[f"cells_{b}"].sum() for b in BANDS)) == len(df), \
            f"{layer}: band cell counts do not sum to {len(df)}"
        assert abs(sum(agg[f"pop_{b}"].sum() for b in BANDS)
                   - df["pop"].sum()) < 1.0, \
            f"{layer}: band populations do not sum to the total"

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
                        # Over the ground the children actually cover, not the
                        # parent hexagon's nominal area: parents on the state
                        # border are only partly filled, and dividing by the
                        # nominal area would understate their density.
                        "pop_km2": round(r.pop / (r.cells * AREA_R8_KM2), 1),
                        # Same property NAMES as the r8 layer, so one paint
                        # expression per mode works at every zoom instead of
                        # the client keeping two parallel sets of ramps in
                        # step by hand.
                        "rsrp": round(r.rsrp, 1) if pd.notna(r.rsrp) else None,
                        "tree": round(r.tree * 100.0) if pd.notna(r.tree) else None,
                        "built": round(r.built * 100.0) if pd.notna(r.built) else None,
                        "relief": round(r.relief) if pd.notna(r.relief) else None,
                        "elev": round(r.elev) if pd.notna(r.elev) else None,
                        "n_srv": round(r.n_srv, 1) if pd.notna(r.n_srv) else None,
                        # Per-band counts and populations. A rolled-up cell is
                        # not "in" one band -- it CONTAINS cells from several
                        # -- so the filter asks whether it contains any, and
                        # the panel shows the split. Zeros are dropped by
                        # feature(), so an all-covered parent carries none of
                        # the gap keys at all.
                        **{f"c_{b}": int(getattr(r, f"cells_{b}")) or None
                           for b in BANDS},
                        **{f"p_{b}": round(getattr(r, f"pop_{b}")) or None
                           for b in BANDS},
                    }) + "\n")
        counts[layer] = len(agg)
    return counts


def summarise(df: pd.DataFrame) -> dict:
    """Headline numbers for the caption and the ask-panel's context block."""
    cov = df["is_covered"]
    pop, gap = df["pop"].sum(), df.loc[~cov, "pop"].sum()
    return {
        "cells": int(len(df)),
        "cells_covered_pct": round(100.0 * cov.mean(), 1),
        "population": int(round(pop)),
        "population_covered_pct": round(100.0 * (pop - gap) / pop, 1) if pop else None,
        "population_in_gap_cells": int(round(gap)),
        "cells_with_no_link": int(df["best_rsrp_dbm"].isna().sum()),
        # The sharpest number in the dataset and the least obvious: a cell
        # served by exactly one tower is covered until that tower fails.
        "covered_cells_with_one_server": int(((df["n_servers"] == 1) & cov).sum()),
        "median_served_rsrp_dbm": round(float(df.loc[cov, "best_rsrp_dbm"].median()), 1),
        # Drives the filter checkbox labels, so the counts on screen are
        # computed from the same frame the tiles are, never typed in.
        "bands": {b: {"label": BAND_LABELS[b],
                      "cells": int((df["band"] == b).sum()),
                      "population": int(round(df.loc[df["band"] == b, "pop"].sum()))}
                  for b in BANDS},
    }


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
            "bounds": df.attrs["bounds"],
            # Headline aggregates, computed here rather than typed into the
            # page. The ask-panel sends these as context, and a hardcoded
            # summary would start lying the first time the pipeline is re-run
            # with a different threshold -- which is precisely the drift that
            # put "Demo scope: Kanawha County" on a statewide map.
            "summary": summarise(df)}
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
