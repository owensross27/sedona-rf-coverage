SHELL := /bin/bash
VENV  := .venv/bin
SCOPE ?= demo

# Never hardcoded: this repo is public. Export RF_BUCKET and AWS_ACCOUNT_ID, or
# copy infra/terraform/terraform.tfvars.example and let terraform own them.
AWS_REGION     ?= us-west-2
CLUSTER        ?= rf-cov
ECR_REPO       ?= sedona-rf-coverage
IMAGE_TAG      ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
ECR_IMAGE       = $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com/$(ECR_REPO):$(IMAGE_TAG)

# Every stage and every publishing script resolves its paths through this (see
# config.py). Exported rather than passed per-target so `make fetch` writes
# exactly where `make web` then reads -- the two halves of an end-to-end run
# disagreeing about a directory is a silent way to publish stale data.
RFC_DATA_DIR ?= data
export RFC_DATA_DIR

.PHONY: setup test bench smoke demo pipeline cloud-pipeline all dq ookla surface map \
        tiles footprints web fetch web-serve image push preflight \
        spot-check cluster-up cluster-down nodes-up nodes-down spike job events-prefix \
        history history-stop \
        status check-nat watch watch-loop destroy-all clean
# `cost` was listed here with no recipe anywhere in the file. A .PHONY name
# with no rule is worse than a missing target: `make cost` printed "Nothing to
# be done" and exited 0, so it read as success. Removed rather than
# implemented, because the honest version needs cost-allocation tags -- this
# region has more than one tenant, and tags take ~24h to activate and are not
# retroactive. `make status` reports the account total and now says so.

## --- local, no AWS account required ----------------------------------------

setup:
	uv venv --python 3.11 .venv
	uv pip install -p .venv "pyspark==3.5.3"
	uv pip install -p .venv -r requirements.lock
	@$(VENV)/python -c "import pyspark; assert pyspark.__version__=='3.5.3'; print('stack ok')"

# The correctness gate. Runs without pytest so a cold clone needs no framework,
# and without network so it works before any credential exists.
test:
	$(VENV)/python tests/test_propagation.py
	$(VENV)/python tests/test_bronze.py
	$(VENV)/python tests/test_coverage.py
	$(VENV)/python tests/test_siting.py
	# The footprint blob is addressed by byte offset, so a one-byte slip in the
	# record layout paints one transmitter's coverage under another's name --
	# a wrong map that looks entirely plausible. Pure numpy, no data needed.
	$(VENV)/python -c "import sys; sys.path.insert(0,'scripts'); \
	  import make_footprints as f; f.self_check()"
	# The reaper decides whether to delete a Kubernetes cluster, so its two
	# gates are worth a gate of their own. Pure stdlib, no boto3, no network:
	# boto3 is imported inside the handler precisely so this stays runnable.
	$(VENV)/python infra/terraform/lambda/reaper.py

# The performance gate: >= 100k pairs/min/core before any statewide run.
bench:
	$(VENV)/python scripts/bench_kernel.py 200000

# The stranger-clone target: one county, local Spark, writes to ./data, no
# cloud credentials anywhere. If this stops working the repo is not reproducible.
demo:
	$(MAKE) test
	SCOPE=demo LOCAL_OUT=1 $(MAKE) pipeline

# 03_census needs CENSUS_API_KEY and it deliberately lives in .env rather than a
# shell dotfile, so a fresh shell has never heard of it. Sourced here (not in
# each stage) because it is the one secret the local pipeline takes.
pipeline:
	source scripts/java_env.sh; set -e; \
	if [ -f .env ]; then set -a; . ./.env; set +a; fi; \
	for s in 01_towers 02_terrain 03_census 04_grid 05_links 06_coverage 07_dq 08_features 09_siting; do \
	  echo "== $$s"; SCOPE=$(SCOPE) $(VENV)/python src/$$s.py; \
	done

