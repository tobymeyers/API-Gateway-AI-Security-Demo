import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from round_report import (  # noqa: E402
    build_metadata,
    parse_good_traffic_results,
    parse_adversarial_summary_line,
    parse_score_log_line,
    summarize_steps,
    build_run_summary,
)


class TestBuildMetadata(unittest.TestCase):
    def test_returns_inputs_only(self):
        meta = build_metadata("3", "Block nothing.", 60, 20, "/path/attack_script.json", "2026-10-24T20:13:37Z")
        self.assertEqual(meta, {
            "table_id": "3", "prompt": "Block nothing.", "round_seconds": 60, "target_rps": 20,
            "attack_script": "/path/attack_script.json", "started_at": "2026-10-24T20:13:37Z",
        })


class TestParseGoodTrafficResults(unittest.TestCase):
    def test_tallies_each_outcome(self):
        log_text = "\n".join([
            'GOOD_REQUEST_RESULT {"status_code": 200, "outcome": "allowed"}',
            'GOOD_REQUEST_RESULT {"status_code": 200, "outcome": "allowed"}',
            'GOOD_REQUEST_RESULT {"status_code": 403, "outcome": "denied"}',
            'GOOD_REQUEST_RESULT {"status_code": 500, "outcome": "errored"}',
        ])
        self.assertEqual(
            parse_good_traffic_results(log_text),
            {"total": 4, "allowed": 2, "denied": 1, "errored": 1},
        )

    def test_ignores_unrelated_log_lines(self):
        log_text = "\n".join([
            "some sbt/Gatling noise",
            'GOOD_REQUEST_RESULT {"status_code": 200, "outcome": "allowed"}',
        ])
        self.assertEqual(
            parse_good_traffic_results(log_text),
            {"total": 1, "allowed": 1, "denied": 0, "errored": 0},
        )

    def test_returns_all_zero_not_none_when_nothing_printed(self):
        """Unlike parse_adversarial_summary_line (which can return None),
        this tallies every line itself rather than reading one final
        summary - so a step that crashed before any request completed
        still gets a real (all-zero) dict, not a null."""
        self.assertEqual(
            parse_good_traffic_results("some unrelated log text\nwith no Gatling output"),
            {"total": 0, "allowed": 0, "denied": 0, "errored": 0},
        )


class TestSummarizeSteps(unittest.TestCase):
    def test_counts_total_and_failed(self):
        steps = [
            {"step": "trigger_agent", "status": "pass"},
            {"step": "replay_attack", "status": "fail"},
            {"step": "score_table", "status": "pass"},
        ]
        self.assertEqual(
            summarize_steps(steps),
            {"total": 3, "failed": 1, "failed_steps": ["replay_attack"]},
        )

    def test_empty_steps(self):
        self.assertEqual(summarize_steps([]), {"total": 0, "failed": 0, "failed_steps": []})


class TestParseAdversarialSummaryLine(unittest.TestCase):
    def test_parses_last_matching_line(self):
        log_text = "GET /x -> 200\nADVERSARIAL_SUMMARY {\"table_id\": \"1\", \"total\": 40, \"errors\": 0}"
        self.assertEqual(
            parse_adversarial_summary_line(log_text),
            {"table_id": "1", "total": 40, "errors": 0},
        )

    def test_returns_none_if_absent(self):
        self.assertIsNone(parse_adversarial_summary_line("no summary line here"))


class TestParseScoreLogLine(unittest.TestCase):
    def test_parses_score_log(self):
        log_text = 'SCORE_LOG {"table_id": "1", "final_score": 0, "minutes_scored": 2}'
        self.assertEqual(
            parse_score_log_line(log_text),
            {"table_id": "1", "final_score": 0, "minutes_scored": 2},
        )


class TestBuildRunSummary(unittest.TestCase):
    def test_combines_steps_score_and_traffic(self):
        log_text = "\n".join([
            'GOOD_REQUEST_RESULT {"status_code": 200, "outcome": "allowed"}',
            'GOOD_REQUEST_RESULT {"status_code": 403, "outcome": "denied"}',
            'ADVERSARIAL_SUMMARY {"table_id": "1", "total": 40, "status_counts": {"200": 39}, "errors": 0}',
            'SCORE_LOG {"table_id": "1", "final_score": 0, "minutes_scored": 2}',
            'TOOL_CALL {"table_id": "1", "tool": "get_current_config", "input": {}}',
        ])
        summary_jsonl_text = "\n".join([
            json.dumps({"step": "trigger_agent", "status": "pass"}),
            json.dumps({"step": "replay_attack", "status": "pass"}),
        ])
        result = build_run_summary(log_text, summary_jsonl_text, summary_jsonl_path="/tmp/x.summary.jsonl")
        self.assertEqual(result["steps"], {"total": 2, "failed": 0, "failed_steps": []})
        self.assertEqual(result["steps_log_path"], "/tmp/x.summary.jsonl")
        self.assertEqual(result["score"]["final_score"], 0)
        self.assertEqual(result["good_traffic"], {"total": 2, "allowed": 1, "denied": 1, "errored": 0})
        self.assertEqual(result["adversarial_traffic"]["total"], 40)
        self.assertEqual(len(result["agent_actions"]), 1)
        self.assertEqual(result["agent_actions"][0]["tool"], "get_current_config")

    def test_handles_empty_summary_jsonl(self):
        result = build_run_summary("no log lines", "")
        self.assertEqual(result["steps"], {"total": 0, "failed": 0, "failed_steps": []})
        self.assertNotIn("steps_log_path", result)
        self.assertIsNone(result["score"])
        self.assertEqual(result["agent_actions"], [])


if __name__ == "__main__":
    unittest.main()
