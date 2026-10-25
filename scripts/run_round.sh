#!/usr/bin/env bash
# Facilitator tool: run ONE complete real round for a table against an
# ALREADY-DEPLOYED, long-lived stack - the actual event-day primitive.
#
# This does NOT deploy or tear down anything, unlike comprehensive_test.sh/
# comprehensive_gameday.sh - it assumes the stack is already up and will
# stay up for many more rounds, across many tables, for the rest of the
# day. Triggering the agent alone (trigger_agent.sh) only turns it on; a
# round isn't complete without the other two pieces the agent is
# actually supposed to be defending against. This bundles all three,
# concurrently, for the same table_id:
#   1. trigger_agent.sh        - turns on the blue agent
#   2. Good Load Tester         - real Gatling good traffic
#   3. replay_attack.py         - the real, fixed adversarial script
# ...then verifies the agent actually reset config and really called
# Bedrock (not just got invoked), and prints this round's real score -
# the same per-table checks comprehensive_gameday.sh runs during a full
# roster rehearsal, extracted here so a single live round gets them too.
#
# Usage:
#   ./scripts/run_round.sh <table_id> "<prompt text>"
#   ./scripts/run_round.sh --round-seconds 90 --target-rps 15 5 "prompt..."
#   ./scripts/run_round.sh --attack-script other_script.json 7 "prompt..."
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/test_harness_lib.sh"

ROUND_SECONDS=60
# 8, not the original 20: this AWS account's Lambda concurrent-executions
# quota is 10 (confirmed via `aws lambda get-account-settings` and the
# Service Quotas console - new accounts start reduced from AWS's documented
# 1,000 default and scale up automatically with usage, per AWS's own docs).
# At target-rps 20, combined ConcurrentExecutions across all 6 Lambda
# functions in this stack (3 services + authorizer + orchestrator +
# trigger-agent) peaked at ~20 in CloudWatch - double the account's cap -
# and caused real Lambda throttles that API Gateway surfaced as 500s to
# the client, misreadable as the agent's defenses failing to block an
# attack when it was actually an unrelated capacity ceiling. Raise this
# back up only after requesting (and getting) a quota increase via Service
# Quotas - this value has nothing to do with the exercise's own traffic
# design, it's purely this account's current ceiling.
TARGET_RPS=8
ATTACK_SCRIPT=""
AGENT_STARTUP_DELAY_S=10

usage() {
  cat <<'EOF'
Usage: run_round.sh [options] <table_id> <prompt>
  --round-seconds <n>     Round length (default: 60, the real event length -
                          capped repo-wide to control Bedrock cost)
  --target-rps <n>        Good Load Tester rate, passed to Gatling as
                          target.rps (default: 8 - see TARGET_RPS above for
                          why it's not the original 20)
  --attack-script <path>  Fixed attack script to replay (default: the most
                          recently generated one in attack-plans/ - see
                          scripts/lib/attack_plans.py)
  --agent-startup-delay-seconds <n>  Fixed wait after triggering the agent,
                          before any traffic is launched (default: 10).
                          Covers Lambda cold start + the first (uncached)
                          Bedrock call, which otherwise land on top of live
                          traffic with zero defenses in place. This is NOT
                          a guess about how long anything variable-duration
                          takes (that's what end_round.sh replaced) - it's a
                          fixed head start applied before any traffic exists
                          at all, so there's nothing to race against. Does
                          not shrink round_seconds or affect scoring.
  -h, --help              Show this help

Runs one complete round against an ALREADY-DEPLOYED stack: starts the
blue agent, launches real Good Load Tester traffic and the real
adversarial replay concurrently for the same table_id, then checks the
round actually completed cleanly and prints its score. Does NOT deploy
or tear down anything - see comprehensive_test.sh/comprehensive_gameday.sh
for that.
EOF
}

POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    --round-seconds) ROUND_SECONDS="$2"; shift 2 ;;
    --round-seconds=*) ROUND_SECONDS="${1#*=}"; shift ;;
    --target-rps) TARGET_RPS="$2"; shift 2 ;;
    --target-rps=*) TARGET_RPS="${1#*=}"; shift ;;
    --attack-script) ATTACK_SCRIPT="$2"; shift 2 ;;
    --attack-script=*) ATTACK_SCRIPT="${1#*=}"; shift ;;
    --agent-startup-delay-seconds) AGENT_STARTUP_DELAY_S="$2"; shift 2 ;;
    --agent-startup-delay-seconds=*) AGENT_STARTUP_DELAY_S="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; POSITIONAL+=("$@"); break ;;
    -*) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