# The same nine stages, submitted to the cluster instead of run locally. One
# SparkApplication per stage, in order, stopping at the first failure -- `job`
# polls to a terminal state and exits non-zero on FAILED, which is what makes
# a loop like this safe to leave running.
#
# 01-03 are driver-heavy (a download, a warp, an API pull) and get one
# executor; the rest get the full three. Sizing them all alike either idles
# two spot nodes through three stages or starves the join in 05.
#
# NOT chained onto cluster-up/cluster-down: teardown belongs to `spike`, which
# holds a trap, and burying a cluster delete inside a pipeline target means a
# Ctrl-C at the wrong moment leaves one billing.
cloud-pipeline:
	@for s in 01 02 03 04 05 06 07 08 09; do \
	   case $$s in 01|02|03) e=1;; *) e=3;; esac; \
	   $(MAKE) job STAGE=$$s SCOPE=$(SCOPE) EXECUTORS=$$e || exit 1; \
	 done
	@echo "== all nine stages COMPLETED. Next: make fetch && make web"

# End to end, locally, from nothing to a map you can open: nine Spark stages
# into ./data, then every file web/data holds. `make demo` is this at one-county
# scope. Statewide, `pipeline` is the part that wants the cluster --
# cloud-pipeline + fetch + web is the same three steps with the compute moved.
all: pipeline web

dq:
	source scripts/java_env.sh && SCOPE=$(SCOPE) $(VENV)/python src/07_dq.py

# Ookla open data -> bronze/ookla_h3, plus the one validation number it
# supports. Deliberately NOT in `pipeline`, for the same reason as `surface`
# and `map` and one more: nothing in 01-09 reads it, it is a validation input,
# and it is the only CC BY-NC-SA source in the project. A commercial reuser
# drops this target instead of editing the pipeline. Always statewide -- the
# scope filter happens at the join, not at the fetch.
ookla:
	source scripts/java_env.sh; \
	SCOPE=$(SCOPE) LOCAL_OUT=$${LOCAL_OUT:-1} $(VENV)/python src/10_ookla.py

# Proves the JDK, pyspark, Sedona jars and ST_* functions all resolve together.
# Cheapest possible check that the stack is real before any stage runs.
smoke:
	source scripts/java_env.sh && $(VENV)/python scripts/smoke_sedona.py

# Per-pixel propagation surfaces (current towers vs +20 recommended sites)
# and their web overlays. Not in `pipeline`: a visualization product with a
# ~100M-link fan-out that nothing downstream consumes.
surface:
	source scripts/java_env.sh; SCOPE=$(SCOPE) LOCAL_OUT=$${LOCAL_OUT:-1} DRIVER_MEM=8g $(VENV)/python scripts/make_surface.py
	SCOPE=$(SCOPE) LOCAL_OUT=$${LOCAL_OUT:-1} $(VENV)/python scripts/make_overlays.py

# Static PNG of the coverage surface and the recommended build. Deliberately
# not part of `pipeline`: it is a figure, and it must keep working with the
# cluster deleted, so it reads gold/ with pandas and never starts Spark.
map:
	SCOPE=$(SCOPE) LOCAL_OUT=$${LOCAL_OUT:-1} $(VENV)/python scripts/make_map.py
	SCOPE=$(SCOPE) LOCAL_OUT=$${LOCAL_OUT:-1} $(VENV)/python scripts/make_figures.py

tiles:
	bash scripts/make_tiles.sh

# Per-transmitter propagation footprints: click one structure, see its own
# coverage. A repackaging of silver/links (plus the recommended sites, which
# have no link rows and are run through the same kernel here), into one blob
# the page reads with HTTP Range requests.
footprints:
	SCOPE=$(SCOPE) LOCAL_OUT=$${LOCAL_OUT:-1} $(VENV)/python scripts/make_footprints.py

# EVERYTHING web/data holds, from whatever gold/ the current scope points at.
# This is the second half of an end-to-end run: `pipeline` (or `cloud-pipeline`)
# computes, `web` publishes. Kept as one target because the map reads five
# files that must all describe the same run -- tiles built from one scope with
# a meta.json from another is precisely the drift that put "Demo scope:
# Kanawha County" on a statewide map.
web: tiles footprints

