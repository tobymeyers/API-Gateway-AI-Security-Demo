#!/usr/bin/env bash
# Full gameday rehearsal: deploy once, run every table in a roster CSV
# back-to-back with REAL traffic (Gatling good-traffic + the real fixed
# attack script replay, both concurrent with the blue agent's live
# round), then tear down once at the end.
#
# Unlike comprehensive_test.sh (one short synthetic-traffic round, meant
# to be re-run cheaply during development), this is meant to be the dry
# run immediately before the real event: real round length (60s), the
# real attack script, the real Gatling simulation, one table at a time,
# in roster order - so you see the whole day's timing and cost before
# doing it for real.
#
# Roster CSV format (header required):
#   table_id,prompt
#   1,"Watch for clients whose request pattern looks scripted..."
#   2,"Be conservative - only block with strong evidence..."
# See scripts/examples/sample_roster.csv for a working example. Prompts may
# contain commas/newlines as long as they're CSV-quoted - parsed by
# scripts/lib/roster.py (Python's csv module), not naive shell splitting.
#
# A failure on one table (failed round trigger, failed replay, config
# not reset in time, orchestrator health check) is logged as FAIL and
# does NOT abort the rest of the roster - each table_id is an independent
# DynamoDB config row, so one table's trouble can't bleed into the next
# table's round. The script's own exit code is non-zero if ANY step
# across the whole run failed, so CI/a facilitator still sees it at a
# glance.
#
# Shares its run_step/logging/teardown-on-exit/cost-estimate machinery
# with scripts/comprehensive_test.sh via scripts/test_harness_lib.sh -
# see that file (and scripts/lib/*.py) rather than this one for how any
# of those actually work; this script is just the sequence of steps
# specific to a full roster rehearsal.
#
# Safety: same as comprehensive_test.sh - a trap runs teardown.sh on ANY
# exit (success, failure, Ctrl-C) unless --skip-teardown was passed.
#
# Usage:
#   ./scripts/comprehensive_gameday.sh --roster scripts/examples/sample_roster.csv
#   ./scripts/comprehensive_gameday.sh --roster roster.csv --attack-script attack-plans/attack-plan-20261024-130626.json
#   ./scripts/comprehensive_gameday.sh --roster roster.csv --delete-access-key
#   ./scripts/comprehensive_gameday.sh --roster roster.csv --skip-teardown   # debugging only
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/test_harness_lib.sh"

ROSTER=""
ATTACK_SCRIPT=""
ROUND_SECONDS=60
# 8, not 20 - see run_round.sh's TARGET_RPS comment: this account's Lambda
# concurrent-executions quota is 10 (a new-account default, per AWS docs -
# it auto-raises with usage), and 20 drove combined concurrency across
# this stack's 6 Lambda functions to ~20, causing real throttles that
# surfaced as misleading 500s on adversarial requests.
TARGET_RPS=8
ORCHESTRATOR_SMOKE_SECONDS=30
KEEP_ACCESS_KEY=true
SKIP_TEARDOWN=false
RUN_ID="${RUN_ID:-$(date -u +%Y%m%d)-$(openssl rand -hex 4)}"

usage() {
  cat <<'EOF'
Usage: comprehensive_gameday.sh --roster <csv> [options]
  --roster <csv>              Required. CSV with header "table_id,prompt" - see
                               scripts/examples/sample_roster.csv
  --attack-script <path>       Fixed attack script to replay for every table
                               (default: the most recently generated one in
                               attack-plans/ - see scripts/lib/attack_plans.py)
  --round-seconds <n>          Round length per table (default: 60, the real
                               event length - capped repo-wide to control Bedrock cost)
  --target-rps <n>             Good Load Tester rate, passed to Gatling as
                               target.rps (default: 8 - see TARGET_RPS above)
  --run-id <id>                Cost-allocation Project_Run tag + log filename
                               (default: generated)
  --delete-access-key          Full teardown including the IAM access key at the
                               end (default: kept, same reasoning as
                               comprehensive_test.sh)
  --skip-teardown              DANGER: leaves the stack running on exit, even on
                               failure. Tear down manually afterward.
  -h, --help                   Show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --roster) ROSTER="$2"; shift 2 ;;
    --roster=*) ROSTER="${1#*=}"; shift ;;
    --attack-script) ATTACK_SCRIPT="$2"; shift 2 ;;
    --attack-script=*) ATTACK_SCRIPT="${1#*=}"; shift ;;
    --round-seconds) ROUND_SECONDS="$2"; shift 2 ;;
    --round-seconds=*) ROUND_SECONDS="${1#*=}"; shift ;;
    --target-rps) TARGET_RPS="$2"; shift 2 ;;
    --target-rps=*) TARGET_RPS="${1#*=}"; shift ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --run-id=*) RUN_ID="${1#*=}"; shift ;;
    --delete-access-key) KEEP_ACCESS_KEY=false; shift ;;
    --skip-teardown) SKIP_TEARDOWN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [ -z "$ROSTER" ]; then
  echo "ERROR: --roster <csv> is required." >&2
  usage
  exit 1
