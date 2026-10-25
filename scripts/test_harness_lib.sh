#!/usr/bin/env bash
# Shared test-harness helpers for comprehensive_test.sh and
# comprehensive_gameday.sh - single source of truth for the run_step/logging/
# teardown-on-exit/cost-estimate machinery both scripts need, instead of
# each carrying its own copy. The two scripts used to duplicate this
# logic almost verbatim (confirmed by diffing them directly), which is
# exactly why a fix made to one (e.g. the orchestrator-health check, or
# making a step failure actually show up in the final exit code) never
# propagated to the other - there was no single place to fix it.
#
# This file follows the same convention as lib.sh: a plain sourced file
# of functions, no side effects on source (no trap installed, nothing
# executed) - the calling script stays in full control of its own
# lifecycle and must set up the variables each function documents below
# before calling it. Nothing here is "magic": every function's inputs are
# either its own arguments or an explicitly-documented global the caller
# is expected to have already set.
#
# Usage: source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
#        source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/test_harness_lib.sh"
#
# Heavier logic (cost math, config-reset polling, orchestrator log
# health, roster/attack-script parsing) lives in real, importable,
# unit-tested Python modules under scripts/lib/ - see tests/test_*.py.
# The functions here are thin bash wrappers around those modules plus
# the run_step/trap bookkeeping that only makes sense as shell code.

SCRIPT_LIB_PY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib"

# log_section <message> - a visually distinct banner line. No external
# state required.
log_section() {
  echo
  echo "=================================================================="
  echo "[$(date -u +%H:%M:%S)] $1"
  echo "=================================================================="
}

# run_step <step-name> <command...> - runs a command, times it, records
# PASS/FAIL to stdout/log AND to the JSON summary file, returns the
# command's exit code. Never aborts the script itself - callers decide
# whether a given step's failure should stop the whole run (`|| exit 1`)
# or just be logged and continued past.
#
# Requires the caller to have already set: SUMMARY_FILE (path to append
# JSON-lines step records to), ANY_STEP_FAILED (a plain "true"/"false"
# variable this function flips to "true" on any FAIL - the caller's
# on_exit handler is responsible for turning that into a non-zero process
# exit code; see on_exit below).
run_step() {
  local name="$1"; shift
  local started ended duration status=pass code=0
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "--- STEP: $name ---"
  echo "+ $*"
  local t0 t1
  t0=$(date +%s)
  if "$@"; then
    code=0
  else
    code=$?
    status=fail
  fi
  t1=$(date +%s)
  duration=$((t1 - t0))
  ended="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "--- STEP $status: $name (${duration}s, exit $code) ---"
  python3 -c "
import json
print(json.dumps({'step': '$name', 'status': '$status', 'exit_code': $code, 'duration_s': $duration, 'started_at': '$started', 'ended_at': '$ended'}))
" >> "$SUMMARY_FILE"
  if [ "$status" = "fail" ]; then
    ANY_STEP_FAILED=true
  fi
  return "$code"
}

# record_bg_result <step-name> <exit-code> <duration-s> - same bookkeeping
# as run_step, for a command that was already run in the background (so
# it can't be wrapped directly - `wait` only gives us the exit code after
# the fact). Same SUMMARY_FILE/ANY_STEP_FAILED requirements as run_step.
record_bg_result() {
  local name="$1" code="$2" duration="$3" status=pass
  if [ "$code" -ne 0 ]; then status=fail; fi
  echo "--- STEP $status: $name (${duration}s, exit $code) ---"
  python3 -c "
import json
print(json.dumps({'step': '$name', 'status': '$status', 'exit_code': $code, 'duration_s': $duration}))
" >> "$SUMMARY_FILE"
  if [ "$status" = "fail" ]; then
    ANY_STEP_FAILED=true
  fi
}

