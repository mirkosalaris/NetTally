"""Unit tests for NetTally: gap classification, config, folding, and DB behavior."""

import argparse
import ast
import contextlib
import csv
import datetime
import io
import json
import logging
import os
import plistlib
import pty
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import collector
import html_generator as chartjs_html_generator
from app_folder import AppFolder
from collector import check_dark_wake_still_active, detect_and_classify_gap, parse_nettop_proc_id
from config import DEFAULTS, load_config
from db import (
    get_connection,
    init_db,
    load_process_states,
    query_usage_by_5m,
    query_usage_by_5m_layered,
    query_usage_by_day,
    query_usage_by_hour,
    query_usage_coverage,
    query_usage_totals,
    record_usage_deltas,
    update_process_states,
)
from html_generator import build_coverage_summary, generate_html_report
from report import format_bytes, generate_totals_report, parse_exclude_classes


def extract_json_var(content: str, var_name: str):
    """Pull a `const <var_name> = {...};` payload out of a generated dashboard."""
    marker = f"const {var_name} = "
    start = content.find(marker)
    if start == -1:
        raise AssertionError(f"{var_name} not found in generated HTML")
    start += len(marker)
    end = content.find(";\n", start)
    if end == -1:
        raise AssertionError(f"unterminated {var_name} payload in generated HTML")
    return json.loads(content[start:end])


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
        with self.assertLogs("config", level="WARNING") as logs:
            cfg = load_config(malformed_path)
        # Should gracefully fallback to defaults ...
        self.assertEqual(cfg, DEFAULTS)
        # ... and say so in the log (also keeps the last-resort handler from
        # leaking the warning to the test run's real stderr).
        self.assertTrue(any("Failed to load config" in line for line in logs.output))

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


class TestExcludeParsing(unittest.TestCase):
    def test_none_or_empty_returns_none(self):
        parser = argparse.ArgumentParser()
        self.assertIsNone(parse_exclude_classes(parser, None))
        self.assertIsNone(parse_exclude_classes(parser, ""))

    def test_valid_classes_pass_through(self):
        parser = argparse.ArgumentParser()
        self.assertEqual(
            parse_exclude_classes(parser, "dark_wake_only, sleep_then_full_wake"),
            ["dark_wake_only", "sleep_then_full_wake"],
        )

    def test_unknown_class_errors(self):
        parser = argparse.ArgumentParser()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            parse_exclude_classes(parser, "awake_bucket")


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

    def test_csv_totals_round_trip(self):
        # An app name containing a comma exercises quoting in the csv writer.
        day_str = datetime.date.today().isoformat()
        record_usage_deltas(
            self.db_path, f"{day_str} 14:00", day_str, {"Chrome, Canary": (5000, 1000)}
        )

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            generate_totals_report(self.db_path, days=None, app_filter=None, fmt="csv")

        rows = list(csv.reader(io.StringIO(out.getvalue())))
        self.assertEqual(
            rows[0],
            [
                "App Name",
                "Total Received",
                "Total Sent",
                "Total Transfer",
                "Samples",
                "Earliest Day",
                "Latest Day",
            ],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1], ["Chrome, Canary", "5000", "1000", "6000", "1", day_str, day_str])

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

    def test_usage_coverage_reports_the_recorded_extent(self):
        # Coverage answers "was anything recorded here", so it must span the
        # whole window regardless of which classification the rows carry.
        today = datetime.date.today().isoformat()
        older = (datetime.date.today() - datetime.timedelta(days=4)).isoformat()
        record_usage_deltas(
            self.db_path,
            f"{older} 09:00",
            older,
            {"AppA": (10, 10)},
            gap_classification="dark_wake_only",
        )
        record_usage_deltas(self.db_path, f"{today} 14:05", today, {"AppA": (20, 20)})

        coverage = query_usage_coverage(self.db_path, days=30)
        self.assertEqual(coverage["first_bucket"], f"{older} 09:00")
        self.assertEqual(coverage["last_bucket"], f"{today} 14:05")
        self.assertEqual(coverage["bucket_count"], 2)
        self.assertEqual(coverage["day_count"], 2)

        # A narrower window must not report buckets that fall outside it.
        narrow = query_usage_coverage(self.db_path, days=1)
        self.assertEqual(narrow["first_bucket"], f"{today} 14:05")
        self.assertEqual(narrow["bucket_count"], 1)

    def test_usage_coverage_on_an_empty_database(self):
        coverage = query_usage_coverage(self.db_path, days=30)
        self.assertIsNone(coverage["first_bucket"])
        self.assertIsNone(coverage["last_bucket"])
        self.assertEqual(coverage["bucket_count"], 0)
        self.assertEqual(coverage["day_count"], 0)

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
        sums_hour: dict[tuple[str, str], int] = {}
        for _cls, recs in layers_hour.items():
            for r in recs:
                k = key_hour(r)
                sums_hour[k] = sums_hour.get(k, 0) + r["total_bytes"]

        self.assertEqual(un_map_hour, sums_hour)

        un_map_day = {key_day(r): r["total_bytes"] for r in un_day}
        sums_day: dict[tuple[str, str], int] = {}
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
        sums_hour: dict[tuple[str, str], int] = {}
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


