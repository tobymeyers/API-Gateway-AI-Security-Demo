import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from round_timeout_check import check  # noqa: E402


class TestCheck(unittest.TestCase):
    def test_regression_600s_round_against_300s_timeout(self):
        """The exact scenario found salvaging a killed comprehensive_gameday.sh run:
        a 600s round against the (at-the-time) 300s Lambda Timeout -
        every table's invocation was hard-killed, never caught before."""
        ok, message = check(round_seconds=600, lambda_timeout_seconds=300)
        self.assertFalse(ok)
        self.assertIn("hard-killed", message)

    def test_round_seconds_comfortably_under_timeout_passes(self):
        ok, _ = check(round_seconds=60, lambda_timeout_seconds=300)
        self.assertTrue(ok)

    def test_round_seconds_too_close_to_timeout_fails_even_if_technically_less(self):
        # 295 < 300, but with no buffer for the post-deadline reset_config()
        # call, this is still unsafe - the buffer check must catch it.
        ok, _ = check(round_seconds=295, lambda_timeout_seconds=300)
        self.assertFalse(ok)

    def test_round_seconds_equal_to_timeout_fails(self):
        ok, _ = check(round_seconds=300, lambda_timeout_seconds=300)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