# run_with_timeout <seconds> <command...> - portable (no GNU `timeout`
# dependency - not reliably present on macOS) background+deadline
# wrapper. No external state required.
run_with_timeout() {
  local secs="$1"; shift
  "$@" &
  local pid=$!
  local waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$secs" ]; then
      echo "(timed out after ${secs}s, killing pid $pid)"
      kill "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      return 124
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid"
}

# maybe_teardown - idempotent (safe to call more than once; a second call
# is a no-op). Requires: TEARDOWN_DONE, SKIP_TEARDOWN, KEEP_ACCESS_KEY
# ("true"/"false" each), STACK_NAME, AWS_REGION (from lib.sh). Updates
# TEARDOWN_DONE to "true" once it actually runs.
maybe_teardown() {
  if [ "$TEARDOWN_DONE" = "true" ]; then
    return 0
  fi
  if [ "$SKIP_TEARDOWN" = "true" ]; then
    echo
    echo "!!! --skip-teardown set - leaving the stack running. !!!"
    echo "!!! Remember to run ./scripts/teardown.sh manually.   !!!"
    return 0
  fi
  local teardown_args=(--stack-name "$STACK_NAME")
  if [ "$KEEP_ACCESS_KEY" = "true" ]; then
    teardown_args+=(--keep-access-key)
  fi
  run_step "teardown" "$ROOT_DIR/scripts/teardown.sh" "${teardown_args[@]}"
  # teardown.sh's own delete-stack call is asynchronous - it initiates
  # deletion and returns immediately. Without this wait, a post-teardown
  # tag check would run while the stack is still mid-deletion and falsely
  # report live resources as orphans - not a real bug, just checking too
  # early. Also correctly treats "stack no longer exists" as success, and
  # surfaces a real DELETE_FAILED as an actual failure rather than
  # silently moving on.
  run_step "wait_stack_delete_complete" aws cloudformation wait stack-delete-complete \
    --stack-name "$STACK_NAME" --region "$AWS_REGION"
  TEARDOWN_DONE=true
}