class TestHtmlCoverageReporting(unittest.TestCase):
    """The report must state what it covers and only offer ranges that exist."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "coverage.db")
        init_db(self.db_path)

    def _generate(self, name="coverage.html", **kwargs):
        out_html = os.path.join(self.temp_dir.name, name)
        path = generate_html_report(self.db_path, days=30, output_path=out_html, **kwargs)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_summary_reports_extent_and_shortfall(self):
        summary = build_coverage_summary(
            {
                "first_bucket": "2026-09-08 18:15",
                "last_bucket": "2026-10-01 22:40",
                "bucket_count": 3214,
                "day_count": 22,
            },
            30,
        )
        self.assertIn("2026-09-08 18:15", summary)
        self.assertIn("2026-10-01 22:40", summary)
        self.assertIn("22 days with data", summary)
        self.assertIn("3,214 recorded 5-min buckets", summary)
        # The requested window is wider than the data: say so rather than imply
        # the whole 30 days are covered.
        self.assertIn("report window: the last 30 days", summary)

    def test_summary_for_a_fully_covered_window_omits_the_shortfall(self):
        summary = build_coverage_summary(
            {
                "first_bucket": "2026-09-08 18:15",
                "last_bucket": "2026-10-01 22:40",
                "bucket_count": 10,
                "day_count": 30,
            },
            30,
        )
        self.assertNotIn("report window", summary)
        self.assertIn("30 days with data", summary)

    def test_summary_without_data_says_so(self):
        summary = build_coverage_summary(
            {"first_bucket": None, "last_bucket": None, "bucket_count": 0, "day_count": 0}, 30
        )
        self.assertIn("No usage data recorded", summary)

    def test_banner_reports_the_covered_range(self):
        day_str = datetime.date.today().isoformat()
        record_usage_deltas(self.db_path, f"{day_str} 09:05", day_str, {"AppA": (10, 5)})
        record_usage_deltas(self.db_path, f"{day_str} 21:40", day_str, {"AppB": (10, 5)})

        content = self._generate()
        self.assertIn("Data available", content)
        self.assertIn(f"{day_str} 09:05", content)
        self.assertIn(f"{day_str} 21:40", content)
        self.assertIn("1 day with data", content)
        self.assertIn("2 recorded 5-min buckets", content)

    def test_date_inputs_are_bounded_by_the_recorded_extent(self):
        day_str = datetime.date.today().isoformat()
        record_usage_deltas(self.db_path, f"{day_str} 09:05", day_str, {"AppA": (10, 5)})
        record_usage_deltas(self.db_path, f"{day_str} 21:40", day_str, {"AppB": (10, 5)})

        content = self._generate()
        for field in ("startDate", "endDate"):
            with self.subTest(field=field):
                # datetime-local only parses the 'T' form; a ' ' separator would
                # be silently ignored by the browser and leave the input open.
                self.assertIn(f'id="{field}" min="{day_str}T09:05" max="{day_str}T21:40"', content)

    def test_report_without_data_has_no_bounds_and_says_nothing_was_recorded(self):
        content = self._generate("coverage_empty.html")
        self.assertIn("No usage data recorded", content)
        self.assertIn('id="startDate" min="" max=""', content)

    def test_excluded_classes_do_not_shrink_the_reported_coverage(self):
        # Coverage is about what was recorded, not about which classes the
        # report is showing, so --exclude must not move the bounds.
        day_str = datetime.date.today().isoformat()
        record_usage_deltas(
            self.db_path,
            f"{day_str} 03:00",
            day_str,
            {"AppA": (10, 5)},
            gap_classification="dark_wake_only",
        )
        record_usage_deltas(self.db_path, f"{day_str} 22:00", day_str, {"AppB": (10, 5)})

        content = self._generate(
            "coverage_excluded.html", exclude_classifications=["dark_wake_only"]
        )
        self.assertIn(f'id="startDate" min="{day_str}T03:00" max="{day_str}T22:00"', content)


class TestQueryUsageBy5mLayered(unittest.TestCase):
    """The layered query is the single source the table and the coarse views use."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "layered.db")
        init_db(self.db_path)
        self.day = datetime.date.today().isoformat()
        self.ts = f"{self.day} 10:05"

    def _seed_mixed_bucket(self):
        # One bucket, two classifications: a poll boundary split it, so one app
        # was recorded awake and another dark-wake in the same 5 minutes. A
        # single (bucket, app) row keeps one classification -- the primary key is
        # (timestamp_5m, app_name) and the last write wins -- so the split is
        # across apps, which is exactly how it happens in production.
        record_usage_deltas(self.db_path, self.ts, self.day, {"AppA": (100, 10)})
        record_usage_deltas(
            self.db_path, self.ts, self.day, {"AppB": (7, 3)}, gap_classification="dark_wake_only"
        )

    def test_layers_sum_to_the_unfiltered_query(self):
        self._seed_mixed_bucket()
        record_usage_deltas(
            self.db_path, self.ts, self.day, {"AppB": (1, 2)}, gap_classification="unknown_gap"
        )

        flat = query_usage_by_5m(self.db_path, days=30)
        layered = query_usage_by_5m_layered(self.db_path, days=30)
        self.assertEqual(len(layered), 2)

        by_app_flat: dict[str, int] = {}
        for r in flat:
            by_app_flat[r["app_name"]] = by_app_flat.get(r["app_name"], 0) + r["total_bytes"]
        by_app_layered: dict[str, int] = {}
        for r in layered:
            by_app_layered[r["app_name"]] = by_app_layered.get(r["app_name"], 0) + r["total_bytes"]
        self.assertEqual(by_app_layered, by_app_flat)

    def test_one_row_per_bucket_app_and_classification(self):
        self._seed_mixed_bucket()

        layered = query_usage_by_5m_layered(self.db_path, days=30)
        self.assertEqual(
            [(r["app_name"], r["gap_classification"]) for r in layered],
            [("AppA", None), ("AppB", "dark_wake_only")],
        )
        self.assertEqual([r["total_bytes"] for r in layered], [110, 10])

    def test_unknown_gap_is_kept_as_its_own_layer(self):
        # NULL and 'unknown_gap' both fold into the awake *view*, but the query
        # must not merge them away: --exclude and the stripe patterns key off it.
        record_usage_deltas(self.db_path, self.ts, self.day, {"AppA": (5, 5)})
        record_usage_deltas(
            self.db_path, self.ts, self.day, {"AppB": (6, 6)}, gap_classification="unknown_gap"
        )

        layered = query_usage_by_5m_layered(self.db_path, days=30)
        self.assertEqual(
            sorted(str(r["gap_classification"]) for r in layered), ["None", "unknown_gap"]
        )
        self.assertEqual({r["app_name"] for r in layered}, {"AppA", "AppB"})

    def test_exclude_drops_whole_layers(self):
        self._seed_mixed_bucket()

        layered = query_usage_by_5m_layered(
            self.db_path, days=30, exclude_classifications=["dark_wake_only"]
        )
        self.assertEqual([r["gap_classification"] for r in layered], [None])
        self.assertEqual([r["total_bytes"] for r in layered], [110])

    def test_app_filter_still_applies(self):
        self._seed_mixed_bucket()
        record_usage_deltas(self.db_path, self.ts, self.day, {"OtherApp": (9, 9)})

        layered = query_usage_by_5m_layered(self.db_path, days=30, app_filter="AppA")
        self.assertEqual({r["app_name"] for r in layered}, {"AppA"})


