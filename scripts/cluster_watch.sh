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
IDLE_MAX_MIN=${IDLE_MAX_MIN:-10}          # whole cluster idle longer than this is waste
NODES_IDLE_MIN=${NODES_IDLE_MIN:-3}       # spot nodes up with no job; grace for stage boundaries
AGE_WARN_HOURS=${AGE_WARN_HOURS:-3}       # cluster older than this, against a 4h timebox
BOOTSTRAP_GRACE_MIN=${BOOTSTRAP_GRACE_MIN:-20}  # kube-system may be Pending this long after create
CLUSTER=${CLUSTER:-rf-cov}
AWS_REGION=${AWS_REGION:-us-west-2}

# Cluster age is needed by the scheduling check below (bootstrap grace) as well
# as by the age check at the end, so it is resolved once, up front.
# NB AWS CLI v1 returns this as a Unix epoch FLOAT; v2 returns ISO 8601.
created=$(aws eks describe-cluster --name "$CLUSTER" --region "$AWS_REGION" \
          --query 'cluster.createdAt' --output text 2>/dev/null)
CLUSTER_EPOCH=0
if [ -n "$created" ] && [ "$created" != "None" ]; then
  if [ "${created%%.*}" -eq "${created%%.*}" ] 2>/dev/null; then
    CLUSTER_EPOCH=${created%%.*}
  else
    CLUSTER_EPOCH=$(date -u -j -f "%Y-%m-%dT%H:%M:%S" "${created%%.*}" +%s 2>/dev/null || date -u -d "${created%%.*}" +%s 2>/dev/null || echo 0)
  fi
fi
CLUSTER_AGE_MIN=999
[ "$CLUSTER_EPOCH" -gt 0 ] && CLUSTER_AGE_MIN=$(( ($(date -u +%s) - CLUSTER_EPOCH) / 60 ))

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
    # BOOTSTRAP GRACE. eksctl creates the control plane ~12 min before any node
    # joins, so during cluster-up every kube-system pod (coredns first) is
    # legitimately Pending with "no nodes available to schedule pods". Flagging
    # that fires the alarm on every single cluster creation, which trains you to
    # ignore it -- the same cry-wolf failure as the scale-down case below.
    #
    # Scoped deliberately narrowly: only kube-system, and only while the cluster
    # is young. A Spark pod Pending during bootstrap is still a real finding, and
    # so is a kube-system pod Pending after the grace period -- that is exactly
    # the `helm --wait` with zero nodes bug this script was written for.
    if [ "${name%%/*}" = "kube-system" ] && [ "${CLUSTER_AGE_MIN:-999}" -lt "$BOOTSTRAP_GRACE_MIN" ]; then
      say "$name Pending ${mins}m (cluster bootstrapping, ${CLUSTER_AGE_MIN}m old -- expected)"
    elif [ "$mins" -ge "$PENDING_MAX_MIN" ]; then
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
# Nodes already cordoned (SchedulingDisabled) are draining from a scale-down
# that has been issued. They still bill for another minute or two, but the
# action has been taken -- flagging them would make this alert fire on every
# normal teardown, and an alert that cries wolf during routine operation is one
# that gets ignored exactly when it matters. Counted and reported, not flagged.
all_spark=$(kubectl get nodes -l workload=spark --no-headers 2>/dev/null | wc -l | tr -d ' ')
draining=$(kubectl get nodes -l workload=spark --no-headers 2>/dev/null | grep -c 'SchedulingDisabled' || true)
spark_nodes=$(( ${all_spark:-0} - ${draining:-0} ))
active=$(kubectl get sparkapplication -A --no-headers 2>/dev/null \
         | grep -cE 'RUNNING|SUBMITTED|PENDING_RERUN' || true)
[ "${draining:-0}" -gt 0 ] && say "$draining spot node(s) draining (scale-down in flight, still billing briefly)"

# How long since the newest job finished. Both the node check below and the
# idle check further down key on this, so it is resolved once.
last_finish=$(kubectl get sparkapplication -A \
  -o go-template='{{range .items}}{{if .status.terminationTime}}{{.status.terminationTime}}{{"\n"}}{{end}}{{end}}' 2>/dev/null | sort | tail -1)
SINCE_JOB_MIN=-1
if [ -n "$last_finish" ]; then
  lf=$(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_finish" +%s 2>/dev/null || date -u -d "$last_finish" +%s 2>/dev/null || echo 0)
  if [ "$lf" -gt 0 ]; then SINCE_JOB_MIN=$(( ($(date -u +%s) - lf) / 60 ))
  else flag "could not parse terminationTime ([$last_finish]) -- idle checks DID NOT RUN"; fi
fi

# GRACE BETWEEN CHAINED STAGES. This used to fire the instant a job completed,
# which meant it alarmed on every stage boundary of a nine-stage pipeline -- the
# gap while the next stage submits, or while a scale-up waits for nodes to go
# Ready, is normal operation and not waste. Third cry-wolf case found in this
# script; the pattern is always the same, a state that is transient by design
# being reported as a standing fault.
if [ "${spark_nodes:-0}" -gt 0 ] && [ "${active:-0}" -eq 0 ] \
   && [ "$SINCE_JOB_MIN" -ge "$NODES_IDLE_MIN" ]; then
  flag "$spark_nodes schedulable spot node(s), no active job for ${SINCE_JOB_MIN}m -- run 'make nodes-down'"
else
  say "schedulable spark nodes=$spark_nodes active jobs=$active (last job ${SINCE_JOB_MIN}m ago)"
fi

# AN IDLE CLUSTER WITH ZERO SPOT NODES IS STILL BILLING, and the check above
# cannot see it. Found the hard way: after the first spike the spot group went
# to 0 and this script reported "clean" for 2.5 hours while the control plane
# and the serve node quietly burned $0.167/hr. Only the age check noticed, at
# 3h, and only by coincidence.
#
# Scaling nodes down is not the same as stopping the meter. The signal is the
# newest SparkApplication having finished a while ago with nothing to replace
# it -- which distinguishes a genuinely abandoned cluster from the minutes
# between two stages of a pipeline.
if [ "${active:-0}" -eq 0 ] && [ "$SINCE_JOB_MIN" -ge "$IDLE_MAX_MIN" ]; then
  flag "cluster idle ${SINCE_JOB_MIN}m since the last job finished -- billing for nothing. 'make cluster-down'"
fi

# --- cluster age -------------------------------------------------------------
echo "== age"
if [ -n "$created" ] && [ "$created" != "None" ]; then
  if [ "$CLUSTER_EPOCH" -gt 0 ]; then
    hrs=$(( CLUSTER_AGE_MIN / 60 ))
    if [ "$hrs" -ge "$AGE_WARN_HOURS" ]; then
      flag "cluster is ${hrs}h old (timebox 4h, reaper deletes at TTL)"
    else
      say "cluster ${hrs}h old (${CLUSTER_AGE_MIN}m)"
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
