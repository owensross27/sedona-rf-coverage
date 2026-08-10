"""Stage 09 -- where to put the next 20 towers. THE SHIP LINE.

This is the maximum coverage location problem: choose p sites from a candidate
set so the demand they newly cover is greatest. It is NP-hard, and it is also
submodular -- covering a cell twice is worth no more than covering it once --
which is what makes the cheap answer nearly as good as the dear one:

    greedy      pick the candidate with the largest marginal gain, p times.
                Guaranteed within 1 - 1/e (63.2%) of optimal. Milliseconds.
    exact       the same problem as a MILP through HiGHS. Proves optimality,
                or proves how close greedy already was.

Both are run and the gap between them is REPORTED rather than assumed. "Greedy
landed within x% of proven optimum" is a real measurement; "greedy is within
63%" is a textbook bound that is almost always far too pessimistic to be
interesting.

Candidates come from two places, per the approved plan:

    colocation  existing ASR structures within colocation_radius_km of a gap,
                because adding antennas to a standing tower is what carriers
                actually do -- the road, power and backhaul already exist.
    greenfield  the highest DEM pixel in each r7 cell containing a gap. A
                ridgeline is where a new build goes, and the DEM is already in
                memory for the propagation kernel.

⚠️ A colocation candidate is only worth anything here because the baseline
already assumes EVERY registered structure transmits (see docs/data-sources.md
on ASR not being a cell-site registry). So a colocation can only add coverage
by being taller than the registered structure, and its height is therefore
max(registered, candidate_height_m). If colocations never get chosen, that is
a finding about the gaps, not a bug in this stage -- it says the uncovered
ground has no registered structure near it at all.

Usage:
    SCOPE=demo LOCAL_OUT=1 python src/09_siting.py
"""
import importlib
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DEMAND, GRID, RF, SITING, scope  # noqa: E402
from session import assert_versions, get_sedona, out_path  # noqa: E402

links05 = importlib.import_module("05_links")
coverage06 = importlib.import_module("06_coverage")

THRESHOLD = float(RF["rsrp_threshold_dbm"])
MAX_LINK_M = float(RF["max_link_km"]) * 1000.0
N_SITES = int(SITING["n_sites"])
# Wall-clock ceiling for the exact solve. A MILP that has not proven optimality
# still returns its incumbent, and the report below says so rather than calling
# an unproven bound "exact".
MILP_TIME_LIMIT_S = float(60 * 10)


def gap_cells(sedona) -> pd.DataFrame:
    """Uncovered receivers, with the demand that makes them worth covering."""
    cov = sedona.read.parquet(out_path("gold", "coverage"))
    gaps = cov.filter("NOT is_covered").select(
        "h3_r8", "h3_str", "x", "y", "pop", "pop_grown", "poi_score",
        "demand").toPandas()
    if gaps.empty:
        raise RuntimeError(
            "no uncovered cells -- either coverage is total (it is not, at any "
            "scope this project runs) or 06_coverage wrote an empty gap set"
        )
    return gaps


def colocation_candidates(sedona, gaps: pd.DataFrame) -> pd.DataFrame:
    """Existing ASR structures near a gap, assumed rebuilt to at least 60 m."""
    from scipy.spatial import cKDTree

    towers = sedona.read.format("geoparquet").load(
        out_path("bronze", "towers")).selectExpr(
            "asr_id", "x", "y", "height_agl_m").toPandas()
    tree = cKDTree(gaps[["x", "y"]].to_numpy())
    radius = float(SITING["colocation_radius_km"]) * 1000.0
    near = tree.query_ball_point(towers[["x", "y"]].to_numpy(), r=radius)
    keep = towers[[len(n) > 0 for n in near]].copy()
    h = keep["height_agl_m"].fillna(float(RF["default_tx_height_m"]))
    return pd.DataFrame({
        "cand_id": "colo:" + keep["asr_id"].astype(str),
        "kind": "colocation",
        "x": keep["x"].astype(float),
        "y": keep["y"].astype(float),
        # A colocation that is not taller than what is already modelled there
        # cannot add anything, so the assumed build is at least 60 m.
        "tx_height_m": np.maximum(h, float(SITING["candidate_height_m"])),
    })


