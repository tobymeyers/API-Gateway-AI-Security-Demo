import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from orchestrator_tool_calls import parse_tool_call_lines  # noqa: E402


class TestParseToolCallLines(unittest.TestCase):
    def test_parses_a_tool_call_line(self):
        messages = ['TOOL_CALL {"table_id": "10", "tool": "set_rate_limit", "input": {"requests_per_second": 5}}']
        result = parse_tool_call_lines(messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "TOOL_CALL")
        self.assertEqual(result[0]["tool"], "set_rate_limit")
        self.assertEqual(result[0]["input"], {"requests_per_second": 5})

    def test_parses_a_tool_result_line(self):
        messages = ['TOOL_RESULT {"table_id": "10", "tool": "set_rate_limit", "result": {"ok": true}}']
        result = parse_tool_call_lines(messages)
        self.assertEqual(result[0]["type"], "TOOL_RESULT")
        self.assertEqual(result[0]["result"], {"ok": True})

    def test_ignores_unrelated_log_lines(self):
        messages = [
            "START RequestId: abc Version: $LATEST",
            'USAGE_LOG {"table_id": "10", "model": "m"}',
            'TOOL_CALL {"table_id": "10", "tool": "block_client", "input": {"client_id": "x"}}',
            "END RequestId: abc",
        ]
        result = parse_tool_call_lines(messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tool"], "block_client")

    def test_cloudwatch_log_line_prefix_before_our_marker_is_tolerated(self):
        """Real CloudWatch messages sometimes carry a timestamp/request-id
        prefix before the actual print() output - the marker can appear
        partway through the line, not just at position 0."""
        messages = ['2026-10-24T23:12:30.123Z\t1234-5678\tTOOL_CALL {"table_id": "10", "tool": "get_current_config", "input": {}}']
        result = parse_tool_call_lines(messages)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tool"], "get_current_config")

    def test_empty_list_returns_empty(self):
        self.assertEqual(parse_tool_call_lines([]), [])

    def test_preserves_order(self):
        messages = [
            'TOOL_CALL {"table_id": "1", "tool": "a", "input": {}}',
            'TOOL_RESULT {"table_id": "1", "tool": "a", "result": {}}',
            'TOOL_CALL {"table_id": "1", "tool": "b", "input": {}}',
        ]
        result = parse_tool_call_lines(messages)
        self.assertEqual([r["tool"] for r in result], ["a", "a", "b"])


if __name__ == "__main__":
    unittest.main()