# on_exit - install with `trap on_exit EXIT` from the calling script
# (after everything below is set up - not installed automatically by
# this library, to keep sourcing side-effect-free). Requires: everything
# maybe_teardown requires, plus RUN_LABEL (string printed as "<RUN_LABEL>
# COMPLETE"), SUMMARY_FILE, LOG_FILE, ANY_STEP_FAILED.
on_exit() {
  local exit_code=$?
  maybe_teardown
  # Without this, the script's own final exit code would just reflect
  # whatever the LAST command happened to be (e.g. the final tag check) -
  # so a real failure earlier in the run (a table's trigger_agent, replay,
  # config reset, or orchestrator health check) could still exit 0
  # overall as long as every step after it passed. run_step/
  # record_bg_result set ANY_STEP_FAILED on any FAIL; this is what turns
  # that into an actual non-zero process exit code.
  if [ "$ANY_STEP_FAILED" = "true" ] && [ "$exit_code" -eq 0 ]; then
    exit_code=1
  fi
  log_section "${RUN_LABEL:-RUN} COMPLETE (exit $exit_code)"
  echo "Log:     $LOG_FILE"
  echo "Summary: $SUMMARY_FILE"
  if [ -f "$SUMMARY_FILE" ]; then
    echo
    python3 -c "
import json
steps = [json.loads(l) for l in open('$SUMMARY_FILE')]
failed = [s for s in steps if s['status'] == 'fail']
total = sum(s['duration_s'] for s in steps)
for s in steps:
    mark = 'OK  ' if s['status'] == 'pass' else 'FAIL'
    print(f\"  {mark} {s['step']:40s} {s['duration_s']:4d}s\")
print()
print(f'{len(steps)} steps, {len(failed)} failed, {total}s total')
"
  fi
  if [ -f "$LOG_FILE" ]; then
    echo
    python3 -c "
cost_lines = [l.rstrip() for l in open('$LOG_FILE') if l.startswith('ESTIMATED cost this run:')]
print(cost_lines[-1] if cost_lines else 'ESTIMATED cost this run: unavailable (cost step did not complete)')
"
  fi
  exit "$exit_code"
}

# verify_config_reset <table> <table_id> <region> <round_seconds> - thin
# wrapper around scripts/lib/verify_config_reset.py (see that module for
# the actual polling/baseline logic, which is unit-tested independently).
verify_config_reset() {
  python3 "$SCRIPT_LIB_PY_DIR/verify_config_reset.py" "$@"
}

# fetch_orchestrator_usage_logs <function_name> <start_time_ms> <region> -
# thin wrapper around scripts/lib/orchestrator_usage.py.
fetch_orchestrator_usage_logs() {
  python3 "$SCRIPT_LIB_PY_DIR/orchestrator_usage.py" "$@"
}

# fetch_orchestrator_tool_calls <function_name> <start_time_ms> <region> -
# thin wrapper around scripts/lib/orchestrator_tool_calls.py. Prints every
# TOOL_CALL/TOOL_RESULT line the orchestrator logged this round - the only
# record of what the blue agent actually decided (which lever, what value,
# when). Unlike fetch_orchestrator_usage_logs, zero events is NOT a
# failure here - a "do nothing" round may legitimately call no tools.
fetch_orchestrator_tool_calls() {
  python3 "$SCRIPT_LIB_PY_DIR/orchestrator_tool_calls.py" "$@"
}

# cost_estimate <log_file> - thin wrapper around scripts/lib/cost_estimate.py.
cost_estimate() {
  python3 "$SCRIPT_LIB_PY_DIR/cost_estimate.py" "$@"
}

# check_orchestrator_health <table_id> <start_time_epoch_ms> - thin
# wrapper around scripts/lib/orchestrator_health.py. Requires the caller
# to have set ORCHESTRATOR_FUNCTION_NAME and AWS_REGION (from lib.sh).
# See that module's docstring for why this check exists and what it
# catches that trigger_agent's HTTP status and verify_config_reset cannot.
check_orchestrator_health() {
  local table_id="$1" start_ms="$2"
  python3 "$SCRIPT_LIB_PY_DIR/orchestrator_health.py" "$ORCHESTRATOR_FUNCTION_NAME" "$start_ms" "$AWS_REGION" "$table_id"
}

# check_round_seconds_fits_lambda_timeout <round_seconds> - thin wrapper
# around scripts/lib/round_timeout_check.py. Requires ORCHESTRATOR_FUNCTION_NAME
# and AWS_REGION. Run this once, right after deploy, before spending any
# traffic/Bedrock cost on a round that would just get hard-killed - see
# that module's docstring for the incident this closes.
check_round_seconds_fits_lambda_timeout() {
  local round_seconds="$1"
  python3 "$SCRIPT_LIB_PY_DIR/round_timeout_check.py" "$ORCHESTRATOR_FUNCTION_NAME" "$AWS_REGION" "$round_seconds"
}

# score_table <table_id> <start_ms> <end_ms> - thin wrapper around
# scripts/lib/scoring.py. A cumulative running sum (+1 per adversarial
# request blocked, -1 per good request blocked - see that module's
# docstring for why no baseline/rate term is needed), as a standalone,
# scriptable alternative to reading a live dashboard graph - which also
# still works after the stack (and its dashboard) has been torn down,
# since CloudWatch metric data outlives the resources that published it.
# Prints a SCORE_LOG line consumed by print_rankings below.
score_table() {
  local table_id="$1" start_ms="$2" end_ms="$3"
  python3 "$SCRIPT_LIB_PY_DIR/scoring.py" --table-id "$table_id" \
    --start-time-ms "$start_ms" --end-time-ms "$end_ms" --region "$AWS_REGION"
}

# print_rankings <log_file> - thin wrapper around scripts/lib/ranking.py.
# Call after all of a run's score_table calls - it reconstructs the
# ranking from the SCORE_LOG lines already in the log file, no further
# CloudWatch calls needed.
print_rankings() {
  python3 "$SCRIPT_LIB_PY_DIR/ranking.py" "$@"
}