# Bash 3.2 (macOS default) empty-array-under-set-u guard, same reasoning
# as trigger_agent.sh/score_round.sh.
if [ "${#POSITIONAL[@]}" -gt 0 ]; then
  set -- "${POSITIONAL[@]}"
else
  set --
fi

TABLE_ID="${1:?Usage: run_round.sh [options] <table_id> <prompt>}"
PROMPT="${2:?Usage: run_round.sh [options] <table_id> <prompt>}"

# Cheapest checks first, no AWS calls at all - same reasoning as
# generate_attack_script.sh's existing-file check: don't pay for a
# credentials check or a stack lookup if a free local check is already
# going to fail the whole thing anyway. No --attack-script override given
# -> default to the most recently generated one (scripts/lib/attack_plans.py);
# this is also the whole reason to resolve it to a real path BEFORE
# anything below runs, rather than leaving it to whatever
# generate_attack_script.py defaulted to - metadata.json (and the "Attack
# script:" line below) end up recording exactly which file this round
# actually used.
if [ -z "$ATTACK_SCRIPT" ]; then
  ATTACK_SCRIPT="$(resolve_attack_plan)" || exit 1
fi
if [ ! -f "$ATTACK_SCRIPT" ]; then
  echo "ERROR: attack script not found: $ATTACK_SCRIPT" >&2
  echo "  fix:  ./scripts/generate_attack_script.sh --verify-round-seconds $ROUND_SECONDS" >&2
  exit 1
fi

# Fail fast if this script's own pacing won't fit the chosen round
# length - otherwise the replay would get silently truncated mid-script
# by the run_with_timeout wrapper below, discovered only after the round
# (and its traffic/Bedrock cost) already ran. See
# scripts/lib/attack_script_check.py (unit-tested).
ATTACK_SCRIPT_CHECK="$(python3 "$SCRIPT_LIB_PY_DIR/attack_script_check.py" "$ATTACK_SCRIPT" --round-seconds "$ROUND_SECONDS")"
ATTACK_SCRIPT_CHECK_STATUS=$?
echo "Attack script: $(echo "$ATTACK_SCRIPT_CHECK" | sed -n '1p') requests, $(echo "$ATTACK_SCRIPT_CHECK" | sed -n '2p')s total delay_seconds."
if [ "$ATTACK_SCRIPT_CHECK_STATUS" -ne 0 ]; then
  exit 1
fi

if ! python3 -c "import requests" 2>/dev/null; then
  echo "FAIL: 'requests' not importable in this Python environment." >&2
  echo "Run: pip install -r \"$ROOT_DIR/load-testers/adversarial-traffic/requirements.txt\"" >&2
  exit 1
fi
if ! command -v sbt >/dev/null 2>&1; then
  echo "FAIL: 'sbt' not found on PATH - required to run the real Good Load Tester." >&2
  exit 1
fi

require_aws_credentials "running a round"

if ! aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
  echo "ERROR: stack '$STACK_NAME' not found in $AWS_REGION - deploy it first (./scripts/deploy.sh)." >&2
  exit 1
fi

