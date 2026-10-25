"""Real (not Cost-Explorer-lagged) Bedrock cost estimate from a log file's
USAGE_LOG lines. Shared by comprehensive_test.sh and comprehensive_gameday.sh (see
scripts/test_harness_lib.sh) - previously this logic was duplicated as an
inline Python heredoc in each script; extracted here so there is one
place to fix pricing/logic, and so the pure math (cost_of, parse_usage_log)
is importable and unit-testable without spawning a subprocess or touching
AWS. See tests/test_cost_estimate.py.
"""
import argparse
import json
import re
from collections import defaultdict

# Sonnet 4.5 list pricing, per million tokens - update if ModelId changes.
# Cache write/read multipliers are Anthropic's standard prompt-caching
# rates (write ~1.25x base input, read ~0.1x base input), applied to the
# same per-token base price Bedrock bills this model at.
INPUT_PRICE = 3.00
OUTPUT_PRICE = 15.00
CACHE_WRITE_PRICE = INPUT_PRICE * 1.25
CACHE_READ_PRICE = INPUT_PRICE * 0.10

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def empty_totals():
    return {k: 0 for k in TOKEN_KEYS}


def cost_of(totals):
    """Dollar cost of one {token_key: count} dict at current list pricing."""
    return (
        totals.get("input_tokens", 0) / 1e6 * INPUT_PRICE
        + totals.get("output_tokens", 0) / 1e6 * OUTPUT_PRICE
        + totals.get("cache_creation_input_tokens", 0) / 1e6 * CACHE_WRITE_PRICE
        + totals.get("cache_read_input_tokens", 0) / 1e6 * CACHE_READ_PRICE
    )


def parse_usage_log(content):
    """Extract every USAGE_LOG {...} line from raw log text.

    findall over the whole content, not re.search per line: the AWS CLI's
    text formatter can tab-concatenate multiple results onto one line
    (confirmed live - see fetch_orchestrator_usage_logs's own comment), so
    a per-line search would silently keep only the first match on a line
    and undercount. findall catches every occurrence regardless of how
    lines ended up laid out.

    Returns (calls: int, per_table: {table_id: totals}, overall: totals).
    """
    per_table = defaultdict(empty_totals)
    overall = empty_totals()
    calls = 0
    for m in re.finditer(r"USAGE_LOG (\{.*?\})", content):
        try:
            u = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        calls += 1
        table_id = str(u.get("table_id", "unknown"))
        for k in TOKEN_KEYS:
            per_table[table_id][k] += u.get(k, 0)
            overall[k] += u.get(k, 0)
    return calls, dict(per_table), overall


def format_report(calls, per_table, overall):
    lines = [f"Bedrock API calls counted: {calls}"]
    if per_table:
        lines.append("")
        lines.append("Per table:")
        for table_id in sorted(per_table):
            t = per_table[table_id]
            lines.append(
                f"  table_id={table_id:10s} cost=${cost_of(t):.4f}  "
                f"in={t['input_tokens']:,} out={t['output_tokens']:,} "
                f"cache_write={t['cache_creation_input_tokens']:,} cache_read={t['cache_read_input_tokens']:,}"
            )
    total_cost = cost_of(overall)
    lines.append("")
    lines.append(f"ESTIMATED cost this run: ${total_cost:.4f}")
    lines.append("(Bedrock-only - Lambda/API Gateway/DynamoDB/CloudWatch/Gatling-side infra cost is")
    lines.append(" separately small at this volume. This is an estimate from list pricing, not your")
    lines.append(" actual bill - cross-check against check_cost.sh / Cost Explorer tomorrow.)")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log_file", help="Path to a run's full log file (contains USAGE_LOG lines).")
    args = parser.parse_args()

    with open(args.log_file) as fh:
        content = fh.read()
    calls, per_table, overall = parse_usage_log(content)
    print(format_report(calls, per_table, overall))


if __name__ == "__main__":
    main()