fi
if [ ! -f "$ROSTER" ]; then
  echo "ERROR: roster file not found: $ROSTER" >&2
  exit 1
fi
if [ -z "$ATTACK_SCRIPT" ]; then
  ATTACK_SCRIPT="$(resolve_attack_plan)" || exit 1
fi
if [ ! -f "$ATTACK_SCRIPT" ]; then
  echo "ERROR: attack script not found: $ATTACK_SCRIPT" >&2
  echo "Generate it first (requires a deployed stack): ./scripts/generate_attack_script.sh --verify-round-seconds $ROUND_SECONDS" >&2
  exit 1
fi

# Fail fast, before deploying anything, if this script's own pacing won't
# fit in the chosen round length - otherwise every single table's replay
# would get silently truncated by the run_with_timeout wrapper below
# (ROUND_SECONDS+60s), each one only discovered after paying for a full
# round. Also reports the real adversarial request count, purely
# informational (scoring no longer needs a baseline count - see
# scripts/lib/scoring.py's docstring). See scripts/lib/attack_script_check.py
# for the actual logic (unit-tested).
# Note: stderr is intentionally NOT captured here (only stdout, via the
# command substitution below) - this runs before the exec/tee redirection
# further down, so any ERROR message the script prints on stderr goes
# straight to the terminal as normal, same as every other preflight check
# above. Only the two stdout lines (request count, total delay) are data
# we need to capture.
ATTACK_SCRIPT_CHECK="$(python3 "$SCRIPT_LIB_PY_DIR/attack_script_check.py" "$ATTACK_SCRIPT" --round-seconds "$ROUND_SECONDS")"
ATTACK_SCRIPT_CHECK_STATUS=$?
DETECTED_ADVERSARIAL_COUNT="$(echo "$ATTACK_SCRIPT_CHECK" | sed -n '1p')"
ATTACK_SCRIPT_TOTAL_DELAY="$(echo "$ATTACK_SCRIPT_CHECK" | sed -n '2p')"
echo "Attack script: $DETECTED_ADVERSARIAL_COUNT requests, ${ATTACK_SCRIPT_TOTAL_DELAY}s total delay_seconds."
if [ "$ATTACK_SCRIPT_CHECK_STATUS" -ne 0 ]; then
  exit 1
fi

LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/gameday-$RUN_ID.log"
SUMMARY_FILE="$LOG_DIR/gameday-$RUN_ID.summary.jsonl"
RUN_LABEL="GAMEDAY RUN"

# Every subsequent line goes to both the terminal and the log file - same
# convention as comprehensive_test.sh, so this log is a standalone record.
exec > >(tee -a "$LOG_FILE") 2>&1

TEARDOWN_DONE=false
ANY_STEP_FAILED=false
trap on_exit EXIT

log_section "ISC2 Gateway Exercise - Gameday Run $RUN_ID"
echo "Roster:           $ROSTER"
echo "Attack script:    $ATTACK_SCRIPT"
echo "Round length:     ${ROUND_SECONDS}s per table"
echo "Target good RPS:  $TARGET_RPS"
echo "Log file:         $LOG_FILE"
echo "Summary file:     $SUMMARY_FILE"

log_section "0. Preflight: local deps (anthropic, requests) + sbt on PATH"
if ! python3 -c "import anthropic, requests" 2>/dev/null; then
  echo "FAIL: 'anthropic' and/or 'requests' not importable in this Python environment." >&2
  echo "Run: pip install -r \"$ROOT_DIR/load-testers/adversarial-traffic/requirements.txt\"" >&2
  exit 1