API_URL="$(stack_output ApiUrl)"
TOKEN_URL="$(stack_output CognitoTokenEndpoint)"
USER_POOL_ID="$(stack_output UserPoolId)"
GOOD_CLIENT_ID="$(stack_output GoodClientId)"
ADVERSARIAL_CLIENT_ID="$(stack_output AdversarialClientId)"
STATE_TABLE_NAME="$(stack_output StateTableName)"
GOOD_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client --user-pool-id "$USER_POOL_ID" --client-id "$GOOD_CLIENT_ID" --query 'UserPoolClient.ClientSecret' --output text)"
ADVERSARIAL_CLIENT_SECRET="$(aws cognito-idp describe-user-pool-client --user-pool-id "$USER_POOL_ID" --client-id "$ADVERSARIAL_CLIENT_ID" --query 'UserPoolClient.ClientSecret' --output text)"
if [ -z "$API_URL" ] || [ -z "$STATE_TABLE_NAME" ] || [ -z "$GOOD_CLIENT_SECRET" ] || [ -z "$ADVERSARIAL_CLIENT_SECRET" ]; then
  echo "ERROR: one or more stack outputs/secrets came back empty - is '$STACK_NAME' deployed cleanly?" >&2
  exit 1
fi

ORCHESTRATOR_FUNCTION_NAME="$(aws cloudformation describe-stack-resource \
  --stack-name "$STACK_NAME" --logical-resource-id OrchestratorFunction \
  --region "$AWS_REGION" --query 'StackResourceDetail.PhysicalResourceId' --output text)"

RUN_ID="${RUN_ID:-$(date -u +%Y%m%d-%H%M%S)}"
# Everything about this one round lives under one folder - logs (for
# debugging this specific run), the Good Load Tester's HTML report, the
# adversarial replay's HTML report, and the two JSON files below - so a
# facilitator reviewing many rounds after the fact has one place per
# round, not three scattered across logs/ and load-testers/'s build output.
REPORT_DIR="$ROOT_DIR/reports/round-table${TABLE_ID}-$RUN_ID"
LOG_DIR="$REPORT_DIR/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/round-table${TABLE_ID}-$RUN_ID.log"
SUMMARY_FILE="$LOG_DIR/round-table${TABLE_ID}-$RUN_ID.summary.jsonl"
# Every subsequent line goes to both the terminal and the log file - same
# convention as comprehensive_test.sh/comprehensive_gameday.sh, so this
# log is a standalone record (and so a caller that tees THIS script's
# own output, like comprehensive_gameday.sh does, still gets every line -
# including the SCORE_LOG line ranking.py looks for - in its own log too).
exec > >(tee -a "$LOG_FILE") 2>&1

ANY_STEP_FAILED=false
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

log_section "Round for table $TABLE_ID"
echo "Prompt:          $PROMPT"
echo "Round length:    ${ROUND_SECONDS}s"
echo "Target good RPS: $TARGET_RPS"
echo "Attack script:   $ATTACK_SCRIPT"
echo "Report folder:   $REPORT_DIR"

# metadata.json is written up front (inputs only - what was asked for),
# before anything that could fail, so even a round that dies partway
# through still leaves behind a record of what was attempted. Results
# (run_summary.json) are assembled at the very end, once there's
# something to summarize - see scripts/lib/round_report.py.
python3 "$SCRIPT_LIB_PY_DIR/round_report.py" metadata \
  --table-id "$TABLE_ID" --prompt "$PROMPT" --round-seconds "$ROUND_SECONDS" \
  --target-rps "$TARGET_RPS" --attack-script "$ATTACK_SCRIPT" --started-at "$STARTED_AT" \
  --output "$REPORT_DIR/metadata.json" >/dev/null

run_step "round_seconds_fits_lambda_timeout" check_round_seconds_fits_lambda_timeout "$ROUND_SECONDS" || exit 1

TABLE_START_MS=$(($(date +%s) * 1000))
run_step "trigger_agent" "$ROOT_DIR/scripts/trigger_agent.sh" --round-seconds "$ROUND_SECONDS" "$TABLE_ID" "$PROMPT"
TRIGGER_AGENT_OK=$?
if [ "$TRIGGER_AGENT_OK" -ne 0 ]; then
  echo "trigger_agent failed - not launching traffic for a round whose agent never started." >&2
  exit 1
fi

# Head start before any traffic exists at all, so Lambda cold start and
# the first (cache-write, not cache-hit) Bedrock call land on dead air
# instead of on live, unprotected requests - confirmed live: the first
# ~8s of a real round were spent on exactly that, during which 7/48
# adversarial requests went through before the agent's first action
# landed. Unlike REPLAY_MAX_DURATION_S below, this is NOT a guess about
# how long something variable-duration takes - it's a fixed wait applied
# before anything variable starts, so there's nothing to race against.
echo "Waiting ${AGENT_STARTUP_DELAY_S}s for the agent to clear cold start before launching traffic..."
sleep "$AGENT_STARTUP_DELAY_S"

