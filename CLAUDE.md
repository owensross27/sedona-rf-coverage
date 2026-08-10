# sedona-rf-coverage — working conventions

## Verify, don't assume

Three gates, all runnable locally with no cloud account:

```bash
make test    # 18 kernel correctness checks, no framework, no network
make bench   # throughput gate: >= 100k pairs/min/core
make smoke   # JDK + pyspark + Sedona jars + ST_* functions resolve
```

`source scripts/java_env.sh` before any local Spark invocation — JDK 17 is keg-only under
Homebrew, so `java -version` fails even when it is installed. Never source it before
`spark-submit --master k8s://`: it exports a laptop venv python path that does not exist
inside the driver pod.

## The rule that matters most here

**A propagation model can be beautiful and completely wrong.** Both bugs found so far
produced plausible maps and were caught only because tests asserted *physics* (flat
ground must cost zero diffraction; line of sight must fail at the radio horizon) rather
than "the code ran".

So: any change to `src/propagation.py` needs a test that would fail if the physics broke,
not just one that exercises the code path. And RF parameters are **pre-registered** in
`config.yml` before results are computed — if you change one, the commit message must say
what measurement forced the change.

## Numbers

Never present a modelled number as a measured one. `docs/benchmarks.md` has a measured
table and a pending table; rows move between them only when a command produces them.
Same discipline for model accuracy in `docs/validation.md`.

## Scope tiers

Every stage reads `SCOPE` from the environment (`demo` = one county, `mvp` = five,
`state` = all 55). `LOCAL_OUT=1` writes to `./data` instead of S3, which is what keeps
`make demo` runnable with no AWS account. If that stops working, the repo is not
reproducible and it is a bug, not an inconvenience.

## Public repo hygiene

This repository is intended to be public. The AWS account ID and bucket name must never
appear in a tracked file — they come from `$AWS_ACCOUNT_ID` and `$RF_BUCKET` and are
substituted with `envsubst` at apply time. Docs use `<ACCOUNT_ID>` placeholders.

## Stack pins — do not bump casually

- **Sedona 1.9.1, never 1.9.0.** 1.9.0 has a `ST_Transform` regression over 180 m
  (GH-3161) that silently corrupts reprojection, and this pipeline reprojects constantly.
- `geotools-wrapper:1.9.1-33.5` — required for raster; its absence is a
  `NoClassDefFoundError` at the first `RS_*` call, not at startup.
- `hadoop-aws:3.3.4` — must match the Hadoop line pyspark 3.5.x is built against.
- Jars are **baked** into the image, never resolved with `--packages` at job start.

## Kubernetes gotchas that cost real time

- A `spark-defaults.conf` baked into the image is **inert on Kubernetes** — Spark mounts
  a generated ConfigMap over `SPARK_CONF_DIR`. s3a config lives in the SparkConf builder
  in `src/session.py`.
- Anonymous open-data reads and authenticated writes must coexist in one session, which
  requires **per-bucket** credential providers. A typo in a bucket name there fails as a
  403 that looks exactly like a missing object.
- PySpark's memory overhead factor is **0.4, not 0.1**. Size against node *allocatable*.
- Everything is **arm64**. `make preflight` asserts third-party images publish it before
  a pod can fail with `exec format error`.
