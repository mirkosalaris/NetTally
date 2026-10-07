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
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import collector
import html_generator as chartjs_html_generator
import passthrough
import pause
import report
from app_folder import AppFolder
from collector import check_dark_wake_still_active, detect_and_classify_gap, parse_nettop_proc_id
from config import DEFAULTS, load_config
from db import (
    clear_process_states,
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
from pause import (
    clear_pause_state,
    format_duration,
    parse_duration,
    read_pause_state,
    write_pause_state,
)
from report import (
    format_bytes,
    generate_by_day_report,
    generate_totals_report,
    parse_exclude_classes,
)


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


_registry_patcher = None
_registry_tempdir = None


def setUpModule():
    """Point the pass-through registry at a throwaway path.

    The installed ~/Library/.../passthrough.json on a dev machine may already
    name live apps; a test that forgets an explicit registry must still see an
    empty one, or report output assertions become machine-dependent.
    """
    global _registry_patcher, _registry_tempdir
    _registry_tempdir = tempfile.mkdtemp(prefix="nettally-passthrough-")
    _registry_patcher = patch.object(
        passthrough,
        "DEFAULT_REGISTRY_PATH",
        os.path.join(_registry_tempdir, "passthrough.json"),
    )
    _registry_patcher.start()


def tearDownModule():
    if _registry_patcher is not None:
        _registry_patcher.stop()
    if _registry_tempdir is not None:
        shutil.rmtree(_registry_tempdir, ignore_errors=True)


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


class TestPassThroughDashboardToggle(unittest.TestCase):
    """The dashboard embeds pass-through rows but must describe the hidden default."""

    PASS_THROUGH = "GlobalProtect VPN"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "toggle.db")
        init_db(self.db_path)
        self.day = datetime.date.today().isoformat()
        # Eight non-pass-through apps that fill the default top-8 list, so the
        # tiny pass-through app would fold into "Other Apps" if the generator
        # partitioned on totals alone.
        for i, size in enumerate((8000, 7000, 6000, 5000, 4000, 3000, 2000, 1000), start=1):
            record_usage_deltas(self.db_path, f"{self.day} 10:05", self.day, {f"App{i}": (size, 0)})
        record_usage_deltas(
            self.db_path, f"{self.day} 10:05", self.day, {self.PASS_THROUGH: (10, 5)}
        )
        # The pass-through app's only other bucket is a dark-wake one: hiding
        # the rows must not change how the bucket itself is classified.
        record_usage_deltas(
            self.db_path,
            f"{self.day} 10:10",
            self.day,
            {self.PASS_THROUGH: (10, 5)},
            gap_classification="dark_wake_only",
        )

    def _payloads(self, name="toggle.html", **kwargs):
        out = os.path.join(self.temp_dir.name, name)
        path = generate_html_report(self.db_path, days=30, output_path=out, **kwargs)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        return (
            extract_json_var(content, "viewsData"),
            extract_json_var(content, "tableData"),
            extract_json_var(content, "passThroughNames"),
            content,
        )

    def test_hidden_rows_stay_embedded_with_their_own_series(self):
        views, table, names, _ = self._payloads(pass_through_apps=frozenset({self.PASS_THROUGH}))

        # The toggle can only reveal what was embedded, so the payload keeps
        # every app -- but the pass-through app must own a series instead of
        # being aggregated into "Other Apps", which could not be split apart
        # again at render time.
        self.assertEqual(names, [self.PASS_THROUGH])
        self.assertIn(self.PASS_THROUGH, table["apps"])
        labels = [ds["label"] for ds in views["5m"]["datasets"]]
        self.assertIn(self.PASS_THROUGH, labels)
        self.assertNotIn(chartjs_html_generator.OTHER_APP_LABEL, labels)
        gp = next(ds for ds in views["5m"]["datasets"] if ds["label"] == self.PASS_THROUGH)
        # The palette's muted gray (#94a3b8) is reserved for "Other Apps"; a
        # vivid color keeps the pass-through series from impersonating it.
        self.assertNotEqual(gp["backgroundColor"], "#94a3b8")
        for view_key in ("hourly", "daily"):
            for layer, layer_view in views[view_key]["layers"].items():
                with self.subTest(view=view_key, layer=layer):
                    self.assertIn(self.PASS_THROUGH, [ds["label"] for ds in layer_view["datasets"]])

    def test_server_rendered_state_describes_the_hidden_default(self):
        _, _, _, content = self._payloads(pass_through_apps=frozenset({self.PASS_THROUGH}))

        # 8000+...+1000 = 36000 visible bytes; the pass-through app's 30 are
        # embedded but excluded from the headline figures and table rows.
        self.assertIn(format_bytes(36000), content)
        self.assertNotIn(format_bytes(36030), content)
        self.assertNotIn(f"<strong>{self.PASS_THROUGH}</strong>", content)
        self.assertIn("<strong>App8</strong>", content)
        self.assertIn(f"[{self.PASS_THROUGH}]", content)

    def test_bucket_classification_still_counts_hidden_rows(self):
        views, _, _, _ = self._payloads(pass_through_apps=frozenset({self.PASS_THROUGH}))
        shown, _, _, _ = self._payloads("shown.html", pass_through_apps=frozenset())

        # bucketClassification is computed from the raw rows on purpose: a
        # hidden app must not be able to downgrade a dark-wake bucket to awake.
        self.assertIn("dark_wake_only", views["5m"]["bucketClassification"])
        self.assertEqual(views["5m"]["bucketClassification"], shown["5m"]["bucketClassification"])

    def test_toggle_markup_and_wiring(self):
        # Pure client-side behaviour: the generator only has to guarantee the
        # control exists, starts off, and calls the handlers the dashboard
        # defines -- same contract as the axis controls.
        _, _, _, content = self._payloads(pass_through_apps=frozenset({self.PASS_THROUGH}))

        self.assertIn('id="chk-passthrough" onchange="setShowPassThrough(this.checked)"', content)
        self.assertNotIn('id="chk-passthrough" checked', content)
        self.assertIn("function setShowPassThrough(", content)
        self.assertIn("state.showPassThrough = false", content)
        self.assertIn("function isPassThrough(", content)
        # A hidden series must not sit in the legend as an all-zero entry.
        self.assertIn("isPassThrough(item.text)", content)
        # The filter group disappears entirely when nothing is registered.
        self.assertIn('id="passThroughGroup"', content)
        self.assertIn("passThroughLower.size === 0", content)
        # Summary, table and selection line all follow the toggle.
        self.assertIn("isPassThrough(name)", content)
        self.assertIn("isPassThrough(app)", content)
        self.assertIn("pass-through hidden", content)
        self.assertIn("chk-passthrough').checked = false", content)
        # The empty-slot rule must not keep a bucket whose only bytes are hidden.
        self.assertIn("hasVisibleTraffic(view.datasets, at, hiddenFlags)", content)

    def test_all_placeholders_are_substituted(self):
        registered = self._payloads(pass_through_apps=frozenset({self.PASS_THROUGH}))
        for _, _, _, content in (registered, self._payloads("plain.html")):
            self.assertEqual(re.findall(r"__[A-Z][A-Z0-9_]*__", content), [])

    def test_no_registered_apps_carries_no_toggle_state(self):
        # Default registry (patched to an empty file for the whole module):
        # nothing hidden, so the dashboard must not claim otherwise.
        _, _, names, content = self._payloads("none.html")

        self.assertEqual(names, [])
        self.assertIn('id="passThroughNote"></span>', content)
        # The group markup exists but window.onload hides it on this payload.
        self.assertIn("passThroughLower.size === 0", content)


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


