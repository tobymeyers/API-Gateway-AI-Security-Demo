import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from scoring import (  # noqa: E402
    _fmt_iso,
    _parse_iso,
    build_minute_grid,
    build_running_score_series,
    cumulative_score,
    fetch_minute_sums,
    score_table,
)


class TestCumulativeScore(unittest.TestCase):
    """The whole formula: +1 per adversarial request blocked, -1 per
    good request blocked. No baseline/target-rate term - see this
    module's docstring for why that's exactly docs/proposal.md's original
    formula, algebraically reduced."""

    def test_no_op_defense_scores_zero(self):
        # Blocks nothing on either side - net zero, not a penalty.
        self.assertEqual(cumulative_score(good_denied=0, adversarial_denied=0), 0)

    def test_blocking_everything_scores_deeply_negative(self):
        # A defense that shuts down all traffic should score WORSE than
        # doing nothing, not better - this is the sanity check the whole
        # redesign was built around.
        score = cumulative_score(good_denied=12000, adversarial_denied=60)
        self.assertEqual(score, 60 - 12000)
        self.assertLess(score, cumulative_score(good_denied=0, adversarial_denied=0))

    def test_perfect_defense_blocks_all_adversarial_no_good(self):
        self.assertEqual(cumulative_score(good_denied=0, adversarial_denied=60), 60)

    def test_only_blocks_good_traffic_is_negative(self):
        self.assertEqual(cumulative_score(good_denied=10, adversarial_denied=0), -10)


class TestTimestampNormalization(unittest.TestCase):
    """Regression test: the AWS CLI returns CloudWatch timestamps as
    "...+00:00", not "...Z" - confirmed live, these are the same instant
    but different strings, which silently broke every minute-grid lookup
    (defaulting everything to 0) until _parse_iso/_fmt_iso normalized
    both forms to the same representation."""

    def test_plus_00_00_and_z_forms_normalize_identically(self):
        a = _fmt_iso(_parse_iso("2026-10-05T04:19:00+00:00"))
        b = _fmt_iso(_parse_iso("2026-10-05T04:19:00Z"))
        self.assertEqual(a, b)


class TestBuildMinuteGrid(unittest.TestCase):
    def test_produces_one_entry_per_minute(self):
        grid = build_minute_grid("2026-10-05T04:00:00Z", "2026-10-05T04:03:00Z")
        self.assertEqual(grid, [
            "2026-10-05T04:00:00Z",
            "2026-10-05T04:01:00Z",
            "2026-10-05T04:02:00Z",
        ])

    def test_accepts_aws_cli_offset_form_too(self):
        grid = build_minute_grid("2026-10-05T04:00:00+00:00", "2026-10-05T04:02:00+00:00")
        self.assertEqual(grid, ["2026-10-05T04:00:00Z", "2026-10-05T04:01:00Z"])


class TestBuildRunningScoreSeries(unittest.TestCase):
    def test_running_total_accumulates_across_minutes(self):
        grid = build_minute_grid("2026-10-05T04:00:00Z", "2026-10-05T04:03:00Z")
        gd = {"2026-10-05T04:00:00Z": 5}  # 5 good blocked minute 0
        ad = {"2026-10-05T04:01:00Z": 10}  # 10 adversarial blocked minute 1
        series = build_running_score_series(grid, gd, ad)
        self.assertEqual(series, [
            ("2026-10-05T04:00:00Z", -5),   # -5 (good blocked)
            ("2026-10-05T04:01:00Z", 5),    # -5 + 10 = 5
            ("2026-10-05T04:02:00Z", 5),    # no change, nothing happened
        ])

    def test_missing_minutes_contribute_zero_not_skipped(self):
        grid = build_minute_grid("2026-10-05T04:00:00Z", "2026-10-05T04:02:00Z")
        series = build_running_score_series(grid, {}, {})
        self.assertEqual(series, [("2026-10-05T04:00:00Z", 0), ("2026-10-05T04:01:00Z", 0)])

    def test_final_entry_is_the_score(self):
        grid = build_minute_grid("2026-10-05T04:00:00Z", "2026-10-05T04:02:00Z")
        gd = {"2026-10-05T04:00:00Z": 2}
        ad = {"2026-10-05T04:00:00Z": 7, "2026-10-05T04:01:00Z": 3}
        series = build_running_score_series(grid, gd, ad)
        self.assertEqual(series[-1], ("2026-10-05T04:01:00Z", 8))  # (7-2) + 3 = 8


