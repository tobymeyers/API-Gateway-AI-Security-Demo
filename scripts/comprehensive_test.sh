#!/usr/bin/env bash
# End-to-end exercise of the whole system in one run: deploy, smoke test,
# generate a (small, cheap) test attack script, run one short test round
# with concurrent synthetic good + adversarial traffic, verify the config
# actually reset afterward AND that the orchestrator actually did
# something (not just that it was invoked), check cost and tag coverage
# BOTH before and after teardown, then tear down. Every run writes a full
# log + a machine-readable JSON summary under logs/, meant to be readable
# on its own afterward by a human or an AI assistant without re-running
# anything.
#
# This is a TEST run, not a real event round: it uses the same 60s round
# length as a real event (capped repo-wide to control Bedrock cost) but a
# much smaller attack-script budget (default 5 requests, not the real
# 100-150), specifically to keep repeated review/test runs fast and
# cheap. It does NOT touch anything in attack-plans/ - it writes its
# own disposable script under logs/.
#
# Shares its run_step/logging/teardown-on-exit/cost-estimate machinery
# with scripts/comprehensive_gameday.sh via scripts/test_harness_lib.sh - see that
# file (and scripts/lib/*.py) rather than this one for how any of those
# actually work; this script is just the sequence of steps specific to a
# quick dev-loop test run.
#
# Safety: a trap runs teardown.sh on ANY exit (success, failure, Ctrl-C)
# unless --skip-teardown was passed, so a failure partway through this
# script doesn't leave a billable stack running untracked. The IAM
# access key is KEPT by default (--keep-access-key passed to
# teardown.sh) - this script exists to be re-run repeatedly during
# review/development, and deleting the key every single run would mean
# redoing the console-only access-key-creation step every single run
# too. Pass --delete-access-key when you're actually done testing.
#
# Usage:
#   ./scripts/comprehensive_test.sh
#   ./scripts/comprehensive_test.sh --round-seconds 90 --max-attack-requests 8
#   ./scripts/comprehensive_test.sh --delete-access-key   # truly done - also deletes the IAM access key
#   ./scripts/comprehensive_test.sh --skip-teardown       # debugging only - leaves the stack up, see warning below
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/test_harness_lib.sh"

ROUND_SECONDS=60
MAX_ATTACK_REQUESTS=5
KEEP_ACCESS_KEY=true
SKIP_TEARDOWN=false
RUN_ID="${RUN_ID:-$(date -u +%Y%m%d)-$(openssl rand -hex 4)}"

usage() {
  cat <<'EOF'
Usage: comprehensive_test.sh [options]
  --round-seconds <n>         Test round length (default 60, same as the real event length)
  --max-attack-requests <n>   Test attack script size (default 5 - much smaller than the real 100-150)
  --run-id <id>               Cost-allocation Project_Run tag + log filename (default: generated)
  --delete-access-key         Full teardown including the IAM access key (default: kept,
                              since this script is meant to be re-run repeatedly)
  --skip-teardown             DANGER: leaves the stack running on exit, even on failure.
                              Only for debugging a failure in place - you must tear down
                              manually afterward (./scripts/teardown.sh).
  -h, --help                  Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --round-seconds) ROUND_SECONDS="$2"; shift 2 ;;
    --round-seconds=*) ROUND_SECONDS="${1#*=}"; shift ;;
    --max-attack-requests) MAX_ATTACK_REQUESTS="$2"; shift 2 ;;
    --max-attack-requests=*) MAX_ATTACK_REQUESTS="${1#*=}"; shift ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --run-id=*) RUN_ID="${1#*=}"; shift ;;
    --delete-access-key) KEEP_ACCESS_KEY=false; shift ;;
    --skip-teardown) SKIP_TEARDOWN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

TEST_TABLE_ID="comprehensive-test-$RUN_ID"
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/run-$RUN_ID.log"
SUMMARY_FILE="$LOG_DIR/run-$RUN_ID.summary.jsonl"
TEST_ATTACK_SCRIPT="$LOG_DIR/run-$RUN_ID.attack_script.json"
RUN_LABEL="RUN"

# Every subsequent line this script (and everything it calls) prints
# goes to both the terminal and the log file, with no per-line effort -
# this is what makes the log file a complete standalone record.
exec > >(tee -a "$LOG_FILE") 2>&1

TEARDOWN_DONE=false
ANY_STEP_FAILED=false
trap on_exit EXIT

log_section "ISC2 Gateway Exercise - Comprehensive Test Run $RUN_ID"
echo "Test table_id:    $TEST_TABLE_ID"
echo "Round length:     ${ROUND_SECONDS}s (real event rounds are also 60s)"
echo "Attack script:    max $MAX_ATTACK_REQUESTS requests (real event scripts are ~10)"
echo "Log file:         $LOG_FILE"
echo "Summary file:     $SUMMARY_FILE"

