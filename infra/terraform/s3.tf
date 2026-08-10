# Ported from ~/geospatial-sms/infrastructure/terraform/s3.tf, with three
# deliberate deviations, each commented where it happens.

resource "aws_s3_bucket" "rf" {
  # DEVIATION 1: no account id in the name. The port source used
  # "${prefix}-data-${account_id}", which is fine until the bucket name shows
  # up in a Spark log line, a Spark UI screenshot, or the terminal recording
  # this project plans to publish -- at which point a public artefact carries
  # the AWS account number. bucket_prefix lets S3 generate the unique suffix
  # instead; the name comes back out of `terraform output` and never enters a
  # tracked file.
  bucket_prefix = "${var.project}-"

  # DELIBERATE: `terraform destroy` has to actually complete. A destroy that
  # aborts on BucketNotEmpty leaves the bucket billing indefinitely, which is
  # precisely the failure this project exists to avoid. Everything in here is
  # a regenerable pipeline output -- `make demo` rebuilds it from open buckets
  # with no credentials at all.
  force_destroy = true
}

# DEVIATION 2, the important one: geospatial-sms/s3.tf ENABLES versioning.
# This bucket must not have it. Spark and GDAL overwrite the same GeoParquet
# and COG keys on every re-run; with versioning on, each overwrite silently
# retains the previous object as a noncurrent version that still bills at full
# Standard rate and that `aws s3 rm` does not remove. A handful of statewide
# re-runs at ~10 GB each becomes tens of GB of storage nobody can see.
#
# Written as "Disabled" rather than omitting the resource, because that makes
# it an assertion: Terraform now shows drift if versioning is ever switched on
# in the console. "Disabled" is only a valid value on a bucket that has never
# been versioned, which is the case for a bucket this module creates.
resource "aws_s3_bucket_versioning" "rf" {
  bucket = aws_s3_bucket.rf.id

  versioning_configuration {
    status = "Disabled"
  }
}

# DEVIATION 3: the port source's lifecycle rules were intelligent-tiering plus
# noncurrent-version expiry. Both are wrong here -- there are no noncurrent
# versions to expire, and objects that live days to weeks never reach
# intelligent-tiering's 30-day break-even. This one rule replaces both.
resource "aws_s3_bucket_lifecycle_configuration" "rf" {
  bucket = aws_s3_bucket.rf.id

  # The rule that pays for itself. BOTH writers in this pipeline do multipart
  # uploads: hadoop-aws for the GeoParquet, and GDAL's /vsis3 for every COG
  # (02_terrain, 06_coverage and 08_features all write through
  # sources.gdal_path()). A driver that is OOMKilled, a spot reclaim mid-PUT or
  # a Ctrl-C leaves the already-uploaded parts on S3: invisible to
  # `aws s3 ls`, not removed by `aws s3 rm`, not removed by emptying the bucket
  # in the console, and billed at Standard rate forever.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {} # whole bucket

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "rf" {
  bucket = aws_s3_bucket.rf.id

  rule {
    apply_server_side_encryption_by_default {
      # SSE-S3, free. SSE-KMS would add a KMS request charge against every one
      # of the millions of objects a statewide Spark write produces.
      sse_algorithm = "AES256"
    }
  }
}

# Stays fully on even after CloudFront arrives: the correct wiring there is
# Origin Access Control plus a bucket policy, not a public bucket.
resource "aws_s3_bucket_public_access_block" "rf" {
  bucket                  = aws_s3_bucket.rf.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
