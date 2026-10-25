import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from roster import RosterError, find_duplicate_table_ids, parse_roster  # noqa: E402


class TestParseRoster(unittest.TestCase):
    def test_valid_roster(self):
        csv_text = 'table_id,prompt\n1,"Watch closely"\n2,"Be conservative"\n'
        rows = parse_roster(csv_text)
        self.assertEqual(rows, [
            {"table_id": "1", "prompt": "Watch closely"},
            {"table_id": "2", "prompt": "Be conservative"},
        ])

    def test_missing_column_raises(self):
        with self.assertRaises(RosterError):
            parse_roster("table_id,note\n1,x\n")

    def test_empty_field_raises(self):
        with self.assertRaises(RosterError):
            parse_roster('table_id,prompt\n1,""\n')

    def test_invalid_table_id_raises(self):
        with self.assertRaises(RosterError):
            parse_roster('table_id,prompt\n"1;DROP TABLE",bad\n')

    def test_no_data_rows_raises(self):
        with self.assertRaises(RosterError):
            parse_roster("table_id,prompt\n")

    def test_prompt_with_embedded_comma_is_quoted_correctly(self):
        csv_text = 'table_id,prompt\n1,"rate-limit, then block"\n'
        rows = parse_roster(csv_text)
        self.assertEqual(rows[0]["prompt"], "rate-limit, then block")


class TestFindDuplicateTableIds(unittest.TestCase):
    def test_no_duplicates(self):
        rows = [{"table_id": "1", "prompt": "a"}, {"table_id": "2", "prompt": "b"}]
        self.assertEqual(find_duplicate_table_ids(rows), [])

    def test_detects_duplicate(self):
        rows = [{"table_id": "1", "prompt": "a"}, {"table_id": "1", "prompt": "b"}]
        self.assertEqual(find_duplicate_table_ids(rows), ["1"])


if __name__ == "__main__":
    unittest.main()