class TestStatusErrLogLabel(unittest.TestCase):
    """`nettally status` must not present a stale collector.err.log as current.

    The err log is only written by a failure before logging is configured, so a
    healthy daemon never rewrites it: without a timestamp the pre-logging-rewrite
    file would sit under "Startup/Crash Output" forever.
    """

    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.log_dir = os.path.join(self.tmp, "Library", "Logs", "NetTally")
        os.makedirs(self.log_dir)
        self.err_log = os.path.join(self.log_dir, "collector.err.log")

    def _status(self):
        env = dict(os.environ, HOME=self.tmp)
        proc = subprocess.run(
            [os.path.join(self.root, "nettally"), "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=30,
        )
        return proc.returncode, proc.stdout.decode()

    def test_status_labels_the_err_log_with_its_last_written_time(self):
        with open(self.err_log, "w", encoding="utf-8") as f:
            f.write("Error running nettop: Command timed out after 10 seconds\n")
        stale = datetime.datetime(2026, 9, 26, 10, 1).timestamp()
        os.utime(self.err_log, (stale, stale))

        returncode, out = self._status()

        self.assertEqual(returncode, 0, out)
        self.assertIn("Startup/Crash Output", out)
        self.assertIn("last written 2026-09-26 10:01", out)

    def test_status_omits_the_block_when_the_err_log_is_empty(self):
        with open(self.err_log, "w", encoding="utf-8") as f:
            f.write("")

        returncode, out = self._status()

        self.assertEqual(returncode, 0, out)
        self.assertNotIn("Startup/Crash Output", out)


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


def require_pause_state(path=None):
    """read_pause_state with the None case asserted away, for terser assertions."""
    state = read_pause_state(path)
    if state is None:
        raise AssertionError(
            f"expected a pause state file at {path or pause.DEFAULT_PAUSE_STATE_PATH}"
        )
    return state


class _StopLoop(Exception):
    """Breaks out of collector.main()'s otherwise infinite polling loop."""


class FakeClock:
    """time.time() with a manually applied offset.

    Advancing the offset between phases simulates a multi-hour pause without
    making the clock run backwards or fast for anything else in the process.
    """

    def __init__(self):
        self.offset = 0.0
        # Bound up front: while the clock is installed, `time.time` is this method.
        self._real_time = time.time

    def time(self):
        return self._real_time() + self.offset

    def advance(self, seconds):
        self.offset += seconds


class ScriptedNettop:
    """Stands in for fetch_nettop_sample with one app whose counters only climb.

    `bump()` simulates traffic that happens while nobody is watching, which is
    exactly the case a pause has to survive without misattributing it.
    """

    def __init__(self, step=1000):
        self.step = step
        self.bytes = 0
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.bytes += self.step
        return [(4242, "FakeApp", self.bytes, self.bytes)]

    def bump(self, amount):
        self.bytes += amount


class TestPauseDurationParsing(unittest.TestCase):
    def test_bare_number_is_seconds(self):
        self.assertEqual(parse_duration("90"), 90)

    def test_single_units(self):
        self.assertEqual(parse_duration("90s"), 90)
        self.assertEqual(parse_duration("45m"), 2700)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("1d"), 86400)

    def test_compound_durations_sum(self):
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("3 m 0 s"), 180)

    def test_case_and_spacing_are_forgiving(self):
        self.assertEqual(parse_duration(" 2H "), 7200)

    def test_rejects_garbage_rather_than_reinterpreting_it(self):
        """'5x' must fail, not quietly become 5 seconds."""
        for text in ("", "0", "abc", "5x", "2h!", "1.5h", "-5m", "m"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_duration(text)

    def test_format_duration_is_compact_and_human_readable(self):
        cases = {45: "45s", 90: "1m 30s", 3600: "1h", 5400: "1h 30m", 3661: "1h 1m 1s"}
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), expected)

    def test_format_duration_covers_days(self):
        self.assertEqual(format_duration(86400), "1d")
        self.assertEqual(format_duration(90000), "1d 1h")


class TestPauseStateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "nested", "pause_state.json")
        os.makedirs(os.path.dirname(self.path))

    def test_round_trip(self):
        write_pause_state(None, self.path)
        state = require_pause_state(self.path)
        self.assertIsNone(state["until_epoch"])
        self.assertGreater(state["paused_at"], 0)

    def test_timed_pause_records_the_deadline(self):
        write_pause_state(time.time() + 3600, self.path)
        state = require_pause_state(self.path)
        self.assertAlmostEqual(state["until_epoch"], state["paused_at"] + 3600, delta=5)

    def test_write_leaves_no_temp_file_behind(self):
        """The collector reads this file mid-poll; it must never see a partial one."""
        write_pause_state(None, self.path)
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.path)) if ".tmp." in n], [])

    def test_missing_file_means_not_paused(self):
        self.assertIsNone(read_pause_state(self.path))

    def test_unreadable_file_means_not_paused(self):
        """A corrupt pause file must never be able to wedge the collector."""
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        # assertLogs both keeps logging's last-resort handler from leaking the
        # warning to the test run's real stderr and pins the one diagnostic an
        # operator would grep collector.log for.
        with self.assertLogs("pause", level="WARNING") as logs:
            self.assertIsNone(read_pause_state(self.path))
        self.assertTrue(any("unreadable pause state file" in line for line in logs.output))

    def test_wrongly_typed_fields_are_rejected(self):
        for payload in (
            "[]",
            '{"paused_at":"nope"}',
            '{"paused_at":123,"until_epoch":"soon"}',
            '{"until_epoch":null}',
            '{"paused_at":true}',
        ):
            with self.subTest(payload=payload):
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(payload)
                # Every malformed shape has to be rejected *and* say so: a silent
                # fallback would leave a corrupt file looking like a deliberate
                # pause to whoever reads the log.
                with self.assertLogs("pause", level="WARNING") as logs:
                    self.assertIsNone(read_pause_state(self.path))
                self.assertEqual(len(logs.output), 1)
                self.assertIn("pause state file", logs.output[0])

    def test_indefinite_pause_never_expires(self):
        write_pause_state(None, self.path)
        state = require_pause_state(self.path)
        self.assertTrue(pause.pause_is_active(state, now=time.time() + 10**9))

    def test_timed_pause_expires_against_the_wall_clock(self):
        """Comparing epoch deadlines is what lets a timeout survive a reboot."""
        write_pause_state(1000.0, self.path)
        state = require_pause_state(self.path)
        self.assertTrue(pause.pause_is_active(state, now=999.0))
        self.assertFalse(pause.pause_is_active(state, now=1000.0))
        self.assertEqual(pause.seconds_remaining(state, now=900.0), 100.0)

    def test_no_state_is_never_active(self):
        self.assertFalse(pause.pause_is_active(None))
        self.assertIsNone(pause.seconds_remaining(None))

    def test_clear_reports_whether_there_was_anything_to_remove(self):
        write_pause_state(None, self.path)
        self.assertTrue(clear_pause_state(self.path))
        self.assertFalse(clear_pause_state(self.path))
        self.assertIsNone(read_pause_state(self.path))


class TestPauseCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "pause_state.json")

    def _run(self, *args):
        result = subprocess.run(
            [sys.executable, "pause.py", *args, "--state", self.path],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True,
            text=True,
        )
        return result

    def test_pause_without_duration_pauses_indefinitely(self):
        result = self._run("pause")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("indefinitely", result.stdout)
        self.assertIsNone(require_pause_state(self.path)["until_epoch"])

    def test_pause_with_duration_sets_a_deadline(self):
        result = self._run("pause", "--for", "2h")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = require_pause_state(self.path)
        self.assertAlmostEqual(state["until_epoch"] - state["paused_at"], 7200, delta=5)

    def test_bad_duration_is_a_usage_error(self):
        result = self._run("pause", "--for", "5x")
        self.assertEqual(result.returncode, 2)
        self.assertIsNone(read_pause_state(self.path))

    def test_pausing_again_replaces_the_previous_deadline(self):
        self._run("pause")
        result = self._run("pause", "--for", "45m")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already paused", result.stdout)
        state = require_pause_state(self.path)
        self.assertAlmostEqual(state["until_epoch"] - state["paused_at"], 2700, delta=5)

    def test_resume_clears_the_pause(self):
        self._run("pause", "--for", "2h")
        result = self._run("resume")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resumed tracking", result.stdout)
        self.assertIsNone(read_pause_state(self.path))

    def test_resume_when_not_paused_is_a_no_op(self):
        result = self._run("resume")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("was not paused", result.stdout)

    def test_state_reports_paused_and_active(self):
        self.assertIn("active", self._run("state").stdout)
        self._run("pause")
        self.assertIn("PAUSED", self._run("state").stdout)

    def test_state_json_is_machine_readable(self):
        self._run("pause", "--for", "2h")
        payload = json.loads(self._run("state", "--format", "json").stdout)
        self.assertTrue(payload["paused"])
        self.assertGreater(payload["until_epoch"], time.time())
        self.assertGreater(payload["seconds_remaining"], 0)


