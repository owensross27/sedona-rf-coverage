"""Offline checks for the coverage summary arithmetic.

The distributed part of stage 06 is a GROUP BY and a LEFT JOIN, which Spark can
be trusted with. What cannot be trusted without a test is the weighting: a
coverage number that silently reports cells when it claims to report people is
wrong in the direction that flatters the result, because gaps are rural and
rural cells hold fewer residents each.

No Spark, no network -- summarise() takes the pandas frame that stage 06 reads
back from gold/coverage.
"""
import importlib
import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

coverage = importlib.import_module("06_coverage")

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def frame(rows):
    """rows = (pop, demand, best_rsrp_dbm, n_links, n_servers)."""
    df = pd.DataFrame(rows, columns=["pop", "demand", "best_rsrp_dbm",
                                     "n_links", "n_servers"])
    df["is_covered"] = df["best_rsrp_dbm"].fillna(-999.0) >= -105.0
    return df


@check
def test_population_weighting_is_not_cell_weighting():
    """One dense covered city cell and three empty rural gaps: 25% of cells
    are covered but 91% of people are. Reporting either as the other is the
    single easiest way to make this project say something false."""
    s = coverage.summarise(frame([
        (10000.0, 10000.0, -90.0, 12, 3),
        (   300.0,   300.0, -120.0, 4, 0),
        (   400.0,   400.0, -130.0, 2, 0),
        (   300.0,   300.0,   None, 0, 0),
    ]))
    assert s["cells_covered"] == 1, s
    assert math.isclose(s["cells_covered_frac"], 0.25), s
    assert math.isclose(s["population_covered_frac"], 10000 / 11000), s
    assert math.isclose(s["population_uncovered"], 1000.0), s


@check
def test_a_receiver_with_no_link_is_uncovered_not_missing():
    """The LEFT JOIN in best_server() gives these rows a null RSRP. If they
    ever stop being counted, the uncovered population falls towards zero and
    the map looks best exactly where the model knows least."""
    s = coverage.summarise(frame([
        (100.0, 100.0, -80.0, 5, 2),
        (900.0, 900.0, None, 0, 0),
    ]))
    assert s["cells"] == 2, s
    assert s["cells_no_link"] == 1, s
    assert math.isclose(s["population_uncovered"], 900.0), s
    assert math.isclose(s["population_covered_frac"], 0.1), s


@check
def test_zero_population_scope_does_not_divide_by_zero():
    """A scope of pure wilderness has real demand and no residents. This must
    return 0.0, not nan -- a nan would propagate into the printed headline and
    into any downstream comparison as a silently false answer."""
    s = coverage.summarise(frame([
        (0.0, 5000.0, -90.0, 3, 1),
        (0.0, 2000.0, -140.0, 1, 0),
    ]))
    assert s["population_covered_frac"] == 0.0, s
    assert math.isclose(s["demand_covered_frac"], 5000 / 7000), s


@check
def test_single_server_cells_are_counted_from_n_servers():
    """Redundancy, not coverage: a cell above threshold from exactly one tower
    goes dark when that tower does."""
    s = coverage.summarise(frame([
        (1.0, 1.0, -90.0, 9, 3),
        (1.0, 1.0, -100.0, 4, 1),
        (1.0, 1.0, -130.0, 2, 0),
    ]))
    assert s["cells_single_server"] == 1, s
    assert math.isclose(s["median_served_rsrp_dbm"], -95.0), s


if __name__ == "__main__":
    failed = 0
    for fn in CHECKS:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} coverage checks passed")
    raise SystemExit(1 if failed else 0)
