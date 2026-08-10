# sedona-rf-coverage

Modelling cellular coverage across West Virginia from open data, finding the
population it misses, and picking the tower sites that would close the most of
that gap.

Apache Sedona and Spark for the distributed geospatial work, a vectorized
terrain-diffraction kernel for the physics, Kubernetes on EKS for the
production run, and a static MapLibre map that stays up after the cluster is
torn down.

> **Status: in progress.** The propagation kernel is written, tested and
> benchmarked. The ingestion, coverage and siting stages are being built. No
> coverage results are published yet, and this README will not claim any until
> they exist. See [Verification](#verification) for what is and is not measured.

---

## Why West Virginia

The hardest terrain in the eastern United States, some of the worst mobile
coverage, and three things that make the siting problem more interesting than
a population raster:

- **The National Radio Quiet Zone.** A 13,000 square mile federal zone around
  the Green Bank Observatory where new transmitters require coordination. It
  is a real legal constraint on where a tower can go, and the optimizer treats
  it as one -- candidates inside it are flagged, never silently dropped.
- **Population moving in two directions at once.** The Eastern Panhandle is
  growing as a DC exurb while the southern coalfields decline. A single
  statewide growth figure would hide both.
- **Demand without residents.** New River Gorge, Snowshoe, the Hatfield-McCoy
  trail system. A trailhead has no population and real demand, which is why
  tourism enters the demand score additively rather than as a multiplier.

## The model

```
RSRP = EIRP - 10*log10(subcarriers) - FSPL - L_diffraction - L_clutter - shadow margin
```

700 MHz low-band LTE, the band that actually carries rural coverage. Free-space
loss, plus Deygout three-edge knife-edge diffraction over the terrain profile
with an effective-earth correction for atmospheric refraction, plus an excess
loss for the receiver's land cover class. Covered means RSRP at or above
**-105 dBm** -- chosen because it is the FCC Broadband Data Collection's own
4G LTE reference value, so validation compares like with like instead of
against a threshold of our choosing.

Every parameter is pre-registered in [`config.yml`](config.yml) with the
reasoning attached, committed before any result was computed.

## Architecture

```
open S3 buckets                    this pipeline                      output
─────────────────                  ─────────────                      ──────
Copernicus GLO-30 DEM  ─┐
ESA WorldCover 10m     ─┼─ 02_terrain  → one EPSG:5070 90 m grid ─┐
                        │                (DEM + clutter, co-registered)
FCC ASR registrations  ─┼─ 01_towers   → GeoParquet                │
TIGER + ACS            ─┼─ 03_census   → block groups, population  ├─ 05_links
Overture places        ─┴─ 04_grid     → H3 r8 receivers + demand ─┘   ST_DWithin
                                                                       + kernel
                                                                          │
                                            06_coverage ←─────────────────┘
                                            best-server, gaps, COGs
                                                  │
                                    ┌─────────────┼──────────────┐
                              08_validate    09_siting      06_publish
                              vs FCC BDC     greedy + MILP   PMTiles + COG
                              and Ookla      under NRQZ      → CloudFront
```

### Where Sedona does the work

Pair generation is the genuinely hard distributed problem: roughly 5,000
registered structures against ~85,000 H3 receiver cells is 425 million
combinations as a cross join. `ST_DWithin` with a 40 km predicate and a
spatial index turns that into a few million. Sedona also carries the
block-group-to-hex areal interpolation (`ST_Intersection` / `ST_Area`), the
H3 grid construction (`ST_H3CellIDs`), zonal statistics over the terrain
rasters (`RS_ZonalStats`), the best-server raster composite (`RS_MapAlgebra`),
and all GeoParquet I/O.

### Where it deliberately does not

The per-link physics runs in numpy on broadcast arrays, not through
`RS_Value`. The obvious implementation explodes each pair into its terrain
samples and looks each one up in the raster: at state scope that is ~380
million raster calls across the JVM boundary. Instead the state's DEM and
clutter are broadcast once as two small arrays (61 MB measured, int16 + uint8
at 90 m) and every sample becomes a fancy index into RAM.

Both numbers will be published side by side in
[`docs/benchmarks.md`](docs/benchmarks.md). "I profiled the obvious approach
and here is why I moved off it" is the claim worth making; "I used the fast
one" is not.

## Measured so far

| Metric | Value | How |
|---|---|---|
| Kernel throughput, 1 core | **9,436,560 pairs/min** | `make bench`, M4, 200k pairs x 128 samples in 1.27 s |
| Go/no-go gate from the build plan | 100,000 pairs/min | 94x headroom |
| Broadcast terrain payload | 61 MB | WV at 90 m, int16 DEM + uint8 clutter |
| Correctness checks passing | 18 / 18 | `make test` |

The headroom is deliberately **not** spent on a finer grid. The 90 m cell and
H3 r8 receivers were pre-registered on physical grounds -- the first Fresnel
radius at 700 MHz over 20 km is about 65 m, and the 8 dB shadow-fading margin
already exceeds what a finer receiver grid could resolve. Adding precision the
model cannot support would be false precision that happens to be affordable.
It is spent instead on running the pipeline at 30 m / r9 as a sensitivity
check, to show the conclusions do not move.

At this rate the whole statewide link pass is roughly 20 seconds of
single-core work, so **Spark is not what makes this problem tractable** and the
repo does not pretend otherwise. What the cluster buys is the ingestion joins,
the raster reprojection, and a realistic deployment story.

## Two bugs worth reading about

Both were caught by the test suite before any map was drawn. They are written
up because the failure mode they share -- a model that produces a beautiful,
confident, wrong picture -- is the one that matters in this domain.

**Phantom diffraction over flat ground.** The first kernel searched for the
strongest Fresnel obstruction across every profile sample. A receiver 1.5 m
above flat ground has about 3 m of clearance against an 8 m first-Fresnel
radius at 5 km, so the ground immediately beside it always registered as an
obstruction -- and Deygout charged for it three times, principal edge plus both
sub-path edges. Measured result: **11.4 dB of diffraction loss across
dead-flat terrain**, on every link in the state, which would have inflated the
headline uncovered-population figure. The physical error was treating a smooth
ground plane as a knife edge. Diffraction is now restricted to terrain that
actually rises above the geometric ray; the near-antenna ground plane is a
height-gain effect carried by the clutter term and the shadow margin.

**A test that was wrong, not the code.** A link-budget test asserted that a
40 km free-space link lands just above threshold. It came out 19 dB lower.
The kernel was right: at 40 km a 50 m mast is *beyond the radio horizon* of a
1.5 m handset (√(2kRh) ≈ 29 km + 5 km ≈ 34 km), so the earth's own curvature
obstructs the path. The test was replaced with one that verifies the
line-of-sight flag flips at the computed horizon, and `config.yml` now records
that the 40 km pair-generation cap is generous rather than binding.

## Reproduce it

```bash
make setup   # uv venv, python 3.11, pinned pyspark 3.5.3 + sedona 1.9.1
make test    # 18 correctness checks, no framework, no network
make bench   # throughput gate
```

`make demo` runs one county end to end with local Spark, writing to `./data`.
**No AWS account, no credentials, no cloud spend.** If that stops working, the
repo is not reproducible and the failure is a bug.

The cloud tier is opt-in and needs `RF_BUCKET` and `AWS_ACCOUNT_ID` exported;
no account identifier appears anywhere in this repository.

## Running Sedona on Kubernetes

There is no official Sedona Kubernetes image, no Helm chart, and no guide. The
`apache/sedona` image on Docker Hub is explicitly a single-node dev/demo
build. [`docs/eks-runbook.md`](docs/eks-runbook.md) documents the gap and
ships a working answer. The parts that cost real time:

- **Sedona must be 1.9.1, not 1.9.0.** 1.9.0 carries a `ST_Transform`
  regression over 180 m (GH-3161) that silently corrupts reprojection. This
  pipeline reprojects every tower and DEM tile.
- **`spark-defaults.conf` baked into an image does nothing on Kubernetes.**
  Spark generates a ConfigMap from the submit-time config and mounts it over
  `SPARK_CONF_DIR`, shadowing the image's copy. s3a settings have to be set in
  the `SparkConf` builder — see [`src/session.py`](src/session.py).
- **Anonymous open-data reads and authenticated writes in one session.** A
  global anonymous provider breaks the writes; the default chain breaks the
  public reads. Per-bucket overrides are the only correct answer, and a typo
  in a bucket name there fails as a 403 that looks exactly like a missing
  object.
- **PySpark's memory overhead factor is 0.4, not 0.1.** `memory: 24g` requests
  ~33.6 GiB. Size against node allocatable or pods sit Pending.
- **arm64 end to end.** Graviton measured ~35% cheaper on spot, and building
  natively on Apple Silicon removes QEMU from the iteration loop. `make
  preflight` asserts every third-party image publishes arm64 before a pod can
  fail with `exec format error`.

## Cost

Modelled from live us-west-2 spot prices measured 2026-08-09, and labelled
modelled until a real run replaces it. The design targets about **$21**, worst
case ~$35. The same architecture with eksctl's default NAT gateway, a load
balancer, and the cluster left up for a week runs about **$115** — the saving
is architectural, not disciplinary:

- NAT gateway **disabled** (public subnets + IGW reach ECR and S3 for free)
- single-AZ nodegroups (no cross-AZ shuffle at $0.01/GB each way)
- no load balancer and no ingress controller (NodePort behind CloudFront)
- **the public map does not depend on the cluster**, so there is never a
  reason to leave it running

`make status` reports month-to-date spend and asserts no NAT gateway exists;
`make destroy-all` tears down the cluster, the Terraform resources, and scans
for orphaned EBS volumes.

## Evaluated and rejected

| Choice | Why not |
|---|---|
| Karpenter / cluster-autoscaler | Two known workload shapes, ~20 hours total. An hour of IAM setup that saves nothing and adds a demo-day failure mode. |
| ALB or NLB | ~$17/mo plus a controller install, to expose one NodePort that CloudFront already fronts. |
| martin / pg_tileserv | Both are excellent, and both exist to serve tiles from PostGIS. PMTiles serves the same tiles as one static file with no database in the path. |
| Iceberg | No schema-evolution or incremental-refresh requirement here; outputs are COGs and GeoParquet that a browser and DuckDB read directly. (The sibling `s2-field-ndvi` repo does use it, and earns it.) |
| IRSA | hadoop-aws 3.3.4 pairs with AWS SDK v1, making web identity an hour of yak-shaving for ~zero security delta on an ephemeral single-tenant cluster. Documented as the production upgrade. |
| cert-manager + Let's Encrypt | LE allows 5 duplicate certificates per week; this cluster is recreated 6–10 times in a fortnight. ACM is free and does not rate-limit. |
| Overture buildings (257 GB) | Height and floor counts are sparse, and the WorldCover clutter class already carries the same correction. |
| Hata / COST-231 | Invalid past ~20 km; West Virginia links routinely exceed that. |
| Wherobots Cloud | No free tier as of 2026-08-09 (trial only), so the deployment story had to be self-hosted. |

## Limitations

Stated up front rather than discovered by a reader:

- **FCC ASR structures are not cell sites.** The registry covers structures
  over 200 ft, includes broadcast masts, and misses rooftop and small-cell
  installations entirely. Reconciling ASR against FCC BDC coverage polygons —
  to infer which structures plausibly carry cellular — is planned as a
  published output, because the weakest input makes the most interesting
  finding.
- Isotropic transmitters: no antenna patterns, downtilt, or sectorization.
- No interference, no capacity modelling, no building penetration.
- Deygout over-predicts loss when several edges are of similar prominence. It
  is used anyway because the alternative, a single knife edge, systematically
  *under*-predicts in multi-ridge terrain — and the direction of error that
  flatters the result is the one to avoid.
- Partial Fresnel obstruction by terrain staying below the ray is scored as
  0 dB rather than up to 6 dB: a bounded optimism, inside the 8 dB shadow
  margin already subtracted from every link.

## Verification

| Gate | Status |
|---|---|
| Kernel correctness (`make test`) | **18/18 passing** — hand-computed FSPL, published J(0)=6.02 dB and J(1)=13.9 dB knife-edge anchors, radio-horizon geometry, batch-vs-single equivalence |
| Kernel throughput (`make bench`) | **passing, 94x margin** |
| `make demo`, one county end to end | not yet — ingestion stages in progress |
| DQ gate fails non-zero on bad input | not yet |
| Model vs FCC BDC and Ookla | not yet — see `docs/validation.md` |
| Statewide run on EKS | not yet |

## Data

All open, all cloud-native, all verified live on 2026-08-09. Full catalogue
with S3 URIs and licences: [`docs/data-sources.md`](docs/data-sources.md).

Copernicus GLO-30 DEM · ESA WorldCover 10m (CC BY 4.0) · Overture Maps places
(CDLA Permissive 2.0) · FCC Antenna Structure Registration (public domain) ·
FCC Broadband Data Collection · Ookla Open Data (CC BY-NC-SA 4.0, non-commercial)
· US Census TIGER/Line and ACS 5-year (public domain).

There is no public GeoParquet of TIGER block groups anywhere, so this pipeline
makes one.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
