output "url" {
  description = "The public ChartSight URL (UI at /, API docs at /docs)."
  value       = aws_lambda_function_url.app.function_url
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN variable in the GitHub repo."
  value       = aws_iam_role.deploy.arn
}

output "function_name" {
  value = aws_lambda_function.app.function_name
}
