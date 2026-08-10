# Measured numbers

Every figure here was produced by a command in this repo on the machine
named. Modelled or estimated numbers are labelled as such and kept in a
separate table -- nothing estimated is presented as measured.

## Propagation kernel throughput

`python scripts/bench_kernel.py 200000`, Apple M4 (arm64), single core,
synthetic West-Virginia-sized grid (4222 x 4777 cells at 90 m).

| Metric | Measured |
|---|---|
| Broadcast payload (int16 DEM + uint8 clutter) | 61 MB |
| Pairs per second, one core | 157,276 |
| Pairs per minute per core | 9,436,560 |
| Go/no-go gate from the build plan | 100,000 |
| Headroom over gate | 94x |

Date: 2026-08-09. 200,000 pairs x 128 profile samples in 1.27 s.

The row above is a **frozen record of one run**, not a value later runs should match.
`make bench` is a live wall-clock measurement on a laptop with other things running;
repeat runs on the same machine and same commit spanned roughly 9.4M to 10.6M
pairs/min/core. Treat a differing rerun as normal variance. The only result that means
anything is a fall toward the 100,000 gate.

### What the headroom does and does not buy

At this rate the entire statewide link pass -- on the order of 3M
tower/receiver pairs -- is roughly 20 seconds of single-core work. The
distributed run is therefore not justified by wall-clock necessity, and the
README says so plainly rather than implying Spark was required to make the
problem tractable.

The headroom is deliberately **not** spent on a finer grid. The 90 m cell and
H3 r8 receiver resolution were pre-registered on physical grounds -- the first
Fresnel radius at 700 MHz over 20 km is ~65 m, and the 8 dB shadow-fading
margin already exceeds what a finer receiver grid could resolve. Spending
spare compute to add precision the model cannot support would be false
precision that happens to be affordable.

What it is spent on instead: running the pipeline at 30 m / r9 as a
*sensitivity check*, to demonstrate the conclusions do not move. That is a
validation artifact rather than a resolution upgrade.

## Fallbacks not needed

The build plan carried three staged fallbacks in case the kernel missed its
gate (180 m DEM, 64 profile samples, 30 km link radius, each roughly a 2x
saving). None were used. They are recorded here only so the decision trail is
legible.

## Pending measurements

These rows are intentionally empty until a real run fills them; see
`docs/validation.md` for the same discipline applied to model accuracy.

| Metric | Status |
|---|---|
| `RS_Value` per-sample vs broadcast-array kernel | not yet measured |
| Overture buildings bbox row-group pruning, global -> WV | not yet measured |
| End-to-end pipeline wall clock, SCOPE=state on EKS | not yet measured |
| EKS spot node-hours and total AWS spend | not yet measured |