class TestCollectorHonorsPause(unittest.TestCase):
    """End-to-end behavior of the pause state inside the collector's poll loop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db = os.path.join(self.tmp, "usage.db")
        init_db(self.db)
        self.state_path = os.path.join(self.tmp, "pause_state.json")

        self.clock = FakeClock()
        self.nettop = ScriptedNettop()
        # Point the module default at a temp file so the real installed pause
        # state is never touched by a test run.
        patcher = patch.object(pause, "DEFAULT_PAUSE_STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, collector, "RUNNING", True)

    def _run_cycles(self, count, pause_at=None, resume_at=None, pause_bytes=0, pause_seconds=7200):
        """Run exactly `count` iterations of one collector.main() polling loop.

        `pause_at` / `resume_at` are 1-based cycle numbers at which the pause
        state file is written / cleared, and `pause_bytes` is traffic that
        accumulates while nobody is polling. They fire from inside a *single*
        main() run on purpose: the collector's in-memory poll timing is what
        decides whether the pause is later mistaken for a sleep, and that state
        only exists within one process, exactly as in production.
        """

        def scripted_read_pause_state(state_path=None):
            # main() calls this exactly once per loop iteration, so it is both
            # the hook that scripts the transitions and the one place the test
            # can bound an otherwise infinite loop.
            self._cycle += 1
            if self._cycle > count:
                raise _StopLoop
            if self._cycle == pause_at:
                write_pause_state(None, self.state_path)
                self.nettop.bump(pause_bytes)
                self.clock.advance(pause_seconds)
            elif self._cycle == resume_at:
                clear_pause_state(self.state_path)
            return read_pause_state(state_path)

        self._cycle = 0
        collector.RUNNING = True
        with (
            patch.object(sys, "argv", ["collector.py", "--db", self.db, "--interval", "1"]),
            patch.object(collector, "setup_logging"),
            patch.object(collector, "read_pause_state", scripted_read_pause_state),
            patch.object(collector, "fetch_nettop_sample", self.nettop),
            patch.object(collector.time, "sleep", lambda _seconds: None),
            patch.object(collector.time, "time", self.clock.time),
        ):
            with contextlib.suppress(_StopLoop):
                collector.main()
        return self._cycle

    def _total_bytes(self):
        conn = get_connection(self.db)
        with conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(bytes_in + bytes_out), 0) AS total FROM usage_5m"
            ).fetchone()
        conn.close()
        return row["total"]

    def _classifications(self):
        conn = get_connection(self.db)
        with conn:
            rows = conn.execute("SELECT DISTINCT gap_classification FROM usage_5m").fetchall()
        conn.close()
        return {row["gap_classification"] for row in rows}

    def test_records_usage_when_not_paused(self):
        self._run_cycles(3)
        self.assertEqual(self.nettop.calls, 3)
        self.assertGreater(self._total_bytes(), 0)

    def test_paused_collector_neither_polls_nor_writes(self):
        """Cycles 3-6 are paused; only the two before and nothing after should poll."""
        self._run_cycles(6, pause_at=3, resume_at=99)
        self.assertEqual(self.nettop.calls, 2, "paused cycles still called nettop")

    def test_pause_writes_no_usage_rows(self):
        self._run_cycles(2)
        bytes_after_two = self._total_bytes()
        self._run_cycles(6, pause_at=1, resume_at=99)
        self.assertEqual(self._total_bytes(), bytes_after_two)

    def test_resume_does_not_dump_the_pause_window_into_one_bucket(self):
        """The regression this feature exists to prevent.

        Byte counters are cumulative, so polling after a pause against stale
        baselines would attribute the whole window's traffic to a single 5-minute
        bucket -- which the existing gap classifier would then stamp as awake.
        Cycles: 1-2 active, 3-4 paused, 5-6 active again.
        """
        self._run_cycles(6, pause_at=3, resume_at=5, pause_bytes=50_000_000)

        total = self._total_bytes()
        self.assertGreater(total, 0, "recording did not resume")
        self.assertLess(total, 50_000_000, "the 50 MB pause window leaked into usage_5m")

    def test_pause_stops_recording_even_with_traffic_flowing(self):
        """The paused window is a hole in the data, by design."""
        self._run_cycles(4, pause_at=2, resume_at=99, pause_bytes=50_000_000)

        self.assertEqual(self.nettop.calls, 1, "only the pre-pause cycle should have polled")
        self.assertLess(self._total_bytes(), 50_000_000)

    def test_resume_does_not_run_gap_detection(self):
        """A deliberate pause is not a sleep, so pmset must not be consulted.

        Without last_poll_epoch advancing through the paused cycles, the resume
        poll looks two hours late and the collector would go run pmset over the
        pause window.
        """
        with patch.object(collector, "detect_and_classify_gap") as detect:
            self._run_cycles(6, pause_at=3, resume_at=5)
        detect.assert_not_called()
        self.assertEqual(self._classifications(), {None})

    def test_expired_pause_resumes_without_help(self):
        self._run_cycles(1)
        calls_before = self.nettop.calls
        write_pause_state(self.clock.time() - 1, self.state_path)

        self._run_cycles(1)

        self.assertEqual(self.nettop.calls, calls_before + 1)
        self.assertIsNone(
            read_pause_state(self.state_path), "an expired pause file should be cleaned up"
        )

    def test_pause_state_is_left_to_the_status_command_not_the_database(self):
        """A pause is not a sleep: nothing about it may reach usage_5m.

        It is deliberately *not* appended to the write-only `gaps` audit trail
        either. Doing so would depend on the collector observing both ends of the
        pause, which silently loses the row whenever launchd restarts the daemon
        in between -- incomplete audit data is worse than none. collector.log and
        `nettally status` already report the window.
        """
        self._run_cycles(2)
        write_pause_state(None, self.state_path)
        self.clock.advance(3600)
        self._run_cycles(2)
        clear_pause_state(self.state_path)
        self._run_cycles(2)

        conn = get_connection(self.db)
        with conn:
            gaps = conn.execute("SELECT classification FROM gaps").fetchall()
            usage_classes = conn.execute(
                "SELECT DISTINCT gap_classification FROM usage_5m"
            ).fetchall()
        conn.close()

        self.assertEqual(gaps, [], "a pause must not create a gap row")
        self.assertEqual({row["gap_classification"] for row in usage_classes}, {None})

    def test_pausing_drops_the_byte_baselines(self):
        """They are what would otherwise turn the pause window into one bucket."""
        self._run_cycles(2)
        self.assertNotEqual(load_process_states(self.db), {})

        write_pause_state(None, self.state_path)
        self._run_cycles(1)

        self.assertEqual(load_process_states(self.db), {})

    def test_once_refuses_to_poll_while_paused(self):
        write_pause_state(None, self.state_path)
        buffer = io.StringIO()
        with (
            patch.object(sys, "argv", ["collector.py", "--db", self.db, "--once"]),
            patch.object(collector, "setup_logging"),
            patch.object(collector, "fetch_nettop_sample", self.nettop),
            contextlib.redirect_stdout(buffer),
        ):
            collector.main()

        self.assertEqual(self.nettop.calls, 0)
        self.assertEqual(self._total_bytes(), 0)
        self.assertIn("paused", buffer.getvalue().lower())

    def test_restarting_during_a_pause_still_recovers_cleanly(self):
        """launchd can restart the daemon at any time, including mid-pause."""
        self._run_cycles(2)
        bytes_before = self._total_bytes()
        write_pause_state(None, self.state_path)
        self.clock.advance(3600)
        self.nettop.bump(20_000_000)
        self._run_cycles(1)

        # Fresh process, same DB, still paused: nothing stale should survive.
        states = load_process_states(self.db)
        self.assertEqual(states, {})

        clear_pause_state(self.state_path)
        self._run_cycles(2)
        self.assertLess(self._total_bytes() - bytes_before, 10_000)


class TestClearProcessStates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.db = os.path.join(self.tmp, "usage.db")
        init_db(self.db)

    def test_removes_every_baseline(self):
        update_process_states(self.db, {(1, "App"): (10, 20, time.time())})
        self.assertEqual(len(load_process_states(self.db)), 1)

        clear_process_states(self.db)

        self.assertEqual(load_process_states(self.db), {})

    def test_is_safe_on_an_empty_table(self):
        clear_process_states(self.db)
        clear_process_states(self.db)
        self.assertEqual(load_process_states(self.db), {})


class TestPassThroughRegistry(unittest.TestCase):
    """passthrough.json round-trip: fold on add, tolerate broken files, atomic writes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "passthrough.json")
        self.app_map = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app_map.json"
        )

    def test_missing_file_is_an_empty_registry(self):
        self.assertEqual(passthrough.read_registry(self.path), frozenset())

    def test_add_then_read_round_trip(self):
        changed, canonical = passthrough.add_name("GlobalProtect VPN", self.path)
        self.assertTrue(changed)
        self.assertEqual(canonical, "GlobalProtect VPN")
        self.assertEqual(passthrough.read_registry(self.path), frozenset({"GlobalProtect VPN"}))

    def test_add_folds_raw_nettop_names(self):
        changed, canonical = passthrough.add_name("PanGPS", self.path, config_path=self.app_map)
        self.assertTrue(changed)
        self.assertEqual(canonical, "GlobalProtect VPN")
        self.assertEqual(passthrough.read_registry(self.path), frozenset({"GlobalProtect VPN"}))

    def test_add_is_idempotent_case_insensitively(self):
        passthrough.add_name("GlobalProtect VPN", self.path)
        changed, _ = passthrough.add_name("globalprotect vpn", self.path)
        self.assertFalse(changed)
        self.assertEqual(passthrough.read_registry(self.path), frozenset({"GlobalProtect VPN"}))

    def test_remove_accepts_the_raw_spelling(self):
        passthrough.add_name("PanGPS", self.path, config_path=self.app_map)
        removed = passthrough.remove_name("PanGPS", self.path, config_path=self.app_map)
        self.assertEqual(removed, "GlobalProtect VPN")
        self.assertEqual(passthrough.read_registry(self.path), frozenset())

    def test_remove_unknown_returns_none(self):
        self.assertIsNone(passthrough.remove_name("NotRegistered", self.path))

    def test_corrupt_file_reads_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        with self.assertLogs("passthrough", level="WARNING"):
            self.assertEqual(passthrough.read_registry(self.path), frozenset())

    def test_wrong_shape_reads_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"pass_through_apps": "nope"}, f)
        with self.assertLogs("passthrough", level="WARNING"):
            self.assertEqual(passthrough.read_registry(self.path), frozenset())

    def test_writes_leave_no_temp_file_behind(self):
        passthrough.add_name("GlobalProtect VPN", self.path)
        self.assertEqual(os.listdir(self.tmp), ["passthrough.json"])

    def test_exclude_rows_drops_matches_case_insensitively(self):
        rows = [
            {"app_name": "GlobalProtect VPN", "bytes_in": 700},
            {"app_name": "OneDrive", "bytes_in": 150},
        ]
        kept, matched = passthrough.exclude_rows(rows, frozenset({"globalprotect vpn"}))
        self.assertEqual([r["app_name"] for r in kept], ["OneDrive"])
        self.assertEqual(matched, ["GlobalProtect VPN"])

    def test_exclude_rows_resolves_the_live_registry_by_default(self):
        passthrough.add_name("GlobalProtect VPN", self.path)
        rows = [{"app_name": "GlobalProtect VPN"}, {"app_name": "OneDrive"}]
        with patch.object(passthrough, "DEFAULT_REGISTRY_PATH", self.path):
            kept, matched = passthrough.exclude_rows(rows, None)
        self.assertEqual([r["app_name"] for r in kept], ["OneDrive"])
        self.assertEqual(matched, ["GlobalProtect VPN"])

    def test_exclude_rows_reports_only_names_actually_present(self):
        rows = [{"app_name": "OneDrive"}]
        kept, matched = passthrough.exclude_rows(rows, frozenset({"GlobalProtect VPN", "OneDrive"}))
        self.assertEqual(kept, [])
        self.assertEqual(matched, ["OneDrive"])

    def test_exclude_rows_without_any_registry_keeps_everything(self):
        rows = [{"app_name": "GlobalProtect VPN"}]
        kept, matched = passthrough.exclude_rows(rows, frozenset())
        self.assertEqual(kept, rows)
        self.assertEqual(matched, [])


