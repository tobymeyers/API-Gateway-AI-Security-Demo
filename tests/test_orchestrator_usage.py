import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

import orchestrator_usage  # noqa: E402


def _completed_process(returncode=0, stdout="", stderr=""):
    class _Result:
        pass

    result = _Result()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class TestFetchUsageLogEvents(unittest.TestCase):
    def test_parses_events_from_json_output(self):
        payload = json.dumps({"events": [{"message": "USAGE_LOG {}"}, {"message": "USAGE_LOG {}"}]})
        with patch("orchestrator_usage.subprocess.run", return_value=_completed_process(0, payload)):
            events = orchestrator_usage.fetch_usage_log_events("fn", "0", "us-west-2")
        self.assertEqual(len(events), 2)

    def test_empty_stdout_returns_empty_list(self):
        with patch("orchestrator_usage.subprocess.run", return_value=_completed_process(0, "")):
            events = orchestrator_usage.fetch_usage_log_events("fn", "0", "us-west-2")
        self.assertEqual(events, [])

    def test_nonzero_returncode_raises_runtime_error(self):
        with patch("orchestrator_usage.subprocess.run", return_value=_completed_process(1, "", "boom")):
            with self.assertRaises(RuntimeError):
                orchestrator_usage.fetch_usage_log_events("fn", "0", "us-west-2")


class TestMain(unittest.TestCase):
    def _run_main(self, argv):
        with patch.object(sys, "argv", ["orchestrator_usage.py"] + argv):
            orchestrator_usage.main()

    def test_exits_nonzero_when_zero_events_found(self):
        with patch("orchestrator_usage.fetch_usage_log_events", return_value=[]):
            with self.assertRaises(SystemExit) as ctx:
                self._run_main(["fn", "0", "us-west-2"])
        self.assertEqual(ctx.exception.code, 1)

    def test_exits_nonzero_on_cli_failure(self):
        with patch("orchestrator_usage.fetch_usage_log_events", side_effect=RuntimeError("boom")):
            with self.assertRaises(SystemExit) as ctx:
                self._run_main(["fn", "0", "us-west-2"])
        self.assertEqual(ctx.exception.code, 1)

    def test_does_not_exit_when_events_found(self):
        events = [{"message": "USAGE_LOG {}"}]
        with patch("orchestrator_usage.fetch_usage_log_events", return_value=events):
            # Should return normally (no SystemExit) once at least one event exists.
            self._run_main(["fn", "0", "us-west-2"])


if __name__ == "__main__":
    unittest.main()