log_section "0. Preflight: local Python deps for load-testers/adversarial-traffic/*.py"
if ! python3 -c "import anthropic, requests" 2>/dev/null; then
  echo "FAIL: 'anthropic' and/or 'requests' not importable in this Python environment." >&2
  echo "Run: pip install -r \"$ROOT_DIR/load-testers/adversarial-traffic/requirements.txt\"" >&2
  exit 1
fi
echo "OK: anthropic + requests importable."

log_section "1. Deploy"
run_step "deploy" "$ROOT_DIR/scripts/deploy.sh" --run-id "$RUN_ID" || exit 1

log_section "2. Load stack outputs + credentials"
API_URL="$(stack_output ApiUrl)"
TOKEN_URL="$(stack_output CognitoTokenEndpoint)"
USER_POOL_ID="$(stack_output UserPoolId)"
GOOD_CLIENT_ID="$(stack_output GoodClientId)"
ADVERSARIAL_CLIENT_ID="$(stack_output AdversarialClientId)"
STATE_TABLE_NAME="$(stack_output StateTableName)"
GOOD_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client --user-pool-id "$USER_POOL_ID" --client-id "$GOOD_CLIENT_ID" --query 'UserPoolClient.ClientSecret' --output text)"
ADVERSARIAL_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client --user-pool-id "$USER_POOL_ID" --client-id "$ADVERSARIAL_CLIENT_ID" --query 'UserPoolClient.ClientSecret' --output text)"
export API_URL TOKEN_URL GOOD_CLIENT_ID GOOD_CLIENT_SECRET ADVERSARIAL_CLIENT_ID ADVERSARIAL_CLIENT_SECRET
if [ -z "$API_URL" ] || [ -z "$STATE_TABLE_NAME" ] || [ -z "$GOOD_CLIENT_SECRET" ] || [ -z "$ADVERSARIAL_CLIENT_SECRET" ]; then
  echo "FAIL: one or more stack outputs/secrets came back empty - deploy may not have finished cleanly." >&2
  exit 1
fi
echo "ApiUrl:         $API_URL"
echo "StateTableName: $STATE_TABLE_NAME"
echo "(secrets loaded, not printed)"

log_section "3. Smoke test (auth chain: Cognito -> API Gateway -> Lambda -> DynamoDB)"
run_step "smoke_test" "$ROOT_DIR/scripts/smoke_test.sh" "$TEST_TABLE_ID" || exit 1

ORCHESTRATOR_FUNCTION_NAME="$(aws cloudformation describe-stack-resource \
  --stack-name "$STACK_NAME" --logical-resource-id OrchestratorFunction \
  --region "$AWS_REGION" --query 'StackResourceDetail.PhysicalResourceId' --output text)"

log_section "3b. Preflight: --round-seconds fits the orchestrator Lambda's actual Timeout"
run_step "round_seconds_fits_lambda_timeout" check_round_seconds_fits_lambda_timeout "$ROUND_SECONDS" || exit 1

log_section "4. Generate a small test attack script (NOT the real event's attack-plans/ file)"
# --max-requests AND --max-total-seconds both bound generation now (see
# generate_attack_script.py) - still wrapped in a timeout here as defense
# in depth against model latency itself (not pacing) running long.
# 300s is generous for a 5-request budget.
run_step "generate_attack_script" run_with_timeout 300 python3 "$ROOT_DIR/load-testers/adversarial-traffic/generate_attack_script.py" \
  --staging-url "$API_URL" --token-url "$TOKEN_URL" \
  --client-id "$ADVERSARIAL_CLIENT_ID" --client-secret "$ADVERSARIAL_CLIENT_SECRET" \
  --max-requests "$MAX_ATTACK_REQUESTS" --output "$TEST_ATTACK_SCRIPT" || exit 1
echo "Wrote $TEST_ATTACK_SCRIPT"

log_section "5. Run one short test round (synthetic good traffic + adversarial replay, concurrently)"

synthetic_good_traffic() {
  local token
  token="$(curl -sS -u "$GOOD_CLIENT_ID:$GOOD_CLIENT_SECRET" "$TOKEN_URL" \
    -d grant_type=client_credentials \
    -d 'scope=gateway/service1.access gateway/service2.access gateway/service3.access' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token",""))')"
  if [ -z "$token" ]; then
    echo "synthetic good traffic: FAILED to fetch a token - sending nothing this round (not aborting the whole test for this)." >&2
    return 1
  fi
  local services=(service1 service2 service3)
  local elapsed=0
  while [ "$elapsed" -lt "$ROUND_SECONDS" ]; do
    local svc="${services[$((RANDOM % 3))]}"
    curl -sS -o /dev/null "$API_URL/$svc/ping" \
      -H "Authorization: Bearer $token" -H "X-Expected: true" -H "X-Table-Id: $TEST_TABLE_ID" &
    sleep 1
    elapsed=$((elapsed + 1))
  done
  wait
  echo "synthetic good traffic: sent ~${ROUND_SECONDS} requests over ${ROUND_SECONDS}s"
}