# S3 -> the local data dir, so the publishing half can run with the cluster
# already deleted. The stages write to s3a:// from the cluster; make_web_data
# and make_footprints are plain pandas by design (they must outlive the
# cluster), and pandas cannot read s3a://. This is the bridge, and before it
# existed the bridge was an aws s3 sync somebody had to remember.
#
# Silenced: the recipe line would print the bucket name. See `job`.
fetch:
	@test -n "$(RF_BUCKET)" || { echo "RF_BUCKET is unset"; exit 1; }
	@# Exactly what make_web_data.py and make_footprints.py read. Keep this
	@# list honest with:
	@#   grep -ho 'out_path("[a-z]*", *"[a-z_0-9.]*"' scripts/make_web_data.py \
	@#     scripts/make_footprints.py | sort -u
	@for p in bronze/towers silver/links silver/hex_features \
	          gold/coverage gold/siting; do \
	   echo "== fetching $$p"; \
	   aws s3 sync "s3://$(RF_BUCKET)/$$p/" "$(RFC_DATA_DIR)/$$p/" --quiet; \
	 done
	@echo "== fetching cog"
	@aws s3 sync "s3://$(RF_BUCKET)/cog/" "$(RFC_DATA_DIR)/cog/" --quiet
	@du -sh $(RFC_DATA_DIR)

# PMTiles needs HTTP range requests, which python -m http.server does not do.
web-serve:
	cd web && ../$(VENV)/python -m RangeHTTPServer 8000

## --- image ------------------------------------------------------------------

# Native arm64 build. There is deliberately no buildx/--platform amd64 target:
# the cluster is all-Graviton, so cross-compiling under QEMU would add minutes
# to every iteration for nothing.
image:
	docker build -t $(ECR_REPO):$(IMAGE_TAG) -f docker/Dockerfile .

# Silenced: every line here interpolates the account id. See `job` for why that
# matters even when nothing is being recorded -- `docker push` prints the
# registry host itself, which is unavoidable, but make need not print it twice.
push: preflight
	@aws ecr get-login-password --region $(AWS_REGION) \
	  | docker login --username AWS --password-stdin \
	    $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com
	@docker tag $(ECR_REPO):$(IMAGE_TAG) $(ECR_IMAGE)
	@docker push $(ECR_IMAGE)

# The tax for going single-arch: assert every third-party image the cluster
# pulls actually publishes arm64, before a pod fails with `exec format error`.
preflight:
	@for img in apache/spark:3.5.9-scala2.12-java17-python3-ubuntu \
	            ghcr.io/kubeflow/spark-operator/controller:2.5.2 \
	            ghcr.io/developmentseed/titiler:latest \
	            nginx:alpine; do \
	  if docker manifest inspect $$img 2>/dev/null | grep -q arm64; then \
	    echo "  arm64 ok   $$img"; \
	  else echo "  MISSING    $$img"; exit 1; fi; \
	done

## --- cluster ----------------------------------------------------------------

# Three seconds of measurement beats a stale comment: re-check which AZ and
# instance type is actually cheapest before every create.
spot-check:
	@aws ec2 describe-spot-price-history --region $(AWS_REGION) \
	  --instance-types r7g.2xlarge m7g.2xlarge c7g.4xlarge m7g.4xlarge \
	  --product-descriptions Linux/UNIX --max-items 40 \
	  --query 'SpotPriceHistory[].[AvailabilityZone,InstanceType,SpotPrice]' \
	  --output table

