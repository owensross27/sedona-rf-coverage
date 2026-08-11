# EKS runbook

Operating notes for the cloud tier. The local tier (`make demo`) needs none of
this: it runs the whole pipeline on one county with no AWS account at all.

Everything below was written against a cluster that actually ran the statewide
job, not against the documentation. Where a symptom is quoted, it is a symptom
this project hit.

## What the cluster is

| | |
|---|---|
| Cluster | `rf-cov`, EKS, `us-west-2`, defined in [`infra/eks/cluster.yaml`](../infra/eks/cluster.yaml) |
| `serve` nodegroup | 1 on-demand arm64 node. Spark **driver** and the operator live here |
| `spark-spot` nodegroup | 0-3 spot arm64 nodes, diversified over four instance types. **Executors only** |
| Submission | kubeflow spark-operator 2.5.2, `SparkApplication` CRD |
| Storage | S3 only. No PVs, no EBS beyond the node root volumes, nothing to orphan |

Both nodegroups are created at `desiredCapacity: 0`. A cluster that exists is
not automatically a cluster that costs node-hours.

The driver sits on on-demand deliberately. A spot reclaim then costs executors,
which Spark re-requests; a reclaimed driver kills the whole job.

## The sequence

```bash
export RF_BUCKET=...            # your bucket
export AWS_ACCOUNT_ID=...       # never hardcoded: this repo is public

make preflight                  # asserts every third-party image publishes arm64
make image push                 # build, then push to ECR -- ALWAYS in that order
make cluster-up                 # ~15 min. Creates the cluster, scales serve to 1,
                                # installs the operator, loads the Census key Secret
make nodes-up                   # ~3 min. Scales spark-spot to 3
make job STAGE=05 SCOPE=state   # submit one stage
make watch                      # deterministic waste check -- non-zero if billing without progressing
make cluster-down               # ~10 min. Deletes everything
```

`make spike` wraps `cluster-up`/`nodes-up` in a shell trap so teardown fires on
exit, failure, Ctrl-C, or the terminal closing. Do the work in a second
terminal. It is still laptop-bound; [`infra/terraform/reaper.tf`](../infra/terraform/reaper.tf)
is the server-side backstop that does not care whether a lid is closed.

### Stages that do not need executors

01/02/03 are driver-heavy (HTTP fetches, raster merges) and barely touch
executors. Holding three spot nodes through them is real money for nothing:

```bash
make job STAGE=02 SCOPE=state EXECUTORS=1
```

## The Spark UI after the job is gone

The UI is served by the driver pod and dies with it. There is no history server
running on the cluster, which turns "screenshot the UI" into a task that
competes with teardown for billable minutes.

`spark.eventLog.dir` in [`k8s/sparkapplication.yaml`](../k8s/sparkapplication.yaml)
persists the event log to S3 instead, so the UI can be rendered afterwards, for
free, offline, and as many times as wanted:

```bash
make history        # syncs the logs down, serves the real UI on localhost:18080
make history-stop
```

Two constraints are load-bearing:

- **The log prefix must exist before the job starts.** `EventLogFileWriter`
  calls `getFileStatus` on it at `SparkContext` init, and on s3a a missing
  prefix is a `FileNotFoundException`. The job dies before stage one, with a
  stack trace about event logging that reads nothing like a bucket problem.
  `make job` depends on `events-prefix`, which creates the zero-byte marker
  s3a reads as a directory.
- **s3a has no real `hflush`.** The log is buffered locally and PUT on close,
  i.e. at `SparkContext.stop()`. You will see
  `WARN S3ABlockOutputStream: Application invoked the Syncable API ... This is
  unsupported`, which is expected. A driver killed with SIGKILL leaves nothing,
  so a crashed pod still has no UI.

To capture the UI as images rather than look at it, use headless Chrome, the
Spark UI draws its DAG with JavaScript, so it needs a virtual time budget or
the screenshot lands on an empty page:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --hide-scrollbars \
  --window-size=1250,2400 --virtual-time-budget=9000 \
  --screenshot=out.png \
  "http://localhost:18080/history/<app-id>/SQL/execution/?id=<query-id>"
```

`make history` syncs the logs down rather than pointing the history server at
`s3a://` directly. The server's header prints the log directory verbatim, and a
screenshot of the real bucket name is exactly what a public repo must not
carry. And hadoop-aws 3.3.4's provider chain (Temporary → Simple → Environment →
IAMInstance) has **no `ProfileCredentialsProvider`**, so s3a cannot see
`~/.aws/credentials` at all: a laptop with a working `aws` CLI still fails with
`Unable to load AWS credentials from environment variables`. Reading a local
directory needs no credentials.

## Symptom → cause

Every row here cost real time or real money once.

