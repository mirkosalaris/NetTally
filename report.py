#!/usr/bin/env python3
"""CLI text reporting for NetTally: totals, daily, hourly, and 5-minute views.

Formatting helpers (print_table, format_bytes) are also reused by the HTML
dashboard generator, so keep them here rather than duplicating them.
"""

import argparse
import json
from typing import Optional

from config import load_config
from db import (
    get_db_path,
    query_usage_by_5m,
    query_usage_by_day,
    query_usage_by_hour,
    query_usage_totals,
)


def format_bytes(num_bytes: int) -> str:
    """Format a byte count as a human-readable binary string (KiB/MiB/GiB/TiB)."""
    if num_bytes < 0:
        return "0 B"
    if num_bytes < 1024:
        return f"{num_bytes} B"
    val = float(num_bytes)
    for unit in ["KiB", "MiB", "GiB", "TiB"]:
        val /= 1024.0
        if val < 1024.0:
            return f"{val:.2f} {unit}"
    return f"{val:.2f} TiB"


def _emit_json_or_csv(results, fmt, csv_header, csv_row):
    if fmt == "json":
        print(json.dumps(results, indent=2))
        return True
    if fmt == "csv":
        print(csv_header)
        for r in results:
            print(csv_row(r))
        return True
    return False


def _print_report(title, headers, rows, exclude_classifications=None, footer=None):
    print(f"\n--- {title} ---")
    if exclude_classifications:
        print(f"[Excluded classifications: {', '.join(exclude_classifications)}]")
    _print_table(headers, rows)
    if footer:
        print(footer)


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("No usage data found for the specified criteria.")
        return

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(cell))

    header_str = " | ".join(headers[i].ljust(col_widths[i]) for i in range(len(headers)))
    divider_str = "-+-".join("-" * col_widths[i] for i in range(len(headers)))
    print(header_str)
    print(divider_str)

    for row in rows:
        line_str = " | ".join(row[i].ljust(col_widths[i]) for i in range(len(row)))
        print(line_str)


def generate_totals_report(
    db_path: str,
    days: Optional[int],
    app_filter: Optional[str],
    fmt: str,
    exclude_classifications: Optional[list[str]] = None,
) -> None:
    """Emit the per-app totals report (table/csv/json)."""
    results = query_usage_totals(
        db_path, days=days, app_filter=app_filter, exclude_classifications=exclude_classifications
    )

    if _emit_json_or_csv(
        results,
        fmt,
        "App Name,Total Received,Total Sent,Total Transfer,Samples,Earliest Day,Latest Day",
        lambda r: (
            f'"{r["app_name"]}",{r["total_bytes_in"]},{r["total_bytes_out"]},{r["total_bytes"]},{r["total_samples"]},{r["earliest_day"]},{r["latest_day"]}'
        ),
    ):
        return

    headers = ["App Name", "Received", "Sent", "Total Usage", "Samples", "Period"]
    rows = []
    grand_in = 0
    grand_out = 0
    grand_total = 0

    for r in results:
        b_in = r["total_bytes_in"] or 0
        b_out = r["total_bytes_out"] or 0
        b_tot = r["total_bytes"] or 0
        period = (
            f"{r['earliest_day']} to {r['latest_day']}"
            if r["earliest_day"] != r["latest_day"]
            else r["earliest_day"]
        )

        grand_in += b_in
        grand_out += b_out
        grand_total += b_tot

        rows.append(
            [
                r["app_name"],
                format_bytes(b_in),
                format_bytes(b_out),
                format_bytes(b_tot),
                str(r["total_samples"] or 0),
                str(period),
            ]
        )

    period_label = "Last " + str(days) + " days" if days else "All time"
    footer = (
        f"\nTOTAL: Received {format_bytes(grand_in)} | Sent {format_bytes(grand_out)} | Total {format_bytes(grand_total)} across {len(rows)} apps."
        if rows
        else None
    )
    _print_report(
        f"NetTally Usage Report (Totals: {period_label})",
        headers,
        rows,
        exclude_classifications,
        footer,
    )


def generate_by_day_report(
    db_path: str,
    days: Optional[int],
    app_filter: Optional[str],
    fmt: str,
    exclude_classifications: Optional[list[str]] = None,
) -> None:
    """Emit the daily breakdown report (table/csv/json)."""
    results = query_usage_by_day(
        db_path, days=days, app_filter=app_filter, exclude_classifications=exclude_classifications
    )

    if _emit_json_or_csv(
        results,
        fmt,
        "Date,App Name,Received,Sent,Total Transfer,Samples",
        lambda r: (
            f'{r["day"]},"{r["app_name"]}",{r["bytes_in"]},{r["bytes_out"]},{r["total_bytes"]},{r["sample_count"]}'
        ),
    ):
        return

    headers = ["Date", "App Name", "Received", "Sent", "Total Usage", "Samples"]
    rows = [
        [
            r["day"],
            r["app_name"],
            format_bytes(r["bytes_in"]),
            format_bytes(r["bytes_out"]),
            format_bytes(r["total_bytes"]),
            str(r["sample_count"]),
        ]
        for r in results
    ]
    _print_report("NetTally Usage Report (Daily Breakdown)", headers, rows, exclude_classifications)


