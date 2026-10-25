#!/usr/bin/env bash
# Shared helpers sourced by the other scripts/*.sh files - single source
# of truth for the credential/permission preflight checks and the
# repeated `describe-stacks --query Outputs[...]` lookup pattern, instead
# of duplicating each per script.
#
# Usage: source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

SCRIPT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_LIB_DIR/.." && pwd)"
STACK_NAME="${STACK_NAME:-isc2-gateway-exercise}"

# Where every generated attack plan lives, one file per generation
# (attack-plan-<run_id>.json, never overwritten) - see
# scripts/lib/attack_plans.py and generate_attack_script.sh. (The
# generating tool/CLI flag are still called "attack script" - only this
# stored, versioned artifact's own folder/filename convention uses
# "plan", since that's what it actually is: a timed sequence of
# actions, not source code.)
ATTACK_PLANS_DIR="$ROOT_DIR/attack-plans"

# resolve_attack_plan - prints the path to the most recently generated
# attack plan, or fails with exit 1 and a clear message if none exist
# yet. Every tool that replays/reuses "the" attack script without an
# explicit --attack-script override goes through this one function, so
# "pick whatever's newest" has a single implementation - see
# scripts/lib/attack_plans.py (unit-tested).
resolve_attack_plan() {
  python3 "$SCRIPT_LIB_DIR/lib/attack_plans.py" "$ATTACK_PLANS_DIR"
}

# Every resource this project creates (stack, bucket, Cognito, etc.) is
# hard-pinned to this region, overriding whatever region a user's own AWS
# CLI profile/config or ambient AWS_REGION env var says - deliberate, not
# a soft default, so nothing accidentally lands somewhere else because of
# leftover shell state. To retarget the whole project elsewhere, edit this
# line (a deliberate, visible change) rather than relying on env vars -
# and re-verify ModelId first, since the default assumes a "us." Bedrock
# inference profile (see DEPLOYMENT.md 1b) that isn't valid everywhere.
export AWS_REGION="us-west-2"
export AWS_DEFAULT_REGION="us-west-2"

# Sets AWS_ACCOUNT_ID as a side effect, so callers needing the account ID
# (e.g. for a deterministic bucket name) don't need a second STS round-trip.
AWS_ACCOUNT_ID=""
require_aws_credentials() {
  local context="${1:-running this}"
  if ! AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null); then
    echo "ERROR: no valid AWS credentials found. Run 'aws configure' (or refresh your session) before $context." >&2
    exit 1
  fi
}

require_permissions() {
  "$SCRIPT_LIB_DIR/check_permissions.sh"
}

# stack_output <OutputKey> - prints one CloudFormation stack output value.
stack_output() {
  local key="$1"
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='$key'].OutputValue" --output text
}

# ensure_artifact_bucket <bucket> - creates the S3 bucket `aws
# cloudformation package` stages the orchestrator zip in, if it doesn't
# already exist, and (re-)applies a 1-day expiration lifecycle rule every
# call - not just on first creation, so a bucket created by an older
# version of this script before the rule existed still gets it applied
# retroactively. 1 day is S3 lifecycle's actual floor (same as CloudWatch
# Logs retention - no sub-day granularity exists in either API), kept
# consistent with every CloudWatch Logs RetentionInDays in
# infra/template.yaml even though this bucket holds build artifacts, not
# exercise logs. Idempotent (put-bucket-lifecycle-configuration always
# sets the same rule), so safe to call on every deploy.
ensure_artifact_bucket() {
  local bucket="$1"
  if ! aws s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    echo "Artifact bucket s3://$bucket doesn't exist yet - creating it..."
    aws s3 mb "s3://$bucket"
  fi
  aws s3api put-bucket-lifecycle-configuration --bucket "$bucket" --lifecycle-configuration '{
    "Rules": [{
      "ID": "expire-after-1-day",
      "Status": "Enabled",
      "Filter": {},
      "Expiration": {"Days": 1}
    }]
  }'
}

# Cost-allocation tags. PROJECT_TAG is constant across every deploy, so
# it's the filter that rolls up ALL spend for this project in Cost
# Explorer/Billing. RUN_ID is generated fresh per deploy.sh invocation
# (below) so a specific deploy-to-teardown lifecycle can be isolated from
# others (e.g. a practice run vs. the real event, or a redeploy after a
# fix). CloudFormation stack-level tags auto-propagate to every taggable
# resource IN the stack - no per-resource Tags block needed in the
# template - but resources created outside the stack (the artifact
# bucket) need tagging separately; tag_bucket below handles that one.
PROJECT_TAG="ISC2_AI_Gateway"

# tag_bucket <bucket> <run_id> - applies the Project/Project_Run tags to
# an S3 bucket (which lives outside the CFN stack, so doesn't get them
# via stack-level tag propagation). Overwrites any existing tag set, so
# it's safe to call on every deploy even if the bucket is being reused.
tag_bucket() {
  local bucket="$1" run_id="$2"
  aws s3api put-bucket-tagging --bucket "$bucket" --tagging \
    "{\"TagSet\":[{\"Key\":\"Project\",\"Value\":\"$PROJECT_TAG\"},{\"Key\":\"Project_Run\",\"Value\":\"$run_id\"}]}"
}

# require_bedrock_model_access <model_id> - confirms the current
# IAM identity can actually invoke the given Bedrock model, before a
# multi-minute CFN deploy rather than mid-round when the orchestrator
# Lambda first calls it. Unlike the old direct-Anthropic-API check (a
# free /v1/models call), Bedrock has no free auth-only endpoint - this
# is a real 1-token invoke, so it costs a negligible fraction of a cent.
# A failure here is most often the model/inference-profile ID being
# wrong for this region, OR - on a brand-new AWS account's first-ever
# Claude invocation - a one-time "use case details" gate that Anthropic
# specifically requires and that only clears via the console (see
# DEPLOYMENT.md 1b); there's no CLI-only way to satisfy that gate.
require_bedrock_model_access() {
  local model_id="$1"
  local tmp_out
  tmp_out="$(mktemp)"
  if ! aws bedrock-runtime invoke-model \
    --model-id "$model_id" \
    --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' \
    --cli-binary-format raw-in-base64-out \
    "$tmp_out" >/dev/null 2>"$tmp_out.err"; then
    echo "ERROR: could not invoke Bedrock model '$model_id'. Check that:" >&2
    echo "  1) the model/inference-profile ID is correct for your region (Bedrock console -> Model catalog)" >&2
    echo "  2) if this AWS account has never invoked a Claude model on Bedrock before, open the model" >&2
    echo "     once in the Bedrock console (Model catalog -> Playground) - a first-time account may need" >&2
    echo "     to submit a one-time 'use case details' form there before InvokeModel is allowed" >&2
    cat "$tmp_out.err" >&2
    rm -f "$tmp_out" "$tmp_out.err"
    exit 1
  fi
  rm -f "$tmp_out" "$tmp_out.err"
}