cluster-up:
	@# Silenced: the echoed recipe line would print the bucket name. See `job`.
	@RF_BUCKET=$(RF_BUCKET) envsubst < infra/eks/cluster.yaml | eksctl create cluster -f -
	@# Both nodegroups are created at desiredCapacity 0, so the cluster comes up
	@# with ZERO nodes -- coredns sits Pending, and so would anything else. The
	@# helm install below uses --wait, which would then block on a pod that has
	@# nowhere to run until it times out. Bring the single on-demand serve node
	@# up first. It is not extra cost: the operator and the Spark driver both
	@# live on this node whenever the cluster is doing anything at all.
	eksctl scale nodegroup --cluster $(CLUSTER) --name serve --nodes 1 --region $(AWS_REGION)
	helm repo add spark-operator https://kubeflow.github.io/spark-operator 2>/dev/null || true
	helm install spark-operator spark-operator/spark-operator \
	  --namespace spark-operator --create-namespace --version 2.5.2 --wait
	@# Stage 03's Census key, from the gitignored .env into a Secret the
	@# SparkApplication reads by secretKeyRef -- so it reaches the pods without
	@# ever being written into a tracked file. Idempotent: re-running cluster-up
	@# or rotating the key applies over the existing Secret rather than erroring.
	@if [ -f .env ]; then set -a; . ./.env; set +a; \
	   kubectl create secret generic rf-secrets \
	     --from-literal=CENSUS_API_KEY="$$CENSUS_API_KEY" \
	     --dry-run=client -o yaml | kubectl apply -f - >/dev/null; \
	   echo "== rf-secrets: CENSUS_API_KEY loaded"; \
	 else echo "== WARNING no .env -- stage 03 will fail at its key guard"; fi
	@$(MAKE) check-nat

nodes-up:
	eksctl scale nodegroup --cluster $(CLUSTER) --name spark-spot --nodes 3 --region $(AWS_REGION)
	eksctl scale nodegroup --cluster $(CLUSTER) --name serve      --nodes 1 --region $(AWS_REGION)

nodes-down:
	eksctl scale nodegroup --cluster $(CLUSTER) --name spark-spot --nodes 0 --region $(AWS_REGION)
	eksctl scale nodegroup --cluster $(CLUSTER) --name serve      --nodes 0 --region $(AWS_REGION)

cluster-down:
	eksctl delete cluster --name $(CLUSTER) --region $(AWS_REGION) --wait

# Teardown made atomic with the session, so forgetting is not one of the
# options. The trap fires on normal exit, on failure, on Ctrl-C and on the
# terminal closing -- including a cluster-up that died half way, which is
# exactly when a partial cluster gets abandoned still billing.
#
# Do the work in a SECOND terminal; this one only holds the trap open. It is
# still laptop-bound (a lid closed at the wrong moment defeats it), which is
# why infra/terraform/reaper.tf exists as the server-side backstop. This target
# makes the good path automatic; the reaper covers the bad one.
spike:
	@trap 'echo "== tearing down"; $(MAKE) cluster-down' EXIT INT TERM HUP; \
	 $(MAKE) cluster-up nodes-up; \
	 echo "== cluster ready. Run the job in another shell."; \
	 echo "== press Enter here (or Ctrl-C) to tear down."; \
	 read _

STAGE ?= 05
# Driver-heavy stages (01/02/03) do not need three spot nodes idling behind them.
EXECUTORS ?= 3
# Spark refuses to start if spark.eventLog.dir does not exist, and on s3a a
# prefix only exists once something is under it. A zero-byte marker is what s3a
# reads as a directory. Idempotent, ~50ms, and a prerequisite of `job` rather
# than a step to remember: forgetting it costs a whole cluster run.
events-prefix:
	@aws s3api put-object --bucket $(RF_BUCKET) --key spark-events/ \
	  --region $(AWS_REGION) >/dev/null && echo "== s3 event-log prefix ready"

