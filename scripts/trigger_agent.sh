#!/usr/bin/env bash
# Starts ONLY the blue agent for one table - what the survey-tool webhook
# should call, or run by hand to debug the agent in isolation. This does
# NOT run a complete round: no Good Load Tester, no adversarial replay.
# For a complete round (agent + real traffic, concurrently, against an
# already-deployed stack), see run_round.sh instead - that's the actual
# event-day command. This script exists for that narrower, explicit
# purpose, and is named to say so (was formerly start_round.sh, which
# read as "start the round" even though it only ever started the agent -
# confirmed to mislead a real user into expecting traffic/scoring from
# it alone).
# Usage: ./scripts/trigger_agent.sh [--stack-name <name>] <table_id> "<prompt text>"
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

ROUND_SECONDS=""

usage() {
  cat <<'EOF'
Usage: trigger_agent.sh [options] <table_id> <prompt>
  --stack-name <name>     CloudFormation stack name (default: isc2-gateway-exercise)
  --round-seconds <n>     Override the round length (default: 60s, set in
                          the orchestrator's DEFAULT_ROUND_SECONDS - also
                          the real event's round length, capped repo-wide
                          to control Bedrock cost).
  -h, --help              Show this help
EOF
}

POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --stack-name=*) STACK_NAME="${1#*=}"; shift ;;
    --round-seconds) ROUND_SECONDS="$2"; shift 2 ;;
    --round-seconds=*) ROUND_SECONDS="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; POSITIONAL+=("$@"); break ;;
    -*) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
# Not a plain `set -- "${POSITIONAL[@]}"`: on bash 3.2 (macOS's default,
# frozen there for GPLv3 reasons - confirmed this is actually what's
# resolved here, not a hypothetical), expanding "${arr[@]}" on an EMPTY
# array under `set -u` throws "unbound variable" instead of just
# expanding to nothing (fixed in bash 4.4+). Reproduced live: running
# this script with zero positional args would hit that raw bash error
# instead of the usage message below. Guarding the count avoids it.
if [ "${#POSITIONAL[@]}" -gt 0 ]; then
  set -- "${POSITIONAL[@]}"
else
  set --
fi

TABLE_ID="${1:?Usage: trigger_agent.sh [options] <table_id> <prompt>}"
PROMPT="${2:?Usage: trigger_agent.sh [options] <table_id> <prompt>}"

require_aws_credentials "triggering the agent"
require_permissions

API_URL=$(stack_output ApiUrl)
API_KEY_ID=$(stack_output FacilitatorApiKeyId)
API_KEY_VALUE=$(aws apigateway get-api-key --api-key "$API_KEY_ID" --include-value --query 'value' --output text)

# Captured separately from the HTTP status so a non-2xx response (bad/
# stale API key, cold-started authorizer error, a malformed payload) is
# actually detected - curl without --fail exits 0 on any completed HTTP
# response regardless of status code, so without this check a 403/500
# here would print the error body but still report success to the caller
# (e.g. run_round.sh, which uses this script's exit code to decide
# whether to launch concurrent traffic for a round whose agent never started).
RESPONSE_BODY_FILE="$(mktemp)"
trap 'rm -f "$RESPONSE_BODY_FILE"' EXIT
HTTP_STATUS="$(curl -sS -o "$RESPONSE_BODY_FILE" -w '%{http_code}' -X POST "$API_URL/trigger-agent" \
  -H "x-api-key: $API_KEY_VALUE" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c '
import json, sys
table_id, prompt, round_seconds = sys.argv[1], sys.argv[2], sys.argv[3]
payload = {"table_id": table_id, "prompt": prompt}
if round_seconds:
    payload["round_seconds"] = int(round_seconds)
print(json.dumps(payload))
' "$TABLE_ID" "$PROMPT" "$ROUND_SECONDS")")"
cat "$RESPONSE_BODY_FILE"
echo
if [ "$HTTP_STATUS" != "202" ]; then
  echo "ERROR: /trigger-agent returned HTTP $HTTP_STATUS (expected 202) - the agent did NOT start." >&2
  exit 1
fi
