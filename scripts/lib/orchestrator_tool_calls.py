"""Fetch the orchestrator's TOOL_CALL/TOOL_RESULT CloudWatch Logs events
for a time window - the only record of what the blue agent actually
decided mid-round (which lever, what value, when). Without this there
was no way to reconstruct a round's agent behavior after the fact;
confirmed live: diagnosing a "block everything" round that only ever
blocked good traffic (never the low-volume adversarial client) required
reverse-engineering the agent's behavior entirely from CloudWatch
request-count metrics and request-timing math, because nothing logged
the actual set_rate_limit()/block_client() calls (see
functions/orchestrator/handler.py, which now prints both).

Unlike scripts/lib/orchestrator_usage.py, zero events here is NOT a
failure - a "do nothing" round may legitimately call no tools at all
(or only read-only ones), so this never hard-fails on an empty result.
"""
import argparse
import json
import subprocess
import sys


def fetch_tool_call_events(function_name, start_time_ms, region):
    # --output json, not text: the AWS CLI's text formatter tab-concatenates
    # multiple list results onto ONE line (same reasoning, and the same
    # confirmed-live bug, as orchestrator_usage.py) - parsed JSON avoids it.
    out = subprocess.run(
        ["aws", "logs", "filter-log-events",
         "--log-group-name", f"/aws/lambda/{function_name}",
         "--start-time", str(start_time_ms),
         "--filter-pattern", "?TOOL_CALL ?TOOL_RESULT",
         "--region", region, "--output", "json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout or "{}").get("events", [])


def parse_tool_call_lines(messages):
    """messages is a list of raw CloudWatch log message strings (each
    either a "TOOL_CALL {...}" or "TOOL_RESULT {...}" line, possibly with
    other unrelated lines mixed in - non-matching lines are ignored).
    Returns a list of parsed dicts, each tagged with its own "type"
    ("TOOL_CALL" or "TOOL_RESULT"), in the order given. Pure function -
    see tests/test_orchestrator_tool_calls.py."""
    parsed = []
    for message in messages:
        for prefix in ("TOOL_CALL ", "TOOL_RESULT "):
            idx = message.find(prefix)
            if idx == -1:
                continue
            try:
                entry = json.loads(message[idx + len(prefix):])
            except json.JSONDecodeError:
                continue
            entry["type"] = prefix.strip()
            parsed.append(entry)
            break
    return parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("function_name")
    parser.add_argument("start_time_ms")
    parser.add_argument("region")
    args = parser.parse_args()

    try:
        events = fetch_tool_call_events(args.function_name, args.start_time_ms, args.region)
    except RuntimeError as exc:
        print(f"FAIL: filter-log-events error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(events)} TOOL_CALL/TOOL_RESULT event(s) in CloudWatch Logs.")
    for e in events:
        print(e["message"])


if __name__ == "__main__":
    main()