job: events-prefix
	@# A SparkApplication is not a Job: re-applying an existing one is a NO-OP,
	@# not a re-run. Without this delete a resubmit prints `unchanged`, nothing
	@# starts, and the old status sits there looking like the new run's.
	@kubectl delete sparkapplication rf-coverage-$(STAGE) --ignore-not-found
	@# SILENCED ON PURPOSE. make echoes a recipe line before running it, and this
	@# one interpolates $$(RF_BUCKET) and $$(ECR_IMAGE) -- the bucket name and the
	@# AWS account id, the two strings this public repo must never carry. Without
	@# the @ they are printed to the terminal, and into any recording of it. The
	@# same reason cluster-up and history are silenced.
	@echo "== submitting rf-coverage-$(STAGE)  SCOPE=$(SCOPE)  EXECUTORS=$(EXECUTORS)"
	@STAGE_NAME=$(STAGE) SCOPE=$(SCOPE) RF_BUCKET=$(RF_BUCKET) ECR_IMAGE=$(ECR_IMAGE) \
	AWS_REGION=$(AWS_REGION) EXECUTORS=$(EXECUTORS) \
	STAGE_FILE=$$(basename $$(ls src/$(STAGE)_*.py)) \
	  envsubst < k8s/sparkapplication.yaml | kubectl apply -f -
	@# `kubectl get sparkapplication -w` never returns, so the target could not
	@# be chained, scripted or recorded -- it had to be Ctrl-C'd, and a Ctrl-C is
	@# indistinguishable from a failure to anything downstream. Poll to a
	@# terminal state instead, and exit non-zero when the job failed.
	@start=$$(date +%s); \
	 while :; do \
	   s=$$(kubectl get sparkapplication rf-coverage-$(STAGE) \
	        -o jsonpath='{.status.applicationState.state}' 2>/dev/null); \
	   printf "\r  rf-coverage-%s  %-20s %4ds" "$(STAGE)" "$${s:-SUBMITTING}" $$(( $$(date +%s) - start )); \
	   case "$$s" in \
	     COMPLETED) echo; echo "== COMPLETED in $$(( $$(date +%s) - start ))s"; exit 0;; \
	     FAILED|SUBMISSION_FAILED) echo; echo "== $$s -- kubectl logs rf-coverage-$(STAGE)-driver"; exit 1;; \
	   esac; \
	   sleep 5; \
	 done

# The Spark UI is a driver-pod service: it dies with the pod, so a finished job
# leaves nothing to look at and "screenshot the UI" turns into a task that
# competes with teardown for billable minutes. `spark.eventLog.dir` in
# k8s/sparkapplication.yaml persists the log to S3 instead, and this renders it
# back into the real UI -- jobs, stages, DAG, SQL, executors -- for free, and as
# many times as wanted, long after the cluster is gone.
#
# Logs are SYNCED DOWN rather than read over s3a, for two reasons:
#   1. The history server's header prints the log directory verbatim, and a
#      screenshot of `s3a://<the actual bucket>` is precisely the string a public
#      repo must never carry. file:// keeps the bucket name out of the image.
#   2. hadoop-aws 3.3.4's default provider chain is Temporary -> Simple ->
#      Environment -> IAMInstance, with NO ProfileCredentialsProvider. So s3a
#      cannot see ~/.aws/credentials: a laptop with a working `aws` CLI still
#      fails with "Unable to load AWS credentials from environment variables".
#      The AWS CLI has no such gap, and reading a local directory needs no
#      credentials at all.
# data/ is gitignored, so the logs themselves never enter the repo either.
HISTORY_DIR ?= data/spark-events
history:
	@mkdir -p $(HISTORY_DIR) $(CURDIR)/data/spark-history-logs
	@aws s3 sync s3://$(RF_BUCKET)/spark-events $(HISTORY_DIR) --region $(AWS_REGION) --only-show-errors
	@source scripts/java_env.sh && \
	 SPARK_HOME=$(CURDIR)/.venv/lib/python3.11/site-packages/pyspark \
	 SPARK_LOG_DIR=$(CURDIR)/data/spark-history-logs \
	 SPARK_HISTORY_OPTS="-Dspark.history.fs.logDirectory=file://$(CURDIR)/$(HISTORY_DIR)" \
	   $(CURDIR)/.venv/lib/python3.11/site-packages/pyspark/sbin/start-history-server.sh
	@echo "== Spark UI on http://localhost:18080 -- 'make history-stop' when done"