# Captured before trigger_agent.sh so the CloudWatch Logs fetch in step 7
# below can scope its query to exactly this round, not the log group's
# entire history (relevant if this stack/function name is ever reused).
ROUND_START_EPOCH_MS=$(($(date +%s) * 1000))

run_step "trigger_agent" "$ROOT_DIR/scripts/trigger_agent.sh" --round-seconds "$ROUND_SECONDS" \
  "$TEST_TABLE_ID" "Rate-limit any client whose request pattern looks scripted or unusually regular; leave everything else alone." || exit 1

# Fixed head start before any traffic exists - see run_round.sh's
# AGENT_STARTUP_DELAY_S for why (Lambda cold start + first uncached
# Bedrock call otherwise land on live, unprotected traffic).
echo "Waiting 10s for the agent to clear cold start before launching traffic..."
sleep 10

synthetic_good_traffic &
GOOD_TRAFFIC_PID=$!
# Not `|| exit 1` - a failed replay shouldn't abort the whole run (the
# round itself may still be worth inspecting), but it must still show up
# as FAIL in the summary rather than being silently swallowed.
run_step "replay_attack" run_with_timeout $((ROUND_SECONDS + 60)) python3 "$ROOT_DIR/load-testers/adversarial-traffic/replay_attack.py" \
  --script "$TEST_ATTACK_SCRIPT" --gateway-url "$API_URL" --table-id "$TEST_TABLE_ID" \
  --token-url "$TOKEN_URL" --client-id "$ADVERSARIAL_CLIENT_ID" --client-secret "$ADVERSARIAL_CLIENT_SECRET"
wait "$GOOD_TRAFFIC_PID"
ROUND_END_EPOCH_MS=$(($(date +%s) * 1000))

log_section "6. End the round, then verify config actually reset"
# Both traffic generators' real exit codes are in hand above this line -
# end_round.sh is the deterministic signal that resets defenses (see
# functions/orchestrator/handler.py's action == "end_round" branch), no
# longer a timer inside the orchestrator racing against traffic that
# isn't pinned to the same clock. verify_config_reset below is now
# confirming a reset we know already happened, not polling to catch one.
run_step "end_round" "$ROOT_DIR/scripts/end_round.sh" "$TEST_TABLE_ID"
run_step "verify_config_reset" verify_config_reset "$STATE_TABLE_NAME" "$TEST_TABLE_ID" "$AWS_REGION" "$ROUND_SECONDS"

log_section "6b. Orchestrator health check (did it actually call Bedrock successfully, not just get invoked)"
# See scripts/lib/orchestrator_health.py's docstring: verify_config_reset
# above can't tell "ran fine and reset cleanly" apart from "crashed before
# ever writing a config row" - both look like baseline. This check reads
# the orchestrator's own CloudWatch Logs instead, which can't be faked.
run_step "orchestrator_health" check_orchestrator_health "$TEST_TABLE_ID" "$ROUND_START_EPOCH_MS"

log_section "6c. Score this round (same cumulative formula as the live ScoringDashboard widget)"
run_step "score_table" score_table "$TEST_TABLE_ID" "$ROUND_START_EPOCH_MS" "$ROUND_END_EPOCH_MS"
run_step "print_rankings" print_rankings "$LOG_FILE"

log_section "7. Fetch the orchestrator's real token usage from CloudWatch Logs"
run_step "fetch_orchestrator_usage_logs" fetch_orchestrator_usage_logs "$ORCHESTRATOR_FUNCTION_NAME" "$ROUND_START_EPOCH_MS" "$AWS_REGION"

log_section "8. Real cost estimate for this run (no Cost Explorer lag - uses actual token counts)"
run_step "cost_estimate" cost_estimate "$LOG_FILE"

log_section "9. Tag coverage check (stack still live)"
run_step "check_tags_pre_teardown" "$ROOT_DIR/scripts/check_tags.sh"

log_section "10. Cost check - PRE-teardown snapshot"
echo "(Cost Explorer lags ~24h behind actual usage - this snapshot is for the record, not an instant total.)"
run_step "check_cost_pre_teardown" "$ROOT_DIR/scripts/check_cost.sh" --run-id "$RUN_ID"

log_section "11. Teardown"
maybe_teardown

log_section "12. Cost check - POST-teardown snapshot"
echo "(Expected to look the same as the pre-teardown snapshot right now - same ~24h lag applies"
echo " to the deletion too. Re-run this same command tomorrow to see the real settled total.)"
run_step "check_cost_post_teardown" "$ROOT_DIR/scripts/check_cost.sh" --run-id "$RUN_ID"

log_section "13. Tag check - POST-teardown (confirm nothing live was left behind)"
run_step "check_tags_post_teardown" "$ROOT_DIR/scripts/check_tags.sh"
