"""Stage 07 -- the data-quality gate. Exits non-zero so CI can hold the line.

Every threshold here was pre-registered in `config.yml: dq` before any surface
existed, for the same reason the RF parameters were: a gate whose numbers get
adjusted until the run passes is not a gate. If a check fires and the data is
right, the fix is a commit message saying what measurement moved the threshold.

The checks are the ones that catch a pipeline that produced a PLAUSIBLE map:

  * counts, so a silently-empty upstream read fails loudly rather than
    producing a beautifully rendered map of nothing;
  * null geometry, because a null geom survives every ST_* call as a null and
    disappears at the next join instead of raising;
  * RSRP bounds, because a sign error or a unit slip in the link budget lands
    somewhere physically impossible long before it looks wrong on a map;
  * referential integrity between links and receivers, because a drifted join
    key produces fewer rows, not an error;
  * row conservation through the best-server join, which is the one place a
    duplicate key would rescale every weighted number in the summary.

All checks run before anything exits, so one run tells you everything that is
wrong rather than only the first thing.

Usage:
    SCOPE=demo LOCAL_OUT=1 python src/07_dq.py
    make dq SCOPE=state
"""
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DQ, scope  # noqa: E402
from session import get_sedona, out_path  # noqa: E402


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None          # None = deliberately not applicable at this scope
    detail: str

    @property
    def label(self) -> str:
        return {True: "PASS", False: "FAIL", None: "SKIP"}[self.ok]


def run_checks(sedona, scope_name: str) -> list[Check]:
    towers = sedona.read.format("geoparquet").load(out_path("bronze", "towers"))
    hexes = sedona.read.format("geoparquet").load(out_path("silver", "hex_grid"))
    links = sedona.read.parquet(out_path("silver", "links"))
    cov = sedona.read.parquet(out_path("gold", "coverage"))

    n_towers, n_hexes, n_cov = towers.count(), hexes.count(), cov.count()
    checks = [
        Check("towers present", n_towers >= DQ["min_towers"],
              f"{n_towers:,} structures (min {DQ['min_towers']:,})"),
    ]

    for name, df, col, total in (("towers", towers, "geom", n_towers),
                                 ("receivers", hexes, "center", n_hexes)):
        n_null = df.filter(f"{col} IS NULL").count()
        frac = n_null / total if total else 0.0
        checks.append(Check(
            f"{name} null geometry", frac <= DQ["max_null_geom_frac"],
            f"{n_null:,} null of {total:,} ({frac:.3%}, "
            f"max {DQ['max_null_geom_frac']:.3%})"))

    # min_hex_count is a STATE-scope number by construction: one county cannot
    # contain 50k r8 hexagons. Applying it at demo scope would break the
    # stranger-clone gate, which is a bug, not a stricter test. Reported as
    # skipped rather than silently dropped.
    if scope_name == "state":
        checks.append(Check("receiver count", n_hexes >= DQ["min_hex_count"],
                            f"{n_hexes:,} cells (min {DQ['min_hex_count']:,})"))
    else:
        checks.append(Check(
            "receiver count", None,
            f"{n_hexes:,} cells; min {DQ['min_hex_count']:,} applies at "
            f"SCOPE=state only, this is SCOPE={scope_name}"))

    lo, hi = links.selectExpr("MIN(rsrp_dbm)", "MAX(rsrp_dbm)").first()
    checks.append(Check(
        "RSRP within physical bounds",
        lo >= DQ["min_rsrp_dbm"] and hi <= DQ["max_rsrp_dbm"],
        f"{lo:.1f} to {hi:.1f} dBm (allowed {DQ['min_rsrp_dbm']:.0f} to "
        f"{DQ['max_rsrp_dbm']:.0f})"))

    # An h3_r8 in links with no matching receiver means the two stages disagree
    # about the grid. It cannot raise anywhere -- the join in 06 just returns
    # fewer rows, and the coverage map comes out slightly smaller than reality.
    orphans = links.select("h3_r8").distinct().join(
        hexes.select("h3_r8"), on="h3_r8", how="left_anti").count()
    checks.append(Check("links reference real receivers", orphans == 0,
                        f"{orphans:,} link h3 keys absent from the hex grid"))

    checks.append(Check("best-server join conserved rows", n_cov == n_hexes,
                        f"coverage {n_cov:,} vs receivers {n_hexes:,}"))

    n_covered = cov.filter("is_covered").count()
    checks.append(Check(
        "coverage is not degenerate", 0 < n_covered < n_hexes,
        f"{n_covered:,} of {n_hexes:,} cells covered "
        f"({n_covered / n_hexes:.1%})"))

    return checks


def main() -> int:
    sc = scope()
    sedona = get_sedona(f"rf-dq-{sc['name']}")
    checks = run_checks(sedona, sc["name"])

    width = max(len(c.name) for c in checks)
    print(f"\ndata quality gate, scope={sc['name']}")
    for c in checks:
        print(f"  {c.label}  {c.name.ljust(width)}  {c.detail}")

    failed = [c for c in checks if c.ok is False]
    skipped = [c for c in checks if c.ok is None]
    print(f"\n{len(checks) - len(failed) - len(skipped)} passed, "
          f"{len(failed)} failed, {len(skipped)} skipped")
    if failed:
        print("DQ GATE FAILED: " + "; ".join(c.name for c in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
