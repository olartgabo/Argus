# ARGUS — one-time IAM setup.
# Run this ONCE with your admin AWS credentials (aws configure), from the repo root:
#   .\infra\setup-permissions.ps1 -IamUserName <your-cli-user>
#
# It creates the argus-dev policy and attaches it to the IAM user whose
# access keys are configured in the AWS CLI. Everything else (Lambda role,
# scheduler role, tables, etc.) can then be created under that policy.

param(
  # The IAM user your local `aws configure` access keys belong to.
  [Parameter(Mandatory = $true)][string]$IamUserName,
  [string]$Region = "us-east-1"
)

$ErrorActionPreference = "Stop"
$AccountId = (aws sts get-caller-identity --query Account --output text)
Write-Host "Account: $AccountId  User: $IamUserName  Region: $Region"

# 1. Dev policy — what the CLI user (and Claude Code) may do while building ARGUS
aws iam create-policy `
  --policy-name argus-dev `
  --policy-document file://infra/iam/argus-dev-policy.json `
  --description "Build-time permissions for the ARGUS MCP watcher project"

aws iam attach-user-policy `
  --user-name $IamUserName `
  --policy-arn "arn:aws:iam::${AccountId}:policy/argus-dev"

# 2. Lambda execution role (runtime permissions of argus-run)
aws iam create-role `
  --role-name argus-lambda-role `
  --assume-role-policy-document file://infra/iam/lambda-trust.json

aws iam put-role-policy `
  --role-name argus-lambda-role `
  --policy-name argus-lambda-runtime `
  --policy-document file://infra/iam/argus-lambda-runtime-policy.json

# 3. EventBridge Scheduler role (only allowed to invoke argus-* Lambdas)
aws iam create-role `
  --role-name argus-scheduler-role `
  --assume-role-policy-document file://infra/iam/scheduler-trust.json

aws iam put-role-policy `
  --role-name argus-scheduler-role `
  --policy-name argus-scheduler-invoke `
  --policy-document file://infra/iam/scheduler-invoke-policy.json

Write-Host ""
Write-Host "Done. Roles created:"
Write-Host "  arn:aws:iam::${AccountId}:role/argus-lambda-role"
Write-Host "  arn:aws:iam::${AccountId}:role/argus-scheduler-role"
Write-Host ""
Write-Host "REMINDER (console, can't be scripted): enable Bedrock model access"
Write-Host "for Nova Pro / Claude in region $Region -> Bedrock console -> Model access."
