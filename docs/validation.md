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

## The National Radio Quiet Zone boundary (measured 2026-08-10)

Constructed from the four corners in 47 CFR 1.924 rather than from NRAO's KMZ,
so the transcription is the thing that can be wrong. Two checks, both offline
in `tests/test_bronze.py`: Green Bank falls inside and Charleston does not, and
the constructed polygon encloses **13,108 sq mi** against the ~13,000
published. One wrong degree moves that by thousands.

Reprojection to EPSG:5070 is **densified at 0.05° before the transform**. The
parallels bow in Albers: joining the north and south corners with straight
lines cuts **461 m** out of the middle of each edge — five times the 90 m
analysis grid — and would silently leave a 105 km² strip of the zone
unflagged. Densified, the error is 0.27 m. The meridians are straight in Albers
and need nothing.

At demo scope **0 of 609 candidates are inside; the nearest is 51 km west of
the boundary**, and the stage prints that distance precisely so a zero cannot
be mistaken for a check that never ran. The constraint is exercised only at
`SCOPE=state`, where Pocahontas, Randolph, Pendleton, Greenbrier and Grant
counties fall inside the zone.

## Ookla: measured, and the result is "not usable at this scope" (2026-08-10)

The false-negative rate on gap calls was computed at demo scope with the
thresholds pre-registered in `config.yml` (`ookla_min_tests: 5`,
`ookla_min_devices: 2`, committed before the number existed). The result:

| | |
|---|---|
| Sample density in scope | **224 of 3,345 hexes (6.7%)** contain any speedtest at all |
| Gap hexes with speedtests | 0 of 1,092 (0.0%) |
| Covered hexes with speedtests | 40 of 2,253 (1.8%) — the control |

**The control is what kills it.** Hexes the model calls *covered* carry
speedtests at only 1.8%, so the sample cannot discriminate covered from
uncovered ground, and the 0.0% is a statement about Ookla's sample density in a
mostly-forested county rather than about this model. `10_ookla.py` prints a
`VERDICT: NOT USABLE at this scope` and refuses to present the number as
validation. Re-run at `SCOPE=state`, where the denominator is ~25x larger.

This is the reason the control is computed at all. Without it, "0.0% of our gap
calls are contradicted by real-world speedtests" is a sentence that reads like
a triumph and means nothing.

Two ways the metric lies, both live at once even when the sample is adequate:

- **Reads too high.** A test served by a rooftop or small-cell site is real
  service that the ASR registry never lists, so the number measures the model
  and the tower list jointly. A test carried by mid-band 5G is service, but not
  the 700 MHz LTE this model predicts. Ookla aggregates a whole quarter, so one
  moment of service anywhere in a tile counts, against a steady-state
  prediction with an 8 dB margin.
- **Reads too low, and this dominates.** Gap cells here average 0.97 tree cover
  and 156 m of relief — mostly nobody is there to test. And the selection
  effect runs the wrong way by construction: **someone with no signal cannot
  complete a speedtest**, so the population whose missing coverage we most want
  to confirm is exactly the one that cannot appear in the numerator.

So the honest claim is one-sided: *at least F% of gap calls are demonstrably
wrong; the true rate is unknown and unbounded above.* Never "the model is F%
wrong".

## Planned: external validation (statewide)

Two ground truths, used asymmetrically because they prove different things:

- **FCC BDC mobile coverage polygons** (per-carrier, 4G LTE): full-coverage
  claims, so they support IoU and a confusion matrix against the modelled
  surface, at the BDC's own -105 dBm reference threshold.
- **Ookla open data**: ingested and run (`make ookla`, stage `10_ookla.py`),
  and the answer at demo scope is **that it cannot be used yet** — see below.
- **ASR-BDC reconciliation**, published as a derived dataset: which
  registered structures sit inside carrier coverage consistent with hosting
  the equipment — turning the model's weakest input into its most useful
  output. The retained `structure_type` column (tower / mast / pole /
  building) is the discriminator to test first.