def generate_by_hour_report(
    db_path: str,
    days: Optional[int],
    app_filter: Optional[str],
    fmt: str,
    exclude_classifications: Optional[list[str]] = None,
) -> None:
    """Emit the hourly breakdown report (table/csv/json)."""
    results = query_usage_by_hour(
        db_path, days=days, app_filter=app_filter, exclude_classifications=exclude_classifications
    )

    if _emit_json_or_csv(
        results,
        fmt,
        "Hour,App Name,Received,Sent,Total Transfer,Samples",
        lambda r: (
            f'{r["timestamp_hour"]},"{r["app_name"]}",{r["bytes_in"]},{r["bytes_out"]},{r["total_bytes"]},{r["sample_count"]}'
        ),
    ):
        return

    headers = ["Hour Block", "App Name", "Received", "Sent", "Total Usage", "Samples"]
    rows = [
        [
            r["timestamp_hour"],
            r["app_name"],
            format_bytes(r["bytes_in"]),
            format_bytes(r["bytes_out"]),
            format_bytes(r["total_bytes"]),
            str(r["sample_count"]),
        ]
        for r in results
    ]
    _print_report(
        "NetTally Usage Report (Hourly Breakdown)", headers, rows, exclude_classifications
    )


def generate_by_5m_report(
    db_path: str,
    days: Optional[int],
    app_filter: Optional[str],
    fmt: str,
    exclude_classifications: Optional[list[str]] = None,
) -> None:
    """Emit the 5-minute breakdown report (table/csv/json)."""
    results = query_usage_by_5m(
        db_path, days=days, app_filter=app_filter, exclude_classifications=exclude_classifications
    )

    if _emit_json_or_csv(
        results,
        fmt,
        "5m Timestamp,App Name,Received,Sent,Total Transfer,Samples",
        lambda r: (
            f'{r["timestamp_5m"]},"{r["app_name"]}",{r["bytes_in"]},{r["bytes_out"]},{r["total_bytes"]},{r["sample_count"]}'
        ),
    ):
        return

    headers = ["5-Min Block", "App Name", "Received", "Sent", "Total Usage", "Samples"]
    rows = [
        [
            r["timestamp_5m"],
            r["app_name"],
            format_bytes(r["bytes_in"]),
            format_bytes(r["bytes_out"]),
            format_bytes(r["total_bytes"]),
            str(r["sample_count"]),
        ]
        for r in results
    ]
    _print_report(
        "NetTally Usage Report (5-Minute Block Breakdown)", headers, rows, exclude_classifications
    )


def main():
    """Parse CLI flags and dispatch to the requested report generator."""
    cfg = load_config()
    parser = argparse.ArgumentParser(description="NetTally CLI Viewer")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database file")
    parser.add_argument(
        "--days",
        type=int,
        default=cfg["default_report_days"],
        help=f"Number of past days to include (default: {cfg['default_report_days']})",
    )
    parser.add_argument(
        "--all", action="store_true", help="Include all historical data regardless of days"
    )
    parser.add_argument("--by-day", action="store_true", help="Group breakdown by day and app")
    parser.add_argument(
        "--by-hour", action="store_true", help="Group breakdown by 1-hour blocks and app"
    )
    parser.add_argument(
        "--by-5m", "--5m", action="store_true", help="Group breakdown by 5-minute blocks and app"
    )
    parser.add_argument("--today", action="store_true", help="Show usage for today only")
    parser.add_argument(
        "--app",
        type=str,
        default=None,
        help="Filter usage by specific app name (case-insensitive substring match)",
    )
    parser.add_argument(
        "--format",
        choices=["table", "csv", "json"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--exclude",
        type=str,
        default=None,
        metavar="CLASS",
        help="Comma-separated gap classifications to exclude: dark_wake_only, sleep_then_full_wake (e.g. --exclude dark_wake_only,sleep_then_full_wake)",
    )
    args = parser.parse_args()

    db_path = get_db_path(args.db)

    days_filter = args.days
    if args.all:
        days_filter = None
    elif args.today:
        days_filter = 0

    # Parse --exclude into a list; drop unknown/empty tokens
    valid_classes = {"dark_wake_only", "sleep_then_full_wake"}
    exclude_classifications: Optional[list[str]] = None
    if args.exclude:
        parsed = [c.strip() for c in args.exclude.split(",") if c.strip()]
        invalid = [c for c in parsed if c not in valid_classes]
        if invalid:
            parser.error(
                f"Unknown classification(s): {', '.join(invalid)}. Valid values: {', '.join(sorted(valid_classes))}"
            )
        exclude_classifications = parsed if parsed else None

    if args.by_5m:
        generate_by_5m_report(
            db_path,
            days=days_filter,
            app_filter=args.app,
            fmt=args.format,
            exclude_classifications=exclude_classifications,
        )
    elif args.by_hour:
        generate_by_hour_report(
            db_path,
            days=days_filter,
            app_filter=args.app,
            fmt=args.format,
            exclude_classifications=exclude_classifications,
        )
    elif args.by_day:
        generate_by_day_report(
            db_path,
            days=days_filter,
            app_filter=args.app,
            fmt=args.format,
            exclude_classifications=exclude_classifications,
        )
    else:
        generate_totals_report(
            db_path,
            days=days_filter,
            app_filter=args.app,
            fmt=args.format,
            exclude_classifications=exclude_classifications,
        )


if __name__ == "__main__":
    main()
