import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "load-testers", "adversarial-traffic"))

from replay_attack import (  # noqa: E402
    summarize_results, render_html, attack_successful, load_requests, is_lambda_deny_response,
    duration_exceeded,
)


class TestDurationExceeded(unittest.TestCase):
    def test_none_cap_never_exceeded(self):
        """No --max-duration-seconds given - old, uncapped behavior."""
        self.assertFalse(duration_exceeded(10_000, None))

    def test_under_cap_not_exceeded(self):
        self.assertFalse(duration_exceeded(10, 60))

    def test_at_cap_is_exceeded(self):
        self.assertTrue(duration_exceeded(60, 60))

    def test_over_cap_is_exceeded(self):
        self.assertTrue(duration_exceeded(74.8, 45))


class TestLoadRequests(unittest.TestCase):
    def test_current_format_dict_with_requests_key(self):
        data = {
            "metadata": {"model": "m", "min_requests": 1, "max_requests": 2, "max_total_seconds": 3},
            "requests": [{"type": "request", "method": "GET", "path": "/x"}],
        }
        self.assertEqual(load_requests(data), [{"type": "request", "method": "GET", "path": "/x"}])

    def test_older_bare_list_format_still_works(self):
        """Backward compatibility: files generated before the metadata
        section existed are just a plain list - must still replay."""
        data = [{"type": "request", "method": "GET", "path": "/x"}]
        self.assertEqual(load_requests(data), data)

    def test_dict_missing_requests_key_returns_empty_list(self):
        self.assertEqual(load_requests({"metadata": {}}), [])


class TestIsLambdaDenyResponse(unittest.TestCase):
    def test_our_deny_helper_body_is_recognized(self):
        """infra/template.yaml's deny() helper always returns exactly
        this shape - see Service1Function/Service2Function/Service3Function."""
        self.assertTrue(is_lambda_deny_response('{"approved": "false", "reason": "rate_limited"}'))

    def test_api_gateway_default_error_body_is_not_recognized(self):
        """API Gateway's own built-in rejection (e.g. a request that never
        matched any resource) returns this shape instead - confirmed live
        via a direct CloudWatch query showing zero Denied datapoints for a
        round whose log still had two "-> 403"s."""
        self.assertFalse(is_lambda_deny_response('{"message": "Missing Authentication Token"}'))

    def test_none_snippet_is_not_recognized(self):
        self.assertFalse(is_lambda_deny_response(None))

    def test_non_json_snippet_is_not_recognized(self):
        self.assertFalse(is_lambda_deny_response("<html>not json</html>"))

    def test_truncated_body_still_recognized(self):
        """response_snippet is only the first 300 chars - "approved"
        always appears first in deny()'s dict, so a truncated-but-valid
        JSON body (within 300 chars) still parses and matches."""
        self.assertTrue(is_lambda_deny_response('{"approved": "false", "reason": "stale_token"}'))


class TestAttackSuccessful(unittest.TestCase):
    def test_403_with_our_deny_body_is_blocked(self):
        self.assertFalse(attack_successful(403, None, '{"approved": "false", "reason": "rate_limited"}'))

    def test_403_with_api_gateway_body_is_unknown_not_blocked(self):
        """The real incident this distinction exists for: a bare path
        probe (e.g. "/service1/") that never satisfies the API's
        "{proxy+}" resource gets API Gateway's own 403, never reaching
        our Lambda at all - confirmed live via CloudWatch showing zero
        Denied datapoints. Labeling this False (as if our defense did
        something) would misrepresent what actually happened."""
        self.assertIsNone(attack_successful(403, None, '{"message": "Missing Authentication Token"}'))

    def test_403_with_no_snippet_is_unknown(self):
        """No response body captured at all - can't confirm which of the
        two 403 sources this was, so don't guess either way."""
        self.assertIsNone(attack_successful(403, None, None))

    def test_200_is_successful(self):
        self.assertTrue(attack_successful(200, None))

    def test_500_is_successful_not_blocked(self):
        """The exact regression this field exists for: a 500 means the
        request passed every defense check (deny() was never called,
        since that always returns exactly 403) and then hit an unrelated
        server error downstream - that's a successful attack, not a
        block. Observed live: round-table1-20261024-204132 scored +0
        (nothing blocked) while carrying one 500 in its log - conflating
        "non-200" with "blocked" would have reported that backwards."""
        self.assertTrue(attack_successful(500, None))

    def test_network_error_is_unknown_not_successful_or_blocked(self):
        self.assertIsNone(attack_successful(None, "Connection timeout"))

    def test_head_403_trusts_status_code_as_blocked(self):
        """HEAD responses never carry a body (RFC 7231), so
        is_lambda_deny_response() can never confirm anything for this
        method - confirmed live, a HEAD request that reached our Lambda
        and got a fully correct, independently-verified answer still
        arrived with an empty response_snippet. Status code alone is
        trusted for HEAD specifically - no response_snippet needed."""
        self.assertFalse(attack_successful(403, None, None, method="HEAD"))
        self.assertFalse(attack_successful(403, None, "", method="HEAD"))

    def test_head_200_trusts_status_code_as_successful(self):
        self.assertTrue(attack_successful(200, None, None, method="HEAD"))

    def test_head_network_error_is_still_unknown(self):
        """The error check still takes priority over the method special
        case - a HEAD request with no response at all is still
        genuinely ambiguous, not blocked."""
        self.assertIsNone(attack_successful(None, "Connection timeout", method="HEAD"))


