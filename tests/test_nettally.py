import contextlib
import io
import os
import sys
import tempfile
import unittest

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime
import json
from unittest.mock import MagicMock, patch

from app_folder import AppFolder
from collector import check_dark_wake_still_active, detect_and_classify_gap, parse_nettop_proc_id
from config import DEFAULTS, load_config
from db import (
    get_connection,
    init_db,
    load_process_states,
    query_usage_by_5m,
    query_usage_by_day,
    query_usage_by_hour,
    query_usage_totals,
    record_usage_deltas,
    update_process_states,
)
from html_generator import generate_html_report


class TestNettopProcIdParsing(unittest.TestCase):
    def test_numeric_pid_suffix(self):
        self.assertEqual(parse_nettop_proc_id("Google Chrome H.1535"), (1535, "Google Chrome H"))
        self.assertEqual(parse_nettop_proc_id("configd.557"), (557, "configd"))

    def test_non_numeric_suffix_is_deterministic(self):
        pid1, name1 = parse_nettop_proc_id("SomeHelper.Foo")
        pid2, name2 = parse_nettop_proc_id("SomeHelper.Foo")
        self.assertEqual((pid1, name1), (pid2, name2))
        self.assertEqual(name1, "SomeHelper.Foo")

    def test_no_dot_is_deterministic(self):
        pid1, name1 = parse_nettop_proc_id("NoDotProcess")
        pid2, name2 = parse_nettop_proc_id("NoDotProcess")
        self.assertEqual((pid1, name1), (pid2, name2))
        self.assertEqual(name1, "NoDotProcess")


