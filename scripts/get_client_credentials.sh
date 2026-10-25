#!/usr/bin/env bash
# Prints the Cognito client_id/client_secret pairs for the good and
# adversarial app clients, for use by the Gatling simulation and
# load-testers/adversarial-traffic/replay_attack.py. CloudFormation doesn't surface client
# secrets as stack outputs, so this reads them directly from Cognito.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
Usage: get_client_credentials.sh [options]
  --stack-name <name>   CloudFormation stack name (default: isc2-gateway-exercise)
  -h, --help            Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --stack-name=*) STACK_NAME="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

require_aws_credentials "running this"
require_permissions

USER_POOL_ID=$(stack_output UserPoolId)
GOOD_CLIENT_ID=$(stack_output GoodClientId)
ADVERSARIAL_CLIENT_ID=$(stack_output AdversarialClientId)

echo "Good client:"
aws cognito-idp describe-user-pool-client --user-pool-id "$USER_POOL_ID" --client-id "$GOOD_CLIENT_ID" \
  --query 'UserPoolClient.{ClientId:ClientId,ClientSecret:ClientSecret}' --output table

echo "Adversarial client:"
aws cognito-idp describe-user-pool-client --user-pool-id "$USER_POOL_ID" --client-id "$ADVERSARIAL_CLIENT_ID" \
  --query 'UserPoolClient.{ClientId:ClientId,ClientSecret:ClientSecret}' --output table
