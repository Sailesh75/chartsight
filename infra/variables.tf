variable "name" {
  description = "Name for every resource (ECR repo, Lambda function, table, roles)."
  type        = string
  default     = "chartsight"
}

variable "region" {
  description = "AWS region. Bedrock's US cross-region inference profile is called from here."
  type        = string
  default     = "us-east-1"
}

variable "github_subject_prefix" {
  description = <<-EOT
    The repo's GitHub OIDC subject prefix; only its workflows can deploy. Repos created after
    2026-07-15 use the immutable form repo:OWNER@OWNER_ID/REPO@REPO_ID (older ones: repo:OWNER/REPO).
    Look it up: curl https://api.github.com/repos/OWNER/REPO/actions/oidc/customization/sub
  EOT
  type        = string
  default     = "repo:Sailesh75@65943201/chartsight@1364939198"
}

variable "github_branch" {
  description = "Only pushes to this branch can assume the deploy role."
  type        = string
  default     = "master"
}

variable "create_github_oidc_provider" {
  description = "Set false if this AWS account already has the token.actions.githubusercontent.com OIDC provider."
  type        = bool
  default     = true
}

variable "bedrock_model_id" {
  description = "Bedrock model or cross-region inference profile the app calls."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "live_daily_limit" {
  description = "Live (billed) analyses allowed per UTC day across all visitors; then the sample engine answers."
  type        = number
  default     = 100
}

variable "reserved_concurrency" {
  description = "Max concurrent requests. null = no reservation (needed on new accounts whose concurrency quota is 10)."
  type        = number
  default     = 3
  nullable    = true
}

variable "memory_mb" {
  description = "Lambda memory. CPU scales with it: below 1769 MB a function gets only part of a vCPU, which slows the single-threaded Python cold start."
  type        = number
  default     = 2048
}

variable "keep_warm" {
  description = "Ping the function every 5 minutes so visitors rarely hit a cold start (~8,600 tiny invocations a month, inside the free tier)."
  type        = bool
  default     = true
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget for the whole account; alerts at 80% actual and 100% forecast."
  type        = number
  default     = 10
}

variable "budget_email" {
  description = "Where budget alerts go."
  type        = string
}
