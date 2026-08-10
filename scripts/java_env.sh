# Source this before any LOCAL Spark invocation. Spark 3.5 supports Java
# 8/11/17 only. Prefers a system JDK 17, falls back to Homebrew's keg-only
# openjdk@17 (which is not symlinked onto PATH, so `java -version` fails even
# when it is installed -- that is the usual confusion here, not a missing JDK).
#
# WARNING: do NOT source this before `spark-submit --master k8s://`. It exports
# PYSPARK_PYTHON pointing at this laptop's .venv, and spark-submit propagates
# that into the driver pod, where the path does not exist. The container sets
# its own PYSPARK_PYTHON (see docker/Dockerfile).
export JAVA_HOME=$(/usr/libexec/java_home -v 17 2>/dev/null)
if [ -z "$JAVA_HOME" ] && command -v brew >/dev/null; then
  _BREW17="$(brew --prefix openjdk@17 2>/dev/null)/libexec/openjdk.jdk/Contents/Home"
  [ -x "$_BREW17/bin/java" ] && export JAVA_HOME="$_BREW17"
fi
# linux: trust the system JDK if it is 17 (e.g. amazon-corretto on EC2)
if [ -z "$JAVA_HOME" ] && command -v java >/dev/null; then
  if java -version 2>&1 | grep -q '"17'; then
    export JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")"
  fi
fi
if [ -z "$JAVA_HOME" ]; then
  echo "ERROR: JDK 17 not found. brew install openjdk@17" >&2
  return 1 2>/dev/null || exit 1
fi
export PATH="$JAVA_HOME/bin:$PATH"
# macOS: the driver binds to the (often unresolvable) machine hostname without
# this and hangs before the gateway ever reports its port.
export SPARK_LOCAL_IP=127.0.0.1
# Python workers must use the same interpreter as the driver. The system
# python here is 3.13; the venv is 3.11, and a mismatch surfaces as a bare
# EOFError from the worker with no stack trace.
_REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
export PYSPARK_PYTHON="$_REPO_DIR/.venv/bin/python"
export PYSPARK_DRIVER_PYTHON="$_REPO_DIR/.venv/bin/python"
