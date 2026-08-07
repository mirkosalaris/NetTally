import os
import sys
import tempfile
import unittest

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_folder import AppFolder
from db import (
    init_db,
    load_process_states,
    update_process_states,
    record_usage_deltas,
    query_usage_totals,
    query_usage_by_day
)

class TestAppFolder(unittest.TestCase):
    def setUp(self):
        self.folder = AppFolder()
        self.folder.exact_map = {
            "Google Chrome H.": "Google Chrome",
            "Code Helper": "Visual Studio Code"
        }
        self.folder.prefix_map = {
            "Google Chrome": "Google Chrome",
            "Claude": "Claude"
        }
        self.folder.suffix_patterns = [
            " Helper (Renderer)",
            " Helper"
        ]

    def test_folding_exact(self):
        self.assertEqual(self.folder.fold("Google Chrome H."), "Google Chrome")
        self.assertEqual(self.folder.fold("Code Helper"), "Visual Studio Code")

    def test_folding_suffix(self):
        self.assertEqual(self.folder.fold("Slack Helper"), "Slack")

    def test_folding_prefix(self):
        self.assertEqual(self.folder.fold("Claude Helper (GPU)"), "Claude")

    def test_folding_unknown(self):
        self.assertEqual(self.folder.fold("CustomProcess"), "CustomProcess")


class TestDatabaseAndDeltas(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_usage.db")
        init_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_db_init(self):
        states = load_process_states(self.db_path)
        self.assertEqual(states, {})

    def test_process_state_upsert(self):
        states = {
            (1234, "Chrome"): (100, 200, 1000.0)
        }
        update_process_states(self.db_path, states)
        loaded = load_process_states(self.db_path)
        self.assertIn((1234, "Chrome"), loaded)
        self.assertEqual(loaded[(1234, "Chrome")], (100, 200, 1000.0))

    def test_usage_daily_recording(self):
        day_1 = "2026-08-01"
        day_2 = "2026-08-02"

        # Record day 1 deltas
        record_usage_deltas(self.db_path, day_1, {"Google Chrome": (5000, 1000)})
        record_usage_deltas(self.db_path, day_1, {"Google Chrome": (3000, 500), "Slack": (2000, 100)})

        # Record day 2 deltas
        record_usage_deltas(self.db_path, day_2, {"Google Chrome": (10000, 2000)})

        # Check day 1 query
        by_day = query_usage_by_day(self.db_path, days=30)
        self.assertEqual(len(by_day), 3) # (day_2, Chrome), (day_1, Chrome), (day_1, Slack)

        # Check total query
        totals = query_usage_totals(self.db_path, days=30)
        chrome_total = next(r for r in totals if r["app_name"] == "Google Chrome")
        self.assertEqual(chrome_total["total_bytes_in"], 18000)
        self.assertEqual(chrome_total["total_bytes_out"], 3500)
        self.assertEqual(chrome_total["total_samples"], 3)

    def test_day_boundary_isolation(self):
        # Verify day boundary logic groups synthetic rows correctly into separate days
        day_a = "2026-08-06"
        day_b = "2026-08-07"

        record_usage_deltas(self.db_path, day_a, {"AppA": (100, 100)})
        record_usage_deltas(self.db_path, day_b, {"AppA": (200, 200)})

        records = query_usage_by_day(self.db_path, days=30)
        app_a_records = [r for r in records if r["app_name"] == "AppA"]
        self.assertEqual(len(app_a_records), 2)
        days = set(r["day"] for r in app_a_records)
        self.assertEqual(days, {day_a, day_b})

if __name__ == "__main__":
    unittest.main()
