#!/usr/bin/env bash
# Deterministic waste detector for a running cluster. Exits non-zero the moment
# the cluster is BILLING WITHOUT PROGRESSING, which is the only failure mode
# that costs money quietly.
#
# Every check here is one that already cost real money during the first spike:
#
#   Pending pods      executors that cannot be scheduled sit Pending forever
#                     while the SparkApplication reports RUNNING and the driver
#                     looks healthy. Nothing appears broken. Four nodes idled
#                     ~8 minutes on the first attempt before anyone noticed.
#   Idle nodes        spot capacity up with no SparkApplication to run is pure
#                     burn -- the usual cause is a job that failed while the
#                     `nodes-down` step was never reached.
#   Backoff loops     ImagePullBackOff and CrashLoopBackOff retry indefinitely.
#                     A wrong image tag bills at the full node rate while
#                     achieving precisely nothing.
#   Age              a cluster past its timebox. reaper.tf is the hard backstop
#                     at TTL; this warns long before that, while a human can act.
#
# Usage:
#   make watch          one pass, exit 1 on any finding (use in a loop or CI)
#   watch -n 60 'make watch'    or just run it from a second terminal
#
# Thresholds are deliberately generous: a node takes ~40 s to join and the
# image is ~3.8 GB, so a first pull legitimately takes a couple of minutes.
set -uo pipefail

PENDING_MAX_MIN=${PENDING_MAX_MIN:-5}     # pod Pending longer than this is stuck
IDLE_MAX_MIN=${IDLE_MAX_MIN:-10}          # nodes up, no job, longer than this is waste
AGE_WARN_HOURS=${AGE_WARN_HOURS:-3}       # cluster older than this, against a 4h timebox
CLUSTER=${CLUSTER:-rf-cov}
AWS_REGION=${AWS_REGION:-us-west-2}

findings=0
say()  { printf '  %s\n' "$1"; }
flag() { printf '  ALERT  %s\n' "$1"; findings=$((findings+1)); }

# --- burn rate ---------------------------------------------------------------
# Priced from the live API rather than a hardcoded table: spot moves, and a
# stale constant here would understate exactly when it matters most.
nodes=$(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.labels.node\.kubernetes\.io/instance-type}{"\n"}{end}' 2>/dev/null | sort | uniq -c)
if [ -z "$nodes" ]; then
  say "no cluster reachable (nothing billing for compute)"
else
  echo "== nodes"
  printf '%s\n' "$nodes" | sed 's/^/  /'
fi

# --- pods stuck Pending ------------------------------------------------------
echo "== scheduling"
stuck=$(kubectl get pods -A --field-selector status.phase=Pending \
        -o go-template='{{range .items}}{{.metadata.namespace}}/{{.metadata.name}} {{.metadata.creationTimestamp}}{{"\n"}}{{end}}' 2>/dev/null)
if [ -n "$stuck" ]; then
  now=$(date -u +%s)
  while read -r name ts; do
    [ -z "$name" ] && continue
    # BSD date on macOS, GNU date in a container -- try both rather than assume.
    born=$(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$ts" +%s 2>/dev/null || date -u -d "$ts" +%s 2>/dev/null || echo "$now")
    mins=$(( (now - born) / 60 ))
    if [ "$mins" -ge "$PENDING_MAX_MIN" ]; then
      flag "$name Pending ${mins}m -- billing, not running. Why:"
      kubectl describe pod "${name#*/}" -n "${name%%/*}" 2>/dev/null \
        | grep -m1 -A2 'FailedScheduling' | sed 's/^/         /'
    else
      say "$name Pending ${mins}m (under ${PENDING_MAX_MIN}m threshold)"
    fi
  done <<< "$stuck"
else
  say "no Pending pods"
fi

# --- backoff loops -----------------------------------------------------------
echo "== container health"
bad=$(kubectl get pods -A -o go-template='{{range .items}}{{$n:=.metadata.name}}{{range .status.containerStatuses}}{{if .state.waiting}}{{$n}} {{.state.waiting.reason}}{{"\n"}}{{end}}{{end}}{{end}}' 2>/dev/null \
      | grep -E 'ImagePullBackOff|ErrImagePull|CrashLoopBackOff|InvalidImageName' || true)
if [ -n "$bad" ]; then
  while read -r line; do [ -n "$line" ] && flag "$line -- retrying forever at full node rate"; done <<< "$bad"
else
  say "no image-pull or crash loops"
fi

# --- nodes up with nothing to do ---------------------------------------------
echo "== utilisation"
spark_nodes=$(kubectl get nodes -l workload=spark --no-headers 2>/dev/null | wc -l | tr -d ' ')
active=$(kubectl get sparkapplication -A --no-headers 2>/dev/null \
         | grep -cE 'RUNNING|SUBMITTED|PENDING_RERUN' || true)
if [ "${spark_nodes:-0}" -gt 0 ] && [ "${active:-0}" -eq 0 ]; then
  flag "$spark_nodes spot node(s) up with no active SparkApplication -- run 'make nodes-down'"
else
  say "spark nodes=$spark_nodes active jobs=$active"
fi

# --- cluster age -------------------------------------------------------------
echo "== age"
created=$(aws eks describe-cluster --name "$CLUSTER" --region "$AWS_REGION" \
          --query 'cluster.createdAt' --output text 2>/dev/null)
if [ -n "$created" ] && [ "$created" != "None" ]; then
  # AWS CLI v1 returns createdAt as a Unix epoch FLOAT (1786415917.459); v2
  # returns ISO 8601. Handle both -- assuming ISO made this check fail silently
  # and print nothing at all, which is the worst possible behaviour for a
  # monitor: it looked like it had passed.
  if [ "${created%%.*}" -eq "${created%%.*}" ] 2>/dev/null; then
    cs=${created%%.*}
  else
    cs=$(date -u -j -f "%Y-%m-%dT%H:%M:%S" "${created%%.*}" +%s 2>/dev/null || date -u -d "${created%%.*}" +%s 2>/dev/null || echo 0)
  fi
  if [ "$cs" -gt 0 ]; then
    hrs=$(( ($(date -u +%s) - cs) / 3600 ))
    if [ "$hrs" -ge "$AGE_WARN_HOURS" ]; then
      flag "cluster is ${hrs}h old (timebox 4h, reaper deletes at TTL)"
    else
      say "cluster ${hrs}h old"
    fi
  else
    # Never fall through quietly: an unparsed timestamp must be visible, or the
    # check silently stops applying and the output still reads as a pass.
    flag "could not parse cluster createdAt ([$created]) -- age check DID NOT RUN"
  fi
else
  say "no cluster"
fi

echo
if [ "$findings" -gt 0 ]; then
  echo "WATCH: $findings finding(s) -- money is being spent without progress"
  exit 1
fi
echo "WATCH: clean"
