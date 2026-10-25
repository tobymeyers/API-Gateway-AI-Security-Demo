"""Preflight check: does --round-seconds fit inside the orchestrator
Lambda's actual configured Timeout?

Confirmed live on this project (salvaging a killed comprehensive_gameday.sh run):
template.yaml's OrchestratorFunction Timeout defaulted to 300s while a
--round-seconds 600 rehearsal was in flight, so every table's invocation
was hard-killed by AWS Lambda at 300s, and nothing before this check
would have caught it: orchestrator_health.py's [ERROR]/Traceback/USAGE_LOG
checks all looked fine (see that module's own REPORT-line check, added
alongside this one, for the other half of the fix). A buffer is required,
not just round_seconds < timeout, because run_agent_loop's last turn can
legitimately finish somewhat after its own deadline passes (Bedrock
latency plus up to MAX_TOOL_ROUNDTRIPS_PER_TURN tool round-trips) and
that overrun also needs to fit inside the Lambda's hard wall-clock limit.
Config reset itself is a separate, later invocation now (see
scripts/end_round.sh / handler.py's action == "end_round" branch) - not
part of this invocation's own timing budget.

`check` is pure and unit-tested directly - see tests/test_round_timeout_check.py.
"""
import argparse
import json
import subprocess
import sys

DEFAULT_BUFFER_SECONDS = 30


def check(round_seconds, lambda_timeout_seconds, buffer_seconds=DEFAULT_BUFFER_SECONDS):
    """Returns (ok: bool, message: str)."""
    if round_seconds + buffer_seconds >= lambda_timeout_seconds:
        return False, (
            f"--round-seconds {round_seconds} leaves less than {buffer_seconds}s of buffer "
            f"before the orchestrator Lambda's configured Timeout ({lambda_timeout_seconds}s) - "
            f"every invocation would be hard-killed mid-round or mid-reset. Either pass a smaller "
            f"--round-seconds (at most {lambda_timeout_seconds - buffer_seconds}), or raise "
            f"OrchestratorFunction's Timeout in infra/template.yaml and redeploy."
        )
    return True, (
        f"OK: --round-seconds {round_seconds} fits inside the orchestrator Lambda's "
        f"Timeout ({lambda_timeout_seconds}s) with {buffer_seconds}s+ of buffer."
    )


def fetch_lambda_timeout(function_name, region):
    out = subprocess.run(
        ["aws", "lambda", "get-function-configuration",
         "--function-name", function_name, "--region", region, "--output", "json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout)["Timeout"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("function_name")
    parser.add_argument("region")
    parser.add_argument("round_seconds", type=int)
    args = parser.parse_args()

    try:
        lambda_timeout = fetch_lambda_timeout(args.function_name, args.region)
    except RuntimeError as exc:
        print(f"FAIL: get-function-configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    ok, message = check(args.round_seconds, lambda_timeout)
    print(message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
