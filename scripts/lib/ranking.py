"""Final table ranking for a run, built by parsing the SCORE_LOG lines
scripts/lib/scoring.py already printed (and the test harness already tees
into the run's full log file) - same pattern as scripts/lib/cost_estimate.py
parsing USAGE_LOG lines, so no second CloudWatch round-trip is needed to
answer "who won."

`parse_score_log`/`rank` are pure (text in, data out) - see
tests/test_ranking.py.
"""
import argparse
import json
import re


def parse_score_log(content):
    """Returns a list of the SCORE_LOG dicts found in the log text, in
    the order scripts/lib/scoring.py printed them. findall over the whole
    content, not per-line, for the same reason cost_estimate.py does:
    some AWS CLI output elsewhere in the pipeline can land multiple
    entries on one line."""
    entries = []
    for m in re.finditer(r"SCORE_LOG (\{.*?\})", content):
        try:
            entries.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    return entries


def rank(entries):
    """Sorted best-to-worst by final_score - the cumulative running total
    (+1 per adversarial request blocked, -1 per good request blocked) as
    of the end of each table's round. See scripts/lib/scoring.py's
    docstring for why this single cumulative number, with no baseline/
    rate term, is the whole score."""
    return sorted(entries, key=lambda e: e["final_score"], reverse=True)


def format_report(entries):
    if not entries:
        return "No SCORE_LOG entries found - no table was scored in this run."
    ranked = rank(entries)
    lines = ["Final ranking (cumulative score: +1/adversarial blocked, -1/good blocked):", ""]
    for i, e in enumerate(ranked, start=1):
        lines.append(f"  {i}. table_id={e['table_id']:10s} score={e['final_score']:+.0f}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_file")
    args = parser.parse_args()

    with open(args.log_file) as fh:
        content = fh.read()
    entries = parse_score_log(content)
    print(format_report(entries))


if __name__ == "__main__":
    main()
