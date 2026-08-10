resource "aws_ecr_repository" "rf" {
  # Must match the Makefile's ECR_REPO, or `make push` pushes into a repository
  # that does not exist and fails with "name unknown". docker push does NOT
  # create the repository for you, which is the whole reason this file exists
  # before the first push rather than after it.
  name = var.ecr_repo

  # MUTABLE, not IMMUTABLE, and this is not laziness. The Makefile tags with
  # `git rev-parse --short HEAD`. The entire shape of the EKS spike is build,
  # push, watch a pod fail, fix, rebuild, re-push -- at the SAME commit,
  # because you are debugging, not committing. IMMUTABLE rejects that second
  # push with "tag invalid: the image tag already exists in the repository",
  # in the middle of a timeboxed session with a control plane billing.
  image_tag_mutability = "MUTABLE"

  # Same reason as the bucket's force_destroy: destroy has to complete with
  # images present, or `make destroy-all` half-fails and leaves ~3.8 GB of
  # layers billing at $0.10/GB-month.
  force_delete = true
}

# An unbounded repo of ~3.8 GB images is a real leak. Layers are shared, so a
# code-only rebuild adds a few MB -- but any change to requirements.txt or the
# jar list rewrites hundreds of MB of layers, and nothing ever deletes the
# previous ones.
#
# One rule with tagStatus "any", so it also sweeps the untagged layers left
# behind whenever a mutable tag is moved. A tagStatus "any" rule must be the
# last rule in an ECR policy; here it is the only rule.
resource "aws_ecr_lifecycle_policy" "rf" {
  repository = aws_ecr_repository.rf.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep only the last ${var.image_retain_count} images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = var.image_retain_count
      }
      action = { type = "expire" }
    }]
  })
}
