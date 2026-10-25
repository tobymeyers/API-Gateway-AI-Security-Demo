import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "load-testers", "adversarial-traffic"))

from generate_attack_script import resolve_min_requests, build_output, openapi_spec_path  # noqa: E402


class TestOpenapiSpecPath(unittest.TestCase):
    def test_resolves_to_a_real_file(self):
        """Regression test for a real incident: fetch_openapi_spec() is a
        @beta_tool only ever invoked by the live Bedrock tool-use loop, so
        a wrong relative-path ".." count here is invisible to bash -n,
        py_compile, --help, and every other test - it only surfaces as a
        FileNotFoundError mid-generation, after real Bedrock tokens have
        already been spent. Confirmed live: exactly this happened after
        the load-test/ -> load-testers/ move added a directory level."""
        self.assertTrue(
            os.path.isfile(openapi_spec_path()),
            f"openapi_spec_path() does not resolve to a real file: {openapi_spec_path()}",
        )


class TestBuildOutput(unittest.TestCase):
    def test_metadata_records_the_minimum_inputs(self):
        requests_list = [{"type": "request", "method": "GET", "path": "/service1/status"}]
        output = build_output("us.anthropic.claude-sonnet-4-5-20250929-v1:0", 100, 150, 45, requests_list)
        self.assertEqual(output["metadata"], {
            "model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "min_requests": 100,
            "max_requests": 150,
            "max_total_seconds": 45,
        })

    def test_requests_list_preserved_unchanged(self):
        requests_list = [{"type": "request", "method": "GET", "path": "/service1/status"}]
        output = build_output("model", 1, 2, 3, requests_list)
        self.assertEqual(output["requests"], requests_list)

    def test_empty_requests_list(self):
        output = build_output("model", 0, 10, 20, [])
        self.assertEqual(output["requests"], [])


class TestResolveMinRequests(unittest.TestCase):
    def test_floor_under_ceiling_passes_through_unchanged(self):
        effective, note = resolve_min_requests(100, 150)
        self.assertEqual(effective, 100)
        self.assertIsNone(note)

    def test_floor_equal_to_ceiling_passes_through_unchanged(self):
        effective, note = resolve_min_requests(5, 5)
        self.assertEqual(effective, 5)
        self.assertIsNone(note)

    def test_floor_above_ceiling_is_clamped_with_a_note(self):
        """The real case this exists for: a disposable test run passes
        --max-requests 5 without overriding --min-requests off its
        100-request default - must not hand the model a contradictory
        "send at least 100 (budget is 5 max)" instruction."""
        effective, note = resolve_min_requests(100, 5)
        self.assertEqual(effective, 5)
        self.assertIsNotNone(note)
        self.assertIn("100", note)
        self.assertIn("5", note)

    def test_zero_floor_is_unconstrained_and_never_clamped(self):
        effective, note = resolve_min_requests(0, 150)
        self.assertEqual(effective, 0)
        self.assertIsNone(note)


if __name__ == "__main__":
    unittest.main()
