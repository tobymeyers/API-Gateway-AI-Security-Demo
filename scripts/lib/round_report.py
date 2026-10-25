"""Builds the two JSON files in a round's combined report folder
(reports/round-table<N>-<run_id>/ - see run_round.sh):

  metadata.json     - INPUTS only: what was asked for (table_id, prompt,
                       round_seconds, target_rps, attack_script path,
                       started_at). Kept separate from run_summary.json
                       so a quick diff of just the inputs across many
                       runs stays clean.
  run_summary.json   - RESULTS: what happened (final score, a steps
                       rollup, good/adversarial traffic counts, and
                       every action the blue agent actually took).
                       Parsed back out of the round's own log/summary
                       files rather than threaded through as new bash
                       state - run_step already records a JSONL line per
                       step (logs/<run>.summary.jsonl - summarize_steps()
                       below rolls that up rather than re-embedding it
                       whole), and the good/adversarial traffic steps
                       already print a greppable line per request/summary
                       (GoodTrafficSimulation.scala's own GOOD_REQUEST_RESULT
                       line per request, and replay_attack.py's
                       ADVERSARIAL_SUMMARY line), and run_round.sh's
                       fetch_orchestrator_tool_calls step pulls the
                       orchestrator's own TOOL_CALL/TOOL_RESULT lines into
                       this same log - this just reads all of that back.
                       good_traffic and adversarial_traffic deliberately
                       share the same {total, allowed, denied, errored}
                       shape - see parse_good_traffic_results's docstring
                       for why a flatter {total, success, failed} would
                       lose the "blocked vs. an unrelated error" signal
                       both now carry.

All parsing functions are pure (text in, dict out) and unit-tested in
tests/test_round_report.py.
"""
import argparse
import json
import sys

# Same directory (scripts/lib/) - resolves whether this module is run
# directly (python3 auto-adds its own script dir to sys.path) or imported
# by a test that's already inserted scripts/lib onto sys.path.
from orchestrator_tool_calls import parse_tool_call_lines


def build_metadata(table_id, prompt, round_seconds, target_rps, attack_script, started_at):
    return {
        "table_id": table_id,
        "prompt": prompt,
        "round_seconds": round_seconds,
        "target_rps": target_rps,
        "attack_script": attack_script,
        "started_at": started_at,
    }


def parse_good_traffic_results(log_text):
    """Tally of GOOD_REQUEST_RESULT {"outcome": "allowed"|"denied"|"errored"}
    lines GoodTrafficSimulation.scala prints per request - our own
    response-body-aware classification, replacing Gatling's native OK/KO
    assertion (status in [200,209] or 304). That native check can't tell
    "the agent legitimately denied this" apart from "an unrelated infra
    error (e.g. a Lambda concurrency throttle) also returned a non-2xx
    status" - confirmed live: that exact ambiguity on the ADVERSARIAL
    side caused a real misdiagnosis earlier (replay_attack.py's
    attack_successful/is_lambda_deny_response fix, applied the same way
    here: "allowed" only on our own deny() helper's {"approved":"true"}
    body, "denied" only on its {"approved":"false"} body, "errored" for
    anything else - a 500, a bare API Gateway error, a dropped
    connection). Returns {"total", "allowed", "denied", "errored"}, all
    zero if the step never printed anything (e.g. it crashed before any
    request completed) - no None case, unlike parse_adversarial_summary_line,
    since this tallies every line itself rather than reading one final
    summary line that may never get printed."""
    total = allowed = denied = errored = 0
    prefix = "GOOD_REQUEST_RESULT "
    for line in log_text.splitlines():
        idx = line.find(prefix)
        if idx == -1:
            continue
        payload = json.loads(line[idx + len(prefix):])
        total += 1
        outcome = payload.get("outcome")
        if outcome == "allowed":
            allowed += 1
        elif outcome == "denied":
            denied += 1
        else:
            errored += 1
    return {"total": total, "allowed": allowed, "denied": denied, "errored": errored}


def _last_json_line(log_text, prefix):
    lines = [line for line in log_text.splitlines() if prefix in line]
    if not lines:
        return None
    line = lines[-1]
    return json.loads(line[line.index(prefix) + len(prefix):])


def parse_adversarial_summary_line(log_text):
    """The last ADVERSARIAL_SUMMARY {...} line replay_attack.py prints
    (only present if it was run with --report-dir)."""
    return _last_json_line(log_text, "ADVERSARIAL_SUMMARY ")


def parse_score_log_line(log_text):
    """The last SCORE_LOG {...} line scoring.py prints."""
    return _last_json_line(log_text, "SCORE_LOG ")


def summarize_steps(steps):
    """Collapses the full per-step records (the exact shape run_step
    already writes, one JSON line per step, to logs/<run>.summary.jsonl)
    down to a rollup. Confirmed live: embedding the full list here made
    run_summary.json byte-for-byte duplicate that file within the same
    report folder - full per-step detail (timestamps, durations) now
    lives in exactly one place; this is just enough to see at a glance
    whether anything failed without opening it."""
    failed = [s["step"] for s in steps if s.get("status") != "pass"]
    return {"total": len(steps), "failed": len(failed), "failed_steps": failed}


def build_run_summary(log_text, summary_jsonl_text, summary_jsonl_path=None):
    steps = [json.loads(line) for line in summary_jsonl_text.splitlines() if line.strip()]
    result = {
        "steps": summarize_steps(steps),
        "score": parse_score_log_line(log_text),
        "good_traffic": parse_good_traffic_results(log_text),
        "adversarial_traffic": parse_adversarial_summary_line(log_text),
        # Every TOOL_CALL/TOOL_RESULT line the orchestrator logged this
        # round (fetch_orchestrator_tool_calls in run_round.sh pulls these
        # from CloudWatch Logs into this same $LOG_FILE first) - the only
        # record of what the blue agent actually decided mid-round. Empty
        # list, not None, when no tools were called at all (e.g. a "do
        # nothing" round) or the fetch step never ran (an older log).
        "agent_actions": parse_tool_call_lines(log_text.splitlines()),
    }
    if summary_jsonl_path is not None:
        # Where the full per-step detail summarize_steps() collapsed
        # actually lives, so a reader who needs it knows where to look
        # instead of this file re-embedding it.
        result["steps_log_path"] = summary_jsonl_path
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    meta_p = sub.add_parser("metadata", help="Write metadata.json (inputs only).")
    meta_p.add_argument("--table-id", required=True)
    meta_p.add_argument("--prompt", required=True)
    meta_p.add_argument("--round-seconds", type=int, required=True)
    meta_p.add_argument("--target-rps", type=int, required=True)
    meta_p.add_argument("--attack-script", required=True)
    meta_p.add_argument("--started-at", required=True)
    meta_p.add_argument("--output", required=True)

    summary_p = sub.add_parser("summary", help="Write run_summary.json (results).")
    summary_p.add_argument("--log-file", required=True)
    summary_p.add_argument("--summary-jsonl", required=True)
    summary_p.add_argument("--output", required=True)

    args = parser.parse_args()

    if args.cmd == "metadata":
        data = build_metadata(
            args.table_id, args.prompt, args.round_seconds, args.target_rps,
            args.attack_script, args.started_at,
        )
    else:
        with open(args.log_file) as fh:
            log_text = fh.read()
        with open(args.summary_jsonl) as fh:
            summary_jsonl_text = fh.read()
        data = build_run_summary(log_text, summary_jsonl_text, summary_jsonl_path=args.summary_jsonl)

    with open(args.output, "w") as fh:
        json.dump(data, fh, indent=2)
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
