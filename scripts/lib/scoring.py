"""Compute a table's real score from CloudWatch metrics: a cumulative
running sum, +1 for every adversarial request blocked, -1 for every good
request blocked. Nothing else - no baseline/target rate, no per-minute
rate comparison.

This is docs/proposal.md's own formula, reduced to its simplest form:
    Score = (N_malicious_baseline - N_malicious_attempt) - (N_good_baseline - N_good_attempt)
N_x_baseline is "how many X requests were sent" (a fixed constant for
malicious, since the same script is replayed everywhere; the actual
total sent for good, since Gatling's real throughput is what it is) and
N_x_attempt is "how many got through" (Allowed) - so N_baseline -
N_attempt is just N_denied. The baseline/rate framing in an earlier
version of this module (and in infra/template.yaml's ScoringDashboard
widget, now fixed to match) was an unnecessary, less-accurate
approximation of the same thing: it doesn't need a target rate or a
known script length at all, just the two DENIED counts, directly
observed. A no-op agent that blocks nothing scores 0 (loses no good
traffic, blocks no malicious traffic either) - better than an agent that
blocks everything (loses every good request for a handful of blocked
malicious ones), exactly as the exercise intends.

Pure functions (cumulative_score, build_minute_grid, build_running_score_series)
take plain data in and return plain data out - no AWS calls, no mocking
needed to test them. See tests/test_scoring.py.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone


def cumulative_score(good_denied, adversarial_denied):
    """+1 per adversarial request blocked, -1 per good request blocked.
    That's the whole formula - see this module's docstring for why no
    baseline/target term is needed."""
    return adversarial_denied - good_denied


def _parse_iso(ts):
    # Accepts both the "...Z" form we generate ourselves and the
    # "...+00:00" form the AWS CLI actually returns for --output json
    # timestamps (confirmed live - these are NOT the same string even
    # though they're the same instant, which silently broke every grid
    # lookup below until this normalization was added: fromisoformat
    # handles numeric offsets but not a bare "Z", so "Z" is swapped for
    # "+00:00" first).
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def _fmt_iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_minute_grid(start_iso, end_iso):
    """Every minute boundary in [start, end), as ISO strings - the
    authoritative time axis, independent of which minutes CloudWatch
    happened to return a datapoint for."""
    start = _parse_iso(start_iso).replace(second=0, microsecond=0)
    end = _parse_iso(end_iso)
    minutes = []
    t = start
    while t < end:
        minutes.append(_fmt_iso(t))
        t += timedelta(minutes=1)
    return minutes


def build_running_score_series(minute_grid, good_denied_by_minute, adversarial_denied_by_minute):
    """Returns a list of (minute_iso, running_score) pairs - the
    cumulative total AS OF that minute, same semantics as CloudWatch
    metric math's RUNNING_SUM() (which is what the live dashboard widget
    uses). The final entry's score is THE score - this is what
    scripts/lib/ranking.py ranks by. A minute with no CloudWatch
    datapoint on either side contributes 0 (no requests blocked that
    minute), not a gap."""
    series = []
    running = 0
    for minute in minute_grid:
        gd = good_denied_by_minute.get(minute, 0)
        ad = adversarial_denied_by_minute.get(minute, 0)
        running += cumulative_score(gd, ad)
        series.append((minute, running))
    return series


def fetch_minute_sums(table_id, traffic_type, outcome, start_iso, end_iso, region,
                       namespace="ISC2/GatewayExercise"):
    """One metric's per-minute Sum datapoints as {minute_iso: sum}."""
    out = subprocess.run(
        ["aws", "cloudwatch", "get-metric-statistics",
         "--namespace", namespace, "--metric-name", "RequestCount",
         "--dimensions",
         f"Name=TableId,Value={table_id}",
         f"Name=TrafficType,Value={traffic_type}",
         f"Name=Outcome,Value={outcome}",
         "--start-time", start_iso, "--end-time", end_iso,
         "--period", "60", "--statistics", "Sum",
         "--region", region, "--output", "json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    data = json.loads(out.stdout or "{}")
    # Normalize through _parse_iso/_fmt_iso so these keys match
    # build_minute_grid's format exactly - see _parse_iso's comment.
    # int(): CloudWatch's get-metric-statistics always returns Sum as a
    # JSON float (e.g. 19.0) even though RequestCount only ever emits 1
    # per request, so every real Sum is a whole number - without this,
    # final_score ends up a float (confirmed live: "-1161.0" in a real
    # run_summary.json) purely because of CloudWatch's wire format, not
    # because the score is ever fractional.
    return {_fmt_iso(_parse_iso(dp["Timestamp"])): int(dp["Sum"]) for dp in data.get("Datapoints", [])}


def score_table(table_id, start_epoch_ms, end_epoch_ms, region):
    """Full pipeline: fetch both Denied metrics, build the running-sum
    series. Returns (series, final_score) - final_score is series[-1][1],
    or 0 if there were no minutes in range at all."""
    start_iso = _fmt_iso(datetime.fromtimestamp(start_epoch_ms / 1000, tz=timezone.utc))
    end_iso = _fmt_iso(datetime.fromtimestamp(end_epoch_ms / 1000, tz=timezone.utc))
    # get-metric-statistics only returns a Period=60 datapoint if that
    # whole 60s bucket is contained within [StartTime, EndTime] - a
    # round's real end_iso almost never lands exactly on a minute
    # boundary, so querying with the raw end_iso silently drops the
    # final (and often most consequential, since agent actions cluster
    # near the end of a round) minute's Denied counts. Confirmed live:
    # a round that denied 584 good requests in its last minute scored
    # +1 instead of -583 until this padding was added. Pad only the
    # CloudWatch query's end time, by one full period - build_minute_grid
    # below still uses the real end_iso, so no extra minute is
    # fabricated in the displayed series. Same technique already used by
    # discover_activity_window's own end-time pad, just not previously
    # applied to this direct single-table path.
    query_end_iso = _fmt_iso(_parse_iso(end_iso) + timedelta(seconds=60))
    gd = fetch_minute_sums(table_id, "Good", "Denied", start_iso, query_end_iso, region)
    ad = fetch_minute_sums(table_id, "Adversarial", "Denied", start_iso, query_end_iso, region)
    grid = build_minute_grid(start_iso, end_iso)
    series = build_running_score_series(grid, gd, ad)
    final_score = series[-1][1] if series else 0
    return series, final_score


def list_known_table_ids(region, namespace="ISC2/GatewayExercise"):
    """Every TableId dimension value that has EVER published RequestCount
    in this namespace (list-metrics has no time filter - CloudWatch keeps
    entries here for months after the metric itself stopped receiving
    data). Just the candidate set for discover_active_tables below to
    narrow down by actual recent activity - not itself "the last round"."""
    out = subprocess.run(
        ["aws", "cloudwatch", "list-metrics", "--namespace", namespace,
         "--metric-name", "RequestCount", "--region", region, "--output", "json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    data = json.loads(out.stdout or "{}")
    table_ids = set()
    for metric in data.get("Metrics", []):
        for dim in metric.get("Dimensions", []):
            if dim.get("Name") == "TableId":
                table_ids.add(dim["Value"])
    return sorted(table_ids)


def discover_activity_window(table_id, region, lookback_start_iso, lookback_end_iso,
                              namespace="ISC2/GatewayExercise"):
    """The actual [first, last] activity span for one table_id within the
    lookback window, found by taking the min/max datapoint timestamp
    across all four TrafficType x Outcome combinations - not just the two
    Denied combos scoring itself needs, since a table that let everything
    through (zero Denied anywhere) would otherwise look inactive and get
    silently excluded from discovery. Returns (start_iso, end_iso) with a
    60s pad on the end (so the final minute's grid boundary is inclusive),
    or None if nothing in this table_id had any activity in the window."""
    timestamps = []
    for traffic_type in ("Good", "Adversarial"):
        for outcome in ("Allowed", "Denied"):
            sums = fetch_minute_sums(table_id, traffic_type, outcome,
                                      lookback_start_iso, lookback_end_iso, region, namespace)
            timestamps.extend(sums.keys())
    if not timestamps:
        return None
    first, last = min(timestamps), max(timestamps)
    end = _fmt_iso(_parse_iso(last) + timedelta(seconds=60))
    return first, end


def discover_active_tables(region, lookback_hours, namespace="ISC2/GatewayExercise"):
    """Returns {table_id: (start_iso, end_iso)} for every table_id with
    real activity in the last lookback_hours - the "score everything from
    the last round(s)" entry point. Each table's own window is its own
    actual first/last activity, not a shared window, so this is safe to
    use across multiple distinct rounds run back-to-back (e.g. a full
    gameday roster) without mixing one table's minutes into another's."""
    now = datetime.now(timezone.utc)
    lookback_start_iso = _fmt_iso(now - timedelta(hours=lookback_hours))
    lookback_end_iso = _fmt_iso(now)
    windows = {}
    for table_id in list_known_table_ids(region, namespace):
        window = discover_activity_window(table_id, region, lookback_start_iso, lookback_end_iso, namespace)
        if window is not None:
            windows[table_id] = window
    return windows


def _print_table_score(table_id, series, final_score):
    print(f"Table {table_id} - cumulative score (running total, +1 per adversarial request "
          f"blocked, -1 per good request blocked):")
    for minute, running in series:
        print(f"  {minute}  {running:+.0f}")
    print()
    print(f"  Final score: {final_score:+.0f}")
    # Greppable from the full run log, same convention as USAGE_LOG - lets
    # scripts/lib/ranking.py reconstruct every table's score after the
    # fact without re-querying CloudWatch.
    entry = {"table_id": table_id, "final_score": final_score, "minutes_scored": len(series)}
    print("SCORE_LOG " + json.dumps(entry))
    print()
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table-id", help="Score just this one table. Omit (with --start-time-ms/"
                                            "--end-time-ms also omitted) to auto-discover and score "
                                            "every table with recent activity instead.")
    parser.add_argument("--start-time-ms", type=int, help="Round start, epoch milliseconds.")
    parser.add_argument("--end-time-ms", type=int, help="Round end, epoch milliseconds.")
    parser.add_argument("--lookback-hours", type=float, default=3,
                         help="Discovery mode only: how far back to look for table activity (default: 3).")
    parser.add_argument("--region", required=True)
    args = parser.parse_args()

    single_table_args = (args.table_id, args.start_time_ms, args.end_time_ms)
    if any(a is not None for a in single_table_args) and not all(a is not None for a in single_table_args):
        print("FAIL: --table-id, --start-time-ms, and --end-time-ms must all be given together, "
              "or all omitted (to auto-discover every recently-active table).", file=sys.stderr)
        sys.exit(1)

    if args.table_id is not None:
        try:
            series, final_score = score_table(args.table_id, args.start_time_ms, args.end_time_ms, args.region)
        except RuntimeError as exc:
            print(f"FAIL: CloudWatch error: {exc}", file=sys.stderr)
            sys.exit(1)
        if not series:
            print(f"FAIL: no minutes in range for table_id={args.table_id} - check start/end times.",
                  file=sys.stderr)
            sys.exit(1)
        _print_table_score(args.table_id, series, final_score)
        return

    # Discovery mode: no --table-id given.
    print(f"No --table-id given - discovering tables with activity in the last {args.lookback_hours}h...")
    try:
        windows = discover_active_tables(args.region, args.lookback_hours)
    except RuntimeError as exc:
        print(f"FAIL: CloudWatch error during discovery: {exc}", file=sys.stderr)
        sys.exit(1)
    if not windows:
        print(f"FAIL: no table had any activity in the last {args.lookback_hours}h - "
              f"try --lookback-hours with a larger value, or score a specific --table-id directly.",
              file=sys.stderr)
        sys.exit(1)
    print(f"Found {len(windows)} table(s) with recent activity: {sorted(windows)}")
    print()

    entries = []
    for table_id in sorted(windows):
        start_iso, end_iso = windows[table_id]
        start_ms = int(_parse_iso(start_iso).timestamp() * 1000)
        end_ms = int(_parse_iso(end_iso).timestamp() * 1000)
        try:
            series, final_score = score_table(table_id, start_ms, end_ms, args.region)
        except RuntimeError as exc:
            print(f"FAIL: CloudWatch error scoring table_id={table_id}: {exc}", file=sys.stderr)
            continue
        if not series:
            continue
        entries.append(_print_table_score(table_id, series, final_score))

    # Local import avoids a hard dependency for callers that only ever
    # use single-table mode (e.g. the test_harness_lib.sh score_table wrapper).
    from ranking import format_report
    print(format_report(entries))


if __name__ == "__main__":
    main()
