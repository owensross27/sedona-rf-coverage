# Base: apache/spark-py and bitnami/spark are both dead. This is the
# maintained multi-arch image and it publishes arm64, which is what lets this
# repo build natively on an Apple Silicon laptop AND run on Graviton EKS nodes
# with no QEMU emulation anywhere in the loop.
FROM apache/spark:3.5.9-scala2.12-java17-python3-ubuntu

# Probed on this exact tag:
#   python3 --version -> 3.10.12   (3.11 installed below)
#   $SPARK_HOME       -> /opt/spark
#   id                -> uid=185(spark) gid=185(spark)
#   OS                -> Ubuntu 22.04.5 LTS (jammy)

USER root

# jammy's own python3.11 is 3.11.0~rc1, a 2022 pre-release, so the real point
# release comes from deadsnakes. python3-pip already exists for the base's 3.10
# under the shared /usr/lib/python3/dist-packages path, so `python3.11 -m pip`
# works immediately. software-properties-common/gnupg only register the PPA and
# are purged in the same layer.
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends software-properties-common gnupg \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends python3.11 \
    && apt-get purge -y -qq software-properties-common gnupg \
    && apt-get autoremove -y -qq \
    && rm -rf /var/lib/apt/lists/*

# Jars are BAKED into $SPARK_HOME/jars, never resolved with --packages at job
# start: executor pods have no guaranteed route to Maven Central mid-job and
# ivy resolution there is slow and flaky.
#
# sedona-spark-shaded MUST be 1.9.1, not 1.9.0. 1.9.0 carries the >180 m
# ST_Transform regression (GH-3161), which silently corrupts reprojection --
# and this pipeline reprojects every tower position and DEM tile, so 1.9.0
# would quietly poison the entire coverage surface rather than fail loudly.
#
# geotools-wrapper is REQUIRED for raster: Sedona's RasterUDT is a GeoTools
# GridCoverage2D, and without this jar the failure is a NoClassDefFoundError at
# the first RS_* call rather than at startup.
#
# hadoop-aws MUST match the Hadoop line pyspark 3.5.x is built against -- 3.3.4,
# confirmed by the base image already shipping hadoop-client-api/runtime 3.3.4.
# aws-java-sdk-bundle pairs with it; a mismatched SDK jar is the classic
# hadoop-aws footgun.
#
# No Iceberg jar here, unlike the sibling s2-field-ndvi image: this pipeline
# writes plain GeoParquet and COGs with no schema-evolution or incremental
# -refresh requirement, so the table format would be unused weight. See the
# README's "evaluated and rejected" section.
RUN cd "$SPARK_HOME/jars" \
    && curl -fsSL -O https://repo1.maven.org/maven2/org/apache/sedona/sedona-spark-shaded-3.5_2.12/1.9.1/sedona-spark-shaded-3.5_2.12-1.9.1.jar \
    && curl -fsSL -O https://repo1.maven.org/maven2/org/datasyslab/geotools-wrapper/1.9.1-33.5/geotools-wrapper-1.9.1-33.5.jar \
    && curl -fsSL -O https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/3.3.4/hadoop-aws-3.3.4.jar \
    && curl -fsSL -O https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/1.12.262/aws-java-sdk-bundle-1.12.262.jar

# NOTE: there is deliberately no spark-defaults.conf baked here. Spark on
# Kubernetes generates a ConfigMap from the submit-time config and mounts it
# over SPARK_CONF_DIR in the driver and executor pods, shadowing anything this
# image writes to $SPARK_HOME/conf. The s3a per-bucket credential providers
# that make anonymous open-data reads and authenticated own-bucket writes
# coexist are set in src/session.py at the SparkConf-builder level, which is
# what actually takes effect on a cluster.

# Do NOT pip-install pyspark. This image already IS Spark 3.5.9;
# spark-submit's PythonRunner prepends $SPARK_HOME/python/lib/pyspark.zip to
# PYTHONPATH ahead of site-packages, so a pip-installed pyspark would never be
# imported -- just a dead ~450 MB and a version mismatch against
# assert_versions(). requirements.txt pins it for the local venv only.
COPY requirements.txt /opt/rfc/requirements.txt
RUN grep -v '^pyspark' /opt/rfc/requirements.txt > /opt/rfc/requirements-image.txt \
    && python3.11 -m pip install --no-cache-dir -r /opt/rfc/requirements-image.txt

# Code last so a source edit does not invalidate the jar and dependency layers
# above -- a code-only change then pushes ~5 MB against a ~3 GB image.
COPY src/ /opt/rfc/src/
COPY scripts/ /opt/rfc/scripts/
COPY config.yml /opt/rfc/config.yml

ENV PYSPARK_PYTHON=/usr/bin/python3.11 \
    PYTHONPATH=/opt/rfc/src \
    RFC_JARS_BAKED=1

RUN mkdir -p /opt/rfc/data && chown -R 185:185 /opt/rfc

USER 185
WORKDIR /opt/rfc
