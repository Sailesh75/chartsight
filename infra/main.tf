# ChartSight on AWS Lambda: the same container image as `docker compose`, run through the
# AWS Lambda Web Adapter (see Dockerfile) and exposed with a public Function URL.

locals {
  # "us.anthropic.claude-…" (inference profile) -> "anthropic.claude-…" (the foundation model it routes to)
  foundation_model_id = replace(var.bedrock_model_id, "/^(us|eu|apac|global)\\./", "")
}

# --------------------------------------------------------------------------- #
# Image registry
# --------------------------------------------------------------------------- #
resource "aws_ecr_repository" "app" {
  name                 = var.name
  image_tag_mutability = "MUTABLE" # CI moves :latest; each deploy also pushes an immutable :<git sha>
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 5 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# --------------------------------------------------------------------------- #
# Daily live-analysis counter (chartsight/quota.py)
# --------------------------------------------------------------------------- #
resource "aws_dynamodb_table" "quota" {
  name         = "${var.name}-live-quota"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "day"

  attribute {
    name = "day"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# --------------------------------------------------------------------------- #
# Function role: logs, the one Bedrock model, the counter. Nothing else.
# --------------------------------------------------------------------------- #
resource "aws_iam_role" "lambda" {
  name = "${var.name}-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "app" {
  name = "${var.name}-app"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "InvokeTheOneModel"
        Effect = "Allow"
        Action = "bedrock:InvokeModel"
        Resource = [
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_model_id}",
          # a cross-region profile routes to the model in several regions
          "arn:aws:bedrock:*::foundation-model/${local.foundation_model_id}",
        ]
      },
      {
        Sid      = "DailyQuotaCounter"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]
        Resource = aws_dynamodb_table.quota.arn
      },
    ]
  })
}

# --------------------------------------------------------------------------- #
# Function + public URL
# --------------------------------------------------------------------------- #
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = 14
}

resource "aws_lambda_function" "app" {
  function_name = var.name
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.app.repository_url}:latest"
  architectures = ["x86_64"]
  memory_size   = var.memory_mb
  timeout       = 60

  reserved_concurrent_executions = var.reserved_concurrency == null ? -1 : var.reserved_concurrency

  environment {
    variables = {
      BEDROCK_MODEL_ID            = var.bedrock_model_id
      CHARTSIGHT_LIVE_DAILY_LIMIT = tostring(var.live_daily_limit)
      CHARTSIGHT_QUOTA_TABLE      = aws_dynamodb_table.quota.name
    }
  }

  # CI deploys new images (aws lambda update-function-code); don't roll them back on apply.
  lifecycle {
    ignore_changes = [image_uri]
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy_attachment.logs]
}

resource "aws_lambda_function_url" "app" {
  function_name      = aws_lambda_function.app.function_name
  authorization_type = "NONE" # a public demo; cost is bounded by the daily quota and concurrency
}

# Since October 2025 a public Function URL needs both statements.
resource "aws_lambda_permission" "url" {
  statement_id           = "PublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.app.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "url_invoke" {
  statement_id             = "PublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.app.function_name
  principal                = "*"
  invoked_via_function_url = true
}

# --------------------------------------------------------------------------- #
# Keep-warm: EventBridge Scheduler invokes the function every 5 minutes. The Web Adapter
# forwards the event to POST /events (chartsight/api.py), so one instance stays initialized.
# --------------------------------------------------------------------------- #
resource "aws_iam_role" "scheduler" {
  count = var.keep_warm ? 1 : 0
  name  = "${var.name}-keep-warm"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  count = var.keep_warm ? 1 : 0
  name  = "${var.name}-keep-warm"
  role  = aws_iam_role.scheduler[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.app.arn
    }]
  })
}

resource "aws_scheduler_schedule" "keep_warm" {
  count               = var.keep_warm ? 1 : 0
  name                = "${var.name}-keep-warm"
  schedule_expression = "rate(5 minutes)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.app.arn
    role_arn = aws_iam_role.scheduler[0].arn
    input    = jsonencode({ source = "keep-warm" })

    retry_policy {
      maximum_retry_attempts = 0 # a missed ping is harmless; the next one comes in 5 minutes
    }
  }
}

# --------------------------------------------------------------------------- #
# Budget alert (account-wide)
# --------------------------------------------------------------------------- #
resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}