fi
if ! command -v sbt >/dev/null 2>&1; then
  echo "FAIL: 'sbt' not found on PATH - required to run the real Good Load Tester." >&2
  exit 1
fi
echo "OK: anthropic + requests importable, sbt on PATH."

log_section "1. Parse roster"
ROSTER_JSONL="$LOG_DIR/gameday-$RUN_ID.roster.jsonl"
run_step "parse_roster" python3 "$SCRIPT_LIB_PY_DIR/roster.py" "$ROSTER" "$ROSTER_JSONL" || exit 1

log_section "2. Deploy"
run_step "deploy" "$ROOT_DIR/scripts/deploy.sh" --run-id "$RUN_ID" || exit 1

log_section "3. Load stack outputs + credentials"
API_URL="$(stack_output ApiUrl)"
TOKEN_URL="$(stack_output CognitoTokenEndpoint)"
USER_POOL_ID="$(stack_output UserPoolId)"
GOOD_CLIENT_ID="$(stack_output GoodClientId)"
ADVERSARIAL_CLIENT_ID="$(stack_output AdversarialClientId)"
STATE_TABLE_NAME="$(stack_output StateTableName)"
DASHBOARD_URL="$(stack_output DashboardUrl)"
GOOD_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client --user-pool-id "$USER_POOL_ID" --client-id "$GOOD_CLIENT_ID" --query 'UserPoolClient.ClientSecret' --output text)"
ADVERSARIAL_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client --user-pool-id "$USER_POOL_ID" --client-id "$ADVERSARIAL_CLIENT_ID" --query 'UserPoolClient.ClientSecret' --output text)"
# smoke_test.sh (next step) reads these as environment variables, not
# arguments - it's designed to be run after `source scripts/load_env.sh`
# by a human, so it needs them exported, not just set as local shell
# vars. ADVERSARIAL_CLIENT_ID/SECRET are resolved above only to sanity-
# check the deploy below (does the adversarial client's secret resolve
# at all) - run_round.sh re-resolves its own copy independently per
# table, so these two are deliberately NOT exported; nothing downstream
# reads them from the environment.
export API_URL TOKEN_URL GOOD_CLIENT_ID GOOD_CLIENT_SECRET
if [ -z "$API_URL" ] || [ -z "$STATE_TABLE_NAME" ] || [ -z "$GOOD_CLIENT_SECRET" ] || [ -z "$ADVERSARIAL_CLIENT_SECRET" ]; then
  echo "FAIL: one or more stack outputs/secrets came back empty - deploy may not have finished cleanly." >&2
  exit 1
fi
echo "ApiUrl:         $API_URL"
echo "DashboardUrl:   $DASHBOARD_URL"
echo "StateTableName: $STATE_TABLE_NAME"
echo "(secrets loaded, not printed)"

log_section "4. Smoke test (auth chain: Cognito -> API Gateway -> Lambda -> DynamoDB)"
run_step "smoke_test" "$ROOT_DIR/scripts/smoke_test.sh" "gameday-smoketest" || exit 1

ORCHESTRATOR_FUNCTION_NAME="$(aws cloudformation describe-stack-resource \
  --stack-name "$STACK_NAME" --logical-resource-id OrchestratorFunction \
  --region "$AWS_REGION" --query 'StackResourceDetail.PhysicalResourceId' --output text)"

log_section "4a. Preflight: --round-seconds fits the orchestrator Lambda's actual Timeout"
run_step "round_seconds_fits_lambda_timeout" check_round_seconds_fits_lambda_timeout "$ROUND_SECONDS" || exit 1

log_section "4b. Orchestrator smoke test ($ORCHESTRATOR_SMOKE_SECONDS s, no Gatling/replay - just confirms the blue agent actually calls Bedrock successfully)"
# Deliberately separate from table 1's real round: this is cheap (one
# short round, no traffic generation) and catches a fully broken
# orchestrator in ~30s/~$0.02 instead of discovering it only after the
# full roster (every table's real traffic + a full round length each)
# has already run to completion.
SMOKE_TABLE_ID="gameday-orchestrator-smoketest"
SMOKE_START_MS=$(($(date +%s) * 1000))
run_step "orchestrator_smoke_trigger_agent" "$ROOT_DIR/scripts/trigger_agent.sh" \
  --round-seconds "$ORCHESTRATOR_SMOKE_SECONDS" "$SMOKE_TABLE_ID" \
  "Automated pre-flight smoke test - hold steady, do not change anything." || exit 1
