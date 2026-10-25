#!/usr/bin/env bash
# The deterministic end-of-round signal. Calls the synchronous /end-round
# route, which invokes the Orchestrator with {"action": "end_round"} and
# waits for its reply - unlike trigger_agent.sh's fire-and-forget 202,
# this blocks until reset_config() has actually run, so a caller (see
# run_round.sh) that calls this AFTER waiting on both traffic generators'
# real exit codes gets a defense-reset boundary pinned to "traffic is
# actually done", not to any script's own internal timer guessing when
# that would be. See functions/orchestrator/handler.py's handler() for
# why this replaced a timer-based self-reset.
# Usage: ./scripts/end_round.sh [--stack-name <name>] <table_id>
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
Usage: end_round.sh [options] <table_id>
  --stack-name <name>     CloudFormation stack name (default: isc2-gateway-exercise)
  -h, --help              Show this help
EOF
}

POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --stack-name=*) STACK_NAME="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; POSITIONAL+=("$@"); break ;;
    -*) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
if [ "${#POSITIONAL[@]}" -gt 0 ]; then
  set -- "${POSITIONAL[@]}"
else
  set --
fi

TABLE_ID="${1:?Usage: end_round.sh [options] <table_id>}"

require_aws_credentials "ending the round"
require_permissions

API_URL=$(stack_output ApiUrl)
API_KEY_ID=$(stack_output FacilitatorApiKeyId)
API_KEY_VALUE=$(aws apigateway get-api-key --api-key "$API_KEY_ID" --include-value --query 'value' --output text)

RESPONSE_BODY_FILE="$(mktemp)"
trap 'rm -f "$RESPONSE_BODY_FILE"' EXIT
HTTP_STATUS="$(curl -sS -o "$RESPONSE_BODY_FILE" -w '%{http_code}' -X POST "$API_URL/end-round" \
  -H "x-api-key: $API_KEY_VALUE" \
  -H "Content-Type: application/json" \
  -d "$(python3 -c '
import json, sys
print(json.dumps({"table_id": sys.argv[1]}))
' "$TABLE_ID")")"
cat "$RESPONSE_BODY_FILE"
echo
if [ "$HTTP_STATUS" != "200" ]; then
  echo "ERROR: /end-round returned HTTP $HTTP_STATUS (expected 200) - defenses may NOT have been reset." >&2
  exit 1
fi
