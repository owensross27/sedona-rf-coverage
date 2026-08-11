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

.PHONY: setup test bench smoke demo pipeline dq ookla surface map tiles web-serve image push preflight \
        spot-check cluster-up cluster-down nodes-up nodes-down spike job \
        status check-nat watch watch-loop cost destroy-all clean

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

# PMTiles needs HTTP range requests, which python -m http.server does not do.
web-serve:
	cd web && ../$(VENV)/python -m RangeHTTPServer 8000

## --- image ------------------------------------------------------------------

# Native arm64 build. There is deliberately no buildx/--platform amd64 target:
# the cluster is all-Graviton, so cross-compiling under QEMU would add minutes
# to every iteration for nothing.
image:
	docker build -t $(ECR_REPO):$(IMAGE_TAG) -f docker/Dockerfile .

push: preflight
	aws ecr get-login-password --region $(AWS_REGION) \
	  | docker login --username AWS --password-stdin \
	    $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com
	docker tag $(ECR_REPO):$(IMAGE_TAG) $(ECR_IMAGE)
	docker push $(ECR_IMAGE)

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
	RF_BUCKET=$(RF_BUCKET) envsubst < infra/eks/cluster.yaml | eksctl create cluster -f -
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
job:
	STAGE_NAME=$(STAGE) SCOPE=$(SCOPE) RF_BUCKET=$(RF_BUCKET) ECR_IMAGE=$(ECR_IMAGE) \
	AWS_REGION=$(AWS_REGION) \
	STAGE_FILE=$$(basename $$(ls src/$(STAGE)_*.py)) \
	  envsubst < k8s/sparkapplication.yaml | kubectl apply -f -
	kubectl get sparkapplication -w

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
	@echo "== month-to-date spend"; \
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
