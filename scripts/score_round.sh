#!/usr/bin/env bash
# Facilitator tool: score a table's round on demand - a cumulative
# running sum, +1 for every adversarial request blocked, -1 for every
# good request blocked (see scripts/lib/scoring.py's docstring for the
# full reasoning). Works whether the stack is still live or already torn
# down, since CloudWatch metric data outlives the resources that
# published it - unlike the live dashboard, which disappears with the
# stack. No stack lookup needed: scoring reads CloudWatch metrics
# directly by table_id, nothing stack-parameter-dependent anymore.
#
# Usage:
#   ./scripts/score_round.sh
#       No args: auto-discovers every table_id with recent CloudWatch
#       activity (default: last 3h, see --lookback-hours) and scores all
#       of them, printing a final ranking - "score everything from the
#       last round(s)" without having to look up table_id/timestamps
#       yourself.
#   ./scripts/score_round.sh <table_id> <start_time_ms> <end_time_ms>
#       Scores exactly that one table/window.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/test_harness_lib.sh"

LOOKBACK_HOURS=""

usage() {
  cat <<'EOF'
Usage: score_round.sh [options] [<table_id> <start_time_ms> <end_time_ms>]
  (no positional args)      Auto-discover and score every table with recent
                             activity - see --lookback-hours
  --lookback-hours <n>       Discovery mode only: how far back to look for
                             table activity (default: 3)
  -h, --help                 Show this help
EOF
}

POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    --lookback-hours) LOOKBACK_HOURS="$2"; shift 2 ;;
    --lookback-hours=*) LOOKBACK_HOURS="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; POSITIONAL+=("$@"); break ;;
    -*) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
# Bash 3.2 (macOS default) empty-array-under-set-u guard, same reasoning
# as trigger_agent.sh.
if [ "${#POSITIONAL[@]}" -gt 0 ]; then
  set -- "${POSITIONAL[@]}"
else
  set --
fi

if [ $# -ne 0 ] && [ $# -ne 3 ]; then
  echo "ERROR: pass either no positional args (auto-discover everything) or exactly" >&2
  echo "       all three: table_id, start_time_ms, end_time_ms." >&2
  usage
  exit 1
fi

require_aws_credentials "scoring a round"

if [ $# -eq 3 ]; then
  python3 "$SCRIPT_LIB_PY_DIR/scoring.py" --table-id "$1" \
    --start-time-ms "$2" --end-time-ms "$3" --region "$AWS_REGION"
else
  # Always-non-empty array built up rather than expanding a possibly-
  # empty one directly: bash 3.2 (macOS default) throws "unbound
  # variable" under `set -u` expanding a genuinely empty array, even as
  # "${ARR[@]}" - confirmed live when --lookback-hours was omitted (the
  # normal case).
  DISCOVER_ARGS=(--region "$AWS_REGION")
  if [ -n "$LOOKBACK_HOURS" ]; then
    DISCOVER_ARGS+=(--lookback-hours "$LOOKBACK_HOURS")
  fi
  python3 "$SCRIPT_LIB_PY_DIR/scoring.py" "${DISCOVER_ARGS[@]}"
fi
