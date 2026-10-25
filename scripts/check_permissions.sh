#!/usr/bin/env bash
# Preflight check: confirms the current AWS CLI identity still has exactly
# the managed policies this project's IAM group was set up with. IAM
# group/policy attachments live outside this repo's version control, so a
# manual console change (detached policy, wrong group, etc.) would
# otherwise only surface as a cryptic AccessDenied deep inside deploy.sh
# or teardown.sh. Run standalone, or sourced by those scripts as a
# preflight - either way it exits non-zero on any mismatch.
#
# Usage: ./scripts/check_permissions.sh
set -euo pipefail

EXPECTED_POLICIES=(
  AWSCloudFormationFullAccess
  IAMFullAccess
  AWSLambda_FullAccess
  AmazonAPIGatewayAdministrator
  AmazonDynamoDBFullAccess
  AmazonCognitoPowerUser
  CloudWatchFullAccess
  AmazonS3FullAccess
  AmazonBedrockFullAccess
  # Required, not optional: we MUST be able to verify actual spend
  # (check_cost.sh / comprehensive_test.sh's cost-estimate steps), not
  # just trust the in-run USAGE_LOG estimate - that estimate is list
  # pricing from token counts, not a substitute for the real billed
  # number Cost Explorer reports.
  AWSBillingReadOnlyAccess
)

CALLER_ARN=$(aws sts get-caller-identity --query 'Arn' --output text)

if [[ "$CALLER_ARN" != *":user/"* ]]; then
  echo "WARNING: current identity ($CALLER_ARN) is not an IAM user (e.g. it's an assumed role) -" >&2
  echo "  this check only knows how to inspect IAM-user group/policy attachments. Skipping." >&2
  exit 0
fi

USER_NAME="${CALLER_ARN##*/}"

GROUP_NAMES=$(aws iam list-groups-for-user --user-name "$USER_NAME" \
  --query 'Groups[].GroupName' --output text)

if [ -z "$GROUP_NAMES" ]; then
  echo "ERROR: IAM user $USER_NAME belongs to no groups. Expected it to be in a group with:" >&2
  printf '  %s\n' "${EXPECTED_POLICIES[@]}" >&2
  exit 1
fi

ATTACHED_POLICIES=()
for GROUP in $GROUP_NAMES; do
  while IFS= read -r POLICY; do
    [ -n "$POLICY" ] && ATTACHED_POLICIES+=("$POLICY")
  done < <(aws iam list-attached-group-policies --group-name "$GROUP" \
    --query 'AttachedPolicies[].PolicyName' --output text | tr '\t' '\n')
done

# Also check policies attached directly to the user, in case someone
# bypassed the group as a quick fix.
while IFS= read -r POLICY; do
  [ -n "$POLICY" ] && ATTACHED_POLICIES+=("$POLICY")
done < <(aws iam list-attached-user-policies --user-name "$USER_NAME" \
  --query 'AttachedPolicies[].PolicyName' --output text | tr '\t' '\n')

MISSING=()
for EXPECTED in "${EXPECTED_POLICIES[@]}"; do
  FOUND=0
  for ATTACHED in "${ATTACHED_POLICIES[@]}"; do
    [ "$EXPECTED" == "$ATTACHED" ] && FOUND=1 && break
  done
  [ "$FOUND" -eq 0 ] && MISSING+=("$EXPECTED")
done

EXTRA=()
for ATTACHED in "${ATTACHED_POLICIES[@]}"; do
  FOUND=0
  for EXPECTED in "${EXPECTED_POLICIES[@]}"; do
    [ "$ATTACHED" == "$EXPECTED" ] && FOUND=1 && break
  done
  [ "$FOUND" -eq 0 ] && EXTRA+=("$ATTACHED")
done

if [ "${#EXTRA[@]}" -gt 0 ]; then
  echo "NOTE: $USER_NAME (via group $GROUP_NAMES) has policies beyond the expected ${#EXPECTED_POLICIES[@]}: ${EXTRA[*]}" >&2
fi

if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "ERROR: $USER_NAME is missing expected policies: ${MISSING[*]}" >&2
  echo "  Expected attached to group(s) [$GROUP_NAMES] or directly to the user:" >&2
  printf '    %s\n' "${EXPECTED_POLICIES[@]}" >&2
  echo "  Re-check IAM console - a policy may have been detached since setup." >&2
  exit 1
fi

echo "Permissions check OK: $USER_NAME has all ${#EXPECTED_POLICIES[@]} expected managed policies (via group: $GROUP_NAMES)."
