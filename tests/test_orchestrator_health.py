import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from orchestrator_health import is_healthy  # noqa: E402


class TestIsHealthy(unittest.TestCase):
    def test_regression_adaptive_thinking_crash(self):
        """The exact failure this module exists to catch: the orchestrator
        crashed on its first Bedrock call, every invocation, for every
        table - reproduced here from the real CloudWatch Logs output."""
        events = [
            {"message": "START RequestId: ab3b3b10 Version: $LATEST"},
            {"message": "[ERROR] BadRequestError: Error code: 400 - "
                        "{'message': 'adaptive thinking is not supported on this model'}"},
            {"message": "Traceback (most recent call last):"},
            {"message": "END RequestId: ab3b3b10"},
        ]
        healthy, message = is_healthy(events, "debug-table2")
        self.assertFalse(healthy)
        self.assertIn("error(s)", message)

    def test_successful_round(self):
        events = [
            {"message": 'USAGE_LOG {"table_id": "debug-table2", "input_tokens": 3, "output_tokens": 96}'},
            {"message": 'USAGE_LOG {"table_id": "debug-table2", "input_tokens": 7, "output_tokens": 217}'},
        ]
        healthy, message = is_healthy(events, "debug-table2")
        self.assertTrue(healthy)
        self.assertIn("2 USAGE_LOG", message)

    def test_fallback_warning_without_error_marker_is_not_a_failure(self):
        """The fix itself logs a plain (non-[ERROR]) message when it falls
        back - that line alone must not trip the error detector, only a
        real [ERROR]/Traceback should."""
        events = [
            {"message": "Adaptive thinking rejected by Bedrock for X, falling back without it: ..."},
            {"message": 'USAGE_LOG {"table_id": "debug-table2", "input_tokens": 3}'},
        ]
        healthy, _ = is_healthy(events, "debug-table2")
        self.assertTrue(healthy)

    def test_regression_hard_lambda_timeout_with_passing_usage_log(self):
        """The gap discovered salvaging a killed comprehensive_gameday.sh run: every
        table hard-timed-out at the Lambda's Timeout (handler's finally:,
        which resets config, never ran), but some USAGE_LOG lines from
        turns that completed before the kill still made it into the logs -
        so the pre-REPORT-check version of is_healthy reported PASS for
        a round that never actually finished."""
        events = [
            {"message": "START RequestId: ab3b3b10 Version: $LATEST"},
            {"message": 'USAGE_LOG {"table_id": "3", "input_tokens": 500, "output_tokens": 80}'},
            {"message": "END RequestId: ab3b3b10"},
            {"message": "REPORT RequestId: ab3b3b10\tDuration: 300000.00 ms\tBilled Duration: 300000 ms\t"
                        "Memory Size: 512 MB\tMax Memory Used: 128 MB\tStatus: timeout"},
        ]
        healthy, message = is_healthy(events, "3")
        self.assertFalse(healthy)
        self.assertIn("timed-out", message)

    def test_no_events_for_this_table_is_unhealthy(self):
        events = [{"message": 'USAGE_LOG {"table_id": "some-other-table", "input_tokens": 3}'}]
        healthy, message = is_healthy(events, "debug-table2")
        self.assertFalse(healthy)
        self.assertIn("no USAGE_LOG entries", message)

    def test_empty_events_is_unhealthy(self):
        healthy, _ = is_healthy([], "debug-table2")
        self.assertFalse(healthy)


if __name__ == "__main__":
    unittest.main()
