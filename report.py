import argparse
import json
import sys
from typing import List, Dict, Optional

from db import get_db_path, query_usage_totals, query_usage_by_day

def format_bytes(num_bytes: int) -> str:
    if num_bytes < 0:
        return "0 B"
    if num_bytes < 1024:
        return f"{num_bytes} B"
    val = float(num_bytes)
    for unit in ["KiB", "MiB", "GiB", "TiB"]:
        val /= 1024.0
        if val < 1024.0 or unit == "TiB":
            return f"{val:.2f} {unit}"
    return f"{val:.2f} TiB"

def print_table(headers: List[str], rows: List[List[str]]) -> None:
    if not rows:
        print("No usage data found for the specified criteria.")
        return

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(cell))

    # Header
    header_str = " | ".join(headers[i].ljust(col_widths[i]) for i in range(len(headers)))
    divider_str = "-+-".join("-" * col_widths[i] for i in range(len(headers)))
    print(header_str)
    print(divider_str)

    # Rows
    for row in rows:
        line_str = " | ".join(row[i].ljust(col_widths[i]) for i in range(len(row)))
        print(line_str)

def generate_totals_report(db_path: str, days: Optional[int], app_filter: Optional[str], fmt: str) -> None:
    results = query_usage_totals(db_path, days=days, app_filter=app_filter)
    
    if fmt == "json":
        print(json.dumps(results, indent=2))
        return
    elif fmt == "csv":
        print("App Name,Total Received,Total Sent,Total Transfer,Samples,Earliest Day,Latest Day")
        for r in results:
            print(f'"{r["app_name"]}",{r["total_bytes_in"]},{r["total_bytes_out"]},{r["total_bytes"]},{r["total_samples"]},{r["earliest_day"]},{r["latest_day"]}')
        return

    # Table format
    headers = ["App Name", "Received", "Sent", "Total Usage", "Samples", "Period"]
    rows = []
    grand_in = 0
    grand_out = 0
    grand_total = 0
    grand_samples = 0

    for r in results:
        b_in = r["total_bytes_in"] or 0
        b_out = r["total_bytes_out"] or 0
        b_tot = r["total_bytes"] or 0
        samples = r["total_samples"] or 0
        period = f"{r['earliest_day']} to {r['latest_day']}" if r['earliest_day'] != r['latest_day'] else r['earliest_day']

        grand_in += b_in
        grand_out += b_out
        grand_total += b_tot
        grand_samples += samples

        rows.append([
            r["app_name"],
            format_bytes(b_in),
            format_bytes(b_out),
            format_bytes(b_tot),
            str(samples),
            period
        ])

    print(f"\n--- Network Usage Report (Totals: {'Last ' + str(days) + ' days' if days else 'All time'}) ---")
    print_table(headers, rows)
    if rows:
        print(f"\nTOTAL: Received {format_bytes(grand_in)} | Sent {format_bytes(grand_out)} | Total {format_bytes(grand_total)} across {len(rows)} apps.")

def generate_by_day_report(db_path: str, days: Optional[int], app_filter: Optional[str], fmt: str) -> None:
    results = query_usage_by_day(db_path, days=days, app_filter=app_filter)

    if fmt == "json":
        print(json.dumps(results, indent=2))
        return
    elif fmt == "csv":
        print("Date,App Name,Received,Sent,Total Transfer,Samples")
        for r in results:
            print(f'{r["day"]},"{r["app_name"]}",{r["bytes_in"]},{r["bytes_out"]},{r["total_bytes"]},{r["sample_count"]}')
        return

    # Table format
    headers = ["Date", "App Name", "Received", "Sent", "Total Usage", "Samples"]
    rows = []
    for r in results:
        rows.append([
            r["day"],
            r["app_name"],
            format_bytes(r["bytes_in"]),
            format_bytes(r["bytes_out"]),
            format_bytes(r["total_bytes"]),
            str(r["sample_count"])
        ])

    print(f"\n--- Network Usage Report (Daily Breakdown) ---")
    print_table(headers, rows)

def main():
    parser = argparse.ArgumentParser(description="NetTally CLI Viewer")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database file")
    parser.add_argument("--days", type=int, default=30, help="Number of past days to include (default: 30)")
    parser.add_argument("--all", action="store_true", help="Include all historical data regardless of days")
    parser.add_argument("--by-day", action="store_true", help="Group breakdown by day and app")
    parser.add_argument("--today", action="store_true", help="Show usage for today only")
    parser.add_argument("--app", type=str, default=None, help="Filter usage by specific app name (case-insensitive substring match)")
    parser.add_argument("--format", choices=["table", "csv", "json"], default="table", help="Output format (default: table)")
    args = parser.parse_args()

    db_path = get_db_path(args.db)

    days_filter = args.days
    if args.all:
        days_filter = None
    elif args.today:
        days_filter = 0

    if args.by_day:
        generate_by_day_report(db_path, days=days_filter, app_filter=args.app, fmt=args.format)
    else:
        generate_totals_report(db_path, days=days_filter, app_filter=args.app, fmt=args.format)

if __name__ == "__main__":
    main()
