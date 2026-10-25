#!/usr/bin/env bash
# Quick end-to-end check that the deployed stack's auth chain actually
# works - fetches one real M2M token for the good client and hits all
# three services with it - before trusting the stack to the blue agent
# or the load testers. Run `source scripts/load_env.sh` first.
#
# Usage: ./scripts/smoke_test.sh [table_id]
set -euo pipefail

: "${API_URL:?Run 'source scripts/load_env.sh' first}"
: "${TOKEN_URL:?Run 'source scripts/load_env.sh' first}"
: "${GOOD_CLIENT_ID:?Run 'source scripts/load_env.sh' first}"
: "${GOOD_CLIENT_SECRET:?Run 'source scripts/load_env.sh' first}"

TABLE_ID="${1:-smoketest}"

echo "Fetching a token for the good client..."
TOKEN="$(
  curl -sS -u "$GOOD_CLIENT_ID:$GOOD_CLIENT_SECRET" "$TOKEN_URL" \
    -d grant_type=client_credentials \
    -d 'scope=gateway/service1.access gateway/service2.access gateway/service3.access' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))'
)"

if [ -z "$TOKEN" ]; then
  echo "ERROR: could not fetch a token - check the client id/secret and scopes." >&2
  exit 1
fi
echo "Token OK."
echo

FAILED=0
for svc in service1 service2 service3; do
  body_file="$(mktemp)"
  status="$(curl -sS -o "$body_file" -w '%{http_code}' "$API_URL/$svc/ping" \
    -H "Authorization: Bearer $TOKEN" \
    -H "X-Expected: true" \
    -H "X-Table-Id: $TABLE_ID")"
  body="$(cat "$body_file")"
  rm -f "$body_file"
  if [ "$status" = "200" ]; then
    echo "OK   $svc -> $status $body"
  else
    echo "FAIL $svc -> $status $body"
    FAILED=1
  fi
done

echo
if [ "$FAILED" = "0" ]; then
  echo "Auth chain confirmed working: Cognito -> API Gateway -> Lambda -> DynamoDB."
else
  echo "At least one service failed - check CloudWatch Logs for that service's Lambda." >&2
  exit 1
fi