def greenfield_candidates(gaps: pd.DataFrame, grid) -> pd.DataFrame:
    """The highest DEM pixel in each r7 cell that contains a gap.

    r7 is the pre-registered optimizer resolution (config.yml: grid). Searching
    the r7 cell's bounding box rather than the exact hexagon is deliberate: the
    boxes of neighbouring cells overlap, so a ridge on a boundary can be found
    from either side, and identical picks are de-duplicated by pixel below.
    """
    import h3

    parents = sorted({h3.cell_to_parent(c, int(GRID["optimizer_res"]))
                      for c in gaps["h3_str"]})
    poly = coverage06.hex_polygons(pd.Series(parents))
    rows, cols, keep_parents = [], [], []
    for parent, geom in zip(parents, poly):
        minx, miny, maxx, maxy = geom.bounds
        c0 = int(np.floor((minx - grid.x0) / grid.cell_m))
        c1 = int(np.ceil((maxx - grid.x0) / grid.cell_m))
        r0 = int(np.floor((grid.y0 - maxy) / grid.cell_m))
        r1 = int(np.ceil((grid.y0 - miny) / grid.cell_m))
        r0, c0 = max(r0, 0), max(c0, 0)
        r1, c1 = min(r1, grid.dem.shape[0]), min(c1, grid.dem.shape[1])
        if r1 <= r0 or c1 <= c0:
            continue
        window = grid.dem[r0:r1, c0:c1]
        dr, dc = np.unravel_index(int(np.argmax(window)), window.shape)
        rows.append(r0 + dr)
        cols.append(c0 + dc)
        keep_parents.append(parent)

    # Two adjacent r7 cells can share one ridge pixel; keeping both would let
    # the optimizer "choose" the same mast twice and overstate the plan.
    df = pd.DataFrame({"row": rows, "col": cols, "parent": keep_parents})
    df = df.drop_duplicates(subset=["row", "col"])
    return pd.DataFrame({
        "cand_id": "green:" + df["parent"].astype(str),
        "kind": "greenfield",
        "x": grid.x0 + df["col"].to_numpy() * grid.cell_m,
        "y": grid.y0 - df["row"].to_numpy() * grid.cell_m,
        "tx_height_m": float(SITING["candidate_height_m"]),
    })


def prune(cands: pd.DataFrame, gaps: pd.DataFrame) -> pd.DataFrame:
    """Keep the max_candidates with the most reachable demand.

    Straight-line demand within max_link_km, ignoring terrain entirely. That is
    the point: this is a cheap UPPER BOUND on what a candidate could serve, so
    pruning by it cannot discard a candidate that terrain would have made good.
    The real propagation runs on whatever survives.
    """
    from scipy.spatial import cKDTree

    cap = int(SITING["max_candidates"])
    if len(cands) <= cap:
        cands = cands.copy()
        cands["naive_demand"] = np.nan
        return cands
    tree = cKDTree(gaps[["x", "y"]].to_numpy())
    dem = gaps["demand"].to_numpy(dtype=float)
    reach = tree.query_ball_point(cands[["x", "y"]].to_numpy(), r=MAX_LINK_M)
    cands = cands.copy()
    cands["naive_demand"] = [float(dem[list(idx)].sum()) for idx in reach]
    print(f"pruning {len(cands)} candidates to {cap} by reachable demand")
    return cands.nlargest(cap, "naive_demand")