class TestPassThroughReportExclusion(unittest.TestCase):
    """Reports hide pass-through apps by default and can be told to include them."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "usage.db")
        init_db(self.db_path)
        self.registry = os.path.join(self.temp_dir.name, "passthrough.json")
        day_str = datetime.date.today().isoformat()
        record_usage_deltas(
            self.db_path,
            f"{day_str} 10:00",
            day_str,
            {"GlobalProtect VPN": (700, 300), "OneDrive": (100, 50)},
        )
        record_usage_deltas(self.db_path, f"{day_str} 10:05", day_str, {"OneDrive": (10, 5)})
        with open(self.registry, "w", encoding="utf-8") as f:
            json.dump({"pass_through_apps": ["GlobalProtect VPN"]}, f)

    def test_table_report_excludes_and_notes_the_footer(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            generate_totals_report(
                self.db_path,
                days=None,
                app_filter=None,
                fmt="table",
                pass_through_apps=frozenset({"GlobalProtect VPN"}),
            )
        text = out.getvalue()
        self.assertIn("[Pass-through apps excluded: GlobalProtect VPN]", text)
        # Exactly one occurrence: the note, not also a data row.
        self.assertEqual(text.count("GlobalProtect VPN"), 1)
        self.assertIn("OneDrive", text)
        self.assertIn("across 1 apps", text)
        # Grand total must be the surviving app's bytes only: 150 + 15.
        self.assertIn("Total 165 B across 1 apps", text)

    def test_table_report_defaults_to_the_live_registry(self):
        out = io.StringIO()
        with patch.object(passthrough, "DEFAULT_REGISTRY_PATH", self.registry):
            with contextlib.redirect_stdout(out):
                generate_totals_report(self.db_path, days=None, app_filter=None, fmt="table")
        self.assertEqual(out.getvalue().count("GlobalProtect VPN"), 1)
        self.assertIn("across 1 apps", out.getvalue())

    def test_explicit_empty_set_includes_everything(self):
        out = io.StringIO()
        with patch.object(passthrough, "DEFAULT_REGISTRY_PATH", self.registry):
            with contextlib.redirect_stdout(out):
                generate_totals_report(
                    self.db_path,
                    days=None,
                    app_filter=None,
                    fmt="table",
                    pass_through_apps=frozenset(),
                )
        text = out.getvalue()
        self.assertNotIn("Pass-through apps excluded", text)
        self.assertIn("GlobalProtect VPN", text)
        self.assertIn("across 2 apps", text)

    def test_csv_excludes_and_notes_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            generate_totals_report(
                self.db_path,
                days=None,
                app_filter=None,
                fmt="csv",
                pass_through_apps=frozenset({"GlobalProtect VPN"}),
            )
        rows = list(csv.reader(io.StringIO(out.getvalue())))
        names = [row[0] for row in rows[1:]]
        self.assertEqual(names, ["OneDrive"])
        self.assertIn("[Pass-through apps excluded: GlobalProtect VPN]", err.getvalue())
        # The note must not leak into the machine-readable stream.
        self.assertNotIn("Pass-through", out.getvalue())

    def test_json_excludes_and_notes_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            generate_totals_report(
                self.db_path,
                days=None,
                app_filter=None,
                fmt="json",
                pass_through_apps=frozenset({"GlobalProtect VPN"}),
            )
        payload = json.loads(out.getvalue())
        self.assertEqual([row["app_name"] for row in payload], ["OneDrive"])
        self.assertIn("[Pass-through apps excluded: GlobalProtect VPN]", err.getvalue())

    def test_daily_report_excludes_too(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            generate_by_day_report(
                self.db_path,
                days=None,
                app_filter=None,
                fmt="table",
                pass_through_apps=frozenset({"GlobalProtect VPN"}),
            )
        text = out.getvalue()
        self.assertEqual(text.count("GlobalProtect VPN"), 1)
        self.assertIn("OneDrive", text)

    def _run_main(self, *extra):
        argv = ["report.py", "--db", self.db_path, "--format", "json", *extra]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                report.main()
        return out.getvalue(), err.getvalue()

    def test_main_excludes_by_default(self):
        with patch.object(passthrough, "DEFAULT_REGISTRY_PATH", self.registry):
            out, err = self._run_main()
        payload = json.loads(out)
        self.assertEqual([row["app_name"] for row in payload], ["OneDrive"])
        self.assertIn("[Pass-through apps excluded", err)

    def test_main_include_passthrough_flag_restores_it(self):
        with patch.object(passthrough, "DEFAULT_REGISTRY_PATH", self.registry):
            out, err = self._run_main("--include-passthrough")
        payload = json.loads(out)
        self.assertEqual(
            sorted(row["app_name"] for row in payload), ["GlobalProtect VPN", "OneDrive"]
        )
        self.assertNotIn("[Pass-through apps excluded", err)


class TestPassThroughCli(unittest.TestCase):
    """`nettally passthrough add|remove|list` as a real subprocess, like pause.py's CLI."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.registry = os.path.join(self.tmp, "passthrough.json")
        self.db = os.path.join(self.tmp, "usage.db")
        init_db(self.db)
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "passthrough.py", *args, "--registry", self.registry],
            cwd=self.root,
            capture_output=True,
            text=True,
        )

    def test_add_list_remove_cycle(self):
        result = self._run("add", "GlobalProtect VPN")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Registered", result.stdout)
        self.assertEqual(passthrough.read_registry(self.registry), frozenset({"GlobalProtect VPN"}))

        result = self._run("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GlobalProtect VPN", result.stdout)

        result = self._run("remove", "GlobalProtect VPN")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Removed", result.stdout)
        self.assertEqual(passthrough.read_registry(self.registry), frozenset())

        result = self._run("list")
        self.assertIn("No pass-through apps registered", result.stdout)

    def test_add_twice_is_a_no_op(self):
        self._run("add", "GlobalProtect VPN")
        result = self._run("add", "GlobalProtect VPN")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already registered", result.stdout)
        self.assertEqual(len(passthrough.read_registry(self.registry)), 1)

    def test_add_suggests_close_recorded_names(self):
        day_str = datetime.date.today().isoformat()
        record_usage_deltas(self.db, f"{day_str} 10:00", day_str, {"Google Chrome": (100, 0)})
        result = self._run("add", "Google Chrom", "--db", self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Closest recorded apps", result.stdout)
        self.assertIn("Google Chrome", result.stdout)
        self.assertEqual(passthrough.read_registry(self.registry), frozenset({"Google Chrom"}))

    def test_add_with_no_database_still_registers(self):
        missing = os.path.join(self.tmp, "not-there.db")
        result = self._run("add", "Some VPN", "--db", missing)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Registered", result.stdout)
        self.assertEqual(passthrough.read_registry(self.registry), frozenset({"Some VPN"}))
        self.assertFalse(os.path.exists(missing))

    def test_remove_unknown_says_so(self):
        result = self._run("remove", "NotRegistered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Not registered", result.stdout)

    def test_quiet_list_prints_only_names(self):
        result = self._run("list", "--quiet")
        self.assertEqual(result.stdout, "")
        self._run("add", "GlobalProtect VPN")
        result = self._run("list", "--quiet")
        self.assertEqual(result.stdout.strip(), "GlobalProtect VPN")


class TestPassThroughIsDeployedWithTheRestOfTheRuntime(unittest.TestCase):
    """report.py imports passthrough.py, so a deploy without it cannot run reports."""

    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, name):
        with open(os.path.join(self.root, name), encoding="utf-8") as f:
            return f.read()

    def test_installer_copies_passthrough_py(self):
        self.assertIn('cp "$SCRIPT_DIR/passthrough.py" "$APP_DIR/"', self._read("install.sh"))

    def test_uninstaller_removes_passthrough_py(self):
        self.assertIn("$APP_DIR/passthrough.py", self._read("uninstall.sh"))

    def test_uninstaller_keeps_the_registry(self):
        # Like usage.db, the registry describes preserved data and must survive
        # a plain uninstall; only --purge's rm -rf removes it.
        self.assertNotIn("$APP_DIR/passthrough.json", self._read("uninstall.sh"))

    def test_wrapper_exposes_the_passthrough_command(self):
        wrapper = self._read("nettally")
        self.assertIn('passthrough.py" "$@"', wrapper)
        self.assertIn("  passthrough ", wrapper)

    def test_wrapper_status_lists_registered_apps_quietly(self):
        self.assertIn('passthrough.py" list --quiet', self._read("nettally"))


class TestPauseIsDeployedWithTheRestOfTheRuntime(unittest.TestCase):
    """collector.py imports pause.py, so a deploy without it cannot start the daemon."""

    def setUp(self):
        self.root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, name):
        with open(os.path.join(self.root, name), encoding="utf-8") as f:
            return f.read()

    def test_installer_copies_pause_py(self):
        self.assertIn('cp "$SCRIPT_DIR/pause.py" "$APP_DIR/"', self._read("install.sh"))

    def test_uninstaller_removes_pause_py_and_the_pause_state(self):
        source = self._read("uninstall.sh")
        self.assertIn("$APP_DIR/pause.py", source)
        self.assertIn("$APP_DIR/pause_state.json", source)

    def test_wrapper_exposes_pause_and_resume(self):
        wrapper = self._read("nettally")
        self.assertIn('pause.py" pause', wrapper)
        self.assertIn('pause.py" resume', wrapper)
        self.assertIn("pause ", wrapper)
        self.assertIn("resume ", wrapper)

    def test_wrapper_status_shows_the_pause_state(self):
        self.assertIn('pause.py" state', self._read("nettally"))


if __name__ == "__main__":
    unittest.main()
