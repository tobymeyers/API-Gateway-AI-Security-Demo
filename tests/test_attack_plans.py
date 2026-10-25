import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "lib"))

from attack_plans import run_id_from_filename, pick_latest  # noqa: E402


class TestRunIdFromFilename(unittest.TestCase):
    def test_extracts_run_id(self):
        self.assertEqual(run_id_from_filename("attack-plan-20261024-193212.json"), "20261024-193212")

    def test_non_conforming_name_returns_none(self):
        self.assertIsNone(run_id_from_filename("attack_plan.json"))
        self.assertIsNone(run_id_from_filename("notes.txt"))
        self.assertIsNone(run_id_from_filename("attack-plan-20261024-193212.json.bak"))


class TestPickLatest(unittest.TestCase):
    def test_picks_the_most_recent_by_run_id(self):
        files = [
            "attack-plan-20260101-000000.json",
            "attack-plan-20261024-193212.json",
            "attack-plan-20260601-120000.json",
        ]
        self.assertEqual(pick_latest(files), "attack-plan-20261024-193212.json")

    def test_ignores_non_conforming_files_in_the_same_directory(self):
        files = ["attack-plan-20260101-000000.json", "README.md", ".DS_Store"]
        self.assertEqual(pick_latest(files), "attack-plan-20260101-000000.json")

    def test_empty_directory_listing_returns_none(self):
        self.assertIsNone(pick_latest([]))

    def test_no_conforming_files_returns_none(self):
        self.assertIsNone(pick_latest(["README.md", "attack_plan.json"]))

    def test_single_file_is_trivially_latest(self):
        self.assertEqual(pick_latest(["attack-plan-20261024-193212.json"]), "attack-plan-20261024-193212.json")


if __name__ == "__main__":
    unittest.main()