echo "Waiting ${ORCHESTRATOR_SMOKE_SECONDS}s for the smoke round to run..."
sleep "$ORCHESTRATOR_SMOKE_SECONDS"
if ! run_step "orchestrator_smoke_health_check" check_orchestrator_health "$SMOKE_TABLE_ID" "$SMOKE_START_MS"; then
  echo "FAIL: the orchestrator is not working - aborting before spending the full (expensive) roster run on it." >&2
  exit 1
fi

RUN_START_EPOCH_MS=$(($(date +%s) * 1000))
TABLE_COUNT=$(wc -l < "$ROSTER_JSONL" | tr -d ' ')

log_section "5. Run every table in roster order ($TABLE_COUNT total, ~${ROUND_SECONDS}s each)"

# Read the roster from fd 3, not stdin (fd 0) - if sbt or any other
# command inside this loop body ever reads from its own stdin (sbt's
# launcher can prompt on first-ever plugin resolution), it would
# otherwise silently steal lines meant for this loop's `read`, skipping
# roster rows with no error. Confirmed live: without this, an inner
# command doing one `read` mid-loop causes the very next roster row to
# vanish silently. Keeping the roster on fd 3 means anything inside the
# loop that touches stdin reads the script's own real stdin instead,
# never the roster file.
i=0
exec 3< "$ROSTER_JSONL"
while IFS= read -r row_json <&3; do
  i=$((i + 1))
  TABLE_ID="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['table_id'])" "$row_json")"
  PROMPT="$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['prompt'])" "$row_json")"

  log_section "Table $i/$TABLE_COUNT: table_id=$TABLE_ID"
  echo "Prompt: $PROMPT"

  # run_round.sh is the same "start the agent + real good traffic + real
  # attack replay, concurrently, then verify/score" sequence this loop
  # used to run inline (trigger_agent, verify_config_reset,
  # orchestrator_health, score_table were all duplicated here and in
  # run_round.sh before this was extracted) - single source of truth now.
  # Not `|| exit`: a failed round for one table shouldn't abort the
  # roster. run_round.sh writes its own full report (logs, Gatling/
  # adversarial HTML reports, metadata.json, run_summary.json) under
  # reports/round-table<N>-<run_id>/ too; its stdout is also captured
  # into THIS run's own LOG_FILE via the tee at the top of this script,
  # so ranking.py still finds every table's SCORE_LOG line in one place.
  run_step "round_table${TABLE_ID}" "$ROOT_DIR/scripts/run_round.sh" \
    --round-seconds "$ROUND_SECONDS" --target-rps "$TARGET_RPS" --attack-script "$ATTACK_SCRIPT" \
    "$TABLE_ID" "$PROMPT"

done
exec 3<&-

log_section "5b. Final ranking (by cumulative score, same formula as the ScoringDashboard widget)"
run_step "print_rankings" print_rankings "$LOG_FILE"

log_section "6. Fetch the orchestrator's real token usage from CloudWatch Logs (whole run)"
run_step "fetch_orchestrator_usage_logs" fetch_orchestrator_usage_logs "$ORCHESTRATOR_FUNCTION_NAME" "$RUN_START_EPOCH_MS" "$AWS_REGION"

log_section "7. Real cost estimate for this run (per table + total, no Cost Explorer lag)"
run_step "cost_estimate" cost_estimate "$LOG_FILE"

log_section "8. Tag coverage check (stack still live)"
run_step "check_tags_pre_teardown" "$ROOT_DIR/scripts/check_tags.sh"

log_section "9. Cost check - PRE-teardown snapshot"
echo "(Cost Explorer lags ~24h behind actual usage - this snapshot is for the record.)"
run_step "check_cost_pre_teardown" "$ROOT_DIR/scripts/check_cost.sh" --run-id "$RUN_ID"

log_section "10. Teardown"
maybe_teardown

log_section "11. Cost check - POST-teardown snapshot"
run_step "check_cost_post_teardown" "$ROOT_DIR/scripts/check_cost.sh" --run-id "$RUN_ID"

log_section "12. Tag check - POST-teardown (confirm nothing live was left behind)"
run_step "check_tags_post_teardown" "$ROOT_DIR/scripts/check_tags.sh"
