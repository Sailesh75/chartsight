# GitHub Actions deploys with short-lived OIDC credentials: no AWS keys stored in GitHub.
# Only workflows on var.github_branch of the repo named by var.github_subject_prefix can assume
# this role. The prefix carries GitHub's immutable owner/repo IDs, so a re-created repo with the
# same name can't.

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_github_oidc_provider ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  github_oidc_arn = (
    var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
  )
}

resource "aws_iam_role" "deploy" {
  name = "${var.name}-github-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_oidc_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "${var.github_subject_prefix}:ref:refs/heads/${var.github_branch}"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "deploy" {
  name = "${var.name}-deploy"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
        ]
        Resource = aws_ecr_repository.app.arn
      },
      {
        Effect = "Allow"
        Action = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:GetFunctionUrlConfig"]
        # By name, so the role can exist (and CI can push the first image) before the function does.
        Resource = "arn:aws:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:${var.name}"
      },
    ]
  })
}