def _completed_process(stdout):
    class _Result:
        pass

    result = _Result()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


def _parse_cli_args(argv):
    """Pull the bits fetch_minute_sums's subprocess.run call cares about
    out of its argv, for the fake CloudWatch responder below."""
    dims_idx = argv.index("--dimensions")
    dims = {}
    for item in argv[dims_idx + 1 : dims_idx + 4]:
        name, value = item.split(",")
        dims[name.split("=")[1]] = value.split("=")[1]
    return {
        "traffic_type": dims["TrafficType"],
        "outcome": dims["Outcome"],
        "start_time": argv[argv.index("--start-time") + 1],
        "end_time": argv[argv.index("--end-time") + 1],
        "period": int(argv[argv.index("--period") + 1]),
    }


class FakeCloudWatch:
    """Reproduces the one piece of real CloudWatch get-metric-statistics
    behavior this regression depends on: a Period datapoint is only
    returned if its whole bucket [Timestamp, Timestamp+Period) is
    contained within the query's [StartTime, EndTime] - confirmed live
    against a real deployed table (see scoring.py's score_table
    docstring/comment). Everything else about real CloudWatch is
    irrelevant here, so this fake only models that one rule."""

    def __init__(self, datapoints_by_metric):
        # {(traffic_type, outcome): [{"Timestamp": iso_str, "Sum": float}]}
        self.datapoints_by_metric = datapoints_by_metric

    def __call__(self, argv, capture_output, text):
        parsed = _parse_cli_args(argv)
        end = _parse_iso(parsed["end_time"])
        period = parsed["period"]
        all_dps = self.datapoints_by_metric.get((parsed["traffic_type"], parsed["outcome"]), [])
        # Only the end-time containment rule is modeled, not a symmetric
        # start-time one - confirmed live against a real deployed table
        # that a bucket starting slightly before the literal --start-time
        # is still returned (CloudWatch aligns StartTime to a period
        # boundary internally), while a bucket whose end falls after
        # --end-time is not. That asymmetry is the actual bug this test
        # guards against, so the fake must reproduce it, not a stricter
        # symmetric version that would mask the real failure mode.
        included = [
            dp for dp in all_dps
            if _parse_iso(dp["Timestamp"]) + __import__("datetime").timedelta(seconds=period) <= end
        ]
        return _completed_process(json.dumps({"Datapoints": included}))


