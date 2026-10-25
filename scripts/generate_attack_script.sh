#!/usr/bin/env bash
# Thin wrapper around load-testers/adversarial-traffic/generate_attack_script.py: resolves the live
# stack's --staging-url/--token-url/adversarial client credentials
# automatically (same stack_output + describe-user-pool-client calls
# comprehensive_test.sh already does inline for its own disposable test
# script), instead of typing all five out by hand every time - see
# DEPLOYMENT.md's "Generate the fixed attack script" step for the manual
# version this replaces.
#
# Requires a stack already deployed - does NOT deploy one itself.
#
# Owns the cost-protection policy generate_attack_script.py itself no
# longer does: every generation writes a brand-new, timestamped file
# (attack-plans/attack-plan-<run_id>.json, never overwritten - see
# scripts/lib/attack_plans.py), so by DEFAULT this reuses whatever's
# already the latest one instead of spending real Bedrock tokens again -
# pass --generate-new to actually generate a fresh one.
#
# Usage:
#   ./scripts/generate_attack_script.sh                      # reuses the latest, or generates
#                                                              # the first one if none exist yet
#   ./scripts/generate_attack_script.sh --generate-new --verify-round-seconds 60
#   ./scripts/generate_attack_script.sh --generate-new -- --max-requests 8 --max-total-seconds 20
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

GENERATE_NEW=false
OUTPUT=""
VERIFY_ROUND_SECONDS=""
MIN_REQUESTS=""
EXTRA_ARGS=()

usage() {
  cat <<'EOF'
Usage: generate_attack_script.sh [options] [-- <extra generate_attack_script.py args>]
  --generate-new                Generate a brand-new attack script even if one already
                                exists in attack-plans/ - costs real Bedrock tokens (see
                                generate_attack_script.py's own docstring for real pricing).
                                Without this, and without --output, the wrapper reuses the
                                latest existing script and does nothing else (no AWS calls,
                                no cost) - see scripts/lib/attack_plans.py.
  --output <path>               Write to this exact path instead of the default
                                attack-plans/attack-plan-<run_id>.json convention.
                                Given explicitly like this, always generates fresh at that
                                path (the reuse-latest policy above only applies to the
                                default convention path).
  --min-requests <n>            Forwarded to generate_attack_script.py (tell the model to send
                                at least this many requests) AND to the --verify-round-seconds
                                check below (fail if the result came in under this). Pair with
                                `-- --max-requests <n-or-more>` since --max-requests isn't
                                raised automatically. E.g. for a 100-request floor:
                                  generate_attack_script.sh --generate-new --min-requests 100 \
                                    --verify-round-seconds 60 -- --max-requests 120
  --verify-round-seconds <n>    After generating, run attack_script_check.py against the
                                result with this --round-seconds (and --min-requests, if given
                                above) and print pass/fail immediately - omit to skip
                                verification (default).
  -h, --help                    Show this help

Anything after a literal "--" is forwarded as-is to generate_attack_script.py
(e.g. --max-requests, --max-total-seconds, --model, --scope).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --generate-new) GENERATE_NEW=true; shift ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --output=*) OUTPUT="${1#*=}"; shift ;;
    --min-requests) MIN_REQUESTS="$2"; shift 2 ;;
    --min-requests=*) MIN_REQUESTS="${1#*=}"; shift ;;
    --verify-round-seconds) VERIFY_ROUND_SECONDS="$2"; shift 2 ;;
    --verify-round-seconds=*) VERIFY_ROUND_SECONDS="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; EXTRA_ARGS=("$@"); break ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

