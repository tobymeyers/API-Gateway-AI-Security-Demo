import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from attack_script_check import summarize, find_stale_token_risks, describe, load_requests  # noqa: E402


class TestLoadRequests(unittest.TestCase):
    def test_current_format_dict_with_requests_key(self):
        data = {
            "metadata": {"model": "m", "min_requests": 1, "max_requests": 2, "max_total_seconds": 3},
            "requests": [{"type": "request"}],
        }
        self.assertEqual(load_requests(data), [{"type": "request"}])

    def test_older_bare_list_format_still_works(self):
        data = [{"type": "request"}]
        self.assertEqual(load_requests(data), data)

    def test_dict_missing_requests_key_returns_empty_list(self):
        self.assertEqual(load_requests({"metadata": {}}), [])


class TestSummarize(unittest.TestCase):
    def test_counts_requests_and_sums_delay(self):
        script = [
            {"type": "request", "delay_seconds": 10},
            {"type": "wait", "delay_seconds": 5},
            {"type": "request", "delay_seconds": 20},
        ]
        count, total_delay = summarize(script)
        self.assertEqual(count, 2)
        self.assertEqual(total_delay, 35)

    def test_empty_script(self):
        self.assertEqual(summarize([]), (0, 0))

    def test_entries_without_delay_seconds_default_to_zero(self):
        script = [{"type": "request"}]
        count, total_delay = summarize(script)
        self.assertEqual(count, 1)
        self.assertEqual(total_delay, 0)

    def test_real_generated_script_shape(self):
        """Regression check against the real pacing mismatch this module
        was built to catch: a generator run with no pacing budget enforced
        can produce a script whose total delay exceeds the 60s round."""
        script = [{"type": "request", "delay_seconds": 8.5} for _ in range(10)]
        count, total_delay = summarize(script)
        self.assertEqual(count, 10)
        self.assertAlmostEqual(total_delay, 85.0)
        self.assertGreater(total_delay, 60)  # would NOT fit the default round length


class TestFindStaleTokenRisks(unittest.TestCase):
    def test_flags_the_real_incident_script(self):
        """The real attack_script.json that produced a +1 score against a
        do-nothing agent: service3/probe lands at 28.368s cumulative delay,
        within 5s of service3's 30s baseline max_token_age."""
        script = [
            {"type": "request", "path": "/service1/status", "delay_seconds": 0.0},
            {"type": "request", "path": "/service2/health", "delay_seconds": 15.257},
            {"type": "request", "path": "/service3/probe", "delay_seconds": 13.111},
        ]
        risks = find_stale_token_risks(script, margin=5.0)
        self.assertEqual(len(risks), 1)
        self.assertEqual(risks[0]["path"], "/service3/probe")

    def test_no_risk_when_comfortably_clear_of_baseline(self):
        script = [{"type": "request", "path": "/service3/probe", "delay_seconds": 1.0}]
        self.assertEqual(find_stale_token_risks(script, margin=5.0), [])

    def test_service1_has_no_baseline_so_never_flagged(self):
        script = [{"type": "request", "path": "/service1/status", "delay_seconds": 9999}]
        self.assertEqual(find_stale_token_risks(script, margin=5.0), [])

    def test_margin_zero_only_flags_requests_at_or_past_the_baseline_itself(self):
        script = [{"type": "request", "path": "/service3/probe", "delay_seconds": 29.0}]
        self.assertEqual(find_stale_token_risks(script, margin=0.0), [])
        script[0]["delay_seconds"] = 30.0
        self.assertEqual(len(find_stale_token_risks(script, margin=0.0)), 1)


class TestDescribe(unittest.TestCase):
    def test_renders_rationale_per_request(self):
        script = [
            {"type": "request", "method": "GET", "path": "/service1/status",
             "delay_seconds": 0.0, "rationale": "initial probe"},
        ]
        out = describe(script)
        self.assertIn("GET /service1/status", out)
        self.assertIn("initial probe", out)

    def test_missing_rationale_shows_placeholder(self):
        script = [{"type": "request", "method": "GET", "path": "/service1/status", "delay_seconds": 0.0}]
        self.assertIn("(no rationale recorded)", describe(script))


if __name__ == "__main__":
    unittest.main()
