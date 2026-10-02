# Deploying ChartSight to AWS

ChartSight runs on **AWS Lambda** behind a public **Function URL**. It's the same
container image as `docker compose`, made Lambda-compatible by the
[AWS Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter)
line in the `Dockerfile`. Terraform in this folder creates everything:

| Resource | Purpose |
| --- | --- |
| ECR repository | Stores the image (the last 5 are kept). |
| Lambda function + Function URL | Serves the UI at `/` and the API, 1 GB memory, 60 s timeout. |
| IAM role (function) | Logs, `bedrock:InvokeModel` on the one Claude model, and the quota counter. Nothing else. |
| DynamoDB table | The daily live-analysis counter (`chartsight/quota.py`). |
| CloudWatch log group | 14-day retention. |
| IAM role + OIDC provider (GitHub) | Lets GitHub Actions deploy from `master` with short-lived credentials. |
| AWS Budget | Emails you at 80% of the monthly budget, and when the forecast passes it. |

## Cost guardrails

A public page that calls Claude needs a ceiling. There are three layers:

1. **Daily live quota.** `live_daily_limit` (default 100) live analyses per
   UTC day, across all visitors. At about $0.01 per note, the worst case is
   about $1/day. Past the limit, the free sample engine answers and the UI
   says so.
2. **Concurrency cap.** `reserved_concurrency` (default 3) bounds how fast
   anyone can use up the quota.
3. **Budget alert** by email.

Idle cost is about $0. Lambda, DynamoDB and logs stay inside the free tier at
demo traffic; ECR storage is a few cents.

## One-time setup

You need the [AWS CLI](https://aws.amazon.com/cli/), configured with an admin
or power-user profile, and [Terraform](https://developer.hashicorp.com/terraform/install)
1.6 or newer. You do **not** need Docker locally: GitHub Actions builds the image.

The order matters, because Lambda can't be created before its image exists.

**1. Create the registry and the deploy role**

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars      # then set budget_email
terraform init
terraform apply -target=aws_ecr_repository.app -target=aws_iam_role_policy.deploy
terraform output deploy_role_arn
```

If AWS says the GitHub OIDC provider already exists, set
`create_github_oidc_provider = false` in `terraform.tfvars` and re-run.

**2. Tell GitHub about the role.** In the repo, go to *Settings → Secrets and
variables → Actions → Variables* and add:

| Variable | Value |
| --- | --- |
| `AWS_DEPLOY_ROLE_ARN` | the `deploy_role_arn` output |
| `AWS_REGION` | `us-east-1` (optional; this is the default) |

These are variables, not secrets: a role ARN is not a credential.

**3. Push to `master`.** CI runs the tests, then the `deploy` job builds the
image and pushes it to ECR. On this first run it notes that the function
doesn't exist yet and stops there.

**4. Create the rest**

```bash
terraform apply
terraform output url
```

Open the URL. The header pill should read *Live · Claude Haiku 4.5 · 100 left
today*.

From now on, **every push to `master` deploys automatically** once the tests
pass. The deploy job also smoke-tests the live URL.

## If something goes wrong

| Symptom | Fix |
| --- | --- |
| `apply` fails on reserved concurrency | New accounts have a concurrency quota of 10, and AWS keeps 10 unreserved. Set `reserved_concurrency = null`. |
| The pill says *Sample engine* | Check the function's logs. Bedrock model access must be enabled for Claude Haiku 4.5 in this account and region. |
| The URL returns 403 | Both `aws_lambda_permission` resources must exist (public URLs need two statements since Oct 2025). |
| The first request is slow | Cold start: about 3–5 s after the function has been idle. Later requests are fast. |

## Tear down

```bash
terraform destroy
```

This removes everything, including the images (`force_delete`). Also delete the
`AWS_DEPLOY_ROLE_ARN` variable so CI stops trying to deploy.

## State

Terraform state is kept locally in `infra/terraform.tfstate`, which is
gitignored. That's fine for one person. For a team, move it to an S3 backend.