# Real Good Load Tester (Gatling), backgrounded - concurrent with the
# agent and the attack replay below, same as a real event round. Routed
# straight to the log file, NOT through the tee above: Gatling's DEBUG-
# level output (tens of thousands of lines) would otherwise interleave
# live, mid-line, with the foreground replay_attack/run_step output above
# it, since this runs concurrently in the background - confirmed to make
# the terminal transcript unreadable, not just long. Still fully captured
# in $LOG_FILE either way.
(
  cd "$ROOT_DIR/load-testers/good-traffic" && run_with_timeout $((ROUND_SECONDS + 120)) \
    sbt "Gatling/testOnly simulations.GoodTrafficSimulation" \
    -Dgateway.url="$API_URL" -Dtoken.url="$TOKEN_URL" \
    -Dclient.id="$GOOD_CLIENT_ID" -Dclient.secret="$GOOD_CLIENT_SECRET" \
    -Dtable.id="$TABLE_ID" -Dround.seconds="$ROUND_SECONDS" -Dtarget.rps="$TARGET_RPS"
  SBT_EXIT=$?
  # Gatling names its own HTML report folder after the simulation class
  # (goodtrafficsimulation-<timestamp>) with no supported way to override
  # that from the outside - the config key that used to do this
  # (outputDirectoryBaseName) was removed upstream (confirmed: Gatling's
  # own community forum says it "will not be restored"). Move the newest
  # matching folder into this round's own report folder after the fact
  # instead, so it's archived alongside the adversarial report and
  # metadata rather than left in target/ (a build-output directory other
  # tools may clean, and gitignored besides).
  LATEST_REPORT_DIR="$(ls -td target/gatling/goodtrafficsimulation-* 2>/dev/null | head -1)"
  if [ -n "$LATEST_REPORT_DIR" ]; then
    mv "$LATEST_REPORT_DIR" "$REPORT_DIR/good-traffic-report"
    echo "Gatling report: $REPORT_DIR/good-traffic-report/index.html"
  fi
  exit $SBT_EXIT
) >> "$LOG_FILE" 2>&1 &
GOOD_TRAFFIC_PID=$!
GOOD_TRAFFIC_T0=$(date +%s)
echo "(Good Load Tester running in the background - its full Gatling output is in $LOG_FILE, not shown here.)"

# Caps replay_attack's own wall-clock send window as a sane upper bound
# (e.g. a malformed attack plan with a runaway delay_seconds value)-
# NOT what keeps defenses active for the real traffic window anymore.
# That's now end_round below, called only after both traffic generators'
# real exit codes are in hand - see handler.py's handler() for why a
# timer-based reset (this script's previous approach) raced against
# traffic that wasn't pinned to the same clock and isn't trustworthy as
# a coordination mechanism.
SAFETY_MARGIN_S=5
ELAPSED_BEFORE_REPLAY_S=$(( ($(date +%s) * 1000 - TABLE_START_MS) / 1000 ))
REPLAY_MAX_DURATION_S=$((ROUND_SECONDS - ELAPSED_BEFORE_REPLAY_S - SAFETY_MARGIN_S))
if [ "$REPLAY_MAX_DURATION_S" -lt 5 ]; then
  echo "WARNING: only ${REPLAY_MAX_DURATION_S}s left in the round before replay_attack even started - using a 5s floor instead. Consider a longer --round-seconds." >&2
  REPLAY_MAX_DURATION_S=5
fi

run_step "replay_attack" run_with_timeout $((ROUND_SECONDS + 60)) \
  python3 "$ROOT_DIR/load-testers/adversarial-traffic/replay_attack.py" \
  --script "$ATTACK_SCRIPT" --gateway-url "$API_URL" --table-id "$TABLE_ID" \
  --token-url "$TOKEN_URL" --client-id "$ADVERSARIAL_CLIENT_ID" --client-secret "$ADVERSARIAL_CLIENT_SECRET" \
  --prompt "$PROMPT" --report-dir "$REPORT_DIR/adversarial-traffic-report" \
  --max-duration-seconds "$REPLAY_MAX_DURATION_S"

