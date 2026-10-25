"""Check whether the orchestrator Lambda actually did its job for one
table's round, by reading its own CloudWatch Logs - not by trusting a
downstream side effect (an HTTP 202 from /trigger-agent, or DynamoDB config
being back at baseline) as a proxy for success.

Both of those proxies are indirect: a 202 only means the async Lambda
invoke was ACCEPTED, not that the orchestrator then ran successfully: and
"config at baseline" is also exactly what a config row that was NEVER
written at all looks like - a total crash before the first write is
indistinguishable from "ran fine and reset cleanly" to that check.

Confirmed live on this project: every orchestrator invocation crashed on
its first Bedrock call (an unsupported `thinking` parameter rejected by
Bedrock), the handler's `finally:` block still reset config, and every
other signal in the test scripts reported PASS while the blue agent never
ran at all for any table. This module exists to close that blind spot.

Also confirmed live, separately: a hard Lambda *timeout* (not a Python
exception) kills the process without running handler()'s `finally:`
block at all - no `[ERROR]`/Traceback, often still some USAGE_LOG lines
from turns that completed before the kill, so the checks above alone
read this as healthy. The real signal is Lambda's own REPORT line
(`REPORT ... Status: timeout`), which is_healthy now also checks for.

`is_healthy` is pure (no I/O) and unit-tested directly - see
tests/test_orchestrator_health.py. `fetch_events`/`check_table` do the
real AWS CLI call and are exercised by the shell scripts, not covered by
pure unit tests (would need to mock subprocess - fine to add later, not
required for the pure logic to already be covered).
"""
import argparse
import json
import subprocess
import sys
import time


def is_healthy(events, table_id):
    """Pure pass/fail decision given already-fetched CloudWatch Logs
    events (list of {"message": str, ...} dicts) for one table_id.

    Returns (healthy: bool, message: str).
    """
    errors = [e["message"] for e in events if "[ERROR]" in e["message"] or "Traceback" in e["message"]]
    timeouts = [
        e["message"] for e in events
        if e["message"].startswith("REPORT") and "Status: timeout" in e["message"]
    ]
    usage_lines = [
        e["message"] for e in events
        if "USAGE_LOG" in e["message"] and f'"table_id": "{table_id}"' in e["message"]
    ]

    if errors:
        detail = "\n".join(m.rstrip() for m in errors[:5])
        return False, f"orchestrator logged {len(errors)} error(s) for table_id={table_id}:\n{detail}"

    if timeouts:
        # Not correlated to this specific table_id - a REPORT line carries
        # no table_id, only a request id - but this check only ever covers
        # one invocation's time window in the test harnesses that call it
        # (comprehensive_gameday.sh runs tables sequentially, never concurrently), so
        # any timeout seen here is this table's own invocation.
        detail = "\n".join(m.rstrip() for m in timeouts[:5])
        return False, (
            f"orchestrator Lambda hard-timed-out for table_id={table_id} - round_seconds "
            f"exceeds the Lambda's configured Timeout:\n{detail}"
        )

    if not usage_lines:
        return False, (
            f"no USAGE_LOG entries for table_id={table_id} - {len(events)} total log event(s) seen, "
            f"but the orchestrator never completed a successful Bedrock call."
        )

    return True, f"{len(usage_lines)} USAGE_LOG entry/entries for table_id={table_id}, no errors logged."


def fetch_events(function_name, start_time_ms, region):
    out = subprocess.run(
        ["aws", "logs", "filter-log-events",
         "--log-group-name", f"/aws/lambda/{function_name}",
         "--start-time", str(start_time_ms),
         "--region", region, "--output", "json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout or "{}").get("events", [])


def check_table(function_name, start_time_ms, region, table_id, retries=5, poll_seconds=3):
    """Poll briefly (CloudWatch Logs can lag a few seconds right after an
    invocation completes) then apply is_healthy. Returns (healthy, message)."""
    events = []
    for _ in range(retries):
        events = fetch_events(function_name, start_time_ms, region)
        if events:
            break
        time.sleep(poll_seconds)
    return is_healthy(events, table_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("function_name")
    parser.add_argument("start_time_ms")
    parser.add_argument("region")
    parser.add_argument("table_id")
    args = parser.parse_args()

    try:
        healthy, message = check_table(args.function_name, args.start_time_ms, args.region, args.table_id)
    except RuntimeError as exc:
        print(f"FAIL: filter-log-events error: {exc}")
        sys.exit(1)

    print(("PASS: " if healthy else "FAIL: ") + message)
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
