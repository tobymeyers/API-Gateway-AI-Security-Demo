#!/usr/bin/env bash
# Usage:
#   ./scripts/teardown.sh                    # full teardown: stack, bucket, AND access key
#   ./scripts/teardown.sh --keep-access-key  # pause overnight: tears down the
#                                             # billable stack/bucket but leaves
#                                             # the IAM access key alone, so you
#                                             # don't have to redo the console
#                                             # setup (group/user/key creation)
#                                             # tomorrow. Delete the key for real
#                                             # (no --keep-access-key) once you're
#                                             # actually done with the event.
#   ./scripts/teardown.sh --stack-name foo --iam-user-name bar --artifact-bucket baz
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
IAM_USER_NAME="${IAM_USER_NAME:-ISC2_2026_Demo_User}"
KEEP_ACCESS_KEY="${KEEP_ACCESS_KEY:-false}"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-}"

usage() {
  cat <<'EOF'
Usage: teardown.sh [options]
  --stack-name <name>        CloudFormation stack name (default: isc2-gateway-exercise)
  --iam-user-name <name>     IAM user whose access key(s) get deleted (default: ISC2_2026_Demo_User)
  --artifact-bucket <name>   Override the artifact bucket name (default: isc2-gateway-exercise-<account-id>)
  --keep-access-key          Skip deleting the access key - pause overnight, not a full teardown
  -h, --help                 Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --stack-name=*) STACK_NAME="${1#*=}"; shift ;;
    --iam-user-name) IAM_USER_NAME="$2"; shift 2 ;;
    --iam-user-name=*) IAM_USER_NAME="${1#*=}"; shift ;;
    --artifact-bucket) ARTIFACT_BUCKET="$2"; shift 2 ;;
    --artifact-bucket=*) ARTIFACT_BUCKET="${1#*=}"; shift ;;
    --keep-access-key) KEEP_ACCESS_KEY="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

require_aws_credentials "tearing down"
require_permissions

aws cloudformation delete-stack --stack-name "$STACK_NAME"
echo "Delete initiated for $STACK_NAME. Watch progress with:"
echo "  aws cloudformation describe-stacks --stack-name $STACK_NAME"

# Access key deletion runs BEFORE the bucket cleanup below on purpose:
# this is the one step in teardown that's actually security-critical (a
# live, leaked-able credential), so it must not get skipped just because
# an unrelated, lower-stakes S3 delete happened to fail first under
# `set -e`. The bucket step is deliberately made non-fatal for the same
# reason - it's cost hygiene, not a security exposure, so a failure
# there shouldn't ever block getting here.
if [ "$KEEP_ACCESS_KEY" = "true" ]; then
  echo "--keep-access-key set - leaving IAM access key(s) for $IAM_USER_NAME in place."
  echo "Remember to delete it for real (run without --keep-access-key) once you're actually done with the event."
else
  echo "Deleting access key(s) for IAM user $IAM_USER_NAME (user itself is kept)..."
  KEY_IDS=$(aws iam list-access-keys --user-name "$IAM_USER_NAME" \
    --query 'AccessKeyMetadata[].AccessKeyId' --output text)
  if [ -z "$KEY_IDS" ]; then
    echo "No access keys found for $IAM_USER_NAME."
  else
    FAILED_KEYS=()
    for KEY_ID in $KEY_IDS; do
      # Deleting the key this very session is authenticated with invalidates
      # the session immediately - if that happens mid-loop, subsequent
      # deletions below will fail with an auth error. Don't let set -e abort
      # the loop on that; keep trying the rest and report failures at the end.
      if aws iam delete-access-key --user-name "$IAM_USER_NAME" --access-key-id "$KEY_ID" 2>/dev/null; then
        echo "Deleted access key $KEY_ID for $IAM_USER_NAME."
      else
        echo "WARNING: failed to delete access key $KEY_ID (likely because an earlier deletion in this loop invalidated the current session)." >&2
        FAILED_KEYS+=("$KEY_ID")
      fi
    done
    if [ "${#FAILED_KEYS[@]}" -gt 0 ]; then
      echo "ERROR: could not delete access key(s): ${FAILED_KEYS[*]}. Delete manually via IAM console or a fresh session: aws iam delete-access-key --user-name $IAM_USER_NAME --access-key-id <id>" >&2
      exit 1
    fi
  fi
fi

# Same default deploy.sh uses - matches unless you passed a custom
# ARTIFACT_BUCKET to deploy.sh too, in which case pass it here as well.
# Holds nothing but regenerable `cloudformation package` build output -
# a failure here is a warning, not a fatal error (see comment above).
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-isc2-gateway-exercise-$AWS_ACCOUNT_ID}"
if aws s3api head-bucket --bucket "$ARTIFACT_BUCKET" >/dev/null 2>&1; then
  echo "Deleting artifact bucket s3://$ARTIFACT_BUCKET ..."
  if ! aws s3 rb "s3://$ARTIFACT_BUCKET" --force; then
    echo "WARNING: failed to delete artifact bucket s3://$ARTIFACT_BUCKET. Delete manually: aws s3 rb s3://$ARTIFACT_BUCKET --force" >&2
  fi
else
  echo "Artifact bucket s3://$ARTIFACT_BUCKET not found (already deleted, or a different ARTIFACT_BUCKET was used) - skipping."
fi
