# Validation

What has been checked, what the checks found, and what is known to be wrong
with the model and by how much. Numbers here follow the same rule as
everywhere else in this repository: each one was produced by a command, and
nothing modelled is presented as measured.

Scope note: everything below is demo scope (Kanawha County) unless marked
otherwise. The statewide validation against FCC BDC polygons and Ookla open
data is the next milestone and its planned form is at the bottom.

## Internal cross-checks that passed

**DEM vs the towers' own reported elevations.** The FCC ASR registry reports
ground elevation at each structure independently of any DEM. Sampling the
warped 90 m surface at all 605 in-scope structures: median residual
**-4.3 m**, p90 |residual| 22.6 m. This validates three things at once — the
D/M/S coordinate conversion, the hemisphere signs, and the raster's
destination transform — because an error in any of them moves the median by
hundreds of metres, not single digits. The slightly negative sign is expected:
bilinear downsampling of a surface model shaves the hilltops that towers
stand on.

**Population conservation through areal interpolation.** 99.25% of block-group
population survives onto the hexagon grid; the 0.75% loss is boundary slivers
from H3 centre-containment. The stage fails above 2% loss.

**Best-server join conserves rows.** Every receiver appears exactly once in
the coverage table; asserted in stages 06 and 07, because a duplicated join
key would silently rescale every weighted statistic.

**Sedona zonal statistics vs an independent recompute.** Stage 08's tiled
RS_ZonalStatsAll plan is checked against a rasterio/numpy recompute of a
three-hex sample on every run.

**The DQ gate fails, not only passes.** Run against a deliberately tampered
config (`RFC_CONFIG` pointing at thresholds the data cannot meet), stage 07
exits 1 and names the failed checks. A gate that has only ever passed is not
evidence of anything.

**Optimizer vs proven optimum.** The greedy site plan reaches 99.8% of the
exact MILP optimum (HiGHS, proven, not time-limited) on the demo instance.
The two solvers are also pitted against each other in `tests/test_siting.py`
on a small instance with a known answer where greedy is provably suboptimal —
so the test fails if either solver silently degrades into the other.

## The building-height clutter model vs its pre-registered baseline

The receiver clutter term was upgraded after first results existed, from a
flat per-class constant to a knife-edge over the pixel's tallest Overture
building (see `config.yml: buildings` for the forcing measurement and
parameters). The pre-registered baseline is retained behind
`BUILDING_CLUTTER=0` and reproduces bit for bit; a test asserts it.

| Demo scope | Cells covered | Population covered | Median served RSRP |
|---|---|---|---|
| Class-based baseline | 67.8% | 89.3% | -88.6 dBm |
| + building heights | 67.4% | 88.1% | -89.5 dBm |

The direction and size are both sensible: the term can only add loss, and it
adds it where buildings are, which is where people are — so population
coverage moves more than cell coverage (2,171 additional people in gap
cells).

The consistency check that makes this a refinement rather than a re-tune: the
median WV building (3.55 m measured over 4.55M footprints) evaluates to
**14.8 dB** under the knife-edge geometry, against the **15.0 dB** flat value
the built-up class pre-registered. Two independent descriptions of the same
ordinary street agree to within 0.2 dB.

## Known limitations, quantified where possible

**ASR is not a cell-site registry.** Structures over 200 ft, broadcast masts
included, rooftops and small cells missed entirely. This is the single
largest error source in the model and it is not fixable from open data. It is
handled as a finding: the ASR-vs-BDC reconciliation (below) publishes which
registered structures plausibly carry cellular service. Until then, coverage
here is expected to be *pessimistic* in towns (missing rooftop sites) and the
67-68% cell figure should not be quoted without that caveat.

**Surface-model double counting.** GLO-30 is a DSM: canopy and large
buildings are partly inside the terrain profile that drives diffraction, and
tree cover / building height are also charged at the receiver by the clutter
term. Two bounds keep this honest: the clutter term takes the max of its two
descriptions rather than stacking them, and the building knife edge is capped
at 30 dB. At 90 m posting with bilinear resampling the DSM largely averages
away ordinary houses (median 3.55 m), so for the typical receiver the
building term and the DSM are close to disjoint; the overlap is real for
downtown high-rises and forest edges. The clean fix is a bare-earth (3DEP)
sensitivity run, listed as an upgrade path in `docs/data-sources.md`.

**Greenfield candidate sites sit on DSM maxima.** The highest pixel in an r7
cell can be canopy rather than ground, so a proposed mast there is
effectively credited with the canopy height. Deliberately not "fixed" by
re-picking candidates after seeing results; the honest fix is the same 3DEP
sensitivity run.

**Clutter at the receiver only.** No path-integrated vegetation loss;
mid-path forest enters only through the DSM. A link grazing 10 km of canopy
is charged the same clutter as one crossing open valleys to the same
receiver.

**Overture under-tags WV state parks.** 4 tagged statewide vs roughly 35
real; most are tagged `park`, which is not in the pre-registered category
list because it would swamp the tourism term with municipal playgrounds. The
tourism demand term therefore under-weights state parks specifically. Left
as-is: re-picking categories to change the result is what pre-registration
exists to prevent.

**The tourism sensitivity is a caveat on the siting result, not a footnote.**
Re-solving the 20-site plan under `tourism_weight` in {0, 1000, 3000} shares
{3, 20, 19} of 20 sites with the published plan. The plan is insensitive to
the weight's magnitude and entirely dependent on tourism being counted at
all. Anyone using the site list should decide which world they are building
for.

## Planned: external validation (statewide)

Two ground truths, used asymmetrically because they prove different things:

- **FCC BDC mobile coverage polygons** (per-carrier, 4G LTE): full-coverage
  claims, so they support IoU and a confusion matrix against the modelled
  surface, at the BDC's own -105 dBm reference threshold.
- **Ookla open data**: crowdsourced speedtests prove *presence* of service
  and can never prove absence — an empty tile may simply hold no people. Used
  for exactly one claim: the false-negative rate on gap calls (of the cells
  this model calls uncovered, how many have real-world tests in them).
- **ASR-BDC reconciliation**, published as a derived dataset: which
  registered structures sit inside carrier coverage consistent with hosting
  the equipment — turning the model's weakest input into its most useful
  output. The retained `structure_type` column (tower / mast / pole /
  building) is the discriminator to test first.