| Symptom | Cause |
|---|---|
| `eksctl create` aborts with `unknown field "spotAllocationStrategy"` | It is a field on **unmanaged** `nodeGroups`, not `managedNodeGroups`. `eksctl create cluster --dry-run` validates the whole file for **free**: run it before every create |
| `helm install --wait` hangs forever right after create | Both nodegroups are at 0, so even `coredns` is Pending. Scale `serve` first (`cluster-up` does) |
| Executors Pending forever, `0/4 nodes are available: 3 Insufficient memory`, **while the job reports RUNNING** | Executor memory sized against the biggest instance in the pool. Spot returns the *cheapest*: 32 GiB machines are in the mix, and 24g × 1.4 overhead = 33.6 GiB > 29.8 GiB allocatable. Size against `kubectl get nodes -l workload=spark -o json` allocatable |
| Job does all its work, then dies at commit with `AccessDeniedException: delete on s3a://.../_temporary/...` | The `serve` node role needs S3 **write** permissions despite the name: the driver commits, and on S3 a rename is copy + **delete** |
| Driver pod 422s; logs show `Please check "kubectl auth can-i create pod"` | Not RBAC. A local-dir volume not named `spark-local-dir-*`, or `spark.local.dir` set alongside one, produces two volumeMounts for one path |
| `IOException: Can't find the home directory at '/nonexistent'` in stage 02 | The Spark image user has no home; duckdb resolves one at startup. `HOME=/tmp` |
| `w+b not supported for /vsis3/` writing a COG | GDAL cannot create a tiled TIFF directly on object storage. `CPL_VSIL_USE_TEMP_FILE_FOR_RANDOM_WRITE=YES` plus `CPL_TMPDIR=/tmp` |
| A COG reads as "not a supported file format" | GDAL with no region talks to us-east-1; a us-west-2 bucket answers 301. Set `AWS_DEFAULT_REGION`. Affects only the raster stages, boto3 and hadoop-aws are unaffected |
| `exec format error` | Something in the path is amd64. Everything here is arm64; `make preflight` checks the third-party images |
| `.dkr.ecr.` with nothing before it | `AWS_ACCOUNT_ID` is not exported. The Makefile never hardcodes it |
| Image tag exists but the code inside is old | **A tag names the SHA at build time, not the source inside it.** `make push` does not depend on `image`. Always `make image` immediately before `make push` (11.6 s, fully cached but `COPY src/`), and verify with `docker run --rm --entrypoint ls <tag> /opt/rfc/src/` |

## Watching for waste

`make watch` is a deterministic detector, not a dashboard. It exits non-zero
when the cluster is **billing without progressing**, and every check in it is
one that already cost money:

- Pending pods past a bootstrap grace period
- Spot nodes schedulable with no active `SparkApplication`
- Idle since the last job finished (this one fired for real at 12 minutes:
  `ALERT cluster idle 12m since the last job finished -- $0.167/hr for nothing`)
- Image-pull loops
- A cluster past its timebox

`make watch-loop` runs it on an interval. It makes no Cost Explorer call, so
looping it is free.

Two things it taught, both worth keeping:

- **Transient-by-design states must not be reported as standing faults.** It
  cried wolf three times, cordoned nodes during scale-down, kube-system pods
  during bootstrap, gaps between chained stages, before the thresholds and
  grace periods were right. An alarm that is usually wrong gets ignored, and
  then it is worse than nothing.
- **A check that quietly stops applying is worse than one that fails.** The
  age check parsed nothing for a while and still reported a pass: `aws` CLI v1
  returns `createdAt` as a Unix epoch float, v2 as ISO 8601. Flag unparseable
  input loudly.

Related: `cmd | tail; echo $?` reports **tail's** exit code. Any exit-code
claim measured through a pipe is worthless, redirect and capture instead.

## Teardown, and how to actually verify it

```bash
make cluster-down
```

Then verify **by name**, not by region:

```bash
eksctl get cluster --name rf-cov --region us-west-2
aws cloudformation describe-stacks --region us-west-2 \
  --query "Stacks[?starts_with(StackName,'rf-cov')].StackName"
aws ec2 describe-instances --region us-west-2 \
  --filters Name=instance-state-name,Values=running --query 'length(Reservations)'
aws ec2 describe-nat-gateways --region us-west-2 \
  --filter Name=state,Values=available --query 'length(NatGateways)'
```

⚠️ **"Is the region empty?" was never a valid check.** This region has more
than one tenant: a sibling project billed $13.80 here over two days and took a
$20 region budget to 74% before this project's first cluster existed. The check
would pass with `rf-cov` still running, or fail because of something unrelated.

The NAT gateway is the one that hurts: it survives node scale-down, bills
hourly regardless of traffic, and is invisible in `kubectl`. `make check-nat`
exists for exactly that.

## Cost control, honestly

**AWS has no hard spending cap.** Budgets notify; they never stop. The only
enforcement primitive is `aws_budgets_budget_action`, and all three forms fail
for this shape:

- `scp_action_definition`, SCPs never apply to an organization's management
  account, and this is one.
- `ssm_action_definition`, needs explicit `instance_ids`, which an EKS
  autoscaling group cannot supply in advance.
- `iam_action_definition`, works, but only blocks the creation of **new**
  resources. It cannot stop a control plane that is already billing, which is
  precisely the failure mode here.

So enforcement is a **TTL reaper**, not a billing control:
[`infra/terraform/reaper.tf`](../infra/terraform/reaper.tf) is an hourly Lambda
that deletes any `rf-cov`-tagged cluster older than its TTL. Budgets remain, as
notification.

Sizing note: a cluster forgotten for a fortnight is ~$34, which is *under* a $45
cap, so the cap would stay silent through the entire scenario it was bought
for. The budget only makes sense because the reaper handles the sub-cap case.
