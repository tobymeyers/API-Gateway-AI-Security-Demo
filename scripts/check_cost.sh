#!/usr/bin/env bash
# Shows actual AWS cost for this project, filtered by the Project (and
# optionally Project_Run) cost-allocation tag set by deploy.sh.
#
# Requires:
#   1) ce:GetCostAndUsage permission, from AWSBillingReadOnlyAccess - one
#      of the 10 required managed policies in DEPLOYMENT.md step 1
#      (required, not optional: we must be able to verify real spend,
#      not just trust the in-run USAGE_LOG estimate). If this fails with
#      AccessDenied, re-check step 1 - the policy was never attached, or
#      was detached since setup.
#   2) Project/Project_Run activated as Cost Allocation Tags (console-only,
#      no API path): Billing console -> Cost Allocation Tags -> activate
#      both. Takes up to 24h to start reflecting after activation, and
#      cost data itself lags ~24h behind actual usage regardless of tag
#      activation.
#
# Usage:
#   ./scripts/check_cost.sh                        # last 7 days, by service
#   ./scripts/check_cost.sh --days 30
#   ./scripts/check_cost.sh --run-id 20261003-abc1  # one specific deploy-to-teardown cycle
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

DAYS=7
RUN_ID=""

usage() {
  cat <<'EOF'
Usage: check_cost.sh [options]
  --days <n>      Look back this many days (default 7)
  --run-id <id>   Filter to one specific Project_Run tag value instead of all runs
  -h, --help      Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --days) DAYS="$2"; shift 2 ;;
    --days=*) DAYS="${1#*=}"; shift ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --run-id=*) RUN_ID="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

require_aws_credentials "checking cost"

START_DATE="$(date -u -v-"${DAYS}"d +%Y-%m-%d 2>/dev/null || date -u -d "-${DAYS} days" +%Y-%m-%d)"
END_DATE="$(date -u -v+1d +%Y-%m-%d 2>/dev/null || date -u -d "+1 day" +%Y-%m-%d)"

if [ -n "$RUN_ID" ]; then
  FILTER="{\"And\":[{\"Tags\":{\"Key\":\"Project\",\"Values\":[\"$PROJECT_TAG\"]}},{\"Tags\":{\"Key\":\"Project_Run\",\"Values\":[\"$RUN_ID\"]}}]}"
else
  FILTER="{\"Tags\":{\"Key\":\"Project\",\"Values\":[\"$PROJECT_TAG\"]}}"
fi

echo "Cost Explorer: $START_DATE to $END_DATE, Project=$PROJECT_TAG${RUN_ID:+, Project_Run=$RUN_ID}"
echo

RAW="$(aws ce get-cost-and-usage \
  --time-period "Start=$START_DATE,End=$END_DATE" \
  --granularity DAILY \
  --metrics "UnblendedCost" \
  --group-by Type=DIMENSION,Key=SERVICE \
  --filter "$FILTER" \
  --query 'ResultsByTime[].{Date:TimePeriod.Start,Groups:Groups[].[Keys[0],Metrics.UnblendedCost.Amount]}' \
  --output json 2>&1)" || {
  echo "$RAW" >&2
  if echo "$RAW" | grep -q "AccessDenied"; then
    echo >&2
    echo "Missing ce:GetCostAndUsage. AWSBillingReadOnlyAccess is a required" >&2
    echo "policy (DEPLOYMENT.md step 1, run ./scripts/check_permissions.sh) -" >&2
    echo "re-check it's attached to the demo user's group in the IAM console." >&2
  fi
  exit 1
}

echo "$RAW" | python3 -c "
import json, sys
days = json.load(sys.stdin)
total = 0.0
any_cost = False
for day in days:
    groups = [(k, float(a)) for k, a in (day.get('Groups') or []) if float(a) > 0.0000005]
    if not groups:
        continue
    any_cost = True
    print(day['Date'] + ':')
    for key, amt in sorted(groups, key=lambda x: -x[1]):
        total += amt
        print(f'  {key:45s} \${amt:.4f}')
if not any_cost:
    print('No cost recorded in this window for this tag filter.')
    print('(Cost data lags ~24h, and the tag must be activated as a Cost')
    print(' Allocation Tag in Billing first - see this script\\'s header.)')
print()
print(f'TOTAL: \${total:.4f}')
"
