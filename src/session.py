"""SparkSession builder. One place for jar pins and s3a credential wiring.

Pins (do not bump casually):
  sedona 1.9.1 -- NOT 1.9.0, which has the >180m ST_Transform regression
                  (GH-3161) that silently corrupts reprojection. This pipeline
                  reprojects every tower position and DEM tile, so 1.9.0 would
                  quietly poison the whole run.
  geotools-wrapper 1.9.1-33.5 -- required for raster; RasterUDT is a GeoTools
                  GridCoverage2D and its absence is a NoClassDefFoundError at
                  the first RS_* call, not at startup.
  hadoop-aws 3.3.4 -- must match the Hadoop line pyspark 3.5.x is built
                  against (the base image ships hadoop-client-* 3.3.4).
"""
import os
import sys

from sedona.spark import SedonaContext

from config import CFG

SEDONA = "org.apache.sedona:sedona-spark-shaded-3.5_2.12:1.9.1"
GEOTOOLS = "org.datasyslab:geotools-wrapper:1.9.1-33.5"
HADOOP_AWS = "org.apache.hadoop:hadoop-aws:3.3.4"

# Set by docker/Dockerfile. Baked-jar containers must not set
# spark.jars.packages: the jars are already on the classpath via
# $SPARK_HOME/jars, and re-resolving them through ivy at job start is both
# redundant and unsafe (executor pods have no guaranteed route to Maven
# Central mid-job).
JARS_BAKED = os.environ.get("RFC_JARS_BAKED") == "1"

# Public buckets that must be read WITHOUT signing. This is the config that
# makes the whole pipeline possible in a single Spark session: the job reads
# anonymous open data and writes to a private bucket at the same time.
#
# A global AnonymousAWSCredentialsProvider breaks the writes; the default
# credential chain breaks the public reads (some of these buckets reject
# signed requests outright). Per-bucket overrides are the only correct answer.
#
# Trap worth knowing: a typo in a bucket name here does not error. The key is
# simply never consulted, the global provider handles that bucket instead, and
# the failure surfaces as a 403 that looks exactly like a missing object.
ANON_BUCKETS = (
    "copernicus-dem-30m",
    "copernicus-dem-30m-stac",
    "esa-worldcover",
    "overturemaps-us-west-2",
    "ookla-open-data",
    "prd-tnm",
    "dataforgood-fb-data",
)

_ANON = "org.apache.hadoop.fs.s3a.AnonymousAWSCredentialsProvider"


def get_sedona(app_name: str = "sedona-rf-coverage", master: str | None = None):
    builder = SedonaContext.builder().appName(app_name)
    if not JARS_BAKED:
        builder = builder.config(
            "spark.jars.packages", ",".join([SEDONA, GEOTOOLS, HADOOP_AWS])
        )
    builder = (
        builder
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.adaptive.enabled", "true")
        .config("spark.sql.shuffle.partitions", os.environ.get("SHUFFLE_PARTS", "64"))
        .config("spark.driver.memory", os.environ.get("DRIVER_MEM", "6g"))
        # Arrow is what makes the pandas_udf propagation kernel worth writing:
        # without it every batch round-trips through pickled rows.
        .config("spark.sql.execution.arrow.pyspark.enabled", "true")
        .config("spark.sql.execution.arrow.maxRecordsPerBatch", "10000")
        # COGs are range-read; sequential fadvise would fetch whole objects.
        .config("spark.hadoop.fs.s3a.experimental.input.fadvise", "random")
        # macOS local runs: unbounded s3a connections + parallel tasks exhaust
        # kernel socket buffers ("No buffer space available") and the python
        # workers die with a bare EOFError.
        .config("spark.hadoop.fs.s3a.connection.maximum", "48")
    )
    # NOTE: the global provider is deliberately left at the hadoop-aws default
    # chain, which includes IAMInstanceCredentialsProvider -- that is what
    # picks up the EKS node role for writes to our own bucket. Only the public
    # buckets get an explicit anonymous override.
    for bucket in ANON_BUCKETS:
        builder = builder.config(
            f"spark.hadoop.fs.s3a.bucket.{bucket}.aws.credentials.provider", _ANON
        )

    # Master resolution: explicit arg > SPARK_MASTER env > whatever
    # spark-submit already set > local[*]. Never override a master that
    # spark-submit (k8s/EKS) provided.
    #
    # Detection uses PYSPARK_GATEWAY_PORT, which PythonRunner exports for every
    # script launched via spark-submit in any deploy mode. Do NOT test
    # SparkConf().contains("spark.master") instead: before the first
    # SparkContext exists, pyspark's SparkConf is a plain dict that never sees
    # the JVM system properties spark-submit set, so the check is always False
    # and would clobber a k8s master with local[4].
    submitted = "PYSPARK_GATEWAY_PORT" in os.environ
    if submitted and os.environ.get("DRIVER_MEM"):
        # DRIVER_MEM only reaches the JVM when pyspark launches it at
        # getOrCreate (a bare `python src/0X_*.py`). Under spark-submit the JVM
        # is already running, so this builder value is silently ignored and the
        # driver sits at Spark's 1g default. Say so loudly rather than let a
        # benchmark row claim a heap size it never had.
        print("WARNING: DRIVER_MEM is inert under spark-submit -- pass "
              "--driver-memory instead", file=sys.stderr)
    master = master or os.environ.get("SPARK_MASTER")
    if master:
        builder = builder.master(master)
    elif not submitted:
        builder = builder.master(os.environ.get("LOCAL_MASTER", "local[4]"))
    return SedonaContext.create(builder.getOrCreate())


def assert_versions(sedona) -> None:
    """Fail loudly if the resolved stack is not the pinned one."""
    import pyspark
    assert pyspark.__version__.startswith("3.5."), \
        f"pyspark {pyspark.__version__} not on the 3.5.x line"
    if JARS_BAKED:
        from pathlib import Path
        jars = Path(os.environ.get("SPARK_HOME", "/opt/spark")) / "jars"
        want = (
            "sedona-spark-shaded-3.5_2.12-1.9.1.jar",
            "geotools-wrapper-1.9.1-33.5.jar",
            "hadoop-aws-3.3.4.jar",
        )
        missing = [f for f in want if not (jars / f).exists()]
        assert not missing, f"baked jars missing from {jars}: {missing}"
        return
    pkgs = sedona.sparkContext.getConf().get("spark.jars.packages", "")
    assert "sedona-spark-shaded-3.5_2.12:1.9.1" in pkgs, pkgs
    assert "geotools-wrapper:1.9.1-33.5" in pkgs, pkgs


def s3_uri(layer: str, *parts: str) -> str:
    """s3://$RF_BUCKET/<layer>/<parts...>. The bucket name is never committed."""
    bucket = os.environ.get("RF_BUCKET")
    if not bucket:
        raise RuntimeError(
            "RF_BUCKET is unset. Export your own bucket name, or run with "
            "SCOPE=demo LOCAL_OUT=1 to write to ./data instead."
        )
    prefix = CFG["paths"][layer]
    return "/".join([f"s3a://{bucket}", prefix, *parts])


def out_path(layer: str, *parts: str) -> str:
    """Local ./data path when LOCAL_OUT=1, else s3. Keeps `make demo`
    runnable with no AWS account at all, which is the stranger-clone gate."""
    if os.environ.get("LOCAL_OUT") == "1":
        from config import DATA_DIR
        p = DATA_DIR / CFG["paths"][layer]
        for part in parts:
            p = p / part
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)
    return s3_uri(layer, *parts)