wait "$GOOD_TRAFFIC_PID"
GOOD_TRAFFIC_EXIT=$?
GOOD_TRAFFIC_DURATION=$(( $(date +%s) - GOOD_TRAFFIC_T0 ))
record_bg_result "good_traffic" "$GOOD_TRAFFIC_EXIT" "$GOOD_TRAFFIC_DURATION"
TABLE_END_MS=$(($(date +%s) * 1000))

# The deterministic end-of-round boundary: both traffic generators' real
# exit codes are in hand ABOVE this line (replay_attack's run_step already
# returned; Gatling's wait just returned), so defenses have genuinely
# covered every request either one could have sent - no timer, no
# estimate. end_round.sh blocks until the orchestrator's reset_config()
# call actually completes (see functions/orchestrator/handler.py's
# action == "end_round" branch), so verify_config_reset below is now
# confirming a reset we know already happened, not racing to catch one.
run_step "end_round" "$ROOT_DIR/scripts/end_round.sh" "$TABLE_ID"
run_step "verify_config_reset" verify_config_reset "$STATE_TABLE_NAME" "$TABLE_ID" "$AWS_REGION" "$ROUND_SECONDS"
run_step "orchestrator_health" check_orchestrator_health "$TABLE_ID" "$TABLE_START_MS"
run_step "score_table" score_table "$TABLE_ID" "$TABLE_START_MS" "$TABLE_END_MS"
# The only record of what the blue agent actually decided mid-round
# (which lever, what value, when) - prints TOOL_CALL/TOOL_RESULT lines
# into $LOG_FILE (via the tee at the top of this script), which
# round_report.py then folds into run_summary.json's "agent_actions"
# below. Not a failure if empty - a "do nothing" round may legitimately
# call no tools at all.
run_step "fetch_orchestrator_tool_calls" fetch_orchestrator_tool_calls "$ORCHESTRATOR_FUNCTION_NAME" "$TABLE_START_MS" "$AWS_REGION"

# run_summary.json is assembled from what's already in $LOG_FILE/$SUMMARY_FILE
# (every run_step's pass/fail, Gatling's own final request count, replay_attack's
# ADVERSARIAL_SUMMARY line, score_table's SCORE_LOG line, and the
# orchestrator's TOOL_CALL/TOOL_RESULT lines) rather than threading each
# of those through as separate bash state - see scripts/lib/round_report.py.
python3 "$SCRIPT_LIB_PY_DIR/round_report.py" summary \
  --log-file "$LOG_FILE" --summary-jsonl "$SUMMARY_FILE" --output "$REPORT_DIR/run_summary.json" >/dev/null

log_section "Round for table $TABLE_ID complete"
echo "Report folder: $REPORT_DIR"
echo "  metadata.json, run_summary.json"
echo "  good-traffic-report/index.html        (Gatling report)"
echo "  adversarial-traffic-report/index.html (replay report)"
echo "  logs/                    (this round's own log + step summary)"

# One unambiguous, greppable last line - everything above this is detail;
# this is the actual answer to "what was the score." Re-reads the
# SCORE_LOG line score_table already printed (and this script's own tee
# already wrote to $LOG_FILE) rather than re-querying CloudWatch.
SCORE_LOG_LINE="$(grep -o 'SCORE_LOG {.*}' "$LOG_FILE" | tail -1)"
if [ -n "$SCORE_LOG_LINE" ]; then
  python3 -c "
import json, sys
entry = json.loads(sys.argv[1][len('SCORE_LOG '):])
print(f\"RESULT: table_id={entry['table_id']} final_score={entry['final_score']:+.0f}\")
" "$SCORE_LOG_LINE"
else
  echo "RESULT: table_id=$TABLE_ID final_score=unavailable (scoring step did not complete)"
fi

if [ "$ANY_STEP_FAILED" = "true" ]; then
  exit 1
fi