class TestSummarizeResults(unittest.TestCase):
    def test_counts_by_status_code(self):
        results = [
            {"status_code": 200, "error": None, "attack_successful": True},
            {"status_code": 200, "error": None, "attack_successful": True},
            {"status_code": 403, "error": None, "attack_successful": False},
        ]
        summary = summarize_results(results)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["status_counts"], {"200": 2, "403": 1})
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["allowed"], 2)
        self.assertEqual(summary["denied"], 1)
        self.assertEqual(summary["errored"], 0)

    def test_errors_counted_separately_not_as_a_status_code(self):
        results = [{"status_code": None, "error": "Connection timeout", "attack_successful": None}]
        summary = summarize_results(results)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["status_counts"], {})
        self.assertEqual(summary["allowed"], 0)
        self.assertEqual(summary["denied"], 0)
        self.assertEqual(summary["errored"], 1)

    def test_ambiguous_403_counts_as_errored_not_denied(self):
        """A 403 that isn't our own deny() response (e.g. API Gateway's
        own routing rejection) - attack_successful() returns None, not
        False, for exactly this case. Must land in "errored", not
        "denied" - counting it as denied would overstate how much the
        agent actually blocked."""
        results = [{"status_code": 403, "error": None, "attack_successful": None}]
        summary = summarize_results(results)
        self.assertEqual(summary["allowed"], 0)
        self.assertEqual(summary["denied"], 0)
        self.assertEqual(summary["errored"], 1)

    def test_500_counts_as_allowed_not_denied(self):
        results = [{"status_code": 500, "error": None, "attack_successful": True}]
        summary = summarize_results(results)
        self.assertEqual(summary["allowed"], 1)
        self.assertEqual(summary["denied"], 0)
        self.assertEqual(summary["errored"], 0)

    def test_empty_results(self):
        self.assertEqual(
            summarize_results([]),
            {"total": 0, "status_counts": {}, "errors": 0, "allowed": 0, "denied": 0, "errored": 0},
        )


class TestRenderHtml(unittest.TestCase):
    def _result(self, **overrides):
        base = {
            "index": 1, "t_offset_seconds": 0.0, "method": "GET", "path": "/service1/status",
            "rationale": "baseline probe", "status_code": 200, "error": None, "latency_ms": 45.2,
            "attack_successful": True,
        }
        base.update(overrides)
        return base

    def test_blocked_request_renders_false_not_true(self):
        html = render_html("1", None, "t", [self._result(status_code=403, attack_successful=False)])
        self.assertIn("success-false", html)
        self.assertIn("FALSE", html)

    def test_500_renders_attack_successful_true_not_blocked(self):
        """The literal bug report this guards: a 500 must render as a
        successful attack (TRUE), never as blocked, even though its
        status badge is understandably red like a block would be."""
        html = render_html("1", None, "t", [self._result(status_code=500, attack_successful=True)])
        self.assertIn("success-true", html)
        self.assertIn("TRUE", html)

    def test_network_error_renders_as_na(self):
        html = render_html(
            "1", None, "t",
            [self._result(status_code=None, error="boom", attack_successful=None)],
        )
        self.assertIn("success-na", html)
        self.assertIn("N/A", html)

    def test_includes_table_id_and_prompt(self):
        html = render_html("3", "Block nothing.", "2026-10-24T20:13:37Z", [self._result()])
        self.assertIn("table 3", html)
        self.assertIn("Block nothing.", html)

    def test_handles_missing_prompt(self):
        html = render_html("3", None, "2026-10-24T20:13:37Z", [self._result()])
        self.assertIn("(not provided)", html)

    def test_escapes_rationale_to_prevent_html_injection(self):
        html = render_html("1", None, "t", [self._result(rationale="<script>alert(1)</script>")])
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_missing_rationale_shows_placeholder(self):
        html = render_html("1", None, "t", [self._result(rationale=None)])
        self.assertIn("(no rationale recorded)", html)

    def test_error_result_renders_without_status_code(self):
        html = render_html("1", None, "t", [self._result(status_code=None, error="boom")])
        self.assertIn("ERROR", html)
        self.assertIn("boom", html)

    def test_empty_results_still_renders_a_valid_page(self):
        html = render_html("1", None, "t", [])
        self.assertIn("<table>", html)
        self.assertIn("Requests sent:</strong> 0", html)

    def test_request_id_shown_when_present(self):
        """Lets a specific anomaly (e.g. the SigV4-parsing 403s seen live)
        be looked up by its exact CloudWatch execution-log entry instead
        of approximate timestamp-matching among concurrent requests."""
        html = render_html("1", None, "t", [self._result(request_id="abc-123-def")])
        self.assertIn("req: abc-123-def", html)

    def test_request_id_omitted_when_absent(self):
        """Network errors (no response at all) and older results
        predating this field both have no request_id - must not render
        a bare 'req: None'."""
        html = render_html("1", None, "t", [self._result()])
        self.assertNotIn("req: ", html)


if __name__ == "__main__":
    unittest.main()
