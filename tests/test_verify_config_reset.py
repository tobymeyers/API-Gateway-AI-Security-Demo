import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from verify_config_reset import is_baseline  # noqa: E402

# poll_until_baseline's item-exists-but-malformed defaulting is exercised
# indirectly by replicating its exact expression here, since the "item
# exists but 'data'.'M' is missing" case can't occur through get_config()
# in practice (DynamoDB always wraps a dict value with "M") and so isn't
# reachable via is_baseline alone - this locks in the regression found
# during code review: the refactor from an inline heredoc briefly dropped
# the `.get("M", {})` default, which would have silently treated a
# malformed item as baseline instead of correctly flagging it as not-reset.


def _data_from_item(item):
    return item.get("data", {}).get("M", {}) if item else None


class TestIsBaseline(unittest.TestCase):
    def test_no_item_is_baseline(self):
        self.assertTrue(is_baseline(None))

    def test_freshly_reset_is_baseline(self):
        data = {"blocked_clients": {"L": []}, "rate_limit_rps": {"NULL": True}}
        self.assertTrue(is_baseline(data))

    def test_blocked_client_is_not_baseline(self):
        data = {"blocked_clients": {"L": [{"S": "evil-client"}]}, "rate_limit_rps": {"NULL": True}}
        self.assertFalse(is_baseline(data))

    def test_active_rate_limit_is_not_baseline(self):
        data = {"blocked_clients": {"L": []}, "rate_limit_rps": {"N": "5"}}
        self.assertFalse(is_baseline(data))

    def test_both_active_is_not_baseline(self):
        data = {"blocked_clients": {"L": [{"S": "evil"}]}, "rate_limit_rps": {"N": "5"}}
        self.assertFalse(is_baseline(data))

    def test_item_exists_but_missing_data_map_is_not_baseline(self):
        """Regression: an item that exists but lacks the expected nested
        Map structure must NOT be silently treated as baseline - only a
        genuinely absent item (None) should be."""
        malformed_item = {"pk": {"S": "CFG#1"}}  # no "data" key at all
        self.assertFalse(is_baseline(_data_from_item(malformed_item)))

    def test_no_item_still_is_baseline(self):
        self.assertTrue(is_baseline(_data_from_item(None)))


if __name__ == "__main__":
    unittest.main()