def coverage_matrix(sedona, cands: pd.DataFrame, gaps: pd.DataFrame, grid
                    ) -> pd.DataFrame:
    """Which candidate covers which gap cell, by full propagation.

    Reuses 05_links' kernel and output schema verbatim rather than restating
    them -- same physics, same broadcast terrain, and any future fix to the
    kernel reaches the optimizer without a second edit. The candidate id rides
    in the `asr_id` column for exactly that reason.
    """
    sedona.sparkContext.addPyFile(
        str(Path(__file__).resolve().parent / "propagation.py"))
    bcast = sedona.sparkContext.broadcast(grid)

    c = sedona.createDataFrame(cands[["cand_id", "x", "y", "tx_height_m"]]
                               .rename(columns={"cand_id": "asr_id"}))
    g = sedona.createDataFrame(gaps[["h3_r8", "x", "y"]])
    c.createOrReplaceTempView("cands")
    g.createOrReplaceTempView("gaps")
    pairs = sedona.sql(f"""
        SELECT c.asr_id, g.h3_r8,
               c.x AS tx_x, c.y AS tx_y, c.tx_height_m,
               g.x AS rx_x, g.y AS rx_y
        FROM cands c JOIN gaps g
          ON  ABS(c.x - g.x) <= {MAX_LINK_M}
          AND ABS(c.y - g.y) <= {MAX_LINK_M}
          AND POWER(c.x - g.x, 2) + POWER(c.y - g.y, 2) <= {MAX_LINK_M ** 2}
    """).repartition(64)

    out = pairs.mapInPandas(links05.make_kernel(bcast),
                            schema=links05.OUT_SCHEMA)
    served = out.filter(f"rsrp_dbm >= {THRESHOLD}").selectExpr(
        "asr_id AS cand_id", "h3_r8").toPandas()
    print(f"propagated {pairs.count():,} candidate/gap pairs, "
          f"{len(served):,} would be served")
    return served


def greedy(cover: dict, demand: np.ndarray, n_sites: int) -> list[dict]:
    """Submodular greedy: repeatedly take the largest marginal gain.

    Marginal, not total -- a candidate that covers the same cells as one
    already chosen is worth nothing, which is exactly the property that makes
    the 1-1/e bound hold and the reason a naive "top 20 by coverage" ranking
    would pick twenty overlapping sites on the same ridge.
    """
    covered = np.zeros(demand.shape, dtype=bool)
    picks = []
    for rank in range(1, n_sites + 1):
        best, gain = None, 0.0
        for cand, idx in cover.items():
            g = float(demand[idx][~covered[idx]].sum())
            if g > gain:
                best, gain = cand, g
        if best is None:
            print(f"  greedy exhausted after {rank - 1} sites: no candidate "
                  "adds any uncovered demand")
            break
        covered[cover[best]] = True
        picks.append({"cand_id": best, "rank": rank, "marginal_demand": gain,
                      "cumulative_demand": float(demand[covered].sum())})
    return picks


def tourism_sensitivity(gaps: pd.DataFrame, cover: dict, n_sites: int,
                        base: set) -> None:
    """Re-run the plan under other tourism weights and report the overlap.

    config.yml pre-registers this: tourism_weight is the one calibration
    number in the demand score, and a site list that reshuffles completely when
    it moves is an artifact of that number rather than a finding about the
    ground. Free to run -- gold/coverage carries `pop_grown` and `poi_score`
    separately, so demand is re-derivable without re-propagating anything.

    w = 0 is the pure-residents plan. Where the two agree, the site is robust.
    """
    print("\ntourism_weight sensitivity (pre-registered in config.yml):")
    for w in (0.0, float(DEMAND["tourism_weight"]), 3000.0):
        d = (gaps["pop_grown"].to_numpy(dtype=float)
             + w * gaps["poi_score"].to_numpy(dtype=float))
        sel = {p["cand_id"] for p in greedy(cover, d, n_sites)}
        # The configured weight must reproduce the plan exactly; if it does
        # not, `demand` in gold/coverage is not pop_grown + w*poi_score and
        # every row below compares against the wrong baseline.
        if w == float(DEMAND["tourism_weight"]) and sel != base:
            raise RuntimeError(
                "re-deriving demand at the configured tourism_weight gave a "
                "different plan; 04_grid's demand formula and this one disagree"
            )
        tag = "  <- the pre-registered plan" if sel == base else ""
        print(f"  weight {w:>6.0f}: {len(sel & base)}/{len(base)} sites shared "
              f"with the pre-registered plan{tag}")