class TestScoreTableCloudWatchEndTimePadding(unittest.TestCase):
    """Regression test for a real incident: a round that denied 584 good
    requests entirely within its last (partial) minute scored +1 instead
    of -583, because fetch_minute_sums queried CloudWatch with the
    round's raw end time - which fell mid-bucket for that final minute,
    so get-metric-statistics silently omitted it. Fixed by padding the
    CloudWatch query's end time by one period; this test fails against
    the pre-fix code (which queries with the raw end time) and passes
    against the fix."""

    def test_denied_counts_in_the_final_partial_minute_are_not_dropped(self):
        # Round: 21:15:19 -> 21:16:32 (mirrors the real incident exactly -
        # a round that does NOT end on a clean minute boundary, which is
        # the normal case, not an edge case). All 584 good-traffic denials
        # and the 1 adversarial denial land in minutes that are still
        # "open" (not yet complete) as of 21:16:32.
        start_ms = 1791321319000  # 2026-10-06T21:15:19Z
        end_ms = 1791321392000  # 2026-10-06T21:16:32Z
        fake_cloudwatch = FakeCloudWatch({
            ("Good", "Denied"): [{"Timestamp": "2026-10-06T21:16:00+00:00", "Sum": 584.0}],
            ("Adversarial", "Denied"): [{"Timestamp": "2026-10-06T21:15:00+00:00", "Sum": 1.0}],
        })
        with patch("scoring.subprocess.run", side_effect=fake_cloudwatch):
            series, final_score = score_table("1", start_ms, end_ms, "us-west-2")

        # The real, correct score: 1 adversarial blocked, 584 good blocked.
        self.assertEqual(final_score, 1 - 584)
        # Both minutes still show up in the series (2 minutes_scored, same
        # as the real run's log), just with the correct counts now.
        self.assertEqual(len(series), 2)
        self.assertEqual(series[-1], ("2026-10-06T21:16:00Z", 1 - 584))

    def test_query_end_time_passed_to_cloudwatch_extends_past_round_end(self):
        """Direct check on the mechanism, independent of the fake's own
        containment logic: whatever end time reaches the AWS CLI call
        must be at least one period later than the round's real end,
        not the raw round-end timestamp itself."""
        start_ms = 1791321319000  # 2026-10-06T21:15:19Z
        end_ms = 1791321392000  # 2026-10-06T21:16:32Z
        raw_end = _parse_iso("2026-10-06T21:16:32Z")

        captured_end_times = []

        def capture(argv, capture_output, text):
            captured_end_times.append(_parse_iso(argv[argv.index("--end-time") + 1]))
            return _completed_process(json.dumps({"Datapoints": []}))

        with patch("scoring.subprocess.run", side_effect=capture):
            score_table("1", start_ms, end_ms, "us-west-2")

        self.assertTrue(captured_end_times, "fetch_minute_sums never called the AWS CLI")
        for end_time in captured_end_times:
            self.assertGreaterEqual((end_time - raw_end).total_seconds(), 60)


class TestFetchMinuteSumsReturnsInts(unittest.TestCase):
    """CloudWatch's get-metric-statistics always returns Sum as a JSON
    float, even though RequestCount only ever emits 1 per request - so
    every real Sum is a whole number. Without casting, final_score ends
    up a float purely from CloudWatch's wire format (confirmed live:
    "-1161.0" in a real run_summary.json), which is what this guards
    against."""

    def test_sum_is_cast_to_int_not_left_as_float(self):
        def fake_run(argv, capture_output, text):
            return _completed_process(json.dumps(
                {"Datapoints": [{"Timestamp": "2026-10-06T21:16:00+00:00", "Sum": 19.0}]}
            ))

        with patch("scoring.subprocess.run", side_effect=fake_run):
            sums = fetch_minute_sums("1", "Good", "Denied", "2026-10-06T21:15:00Z", "2026-10-06T21:17:00Z", "us-west-2")

        value = sums["2026-10-06T21:16:00Z"]
        self.assertEqual(value, 19)
        self.assertIsInstance(value, int)

    def test_final_score_is_an_int(self):
        fake_cloudwatch = FakeCloudWatch({
            ("Good", "Denied"): [{"Timestamp": "2026-10-06T21:16:00+00:00", "Sum": 584.0}],
            ("Adversarial", "Denied"): [{"Timestamp": "2026-10-06T21:15:00+00:00", "Sum": 1.0}],
        })
        start_ms = 1791321319000  # 2026-10-06T21:15:19Z
        end_ms = 1791321392000  # 2026-10-06T21:16:32Z
        with patch("scoring.subprocess.run", side_effect=fake_cloudwatch):
            _, final_score = score_table("1", start_ms, end_ms, "us-west-2")
        self.assertIsInstance(final_score, int)


if __name__ == "__main__":
    unittest.main()
