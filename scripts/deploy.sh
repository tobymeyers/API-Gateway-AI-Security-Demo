#!/usr/bin/env bash
# Builds the orchestrator Lambda package, then packages and deploys the
# whole stack. Requires: AWS CLI configured with credentials that can
# create IAM roles. The blue agent calls Claude via Bedrock (IAM-only,
# no separate Anthropic API key) - Bedrock auto-enables models on first
# invoke, but a brand-new AWS account's first-ever Claude invocation may
# need a one-time "use case details" form submitted in the Bedrock
# console first (see DEPLOYMENT.md 1b); require_bedrock_model_access
# below catches that with a clear error instead of a cryptic one deep in
# a CFN deploy. The S3 bucket `aws cloudformation package` stages the
# orchestrator zip in is created automatically below if it doesn't
# already exist - no separate one-time setup step needed.
#
# Usage:
#   ./scripts/deploy.sh
#   ./scripts/deploy.sh --artifact-bucket my-own-bucket   # to override the default name
#   ./scripts/deploy.sh --help                            # full flag list
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

MODEL_ID="${MODEL_ID:-us.anthropic.claude-sonnet-4-5-20250929-v1:0}"
# Unique per deploy.sh invocation - override to reuse/reproduce a specific
# run's tag (e.g. re-running this same script after a failure, to keep
# cost attributed to the original run rather than starting a new one).
RUN_ID="${RUN_ID:-$(date -u +%Y%m%d)-$(openssl rand -hex 4)}"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-}"

usage() {
  cat <<'EOF'
Usage: deploy.sh [options]
  --stack-name <name>                  CloudFormation stack name (default: isc2-gateway-exercise)
  --model-id <id>                      Bedrock model/inference-profile ID (default: us.anthropic.claude-sonnet-4-5-20250929-v1:0)
  --run-id <id>                        Project_Run cost-allocation tag value (default: generated fresh each run)
  --artifact-bucket <name>             Override the artifact bucket name (default: isc2-gateway-exercise-<account-id>)
  -h, --help                           Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --stack-name=*) STACK_NAME="${1#*=}"; shift ;;
    --model-id) MODEL_ID="$2"; shift 2 ;;
    --model-id=*) MODEL_ID="${1#*=}"; shift ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --run-id=*) RUN_ID="${1#*=}"; shift ;;
    --artifact-bucket) ARTIFACT_BUCKET="$2"; shift 2 ;;
    --artifact-bucket=*) ARTIFACT_BUCKET="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

# Validation only, nothing provisioned yet - safe to run before the local
# build so a bad credential/model-ID fails fast without touching AWS state.
require_aws_credentials "deploying"
require_permissions
require_bedrock_model_access "$MODEL_ID"

# Local build BEFORE any AWS provisioning - if this fails (bad deps, pip
# resolver issue, whatever), nothing has been created in AWS yet to clean
# up. Provisioning (the bucket below) only starts once we know we have
# something to actually upload.
"$ROOT_DIR/scripts/build_lambdas.sh"

# Account-ID-suffixed default: S3 bucket names are unique across ALL AWS
# accounts, not just yours, so a generic name risks colliding with someone
# else's bucket. The account ID makes this one collision-proof in practice.
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-isc2-gateway-exercise-$AWS_ACCOUNT_ID}"
ensure_artifact_bucket "$ARTIFACT_BUCKET"
tag_bucket "$ARTIFACT_BUCKET" "$RUN_ID"

echo "Tagging this deploy: Project=$PROJECT_TAG, Project_Run=$RUN_ID"

aws cloudformation package \
  --template-file "$ROOT_DIR/infra/template.yaml" \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --output-template-file "$ROOT_DIR/infra/packaged.yaml"

aws cloudformation deploy \
  --template-file "$ROOT_DIR/infra/packaged.yaml" \
  --stack-name "$STACK_NAME" \
  --s3-bucket "$ARTIFACT_BUCKET" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ModelId="$MODEL_ID" \
  --tags \
    "Project=$PROJECT_TAG" \
    "Project_Run=$RUN_ID"
  # NB: `deploy`'s --tags shorthand is bare TagKey=TagValue pairs - NOT
  # the "Key=...,Value=..." JSON-shorthand that create-stack/update-stack
  # use. Confirmed via `aws cloudformation deploy help`. Using the wrong
  # one fails with "ValidationError: Tag [Key] contained invalid
  # characters" - a real error hit live, not a hypothetical.

# Force a REAL new API Gateway deployment to the "prod" stage, explicitly,
# every time - confirmed live (twice) that neither DependsOn nor a
# changing Description on the template's own AWS::ApiGateway::Deployment
# resource reliably re-snapshots the API's current routes into what the
# stage actually serves; CloudFormation can report UPDATE_COMPLETE on
# that resource while the stage keeps serving an old snapshot missing a
# just-added route (returns API Gateway's own "Missing Authentication
# Token", indistinguishable from a route that was never defined - see
# DEPLOYMENT.md's troubleshooting entry). This bypasses that resource's
# unreliable update semantics entirely: create-deployment always captures
# the API's state as of right now and always re-points the named stage at
# it, independent of what the CloudFormation update actually did above.
API_ID="$(stack_output ApiUrl | sed -E 's#https://([^.]+)\..*#\1#')"
echo
echo "Forcing a fresh API Gateway deployment to the prod stage..."
aws apigateway create-deployment --rest-api-id "$API_ID" --stage-name prod \
  --description "deploy.sh explicit redeploy, run $RUN_ID, $(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --region "$AWS_REGION" >/dev/null
echo "Done."

echo
echo "Stack outputs:"
aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query 'Stacks[0].Outputs' --output table
echo
echo "Cost-allocation tags for this run: Project=$PROJECT_TAG, Project_Run=$RUN_ID"
echo "Filter by these in AWS Billing -> Cost Explorer (tags can take up to 24h to appear as filterable there)."