history-stop:
	@SPARK_HOME=$(CURDIR)/.venv/lib/python3.11/site-packages/pyspark \
	 SPARK_LOG_DIR=$(CURDIR)/data/spark-history-logs \
	   $(CURDIR)/.venv/lib/python3.11/site-packages/pyspark/sbin/stop-history-server.sh

# REMOVED: `serve-up` (kubectl apply -k k8s/serving/) and `dns`
# (scripts/node_dns.sh). Both pointed at files that were never written, so both
# failed on invocation -- a broken target in a public repo reads as rot, and
# they were broken because the architecture moved rather than because the work
# was pending.
#
# The map is one static PMTiles file on GitHub Pages. Nothing needs to reach
# the cluster from the internet, so there is no NodePort to expose and no A
# record to move when a node's public IP changes on recreate. Serving the
# tiles from the cluster would also undo the property the whole design rests
# on: the public demo has to survive teardown. See README "Cost".

## --- guardrails -------------------------------------------------------------

# First command of every session. A forgotten cluster is ~$12/day, which is
# the single largest cost risk in the project.
status:
	@echo "== nodes";    kubectl get nodes 2>/dev/null || echo "  (no cluster)"
	@echo "== spark";    kubectl get sparkapplication 2>/dev/null || true
	@$(MAKE) check-nat
	@# ACCOUNT-WIDE, and this account has other tenants -- it is not this
	@# project's spend and must never be quoted as such. Labelled rather than
	@# removed: the account total is what tells you whether a budget alert
	@# fired because of you or because of something else in the region.
	@echo "== month-to-date spend (ACCOUNT-WIDE, all projects)"; \
	  aws ce get-cost-and-usage --time-period Start=$$(date -u +%Y-%m-01),End=$$(date -u +%Y-%m-%d) \
	    --granularity MONTHLY --metrics UnblendedCost \
	    --query 'ResultsByTime[0].Total.UnblendedCost.Amount' --output text 2>/dev/null || true

# Deterministic waste detector: exits non-zero when the cluster is BILLING
# WITHOUT PROGRESSING. Every check in it is one that already cost money --
# Pending executors while the job reports RUNNING, spot nodes up with no job,
# image-pull loops retrying forever, a cluster past its timebox.
#
# Unlike `make status` this costs nothing (no Cost Explorer call), so it is the
# one safe to put in a loop. Run it in a second terminal for the whole session:
#   make watch-loop
watch:
	@bash scripts/cluster_watch.sh

# 60 s cadence: a node joins in ~40 s and the image pull is ~2 min, so this
# notices a genuine stall within a couple of cycles while never firing on
# normal startup. Deliberately does not exit on a finding -- it prints and
# keeps watching, because the point is to be looked at, not to be acknowledged.
watch-loop:
	@while true; do date -u +'--- %H:%M:%SZ'; bash scripts/cluster_watch.sh || true; sleep 60; done

# eksctl's default would have created a NAT gateway. Assert it did not.
check-nat:
	@n=$$(aws ec2 describe-nat-gateways --region $(AWS_REGION) \
	      --filter Name=state,Values=available --query 'length(NatGateways)' --output text 2>/dev/null || echo 0); \
	 if [ "$$n" != "0" ]; then echo "COST ALERT: $$n NAT gateway(s) in $(AWS_REGION) -- \$$0.045/hr each"; exit 1; \
	 else echo "== nat: none (good)"; fi

# eksctl delete cluster does NOT touch ECR, S3, or leftover EBS volumes.
destroy-all: cluster-down
	cd infra/terraform && terraform destroy
	@echo "== orphaned EBS volumes in $(AWS_REGION):"
	@aws ec2 describe-volumes --region $(AWS_REGION) \
	  --filters Name=status,Values=available --query 'Volumes[].VolumeId' --output text

clean:
	rm -rf data/tmp __pycache__ src/__pycache__ tests/__pycache__