class TestHtmlTablePayload(unittest.TestCase):
    """The table data has to reproduce the plot's numbers exactly once filtered."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "table.db")
        init_db(self.db_path)
        self.day = datetime.date.today().isoformat()
        for minute in ("10:05", "10:10", "10:15"):
            record_usage_deltas(
                self.db_path, f"{self.day} {minute}", self.day, {"AppA": (100, 10), "AppB": (5, 1)}
            )
        # AppC lands in the same bucket as AppA but from a different poll state,
        # so the bucket itself is classified awake while carrying dark-wake rows.
        record_usage_deltas(
            self.db_path,
            f"{self.day} 10:10",
            self.day,
            {"AppC": (50, 5)},
            gap_classification="dark_wake_only",
        )

    def _generate(self, name="table.html", **kwargs):
        out_html = os.path.join(self.temp_dir.name, name)
        path = generate_html_report(self.db_path, days=30, output_path=out_html, **kwargs)
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _payloads(self, name="table.html", **kwargs):
        content = self._generate(name, **kwargs)
        return (
            extract_json_var(content, "viewsData"),
            extract_json_var(content, "tableData"),
            content,
        )

    def test_table_total_equals_the_server_side_grand_total(self):
        views, table, content = self._payloads()

        table_total = sum(
            entry["in"][k] + entry["out"][k]
            for entry in table["series"]
            for k in range(len(entry["i"]))
        )
        view_total = sum(
            ds["data"][i] or 0 for ds in views["5m"]["datasets"] for i in range(len(ds["data"]))
        )
        # The stat cards are server-rendered from the totals query, so the
        # client-side re-aggregation has to land on exactly that number: if it
        # drifts, filtering the dashboard silently changes the headline figures.
        server_total = sum(r["total_bytes"] or 0 for r in query_usage_totals(self.db_path, days=30))
        self.assertEqual(table_total, view_total)
        self.assertEqual(table_total, server_total)
        self.assertIn(format_bytes(server_total), content)

    def test_series_arrays_are_parallel_and_in_bucket_order(self):
        views, table, _ = self._payloads()

        for entry in table["series"]:
            lengths = {len(entry[key]) for key in ("i", "in", "out", "s")}
            self.assertEqual(lengths, {len(entry["i"])}, "series arrays disagree in length")

        # Delta-encoded indices must decode to strictly increasing positions
        # inside the 5-minute axis.
        for entry in table["series"]:
            index = 0
            for delta in entry["i"]:
                index += delta
                self.assertGreaterEqual(index, 0)
                self.assertLess(index, len(views["5m"]["labels"]))

    def test_bucket_classification_is_carried_once_per_bucket(self):
        views, table, _ = self._payloads()

        self.assertEqual(len(table["bucketClass"]), len(views["5m"]["labels"]))
        self.assertEqual(
            table["bucketClass"],
            [
                chartjs_html_generator.CLASSIFICATION_INDEX.get(cls, 0)
                for cls in views["5m"]["bucketClassification"]
            ],
        )
        # The per-row classification array is gone: it made the table and the
        # plot disagree on buckets a poll boundary split in two.
        for entry in table["series"]:
            self.assertNotIn("c", entry)

    def test_mixed_bucket_takes_the_most_severe_classification(self):
        views, table, _ = self._payloads()

        labels = views["5m"]["labels"]
        self.assertEqual(labels, [f"{self.day} 10:05", f"{self.day} 10:10", f"{self.day} 10:15"])
        # 10:10 holds an awake poll (AppA) and a dark-wake poll (AppC). A bar owns
        # the whole bucket, so it takes the most severe class present, and the
        # table has to select that same bucket with the same class.
        self.assertEqual(views["5m"]["bucketClassification"][1], "dark_wake_only")
        self.assertEqual(
            table["bucketClass"][1], chartjs_html_generator.CLASSIFICATION_INDEX["dark_wake_only"]
        )

        # The awake rows in that bucket stay in the payload. Dropping them here
        # would make the unfiltered totals disagree with the database; the filter
        # decides what to hide, so that the plot and the table always agree.
        app_a = table["series"][table["apps"].index("AppA")]
        self.assertEqual(sum(app_a["in"]), 300)

    def test_axis_controls_are_present_and_wired(self):
        # The scale switch and the empty-interval toggle are pure client-side
        # behaviour, so all the generator has to guarantee is that the controls
        # exist, default to the documented state, and call the handlers the
        # dashboard defines. The behaviour itself is exercised by the browser
        # checks in the dashboard, not from here.
        _, _, content = self._payloads()

        self.assertIn("onclick=\"setScale('logarithmic')\"", content)
        self.assertIn("onclick=\"setScale('linear')\"", content)
        self.assertIn(
            'id="chk-empty" checked onchange="setShowEmptyIntervals(this.checked)"', content
        )
        self.assertIn("function setScale(", content)
        self.assertIn("function setShowEmptyIntervals(", content)
        self.assertIn("function applyChartScale(", content)
        # Both axes stay stacked: an unstacked log chart would stop showing
        # per-app shares of a bucket.
        self.assertIn("stacked: true", content)
        # The gaps control must not touch the summary or the table, which read the
        # embedded axis rather than the plotted one.
        self.assertIn("function dataAxis(", content)
        self.assertIn("dataAxis('5m')", content)

    def test_every_app_in_the_window_gets_a_series(self):
        _, table, _ = self._payloads()

        self.assertEqual(sorted(table["apps"]), ["AppA", "AppB", "AppC"])
        self.assertEqual(len(table["series"]), len(table["apps"]))
        # Ordered by total bytes, descending, as the server-rendered table was.
        self.assertEqual(table["apps"], ["AppA", "AppC", "AppB"])  # by total, descending

    def test_excluded_classifications_leave_the_payload(self):
        _, table, _ = self._payloads("excluded.html", exclude_classifications=["dark_wake_only"])

        app_a = table["series"][table["apps"].index("AppA")]
        self.assertEqual(sum(app_a["in"]), 300)
        self.assertNotIn("AppC", table["apps"])

    def test_hourly_layers_share_the_five_minute_series_axis(self):
        views, _, _ = self._payloads()

        labels = [ds["label"] for ds in views["5m"]["datasets"]]
        for view_key in ("hourly", "daily"):
            for layer, layer_view in views[view_key]["layers"].items():
                with self.subTest(view=view_key, layer=layer):
                    # A layer that drops or reorders series would make the
                    # client-side combine sum the wrong apps together.
                    self.assertEqual([ds["label"] for ds in layer_view["datasets"]], labels)
                    for ds in layer_view["datasets"]:
                        self.assertEqual(len(ds["data"]), len(views[view_key]["labels"]))

    def test_layers_exist_even_for_a_classification_with_no_traffic(self):
        # --exclude only pre-unchecks a box; the layer has to stay embedded or the
        # user could never toggle it back on.
        views, _, _ = self._payloads("layers.html")

        self.assertNotIn("sleep_then_full_wake", views["5m"]["bucketClassification"])
        for view_key in ("hourly", "daily"):
            with self.subTest(view=view_key):
                self.assertEqual(
                    sorted(views[view_key]["layers"]),
                    sorted(chartjs_html_generator.CLASSIFICATIONS),
                )

    def test_no_unsubstituted_placeholders(self):
        _, _, content = self._payloads()
        self.assertNotIn("__TABLE_DATA_JSON__", content)
        self.assertNotIn("__VIEWS_DATA_JSON__", content)


class TestInstallerUserFileHandling(unittest.TestCase):
    """install.sh must never silently clobber a hand-edited config.json/app_map.json."""

    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    REPO_CONFIG = '{"polling_interval_seconds": 30}\n'
    USER_CONFIG = '{"polling_interval_seconds": 60}\n'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.src = os.path.join(self.tmp, "repo_config.json")
        self.dst = os.path.join(self.tmp, "deployed_config.json")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write(self.REPO_CONFIG)

    @staticmethod
    def _install_sh_function():
        """Extract install_user_file() from install.sh so the test can't drift from it."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "install.sh"
        )
        with open(path, encoding="utf-8") as f:
            text = f.read()
        start = text.index("install_user_file() {")
        end = text.index("\n}\n", start) + len("\n}\n")
        return text[start:end]

    def _run(self, answer=None, dst_content=None, use_tty=False):
        """Run the function in a bash subshell under `set -e`; echo a sentinel afterwards.

        The sentinel is what proves `set -e` did not abort the run mid-function. For the
        interactive cases a pty is used so `[ -t 0 ]` is genuinely true; the reply is
        written from a thread because the child blocks on `read` until it arrives.
        """
        if dst_content is not None:
            with open(self.dst, "w", encoding="utf-8") as f:
                f.write(dst_content)
        script = (
            f"{self._install_sh_function()}\n"
            f'install_user_file "{self.src}" "{self.dst}" "config.json"\n'
            "echo __DONE__\n"
        )
        if not use_tty:
            proc = subprocess.run(
                ["/bin/bash", "-c", script],
                input=(answer or "").encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
            )
        else:
            master, slave = pty.openpty()

            def reply_when_prompted():
                # Let the child reach `read`, then answer -- or, for the EOF case,
                # close the master so the pending read fails instead of blocking.
                time.sleep(0.2)
                if answer:
                    os.write(master, answer.encode())
                else:
                    os.close(master)

            feeder = threading.Thread(target=reply_when_prompted)
            feeder.start()
            try:
                proc = subprocess.run(
                    ["/bin/bash", "-c", script],
                    stdin=slave,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=20,
                )
            finally:
                feeder.join()
                for fd in (master, slave):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        return proc.stdout.decode().replace("\r\n", "\n")

    def _backups(self):
        return [n for n in os.listdir(self.tmp) if ".bak-" in n]

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_missing_destination_is_seeded_from_repo(self):
        out = self._run()
        self.assertEqual(self._read(self.dst), self.REPO_CONFIG)
        self.assertEqual(self._backups(), [])
        self.assertIn("__DONE__", out)

    def test_identical_file_is_left_alone(self):
        out = self._run(dst_content=self.REPO_CONFIG)
        self.assertEqual(self._read(self.dst), self.REPO_CONFIG)
        self.assertEqual(self._backups(), [])
        self.assertIn("already matches", out)
        self.assertIn("__DONE__", out)

    def test_prompt_keeps_user_file_on_no(self):
        out = self._run(answer="n\n", dst_content=self.USER_CONFIG, use_tty=True)
        self.assertEqual(self._read(self.dst), self.USER_CONFIG)
        self.assertEqual(self._backups(), [])
        self.assertIn("Keeping your", out)
        self.assertIn("__DONE__", out)

    def test_prompt_defaults_to_keeping_on_empty_answer(self):
        out = self._run(answer="\n", dst_content=self.USER_CONFIG, use_tty=True)
        self.assertEqual(self._read(self.dst), self.USER_CONFIG)
        self.assertEqual(self._backups(), [])
        self.assertIn("__DONE__", out)

    def test_prompt_replaces_and_backs_up_on_yes(self):
        out = self._run(answer="y\n", dst_content=self.USER_CONFIG, use_tty=True)
        self.assertEqual(self._read(self.dst), self.REPO_CONFIG)
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(self._read(os.path.join(self.tmp, backups[0])), self.USER_CONFIG)
        self.assertIn("Backed up", out)
        self.assertIn("__DONE__", out)

    def test_eof_at_prompt_keeps_file_without_aborting(self):
        """A closed stdin must not trip `set -e` on read's non-zero return."""
        out = self._run(dst_content=self.USER_CONFIG, use_tty=True)
        self.assertEqual(self._read(self.dst), self.USER_CONFIG)
        self.assertEqual(self._backups(), [])
        self.assertIn("__DONE__", out)

    def test_piped_stdin_keeps_file_and_explains_how_to_replace(self):
        out = self._run(answer="y\n", dst_content=self.USER_CONFIG)
        self.assertEqual(self._read(self.dst), self.USER_CONFIG)
        self.assertEqual(self._backups(), [])
        self.assertIn("No terminal attached", out)
        self.assertIn("__DONE__", out)


