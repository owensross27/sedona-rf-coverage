"""Delete EKS clusters that outlived their TTL.

WHY THIS EXISTS. An AWS Budget notifies; it does not stop anything. The single
largest cost risk in this project is a control plane left running after a
session: $0.10/hr, $2.40/day, and roughly $34 over a fortnight -- which is
UNDER the hard-cap budget, so no billing alarm would ever fire on it. The
budget catches catastrophes. This catches the realistic failure.

Two independent gates before anything is deleted, because the blast radius of
a wrong answer here is someone else's cluster:

  1. the cluster must carry the tag written by infra/eks/cluster.yaml, and
  2. it must be older than TTL_HOURS.

Fail-safe by construction: an untagged cluster is never touched, so if eksctl
ever stops propagating metadata.tags this reaper does nothing rather than
something. The IAM policy is additionally scoped to one region.

ESCAPE HATCH for a legitimately long run: remove the tag from the cluster and
the reaper ignores it forever. That is a one-liner and needs no terraform:

    aws eks untag-resource --resource-arn <cluster-arn> --tag-keys lifecycle

NOT A TIDY TEARDOWN. This calls the EKS API directly, so it leaves eksctl's
CloudFormation stacks and the VPC behind. That is deliberate: the VPC is free
here (cluster.yaml disables the NAT gateway, which is the only billable thing
in it), and stopping the meter matters more than leaving the account neat.
Follow up with `eksctl delete cluster` to clear the stacks.
"""

import datetime
import os

TAG_KEY = os.environ.get("TAG_KEY", "lifecycle")
TAG_VALUE = os.environ.get("TAG_VALUE", "ephemeral")
TTL_HOURS = float(os.environ.get("TTL_HOURS", "8"))

# A cluster mid-create cannot be deleted, and one already deleting does not
# need to be. Acting only on settled states keeps the sweep idempotent.
REAPABLE_STATUS = ("ACTIVE", "FAILED")


def should_reap(cluster, now, ttl_hours=TTL_HOURS, tag_key=TAG_KEY, tag_value=TAG_VALUE):
    """Pure decision, so it can be tested without AWS. Returns (bool, reason)."""
    name = cluster.get("name", "?")
    status = cluster.get("status")
    if status not in REAPABLE_STATUS:
        return False, f"{name}: status {status}, not settled"

    tags = cluster.get("tags") or {}
    if tags.get(tag_key) != tag_value:
        return False, f"{name}: not tagged {tag_key}={tag_value}, leaving alone"

    created = cluster.get("createdAt")
    if created is None:
        return False, f"{name}: no createdAt, refusing to guess age"

    age_h = (now - created).total_seconds() / 3600.0
    if age_h < ttl_hours:
        return False, f"{name}: age {age_h:.1f}h under TTL {ttl_hours}h"

    return True, f"{name}: age {age_h:.1f}h over TTL {ttl_hours}h"


def handler(event, context):
    import boto3

    eks = boto3.client("eks")
    now = datetime.datetime.now(datetime.timezone.utc)
    acted, skipped = [], []

    for name in eks.list_clusters().get("clusters", []):
        try:
            cluster = eks.describe_cluster(name=name)["cluster"]
            reap, reason = should_reap(cluster, now)
            print(reason)
            if not reap:
                skipped.append(reason)
                continue

            # Nodegroups must go first -- DeleteCluster fails while any exist.
            # Deletion is asynchronous and takes minutes, so this run stops
            # here and the next scheduled run deletes the control plane. The
            # nodes are the expensive half and they stop draining immediately,
            # so converging an hour later costs $0.10, not $0.54.
            nodegroups = eks.list_nodegroups(clusterName=name).get("nodegroups", [])
            if nodegroups:
                for ng in nodegroups:
                    try:
                        eks.delete_nodegroup(clusterName=name, nodegroupName=ng)
                        print(f"{name}: deleting nodegroup {ng}")
                    except eks.exceptions.ResourceInUseException:
                        print(f"{name}: nodegroup {ng} already deleting")
                acted.append(f"{name}: {len(nodegroups)} nodegroup(s) deleting")
                continue

            eks.delete_cluster(name=name)
            acted.append(f"{name}: control plane deleted")
            print(f"{name}: control plane deleted, meter stopped")

        except Exception as exc:  # one bad cluster must not abort the sweep
            print(f"{name}: ERROR {type(exc).__name__}: {exc}")
            skipped.append(f"{name}: error")

    return {"acted": acted, "skipped": skipped}


def _self_check():
    """Runnable with no AWS account: python3 reaper.py"""
    now = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.timezone.utc)
    old = now - datetime.timedelta(hours=20)
    fresh = now - datetime.timedelta(hours=1)
    tagged = {"lifecycle": "ephemeral"}

    def c(**kw):
        base = {"name": "rf-cov", "status": "ACTIVE", "tags": tagged, "createdAt": old}
        base.update(kw)
        return base

    # The two gates, each independently sufficient to spare a cluster.
    assert should_reap(c(), now)[0] is True
    assert should_reap(c(createdAt=fresh), now)[0] is False, "young cluster must survive"
    assert should_reap(c(tags={}), now)[0] is False, "untagged cluster must survive"
    assert should_reap(c(tags={"lifecycle": "durable"}), now)[0] is False
    assert should_reap(c(status="CREATING"), now)[0] is False
    assert should_reap(c(status="DELETING"), now)[0] is False
    assert should_reap(c(createdAt=None), now)[0] is False, "unknown age must survive"

    # The boundary itself: TTL is exclusive, so exactly-TTL reaps.
    assert should_reap(c(createdAt=now - datetime.timedelta(hours=8)), now, 8)[0] is True
    assert should_reap(c(createdAt=now - datetime.timedelta(hours=7.9)), now, 8)[0] is False

    print("reaper self-check: 9 assertions passed")


if __name__ == "__main__":
    _self_check()