class TestGapClassification(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_usage.db")
        init_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch("subprocess.run")
    def test_gap_classification_dark_wake(self, mock_run):
        mock_output = """
2026-08-16 14:00:00 +0200 Sleep                 Entering Sleep state due to 'Lid Close'
2026-08-16 14:05:00 +0200 DarkWake              DarkWake from Deep Idle
"""
        mock_res = MagicMock()
        mock_res.stdout = mock_output
        mock_run.return_value = mock_res

        dt_sleep = datetime.datetime.strptime("2026-08-16 14:00:00 +0200", "%Y-%m-%d %H:%M:%S %z")
        t0 = dt_sleep.timestamp() - 60
        t1 = dt_sleep.timestamp() + 600

        classification = detect_and_classify_gap(self.db_path, t0, t1)
        self.assertEqual(classification, "dark_wake_only")

    @patch("subprocess.run")
    def test_gap_classification_sleep_then_full_wake(self, mock_run):
        mock_output = """
2026-08-16 14:00:00 +0200 Sleep                 Entering Sleep state due to 'Lid Close'
2026-08-16 14:05:00 +0200 DarkWake              DarkWake from Deep Idle
2026-08-16 14:10:00 +0200 Wake                  Wake due to Power Button
"""
        mock_res = MagicMock()
        mock_res.stdout = mock_output
        mock_run.return_value = mock_res

        dt_sleep = datetime.datetime.strptime("2026-08-16 14:00:00 +0200", "%Y-%m-%d %H:%M:%S %z")
        t0 = dt_sleep.timestamp() - 60
        t1 = dt_sleep.timestamp() + 1000

        classification = detect_and_classify_gap(self.db_path, t0, t1)
        self.assertEqual(classification, "sleep_then_full_wake")

    @patch("subprocess.run")
    def test_gap_classification_unknown_gap(self, mock_run):
        mock_res = MagicMock()
        mock_res.stdout = ""
        mock_run.return_value = mock_res

        classification = detect_and_classify_gap(self.db_path, 1000, 2000)
        self.assertEqual(classification, "unknown_gap")

    @patch("subprocess.run")
    def test_still_dark_wake_propagates_with_no_new_events(self, mock_run):
        # Sustained dark-wake window (e.g. a long Power Nap sync): no new pmset log
        # lines since the last check. The previous poll was 'dark_wake_only' and nothing
        # contradicts it, so it should propagate forward rather than default to awake.
        mock_res = MagicMock()
        mock_res.stdout = ""
        mock_run.return_value = mock_res

        classification = check_dark_wake_still_active(self.db_path, 1000, 1030)
        self.assertEqual(classification, "dark_wake_only")

    @patch("subprocess.run")
    def test_still_dark_wake_propagates_on_darkwake_event(self, mock_run):
        # A DarkWake log line since the last check confirms we're still dark-waking.
        mock_output = "2026-08-16 14:05:00 +0200 DarkWake              DarkWake from Deep Idle\n"
        mock_res = MagicMock()
        mock_res.stdout = mock_output
        mock_run.return_value = mock_res

        dt = datetime.datetime.strptime("2026-08-16 14:05:00 +0200", "%Y-%m-%d %H:%M:%S %z")
        classification = check_dark_wake_still_active(
            self.db_path, dt.timestamp() - 30, dt.timestamp() + 30
        )
        self.assertEqual(classification, "dark_wake_only")

    @patch("subprocess.run")
    def test_still_dark_wake_breaks_chain_on_genuine_wake(self, mock_run):
        # A genuine Wake event since the last check means the dark-wake chain is over --
        # this poll (and the one that follows it) should be reclassified, not left
        # tagged as dark-wake or defaulted to plain awake.
        mock_output = "2026-08-16 14:05:00 +0200 Wake                  Wake due to Power Button\n"
        mock_res = MagicMock()
        mock_res.stdout = mock_output
        mock_run.return_value = mock_res

        dt = datetime.datetime.strptime("2026-08-16 14:05:00 +0200", "%Y-%m-%d %H:%M:%S %z")
        classification = check_dark_wake_still_active(
            self.db_path, dt.timestamp() - 30, dt.timestamp() + 30
        )
        self.assertEqual(classification, "sleep_then_full_wake")


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_missing_file(self):
        non_existent_path = os.path.join(self.temp_dir.name, "missing_config.json")
        cfg = load_config(non_existent_path)
        self.assertEqual(cfg, DEFAULTS)

    def test_malformed_json(self):
        malformed_path = os.path.join(self.temp_dir.name, "malformed_config.json")
        with open(malformed_path, "w", encoding="utf-8") as f:
            f.write("{invalid json: //, }")
        cfg = load_config(malformed_path)
        # Should gracefully fallback to defaults
        self.assertEqual(cfg, DEFAULTS)

    def test_malformed_json_warns_on_stderr(self):
        malformed_path = os.path.join(self.temp_dir.name, "malformed_config.json")
        with open(malformed_path, "w", encoding="utf-8") as f:
            f.write("{invalid json: //, }")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            load_config(malformed_path)
        self.assertIn("Failed to load config", err.getvalue())

    def test_partial_file(self):
        partial_path = os.path.join(self.temp_dir.name, "partial_config.json")
        with open(partial_path, "w", encoding="utf-8") as f:
            f.write("""{
              // comment
              "polling_interval_seconds": 15,
              "unknown_key": "hello"
            }""")
        cfg = load_config(partial_path)
        self.assertEqual(cfg["polling_interval_seconds"], 15)
        self.assertEqual(cfg["unknown_key"], "hello")
        # rest should be defaults
        self.assertEqual(cfg["html_top_apps_limit"], DEFAULTS["html_top_apps_limit"])


class TestAppFolder(unittest.TestCase):
    def setUp(self):
        self.folder = AppFolder()
        self.folder.exact_map = {
            "Google Chrome H.": "Google Chrome",
            "Code Helper": "Visual Studio Code",
        }
        self.folder.prefix_map = {
            "Google Chrome": "Google Chrome",
            "Claude": "Claude",
            "Code": "Visual Studio Code",
        }
        self.folder.suffix_patterns = [" Helper (Renderer)", " Helper"]

    def test_folding_exact(self):
        self.assertEqual(self.folder.fold("Google Chrome H."), "Google Chrome")
        self.assertEqual(self.folder.fold("Code Helper"), "Visual Studio Code")

    def test_folding_suffix(self):
        self.assertEqual(self.folder.fold("Slack Helper"), "Slack")

    def test_folding_prefix(self):
        self.assertEqual(self.folder.fold("Claude Helper (GPU)"), "Claude")
        self.assertEqual(self.folder.fold("Code Helper (Plugin)"), "Visual Studio Code")

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

    def test_sqlite_pragmas(self):
        conn = get_connection(self.db_path)
        cursor = conn.execute("PRAGMA journal_mode;")
        row = cursor.fetchone()
        self.assertEqual(row[0].lower(), "wal")
        conn.close()

    def test_process_state_upsert(self):
        states = {(1234, "Chrome"): (100, 200, 1000.0)}
        update_process_states(self.db_path, states)
        loaded = load_process_states(self.db_path)
        self.assertIn((1234, "Chrome"), loaded)
        self.assertEqual(loaded[(1234, "Chrome")], (100, 200, 1000.0))

    def test_usage_5m_recording_and_queries(self):
        day_str = datetime.date.today().isoformat()
        t1 = f"{day_str} 14:00"
        t2 = f"{day_str} 14:05"

        record_usage_deltas(self.db_path, t1, day_str, {"Google Chrome": (5000, 1000)})
        record_usage_deltas(
            self.db_path, t1, day_str, {"Google Chrome": (3000, 500), "Slack": (2000, 100)}
        )
        record_usage_deltas(self.db_path, t2, day_str, {"Google Chrome": (10000, 2000)})

        # 5m query
        by_5m = query_usage_by_5m(self.db_path, days=30)
        self.assertEqual(len(by_5m), 3)

        # Hourly query
        by_hour = query_usage_by_hour(self.db_path, days=30)
        chrome_hour = next(r for r in by_hour if r["app_name"] == "Google Chrome")
        self.assertEqual(chrome_hour["bytes_in"], 18000)

        # Daily query
        by_day = query_usage_by_day(self.db_path, days=30)
        self.assertEqual(len(by_day), 2)  # (Chrome, {day_str}) and (Slack, {day_str})

        # Total query
        totals = query_usage_totals(self.db_path, days=30)
        chrome_total = next(r for r in totals if r["app_name"] == "Google Chrome")
        self.assertEqual(chrome_total["total_bytes_in"], 18000)
        self.assertEqual(chrome_total["total_bytes_out"], 3500)
        self.assertEqual(chrome_total["total_samples"], 3)

    def test_day_boundary_isolation(self):
        # Rows on different days must be stored under separate day values
        # and must not be merged when querying by day.
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        today = datetime.date.today().isoformat()
        t_day_a = f"{yesterday} 23:55"
        t_day_b = f"{today} 00:00"

        record_usage_deltas(self.db_path, t_day_a, yesterday, {"AppA": (100, 100)})
        record_usage_deltas(self.db_path, t_day_b, today, {"AppA": (200, 200)})

        by_day = query_usage_by_day(self.db_path, days=30)
        app_a_records = [r for r in by_day if r["app_name"] == "AppA"]
        self.assertEqual(len(app_a_records), 2)
        days_found = set(r["day"] for r in app_a_records)
        self.assertEqual(days_found, {yesterday, today})

    def test_empty_html_generation(self):
        # Dashboard must render without error and show an empty-state message
        # when no usage data exists yet (fresh install).
        out_html = os.path.join(self.temp_dir.name, "dashboard_empty.html")
        path = generate_html_report(self.db_path, days=30, output_path=out_html)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            content = f.read()
            self.assertIn("NetTally", content)
            self.assertIn("No network usage records found", content)

    def test_html_generation(self):
        day_str = datetime.date.today().isoformat()
        t1 = f"{day_str} 14:00"
        record_usage_deltas(self.db_path, t1, day_str, {"Google Chrome": (1000, 500)})

        out_html = os.path.join(self.temp_dir.name, "dashboard.html")
        path = generate_html_report(self.db_path, days=30, output_path=out_html)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            content = f.read()
            self.assertIn("NetTally", content)
            self.assertIn("5-Min", content)
            self.assertIn("Hourly", content)
            self.assertIn("Daily", content)

    def test_layered_hourly_daily_sum_invariant(self):
        # Insert multiple 5m rows within the same hour/day with different classifications
        day_str = datetime.date.today().isoformat()
        record_usage_deltas(
            self.db_path, f"{day_str} 14:00", day_str, {"AppA": (100, 0)}, gap_classification=None
        )
        record_usage_deltas(
            self.db_path,
            f"{day_str} 14:05",
            day_str,
            {"AppA": (50, 0)},
            gap_classification="dark_wake_only",
        )
        record_usage_deltas(
            self.db_path,
            f"{day_str} 14:10",
            day_str,
            {"AppA": (25, 0)},
            gap_classification="sleep_then_full_wake",
        )
        record_usage_deltas(
            self.db_path, f"{day_str} 14:15", day_str, {"AppB": (300, 0)}, gap_classification=None
        )

        # Unfiltered aggregates
        un_hour = query_usage_by_hour(self.db_path, days=30)
        un_day = query_usage_by_day(self.db_path, days=30)

        # Layered exact-query aggregates using only_classification
        layers_hour = {
            "awake": query_usage_by_hour(self.db_path, days=30, only_classification="awake"),
            "dark_wake_only": query_usage_by_hour(
                self.db_path, days=30, only_classification="dark_wake_only"
            ),
            "sleep_then_full_wake": query_usage_by_hour(
                self.db_path, days=30, only_classification="sleep_then_full_wake"
            ),
        }
        layers_day = {
            "awake": query_usage_by_day(self.db_path, days=30, only_classification="awake"),
            "dark_wake_only": query_usage_by_day(
                self.db_path, days=30, only_classification="dark_wake_only"
            ),
            "sleep_then_full_wake": query_usage_by_day(
                self.db_path, days=30, only_classification="sleep_then_full_wake"
            ),
        }

        def key_hour(r):
            return (r["timestamp_hour"], r["app_name"])

        def key_day(r):
            return (r["day"], r["app_name"])

        un_map_hour = {key_hour(r): r["total_bytes"] for r in un_hour}
        sums_hour = {}
        for _cls, recs in layers_hour.items():
            for r in recs:
                k = key_hour(r)
                sums_hour[k] = sums_hour.get(k, 0) + r["total_bytes"]

        self.assertEqual(un_map_hour, sums_hour)

        un_map_day = {key_day(r): r["total_bytes"] for r in un_day}
        sums_day = {}
        for _cls, recs in layers_day.items():
            for r in recs:
                k = key_day(r)
                sums_day[k] = sums_day.get(k, 0) + r["total_bytes"]

        self.assertEqual(un_map_day, sums_day)

    def test_unknown_gap_counts_as_awake(self):
        day_str = datetime.date.today().isoformat()
        # One explicit awake row and one unknown_gap row in same hour
        record_usage_deltas(
            self.db_path, f"{day_str} 14:00", day_str, {"AppX": (100, 0)}, gap_classification=None
        )
        record_usage_deltas(
            self.db_path,
            f"{day_str} 14:05",
            day_str,
            {"AppX": (999, 0)},
            gap_classification="unknown_gap",
        )

        un_hour = query_usage_by_hour(self.db_path, days=30)
        layers_hour = {
            "awake": query_usage_by_hour(self.db_path, days=30, only_classification="awake"),
            "dark_wake_only": query_usage_by_hour(
                self.db_path, days=30, only_classification="dark_wake_only"
            ),
            "sleep_then_full_wake": query_usage_by_hour(
                self.db_path, days=30, only_classification="sleep_then_full_wake"
            ),
        }

        def key_hour(r):
            return (r["timestamp_hour"], r["app_name"])

        un_map_hour = {key_hour(r): r["total_bytes"] for r in un_hour}
        sums_hour = {}
        for _cls, recs in layers_hour.items():
            for r in recs:
                k = key_hour(r)
                sums_hour[k] = sums_hour.get(k, 0) + r["total_bytes"]

        # The unknown_gap row should be counted as awake so the invariant holds
        self.assertEqual(un_map_hour, sums_hour)

    def test_other_apps_series_uniform_across_layers(self):
        # Create 8 big apps in awake, and a small 9th app only in dark_wake_only
        day_str = datetime.date.today().isoformat()
        big_apps = [f"App{i}" for i in range(1, 9)]
        for a in big_apps:
            record_usage_deltas(
                self.db_path, f"{day_str} 14:00", day_str, {a: (10000, 0)}, gap_classification=None
            )
        # small app only in dark_wake_only
        record_usage_deltas(
            self.db_path,
            f"{day_str} 14:00",
            day_str,
            {"SmallApp": (50, 0)},
            gap_classification="dark_wake_only",
        )

        out_html = os.path.join(self.temp_dir.name, "dashboard_other.html")
        path = generate_html_report(self.db_path, days=30, output_path=out_html)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        start = content.find("const viewsData = ")
        self.assertNotEqual(start, -1)
        start += len("const viewsData = ")
        end = content.find(";\n", start)
        views = json.loads(content[start:end])

        hourly_layers = views["hourly"]["layers"]
        awake_ds = hourly_layers["awake"]["datasets"]
        dark_ds = hourly_layers["dark_wake_only"]["datasets"]

        # Both layers should have the same dataset count (including an Other Apps series)
        self.assertEqual(len(awake_ds), len(dark_ds))
        # Last dataset should be labeled "Other Apps"
        self.assertEqual(awake_ds[-1]["label"], "Other Apps")
        self.assertEqual(dark_ds[-1]["label"], "Other Apps")

    def test_exclude_classification_affects_layers(self):
        # Awake app and a sleep_then_full_wake app; exclude sleep_then_full_wake and ensure layer zeros
        day_str = datetime.date.today().isoformat()
        record_usage_deltas(
            self.db_path,
            f"{day_str} 14:00",
            day_str,
            {"KeepApp": (1000, 0)},
            gap_classification=None,
        )
        record_usage_deltas(
            self.db_path,
            f"{day_str} 14:00",
            day_str,
            {"DropApp": (777, 0)},
            gap_classification="sleep_then_full_wake",
        )

        out_html = os.path.join(self.temp_dir.name, "dashboard_exclude.html")
        path = generate_html_report(
            self.db_path,
            days=30,
            output_path=out_html,
            exclude_classifications=["sleep_then_full_wake"],
        )
        with open(path, encoding="utf-8") as f:
            content = f.read()
        start = content.find("const viewsData = ")
        self.assertNotEqual(start, -1)
        start += len("const viewsData = ")
        end = content.find(";\n", start)
        views = json.loads(content[start:end])

        sleep_layer = views["hourly"]["layers"]["sleep_then_full_wake"]["datasets"]
        # Sum of all values in the sleep layer should be zero because it was excluded
        total = sum(sum(ds["data"]) for ds in sleep_layer)
        self.assertEqual(total, 0)


if __name__ == "__main__":
    unittest.main()