class TestCollectorLogging(unittest.TestCase):
    """The collector owns a rotating log file instead of relying on launchd."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        saved = (
            collector.LOG_DIR,
            collector.LOG_FILE,
            collector.LOG_MAX_BYTES,
            collector.LOG_BACKUP_COUNT,
        )

        def restore():
            for h in list(root.handlers):
                if h not in saved_handlers:
                    h.close()
                    root.removeHandler(h)
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)
            (
                collector.LOG_DIR,
                collector.LOG_FILE,
                collector.LOG_MAX_BYTES,
                collector.LOG_BACKUP_COUNT,
            ) = saved

        self.addCleanup(restore)
        collector.LOG_DIR = self.tmp
        collector.LOG_FILE = os.path.join(self.tmp, "collector.log")
        collector.LOG_MAX_BYTES = 512
        collector.LOG_BACKUP_COUNT = 2

    def test_log_is_written_to_the_collector_log_file(self):
        collector.setup_logging()
        logging.getLogger("test").info("hello from the collector")
        with open(collector.LOG_FILE, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("hello from the collector", content)
        self.assertIn("INFO", content)

    def test_log_rotation_is_bounded(self):
        collector.setup_logging()
        log = logging.getLogger("test")
        for i in range(200):
            log.info("padding line %d %s", i, "x" * 40)
        for h in logging.getLogger().handlers:
            h.flush()

        self.assertLessEqual(os.path.getsize(collector.LOG_FILE), collector.LOG_MAX_BYTES)
        rotated = [n for n in os.listdir(self.tmp) if n.startswith("collector.log.")]
        # backupCount backups, and no more: the cap is what stops the unbounded growth.
        self.assertEqual(len(rotated), collector.LOG_BACKUP_COUNT)
        self.assertNotIn("collector.log.3", rotated)

    def test_unwritable_log_dir_falls_back_to_stderr(self):
        blocked = os.path.join(self.tmp, "not-a-dir")
        with open(blocked, "w", encoding="utf-8") as f:
            f.write("x")
        collector.LOG_DIR = blocked
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            collector.setup_logging()
        logging.getLogger("test").info("still logged")
        self.assertIn("logging to stderr instead", stderr.getvalue())


class TestDaemonPlistAndStatusOutput(unittest.TestCase):
    """The plist and `nettally status` must agree on where the log lives."""

    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(self.root, "com.nettally.daemon.plist"), encoding="utf-8") as f:
            template = f.read()
        # The tracked plist is a template: install.sh substitutes these before it
        # is written to ~/Library/LaunchAgents. Do the same, which also proves the
        # placeholders are the only thing standing between it and a valid plist.
        for placeholder, value in (
            ("__HOME__", "/tmp/nettally-test"),
            ("__PYTHON__", "/usr/bin/python3"),
            ("__THROTTLE_INTERVAL__", "10"),
        ):
            template = template.replace(placeholder, value)
        self.plist = plistlib.loads(template.encode("utf-8"))
        with open(os.path.join(self.root, "nettally"), encoding="utf-8") as f:
            self.wrapper = f.read()

    def test_plist_keeps_stderr_for_startup_crashes(self):
        self.assertIn("StandardErrorPath", self.plist)
        self.assertIn("collector.err.log", self.plist["StandardErrorPath"])

    def test_plist_no_longer_redirects_stdout(self):
        self.assertNotIn("StandardOutPath", self.plist)

    def test_daemon_writes_nothing_to_stdout(self):
        """Only the foreground `--once` path prints, so dropping stdout is safe."""
        tree = ast.parse(self._collector_source())
        stdout_prints = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
            and not any(kw.arg == "file" for kw in node.keywords)
        ]
        once_guards = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and self._is_once_test(node.test)
        ]
        self.assertEqual(len(stdout_prints), 1, "collector.py grew a stdout write under the daemon")
        self.assertTrue(
            any(lineno > guard for lineno in stdout_prints for guard in once_guards),
            "the only stdout print must sit behind the --once guard",
        )

    def test_status_reports_the_collector_log_not_the_retired_one(self):
        self.assertIn("collector.log", self.wrapper)
        self.assertNotIn("collector.out.log", self.wrapper)

    def test_collector_defines_the_rotation_bounds(self):
        source = self._collector_source()
        self.assertIn("RotatingFileHandler", source)
        self.assertIn("LOG_BACKUP_COUNT", source)

    @staticmethod
    def _is_once_test(node):
        """True for the `if args.once:` guard, by shape rather than by source text."""
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "once"
            and isinstance(node.value, ast.Name)
            and node.value.id == "args"
        )

    def _collector_source(self):
        with open(os.path.join(self.root, "collector.py"), encoding="utf-8") as f:
            return f.read()


class TestVendoredChartJs(unittest.TestCase):
    """The dashboard must be self-contained: no CDN, nothing fetched at view time."""

    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def _generate(self):
        db = os.path.join(self.tmp, "usage.db")
        init_db(db)
        day_str = datetime.date.today().isoformat()
        record_usage_deltas(db, f"{day_str} 14:00", day_str, {"Safari": (1000, 2000)})
        out = os.path.join(self.tmp, "report.html")
        generate_html_report(db, days=7, output_path=out)
        with open(out, encoding="utf-8") as f:
            return f.read()

    def test_report_has_no_external_script_or_stylesheet(self):
        html = self._generate()
        self.assertEqual(re.findall(r"<script[^>]*\ssrc=", html), [])
        self.assertEqual(re.findall(r"<link[^>]*\shref=", html), [])

    def test_report_does_not_reference_a_cdn(self):
        html = self._generate()
        for host in ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com", "googleapis.com"):
            self.assertNotIn(host, html, f"generated report still points at {host}")

    def test_chart_js_is_inlined(self):
        html = self._generate()
        self.assertIn("Chart.js v4.4.7", html)
        self.assertIn(chartjs_html_generator.CHART_JS[:200], html)

    def test_report_is_one_self_contained_file(self):
        """Works from any --out path because the bundle travels inside the report."""
        html = self._generate()
        self.assertEqual(
            html.count("<script"), 2, "expected the inlined library plus the dashboard script"
        )

    def test_no_placeholders_are_left_unsubstituted(self):
        html = self._generate()
        self.assertEqual(re.findall(r"__[A-Z][A-Z0-9_]*__", html), [])

    def test_inline_payload_cannot_close_the_script_tag_early(self):
        """A literal </script> in the bundle would truncate the tag in the report."""
        self.assertNotIn("</script", chartjs_html_generator.CHART_JS)

    def test_loading_escapes_a_closing_script_tag(self):
        payload = "var a = '</script>';"
        path = os.path.join(self.tmp, "chart.umd.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
        original = chartjs_html_generator._CHARTJS_PATH
        chartjs_html_generator._CHARTJS_PATH = path
        try:
            loaded = chartjs_html_generator._load_chartjs()
        finally:
            chartjs_html_generator._CHARTJS_PATH = original
        self.assertNotIn("</script", loaded)
        self.assertIn("<\\/script", loaded)

    def test_vendored_bundle_is_the_pinned_version(self):
        with open(os.path.join(self.root, "templates", "chart.umd.js"), encoding="utf-8") as f:
            banner = f.read(200)
        self.assertIn("Chart.js v4.4.7", banner)
        self.assertIn("MIT License", banner)

    def test_vendored_bundle_has_no_sourcemap_pointer(self):
        with open(os.path.join(self.root, "templates", "chart.umd.js"), encoding="utf-8") as f:
            self.assertNotIn("sourceMappingURL", f.read())

    def test_license_is_vendored_next_to_the_bundle(self):
        with open(
            os.path.join(self.root, "templates", "CHARTJS-LICENSE.md"), encoding="utf-8"
        ) as f:
            self.assertIn("MIT License", f.read())

    def test_installer_copies_the_bundle_so_deployed_html_still_works(self):
        """install.sh must ship templates/chart.umd.js or the deployed report breaks."""
        with open(os.path.join(self.root, "install.sh"), encoding="utf-8") as f:
            installer = f.read()
        copied = set(re.findall(r'templates/([A-Za-z0-9_.-]+)"', installer))
        self.assertIn("chart.umd.js", copied)
        self.assertIn("CHARTJS-LICENSE.md", copied)

    def test_template_still_carries_the_placeholder(self):
        with open(
            os.path.join(self.root, "templates", "dashboard_template.html"), encoding="utf-8"
        ) as f:
            self.assertIn("__CHART_JS__", f.read())


if __name__ == "__main__":
    unittest.main()