def exact(cover: dict, demand: np.ndarray, n_sites: int) -> dict:
    """The same MCLP as a MILP, solved by HiGHS through scipy.

        maximise   sum_j demand_j * y_j
        subject to y_j <= sum_{i : i covers j} x_i      for every gap j
                   sum_i x_i <= p
                   x, y binary

    y_j is free to be 0 even when covered, which is safe: the objective only
    ever rewards setting it to 1, so the relaxation cannot cheat.
    """
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import coo_matrix, hstack, eye, csr_matrix

    cands = list(cover)
    n_c, n_g = len(cands), demand.size
    col = {c: i for i, c in enumerate(cands)}

    rows, cols = [], []
    for c, idx in cover.items():
        rows.extend(idx.tolist())
        cols.extend([col[c]] * len(idx))
    # A[j, i] = 1 when candidate i covers gap j; the constraint is y - Ax <= 0.
    a_cov = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n_g, n_c))
    cons = [
        LinearConstraint(hstack([-a_cov, eye(n_g)], format="csr"),
                         lb=-np.inf, ub=0.0),
        LinearConstraint(csr_matrix(np.r_[np.ones(n_c), np.zeros(n_g)]
                                    .reshape(1, -1)), lb=-np.inf, ub=n_sites),
    ]
    obj = np.r_[np.zeros(n_c), -demand]        # milp minimises
    t0 = time.perf_counter()
    res = milp(c=obj, constraints=cons, integrality=np.ones(n_c + n_g),
               bounds=Bounds(0, 1),
               options={"time_limit": MILP_TIME_LIMIT_S, "presolve": True})
    elapsed = time.perf_counter() - t0
    if not res.success or res.x is None:
        print(f"  MILP returned no solution ({res.message.strip()}) after "
              f"{elapsed:.1f}s -- reporting greedy only")
        return {"ok": False, "seconds": elapsed, "message": res.message.strip()}
    x = np.asarray(res.x[:n_c])
    return {
        "ok": True,
        "seconds": elapsed,
        # status 0 is "optimal"; anything else (notably 1, time limit) means
        # this is an incumbent, not a proven optimum, and must not be called one.
        "proven": int(res.status) == 0,
        "message": res.message.strip(),
        "demand": float(-res.fun),
        "sites": [cands[i] for i in np.flatnonzero(x > 0.5)],
    }