# Cheapest check first, no AWS calls at all: if the caller didn't ask for
# a custom --output and didn't force a fresh one, see whether there's
# already a usable script to reuse - the common case, and free.
if [ -z "$OUTPUT" ] && [ "$GENERATE_NEW" != "true" ]; then
  if EXISTING="$(resolve_attack_plan 2>/dev/null)"; then
    echo "Reusing existing attack script (no generation, no cost): $EXISTING"
    echo "  (pass --generate-new to generate a fresh one instead)"
    if [ -n "$VERIFY_ROUND_SECONDS" ]; then
      echo
      echo "Verifying $EXISTING fits --round-seconds $VERIFY_ROUND_SECONDS ..."
      CHECK_ARGS=("$EXISTING" --round-seconds "$VERIFY_ROUND_SECONDS" --describe)
      if [ -n "$MIN_REQUESTS" ]; then
        CHECK_ARGS+=(--min-requests "$MIN_REQUESTS")
      fi
      python3 "$SCRIPT_DIR/lib/attack_script_check.py" "${CHECK_ARGS[@]}"
    fi
    exit 0
  fi
  echo "No existing attack script found in $ATTACK_PLANS_DIR - generating the first one."
fi

if [ -z "$OUTPUT" ]; then
  RUN_ID="${RUN_ID:-$(date -u +%Y%m%d-%H%M%S)}"
  OUTPUT="$ATTACK_PLANS_DIR/attack-plan-$RUN_ID.json"
fi
mkdir -p "$(dirname "$OUTPUT")"

require_aws_credentials "generating an attack script"

# Fail with a clear, actionable message if the stack isn't up, instead of
# letting the first stack_output call's raw AWS CLI ValidationError abort
# the script under set -e - confirmed live: without this check, running
# this wrapper with no stack deployed prints a bare "Stack with id ...
# does not exist" with no indication of what to do about it.
if ! aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "ERROR: stack '$STACK_NAME' not found in $AWS_REGION - deploy it first (./scripts/deploy.sh)." >&2
  exit 1
fi

API_URL="$(stack_output ApiUrl)"
TOKEN_URL="$(stack_output CognitoTokenEndpoint)"
ADVERSARIAL_CLIENT_ID="$(stack_output AdversarialClientId)"
USER_POOL_ID="$(stack_output UserPoolId)"
if [ -z "$API_URL" ] || [ -z "$TOKEN_URL" ] || [ -z "$ADVERSARIAL_CLIENT_ID" ] || [ -z "$USER_POOL_ID" ]; then
  echo "ERROR: one or more stack outputs came back empty - is '$STACK_NAME' deployed in $AWS_REGION?" >&2
  exit 1
fi
ADVERSARIAL_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "$USER_POOL_ID" --client-id "$ADVERSARIAL_CLIENT_ID" \
  --query 'UserPoolClient.ClientSecret' --output text)"

GEN_ARGS=(--staging-url "$API_URL" --token-url "$TOKEN_URL"
  --client-id "$ADVERSARIAL_CLIENT_ID" --client-secret "$ADVERSARIAL_CLIENT_SECRET"
  --output "$OUTPUT")
if [ -n "$MIN_REQUESTS" ]; then
  GEN_ARGS+=(--min-requests "$MIN_REQUESTS")
fi
# Not a plain "${EXTRA_ARGS[@]}": on bash 3.2 (macOS's default, and what
# just crashed this exact line with "unbound variable" under set -u),
# expanding an EMPTY array this way is a hard error - see the same fix in
# trigger_agent.sh.
if [ "${#EXTRA_ARGS[@]}" -gt 0 ]; then
  GEN_ARGS+=("${EXTRA_ARGS[@]}")
fi

echo "Generating $OUTPUT against staging URL $API_URL ..."
python3 "$ROOT_DIR/load-testers/adversarial-traffic/generate_attack_script.py" "${GEN_ARGS[@]}"

if [ -n "$VERIFY_ROUND_SECONDS" ]; then
  echo
  echo "Verifying $OUTPUT fits --round-seconds $VERIFY_ROUND_SECONDS ..."
  CHECK_ARGS=("$OUTPUT" --round-seconds "$VERIFY_ROUND_SECONDS" --describe)
  if [ -n "$MIN_REQUESTS" ]; then
    CHECK_ARGS+=(--min-requests "$MIN_REQUESTS")
  fi
  python3 "$SCRIPT_DIR/lib/attack_script_check.py" "${CHECK_ARGS[@]}"
fi
