"""Offline checks for the site optimizer.

The optimizer is the one part of this project that makes a recommendation
rather than a measurement, so it is the part where being confidently wrong
costs most. Two failure modes are worth a test:

  * ranking candidates by total coverage instead of MARGINAL coverage, which
    picks twenty overlapping sites on the same ridge and looks perfectly
    reasonable in the output table;
  * a MILP whose constraint matrix is subtly wrong, which returns a clean
    "optimal" for a different problem than the one asked.

Both are caught by one small instance with a KNOWN answer where greedy is
provably suboptimal -- so the test fails if greedy silently becomes exact, if
exact silently becomes greedy, or if either loses to the other in the wrong
direction. No Spark, no network.
"""
import importlib
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

siting = importlib.import_module("09_siting")

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


# Six gap cells of equal demand, three candidates, room for two sites.
#   A covers {0,1,2,3}   -- the biggest single site, and the greedy trap
#   B covers {0,1,4}
#   C covers {2,3,5}
# Greedy takes A first (gain 4), after which B and C add one cell each: 5.
# B + C together cover all six. Optimum is 6, greedy gets 5.
TRAP = {
    "A": np.array([0, 1, 2, 3]),
    "B": np.array([0, 1, 4]),
    "C": np.array([2, 3, 5]),
}
TRAP_DEMAND = np.ones(6)


@check
def test_greedy_picks_marginal_gain_not_total_coverage():
    picks = siting.greedy(TRAP, TRAP_DEMAND, 2)
    assert [p["cand_id"] for p in picks][0] == "A", picks
    # The second pick must be worth 1, not 3: three of B's cells are already
    # covered by A. A total-coverage ranking would report 3 here.
    assert picks[1]["marginal_demand"] == 1.0, picks
    assert picks[-1]["cumulative_demand"] == 5.0, picks


@check
def test_exact_beats_greedy_on_an_instance_where_it_should():
    """If this ever reports parity, either the MILP collapsed into greedy or
    the coverage constraint is not binding -- both make the published
    optimality gap meaningless."""
    ex = siting.exact(TRAP, TRAP_DEMAND, 2)
    assert ex["ok"] and ex["proven"], ex
    assert ex["demand"] == 6.0, ex
    assert set(ex["sites"]) == {"B", "C"}, ex
    greedy_total = siting.greedy(TRAP, TRAP_DEMAND, 2)[-1]["cumulative_demand"]
    assert greedy_total < ex["demand"], (greedy_total, ex["demand"])


@check
def test_greedy_stops_when_nothing_is_left_to_gain():
    """Asked for more sites than can help, greedy must return fewer rather
    than pad the plan with towers that cover nothing."""
    picks = siting.greedy(TRAP, TRAP_DEMAND, 10)
    assert len(picks) == 3, picks
    assert picks[-1]["cumulative_demand"] == 6.0, picks
    assert all(p["marginal_demand"] > 0 for p in picks), picks


@check
def test_demand_weighting_changes_the_choice():
    """Coverage is not cell counting here either: one high-demand cell can be
    worth more than three empty ones, and the optimizer must say so."""
    demand = np.array([0.1, 0.1, 0.1, 0.1, 50.0, 0.1])   # cell 4 is B-only
    picks = siting.greedy(TRAP, demand, 1)
    assert picks[0]["cand_id"] == "B", picks


if __name__ == "__main__":
    failed = 0
    for fn in CHECKS:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} siting checks passed")
    raise SystemExit(1 if failed else 0)
