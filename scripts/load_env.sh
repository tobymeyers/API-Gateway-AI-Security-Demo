#!/usr/bin/env bash
# Source this (don't execute it) to load this stack's outputs and client
# secrets directly from AWS into your current shell, instead of
# copy-pasting them by hand - avoids both the "<placeholder>"
# shell-redirection trap and raw secrets sitting in your terminal
# scrollback/chat transcript longer than they need to.
#
# Must be run from the project root (same assumption every other script
# here already makes): `source scripts/load_env.sh [--stack-name NAME]`
#
# Deliberately does NOT use the `${BASH_SOURCE[0]}`-relative-path trick
# the executed (bash-shebang) scripts use to be location-independent -
# that only works when a bash shebang actually spawns a bash process.
# Sourcing instead runs in whatever shell/harness is calling it, and
# BASH_SOURCE's behavior for a sourced (not executed) file is not
# reliably portable across those - confirmed live, not hypothetical.
# A plain CWD-relative path sidesteps the whole problem.
#
# If you source this by accident without being in the project root,
# nothing is exported and you'll see the error below - harmless, just cd
# and retry.

if [ ! -f "scripts/lib.sh" ]; then
  echo "ERROR: run this from the project root, e.g.: source scripts/load_env.sh" >&2
  return 1
fi
source "scripts/lib.sh"

while [ $# -gt 0 ]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --stack-name=*) STACK_NAME="${1#*=}"; shift ;;
    *) echo "Unknown option: $1" >&2; return 1 ;;
  esac
done

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  echo "ERROR: no valid AWS credentials - run 'aws configure' first." >&2
  return 1
fi

API_URL="$(stack_output ApiUrl)"
if [ -z "$API_URL" ]; then
  echo "ERROR: couldn't read stack outputs for '$STACK_NAME' - has it deployed successfully?" >&2
  return 1
fi
TOKEN_URL="$(stack_output CognitoTokenEndpoint)"
USER_POOL_ID="$(stack_output UserPoolId)"
GOOD_CLIENT_ID="$(stack_output GoodClientId)"
ADVERSARIAL_CLIENT_ID="$(stack_output AdversarialClientId)"
STATE_TABLE_NAME="$(stack_output StateTableName)"
DASHBOARD_URL="$(stack_output DashboardUrl)"
FACILITATOR_API_KEY_ID="$(stack_output FacilitatorApiKeyId)"
GOOD_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client --user-pool-id "$USER_POOL_ID" --client-id "$GOOD_CLIENT_ID" --query 'UserPoolClient.ClientSecret' --output text)"
ADVERSARIAL_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client --user-pool-id "$USER_POOL_ID" --client-id "$ADVERSARIAL_CLIENT_ID" --query 'UserPoolClient.ClientSecret' --output text)"

export API_URL TOKEN_URL USER_POOL_ID GOOD_CLIENT_ID GOOD_CLIENT_SECRET ADVERSARIAL_CLIENT_ID ADVERSARIAL_CLIENT_SECRET STATE_TABLE_NAME DASHBOARD_URL FACILITATOR_API_KEY_ID

echo "Loaded into this shell: API_URL, TOKEN_URL, USER_POOL_ID, GOOD_CLIENT_ID, GOOD_CLIENT_SECRET, ADVERSARIAL_CLIENT_ID, ADVERSARIAL_CLIENT_SECRET, STATE_TABLE_NAME, DASHBOARD_URL, FACILITATOR_API_KEY_ID"
echo "(secret values aren't printed here - echo \$ADVERSARIAL_CLIENT_SECRET yourself if you need to see one)"
