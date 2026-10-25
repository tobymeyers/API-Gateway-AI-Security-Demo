import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from cost_estimate import cost_of, parse_usage_log  # noqa: E402


class TestCostOf(unittest.TestCase):
    def test_zero_tokens_is_zero_cost(self):
        self.assertEqual(cost_of({"input_tokens": 0, "output_tokens": 0,
                                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}), 0.0)

    def test_known_values(self):
        # 1000 input @ $3/M + 200 output @ $15/M + 500 cache-write @ $3.75/M
        totals = {"input_tokens": 1000, "output_tokens": 200,
                   "cache_creation_input_tokens": 500, "cache_read_input_tokens": 0}
        expected = 1000 / 1e6 * 3.00 + 200 / 1e6 * 15.00 + 500 / 1e6 * 3.75
        self.assertAlmostEqual(cost_of(totals), expected, places=9)

    def test_missing_keys_default_to_zero(self):
        self.assertEqual(cost_of({}), 0.0)


class TestParseUsageLog(unittest.TestCase):
    def test_counts_and_sums_multiple_tables(self):
        content = (
            "some noise\n"
            'USAGE_LOG {"table_id": "1", "input_tokens": 1000, "output_tokens": 200, '
            '"cache_creation_input_tokens": 500, "cache_read_input_tokens": 0}\n'
            "more noise\n"
            'USAGE_LOG {"table_id": "2", "input_tokens": 2000, "output_tokens": 400, '
            '"cache_creation_input_tokens": 800, "cache_read_input_tokens": 0}\n'
        )
        calls, per_table, overall = parse_usage_log(content)
        self.assertEqual(calls, 2)
        self.assertEqual(set(per_table), {"1", "2"})
        self.assertEqual(per_table["1"]["input_tokens"], 1000)
        self.assertEqual(overall["input_tokens"], 3000)

    def test_zero_events_is_not_an_error(self):
        # This is the exact shape the real bug produced: a log with no
        # USAGE_LOG lines at all (the orchestrator crashed before ever
        # logging one) should parse cleanly to zero calls, not raise -
        # it's the CALLER's job (fetch_orchestrator_usage_logs /
        # orchestrator_health) to treat that as a failure, not this
        # parser's.
        calls, per_table, overall = parse_usage_log("nothing but noise here\n")
        self.assertEqual(calls, 0)
        self.assertEqual(per_table, {})
        self.assertEqual(overall["input_tokens"], 0)

    def test_malformed_json_is_skipped_not_fatal(self):
        content = 'USAGE_LOG {not valid json}\nUSAGE_LOG {"table_id": "1", "input_tokens": 5}\n'
        calls, per_table, overall = parse_usage_log(content)
        self.assertEqual(calls, 1)
        self.assertEqual(overall["input_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
