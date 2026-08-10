# The durable half of the infrastructure, split from eksctl by LIFETIME.
# eksctl owns the cluster (infra/eks/cluster.yaml), created and destroyed every
# session. Terraform owns the things that must outlive it.
#
# THREE resources, not the six the plan first called for. ACM, CloudFront and
# Route53 are deliberately absent: the interactive map is already live and free
# on GitHub Pages, which serves the HTTP range requests PMTiles needs, and
# statewide tiles are projected at 40-80 MB against Pages' 100 MB per-file
# limit. A certificate attached to nothing is dead config in a public repo.
# Trigger to add them, written down so it stays a decision rather than a drift:
# statewide rf.pmtiles exceeds 100 MB, or a custom domain is wanted.
#
# STATE IS LOCAL AND GITIGNORED. No S3 backend on purpose: Terraform 1.5.7 has
# no native S3 state locking (that arrived in 1.10), so a remote backend here
# would mean a bootstrap bucket Terraform cannot manage plus a DynamoDB lock
# table -- two extra permanent resources, in a module with three, to serialise
# applies between one laptop and itself. If terraform.tfstate is ever lost the
# recovery is three CLI deletes, which is cheaper than the backend, forever.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project = var.project
      # eksctl tags its own resources lifecycle=ephemeral. This is the other
      # half of that pair: anything tagged durable is expected to survive
      # `make cluster-down`.
      lifecycle = "durable"
      ManagedBy = "terraform"
    }
  }
}

# The Makefile wants both of these in the environment:
#   export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
#   export RF_BUCKET=$(terraform -chdir=infra/terraform output -raw rf_bucket)
output "rf_bucket" {
  description = "Export as RF_BUCKET. Read by src/session.py, infra/eks/cluster.yaml and k8s/sparkapplication.yaml."
  value       = aws_s3_bucket.rf.id
}

output "ecr_repository_url" {
  description = "Registry URI. The Makefile rebuilds this same string from AWS_ACCOUNT_ID."
  value       = aws_ecr_repository.rf.repository_url
}
