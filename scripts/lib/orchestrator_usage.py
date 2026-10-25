"""Fetch the orchestrator's USAGE_LOG CloudWatch Logs events for a time
window and print them - feeds scripts/lib/cost_estimate.py's input (via
the calling script's own log file, which tees this output into it).

Hard-fails (non-zero exit) if zero USAGE_LOG events were found - this is
a last-resort backstop, not the primary guard: scripts/lib/
orchestrator_health.py's preflight/per-table checks should already have
caught a fully broken orchestrator before this point. See that module's
docstring for why "zero activity" must never be reported as a plain pass.
"""
import argparse
import json
import subprocess
import sys


def fetch_usage_log_events(function_name, start_time_ms, region):
    # --output json, not text: the AWS CLI's text formatter tab-concatenates
    # multiple list results onto ONE line (confirmed live: a 3-item result
    # with --output text produced one tab-separated line, not 3 lines) -
    # which would silently merge multiple USAGE_LOG entries together and
    # break downstream per-line parsing, undercounting cost with no
    # visible error. Parsed JSON here avoids that entirely.
    out = subprocess.run(
        ["aws", "logs", "filter-log-events",
         "--log-group-name", f"/aws/lambda/{function_name}",
         "--start-time", str(start_time_ms),
         "--filter-pattern", "USAGE_LOG",
         "--region", region, "--output", "json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout or "{}").get("events", [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("function_name")
    parser.add_argument("start_time_ms")
    parser.add_argument("region")
    args = parser.parse_args()

    try:
        events = fetch_usage_log_events(args.function_name, args.start_time_ms, args.region)
    except RuntimeError as exc:
        print(f"FAIL: filter-log-events error: {exc}")
        sys.exit(1)

    print(f"Found {len(events)} USAGE_LOG event(s) in CloudWatch Logs.")
    for e in events:
        print(e["message"])

    if not events:
        print("FAIL: zero USAGE_LOG events across the whole run - the orchestrator never completed "
              "a single successful Bedrock call for any table.")
        sys.exit(1)


if __name__ == "__main__":
    main()