def main() -> int:
    sc = scope()
    sedona = get_sedona(f"rf-siting-{sc['name']}")
    assert_versions(sedona)

    gaps = gap_cells(sedona)
    print(f"gap cells: {len(gaps):,}, "
          f"{gaps['pop'].sum():,.0f} people, {gaps['demand'].sum():,.0f} demand")

    grid = links05.load_terrain(
        out_path("cog", "dem_5070_90m.tif"),
        out_path("cog", "clutter_5070_90m.tif"),
    )
    cands = pd.concat([colocation_candidates(sedona, gaps),
                       greenfield_candidates(gaps, grid)], ignore_index=True)
    print("candidates: " + ", ".join(
        f"{k} {v}" for k, v in cands["kind"].value_counts().items()))
    cands = prune(cands, gaps).reset_index(drop=True)

    served = coverage_matrix(sedona, cands, gaps, grid)
    if served.empty:
        raise RuntimeError(
            "no candidate serves any gap cell. Either the candidate set is "
            "wrong or the gaps are unreachable at this link budget"
        )

    # Positional index into the gaps frame, so cover sets are plain int arrays.
    pos = pd.Series(np.arange(len(gaps)), index=gaps["h3_r8"].to_numpy())
    served["gap_i"] = pos.reindex(served["h3_r8"].to_numpy()).to_numpy()
    cover = {c: g["gap_i"].to_numpy() for c, g in served.groupby("cand_id")}
    demand = gaps["demand"].to_numpy(dtype=float)
    pop = gaps["pop"].to_numpy(dtype=float)
    print(f"{len(cover):,} of {len(cands):,} candidates serve at least one gap")

    # The ceiling every candidate TOGETHER could serve. Without it, "20 sites
    # cover 153 of 1,077 gap cells" reads as a weak optimizer when the real
    # constraint may be that most gaps are unreachable at any price -- two very
    # different recommendations to hand an operator.
    ceiling = np.zeros(len(gaps), dtype=bool)
    for idx in cover.values():
        ceiling[idx] = True
    print(f"reachable ceiling (ALL {len(cover)} candidates at once): "
          f"{ceiling.sum():,} of {len(gaps):,} gap cells, "
          f"{demand[ceiling].sum():,.0f} of {demand.sum():,.0f} demand")

    t0 = time.perf_counter()
    picks = greedy(cover, demand, N_SITES)
    greedy_s = time.perf_counter() - t0
    greedy_total = picks[-1]["cumulative_demand"] if picks else 0.0

    chosen = {p["cand_id"] for p in picks}
    reached = np.zeros(len(gaps), dtype=bool)
    for c in chosen:
        reached[cover[c]] = True
    ceil_demand = float(demand[ceiling].sum())
    print(f"\ngreedy {len(picks)} sites in {greedy_s:.2f}s: "
          f"{greedy_total:,.0f} demand newly covered "
          f"({greedy_total / ceil_demand:.1%} of the reachable ceiling, "
          f"{reached.sum():,} of {len(gaps):,} gap cells)")
    # Demand and population are deliberately different measures and they can
    # point in very different directions. Printing only the flattering one
    # would be the same error 06_coverage exists to avoid.
    print(f"  people newly covered: {pop[reached].sum():,.0f} of "
          f"{pop.sum():,.0f} in gap cells ({pop[reached].sum() / pop.sum():.1%})"
          f" -- the gap between this and the demand figure is the tourism term")
    kinds = cands.set_index("cand_id").loc[list(chosen), "kind"].value_counts()
    print("  chosen by kind: " + ", ".join(f"{k} {v}" for k, v in kinds.items()))

    tourism_sensitivity(gaps, cover, N_SITES, chosen)

    ex = exact(cover, demand, N_SITES)
    if ex["ok"]:
        pct = greedy_total / ex["demand"] if ex["demand"] > 0 else float("nan")
        label = "proven optimum" if ex["proven"] else "best incumbent (TIME LIMIT)"
        print(f"exact MILP {label} in {ex['seconds']:.1f}s: "
              f"{ex['demand']:,.0f} demand")
        print(f"  greedy reached {pct:.1%} of it "
              f"(theory only guarantees 63.2%)")
        if ex["proven"] and greedy_total > ex["demand"] + 1e-6:
            raise RuntimeError(
                f"greedy {greedy_total} beat a proven optimum {ex['demand']}; "
                "the two solvers are not looking at the same problem"
            )

    out = cands.copy()
    ranks = {p["cand_id"]: p for p in picks}
    out["selected_greedy"] = out["cand_id"].isin(chosen)
    # Nullable pandas dtypes, not object columns of None: Spark infers schema
    # from the values, and a column that is None all the way down has no type
    # to infer -- createDataFrame fails with CANNOT_DETERMINE_TYPE.
    out["greedy_rank"] = pd.array(
        [ranks[c]["rank"] if c in ranks else None for c in out["cand_id"]],
        dtype="Int64")
    out["marginal_demand"] = np.array(
        [ranks[c]["marginal_demand"] if c in ranks else np.nan
         for c in out["cand_id"]], dtype=float)
    out["selected_exact"] = out["cand_id"].isin(set(ex.get("sites", [])))
    out["gap_cells_served"] = out["cand_id"].map(
        lambda c: len(cover[c]) if c in cover else 0)
    # ⚠️ S21 (the NRQZ polygon) is not built, so nothing here has been checked
    # against the Quiet Zone. Written as null, never as False: claiming a site
    # is outside the NRQZ without looking is the kind of quiet lie this project
    # exists not to tell. config.yml pre-registers nrqz_policy: flag.
    out["nrqz"] = pd.array([None] * len(out), dtype="boolean")
    print("\n⚠️ NRQZ not evaluated -- S21 (the boundary polygon) does not exist "
          "yet, so `nrqz` is null, not false, on every candidate")

    dest = out_path("gold", "siting")
    sedona.createDataFrame(out).write.mode("overwrite").parquet(dest)
    print(f"wrote {len(out)} candidates -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
