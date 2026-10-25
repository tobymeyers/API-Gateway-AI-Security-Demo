"""Unit tests for the thin AWS-CLI-calling wrappers in scripts/lib/*.py
(get_config, fetch_events, fetch_usage_log_events) using a mocked
subprocess.run - these don't touch AWS, but do exercise the real JSON
key-access code between a parsed `aws` CLI response and the pure
functions that act on it, which the pure-function-only tests elsewhere
in this directory don't cover.
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

import orchestrator_health  # noqa: E402
import orchestrator_usage  # noqa: E402
import scoring  # noqa: E402
import verify_config_reset  # noqa: E402


def fake_run(returncode=0, stdout="", stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestVerifyConfigResetGetConfig(unittest.TestCase):
    def test_parses_item_from_aws_cli_json(self):
        stdout = json.dumps({"Item": {"data": {"M": {
            "blocked_clients": {"L": []}, "rate_limit_rps": {"NULL": True},
        }}}})
        with mock.patch("subprocess.run", return_value=fake_run(stdout=stdout)):
            item = verify_config_reset.get_config("table", "1", "us-west-2")
        self.assertTrue(verify_config_reset.is_baseline(item["data"]["M"]))

    def test_no_item_key_returns_none(self):
        with mock.patch("subprocess.run", return_value=fake_run(stdout="{}")):
            item = verify_config_reset.get_config("table", "1", "us-west-2")
        self.assertIsNone(item)

    def test_cli_failure_raises_runtime_error(self):
        with mock.patch("subprocess.run", return_value=fake_run(returncode=255, stderr="AccessDenied")):
            with self.assertRaises(RuntimeError):
                verify_config_reset.get_config("table", "1", "us-west-2")


class TestOrchestratorHealthFetchEvents(unittest.TestCase):
    def test_parses_events_from_aws_cli_json(self):
        stdout = json.dumps({"events": [{"message": 'USAGE_LOG {"table_id": "1", "input_tokens": 5}'}]})
        with mock.patch("subprocess.run", return_value=fake_run(stdout=stdout)):
            events = orchestrator_health.fetch_events("fn", 123, "us-west-2")
        healthy, _ = orchestrator_health.is_healthy(events, "1")
        self.assertTrue(healthy)

    def test_cli_failure_raises_runtime_error(self):
        with mock.patch("subprocess.run", return_value=fake_run(returncode=255, stderr="ResourceNotFound")):
            with self.assertRaises(RuntimeError):
                orchestrator_health.fetch_events("fn", 123, "us-west-2")


class TestOrchestratorUsageFetchEvents(unittest.TestCase):
    def test_parses_events_from_aws_cli_json(self):
        stdout = json.dumps({"events": [{"message": "USAGE_LOG {\"table_id\": \"1\"}"}]})
        with mock.patch("subprocess.run", return_value=fake_run(stdout=stdout)):
            events = orchestrator_usage.fetch_usage_log_events("fn", 123, "us-west-2")
        self.assertEqual(len(events), 1)

    def test_empty_response_is_empty_list_not_error(self):
        with mock.patch("subprocess.run", return_value=fake_run(stdout="{}")):
            events = orchestrator_usage.fetch_usage_log_events("fn", 123, "us-west-2")
        self.assertEqual(events, [])

    def test_cli_failure_raises_runtime_error(self):
        with mock.patch("subprocess.run", return_value=fake_run(returncode=255, stderr="Throttled")):
            with self.assertRaises(RuntimeError):
                orchestrator_usage.fetch_usage_log_events("fn", 123, "us-west-2")


class TestScoringFetchMinuteSums(unittest.TestCase):
    def test_normalizes_aws_cli_offset_timestamp_to_grid_format(self):
        # Regression: the AWS CLI returns "...+00:00", not "...Z" - these
        # looked like different dict keys than build_minute_grid's output
        # until normalized, silently defaulting every lookup to 0. See
        # tests/test_scoring.py::TestTimestampNormalization for the pure
        # version of this same check.
        stdout = json.dumps({"Datapoints": [{"Timestamp": "2026-10-05T04:19:00+00:00", "Sum": 815.0}]})
        with mock.patch("subprocess.run", return_value=fake_run(stdout=stdout)):
            sums = scoring.fetch_minute_sums("1", "Good", "Allowed",
                                              "2026-10-05T04:19:00Z", "2026-10-05T04:20:00Z", "us-west-2")
        self.assertEqual(sums, {"2026-10-05T04:19:00Z": 815.0})

    def test_cli_failure_raises_runtime_error(self):
        with mock.patch("subprocess.run", return_value=fake_run(returncode=255, stderr="AccessDenied")):
            with self.assertRaises(RuntimeError):
                scoring.fetch_minute_sums("1", "Good", "Allowed", "2026-10-05T04:19:00Z",
                                           "2026-10-05T04:20:00Z", "us-west-2")


class TestScoringListKnownTableIds(unittest.TestCase):
    def test_extracts_unique_table_ids_from_list_metrics(self):
        stdout = json.dumps({"Metrics": [
            {"Dimensions": [{"Name": "TableId", "Value": "1"}, {"Name": "Outcome", "Value": "Allowed"}]},
            {"Dimensions": [{"Name": "TableId", "Value": "2"}, {"Name": "Outcome", "Value": "Denied"}]},
            {"Dimensions": [{"Name": "TableId", "Value": "1"}, {"Name": "Outcome", "Value": "Denied"}]},
        ]})
        with mock.patch("subprocess.run", return_value=fake_run(stdout=stdout)):
            table_ids = scoring.list_known_table_ids("us-west-2")
        self.assertEqual(table_ids, ["1", "2"])

    def test_cli_failure_raises_runtime_error(self):
        with mock.patch("subprocess.run", return_value=fake_run(returncode=255, stderr="AccessDenied")):
            with self.assertRaises(RuntimeError):
                scoring.list_known_table_ids("us-west-2")


if __name__ == "__main__":
    unittest.main()
