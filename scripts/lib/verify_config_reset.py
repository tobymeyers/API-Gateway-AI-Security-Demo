"""Poll DynamoDB until a table's defense config is back at baseline after
a round - not a fixed sleep, since the orchestrator's actual finish time
(MAX_TOOL_ROUNDTRIPS_PER_TURN plus Bedrock latency on the last turn) isn't
pinned to round_seconds and can legitimately run somewhat longer.

`is_baseline` is pure (no I/O) and unit-tested directly against
DynamoDB-shaped dicts - see tests/test_verify_config_reset.py. Note this
check alone can't distinguish "the agent ran and correctly reset
everything" from "no config row was ever written at all" (both look like
baseline) - that gap is exactly why scripts/lib/orchestrator_health.py
exists as a separate, independent check.
"""
import argparse
import json
import subprocess
import sys
import time


def is_baseline(config_data):
    """config_data is the DynamoDB-JSON "data" map (M) of a CFG# item, or
    None if no item exists. Returns True if blocked_clients is empty and
    rate_limit_rps is NULL - the state reset_config() writes."""
    if config_data is None:
        return True
    blocked = config_data.get("blocked_clients", {}).get("L", [])
    rate_limit = config_data.get("rate_limit_rps", {})
    return len(blocked) == 0 and rate_limit.get("NULL") is True


def get_config(table, table_id, region):
    out = subprocess.run(
        ["aws", "dynamodb", "get-item", "--table-name", table,
         "--key", json.dumps({"pk": {"S": f"CFG#{table_id}"}}),
         "--region", region, "--output", "json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout or "{}").get("Item")


def poll_until_baseline(table, table_id, region, round_seconds, poll_every=5, overrun_buffer=120):
    max_wait = round_seconds + overrun_buffer
    deadline = time.monotonic() + max_wait
    attempt = 0
    while True:
        attempt += 1
        item = get_config(table, table_id, region)
        # Defaults to {} (not None) when the item exists but is somehow
        # missing the expected nested Map structure - None is reserved
        # for "no item at all" (is_baseline's other baseline case).
        # Unreachable in practice (DynamoDB always wraps a dict value
        # written via boto3's Table resource with "M"), but matches the
        # original inline implementation's defensive default rather than
        # silently treating a malformed item as baseline.
        data = item.get("data", {}).get("M", {}) if item else None
        if is_baseline(data):
            if item is None:
                print(f"PASS (attempt {attempt}): no config item present - baseline either way.")
            else:
                print(f"PASS (attempt {attempt}): config is back to baseline after the round.")
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"FAIL after {max_wait}s: config was NOT reset: {json.dumps(data)}")
            return False
        print(f"attempt {attempt}: not yet baseline ({json.dumps(data)}), {int(remaining)}s left, retrying in {poll_every}s...")
        time.sleep(min(poll_every, remaining))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table")
    parser.add_argument("table_id")
    parser.add_argument("region")
    parser.add_argument("round_seconds", type=int)
    args = parser.parse_args()

    try:
        ok = poll_until_baseline(args.table, args.table_id, args.region, args.round_seconds)
    except RuntimeError as exc:
        print(f"FAIL: get-item error: {exc}")
        sys.exit(1)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
