import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from ranking import format_report, parse_score_log, rank  # noqa: E402


SAMPLE_LOG = """
noise noise
SCORE_LOG {"table_id": "1", "final_score": -329, "minutes_scored": 11}
more noise
SCORE_LOG {"table_id": "2", "final_score": 56, "minutes_scored": 11}
SCORE_LOG {"table_id": "3", "final_score": -665, "minutes_scored": 11}
"""


class TestParseScoreLog(unittest.TestCase):
    def test_finds_all_entries(self):
        entries = parse_score_log(SAMPLE_LOG)
        self.assertEqual(len(entries), 3)
        self.assertEqual({e["table_id"] for e in entries}, {"1", "2", "3"})

    def test_no_entries_is_empty_list_not_error(self):
        self.assertEqual(parse_score_log("nothing here"), [])

    def test_malformed_json_is_skipped(self):
        entries = parse_score_log('SCORE_LOG {not valid}\nSCORE_LOG {"table_id": "1", "final_score": 5}')
        self.assertEqual(len(entries), 1)


class TestRank(unittest.TestCase):
    def test_sorted_best_final_score_first(self):
        entries = parse_score_log(SAMPLE_LOG)
        ranked = rank(entries)
        self.assertEqual([e["table_id"] for e in ranked], ["2", "1", "3"])


class TestFormatReport(unittest.TestCase):
    def test_empty_entries_has_clear_message(self):
        self.assertIn("No SCORE_LOG entries", format_report([]))

    def test_report_lists_winner_first(self):
        entries = parse_score_log(SAMPLE_LOG)
        report = format_report(entries)
        self.assertLess(report.index("table_id=2"), report.index("table_id=1"))
        self.assertLess(report.index("table_id=1"), report.index("table_id=3"))


if __name__ == "__main__":
    unittest.main()
