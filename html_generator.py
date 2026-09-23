#!/usr/bin/env python3
"""Generate the self-contained HTML dashboard for NetTally.

Reads usage from db.py, builds classification-aware 5m/hourly/daily chart
data, and substitutes it into templates/dashboard_template.html. The template
holds the embedded CSS/JS, so keep dashboard JS changes there (and re-run
./install.sh to redeploy).
"""
import argparse
import datetime
import json
import os
import webbrowser
from typing import Optional

from config import load_config
from db import (
    get_db_path,
    query_usage_by_5m,
    query_usage_by_day,
    query_usage_by_hour,
    query_usage_totals,
)
from report import format_bytes

DEFAULT_HTML_PATH = os.path.expanduser("~/Library/Application Support/NetTally/dashboard.html")

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "templates", "dashboard_template.html"
)


def _load_template() -> str:
    with open(_TEMPLATE_PATH, encoding="utf-8") as f:
        return f.read()


HTML_TEMPLATE = _load_template()


def build_view_dataset(
    records: list[dict], time_key_name: str, top_apps: list[str], colors: list[str]
) -> dict:
    """Build the 5m chart dataset (labels, per-app series, bucket classifications)."""
    distinct_times = sorted(list(set(r[time_key_name] for r in records)))

    # Build per-bucket classification map: use MAX(gap_classification) already computed
    # by the SQL query. All rows for the same timestamp share the same classification,
    # so the first non-None value wins. NULL/None → 'awake'.
    bucket_cls_map: dict[str, str] = {}
    for r in records:
        t = r[time_key_name]
        cls = r.get("gap_classification")
        if cls and t not in bucket_cls_map:
            bucket_cls_map[t] = cls

    bucket_classifications = [bucket_cls_map.get(t) or "awake" for t in distinct_times]

    datasets = []

    for idx, app in enumerate(top_apps):
        color = colors[idx % len(colors)]
        data = []
        for t in distinct_times:
            bytes_sum = sum(
                r["total_bytes"] for r in records if r[time_key_name] == t and r["app_name"] == app
            )
            data.append(bytes_sum)
        datasets.append({"label": app, "data": data, "backgroundColor": color})

    if distinct_times and top_apps:
        other_data = []
        for t in distinct_times:
            bytes_sum = sum(
                r["total_bytes"]
                for r in records
                if r[time_key_name] == t and r["app_name"] not in top_apps
            )
            other_data.append(bytes_sum)
        if any(v > 0 for v in other_data):
            datasets.append(
                {"label": "Other Apps", "data": other_data, "backgroundColor": colors[-1]}
            )

    return {
        "labels": distinct_times,
        "datasets": datasets,
        "bucketClassification": bucket_classifications,
    }


def generate_html_report(
    db_path: str,
    days: Optional[int] = None,
    output_path: str = DEFAULT_HTML_PATH,
    exclude_classifications: Optional[list[str]] = None,
) -> str:
    """Query usage and render the dashboard HTML to output_path, returning the path."""
    cfg = load_config()
    if days is None:
        days = cfg["default_report_days"]

    totals = query_usage_totals(db_path, days=days, exclude_classifications=exclude_classifications)
    records_5m = query_usage_by_5m(
        db_path, days=days, exclude_classifications=exclude_classifications
    )

    grand_in = sum(r["total_bytes_in"] or 0 for r in totals)
    grand_out = sum(r["total_bytes_out"] or 0 for r in totals)
    grand_total = sum(r["total_bytes"] or 0 for r in totals)

    if totals:
        table_rows = []
        for r in totals:
            table_rows.append(f"""
                <tr>
                    <td><strong>{r["app_name"]}</strong></td>
                    <td data-raw="{r["total_bytes_in"] or 0}">{format_bytes(r["total_bytes_in"] or 0)}</td>
                    <td data-raw="{r["total_bytes_out"] or 0}">{format_bytes(r["total_bytes_out"] or 0)}</td>
                    <td data-raw="{r["total_bytes"] or 0}"><span class="badge">{format_bytes(r["total_bytes"] or 0)}</span></td>
                    <td>{r["total_samples"] or 0}</td>
                </tr>
            """)
        table_html = "\n".join(table_rows)
    else:
        table_html = '<tr><td colspan="5" style="text-align:center; color: var(--text-secondary); padding: 24px;">No network usage records found for this period.</td></tr>'

    top_apps_limit = cfg.get("html_top_apps_limit", 8)
    top_apps = [r["app_name"] for r in totals[:top_apps_limit]] if totals else []
    colors = [
        "#38bdf8",
        "#818cf8",
        "#c084fc",
        "#f472b6",
        "#fb7185",
        "#34d399",
        "#fbbf24",
        "#a3e635",
        "#94a3b8",
    ]

    # Build layered views for hourly/daily: master aligned labels + per-classification layers
    def align_layer(master_labels, layer_records, time_key_name, force_include_other: bool = False):
        datasets = []
        for idx, app in enumerate(top_apps):
            color = colors[idx % len(colors)]
            data = []
            for t in master_labels:
                bytes_sum = sum(
                    r["total_bytes"]
                    for r in layer_records
                    if r[time_key_name] == t and r["app_name"] == app
                )
                data.append(bytes_sum)
            datasets.append({"label": app, "data": data, "backgroundColor": color})

        # Other Apps
        other_data = []
        for t in master_labels:
            bytes_sum = sum(
                r["total_bytes"]
                for r in layer_records
                if r[time_key_name] == t and r["app_name"] not in top_apps
            )
            other_data.append(bytes_sum)
        if force_include_other or any(v > 0 for v in other_data):
            datasets.append(
                {"label": "Other Apps", "data": other_data, "backgroundColor": colors[-1]}
            )

        return datasets

    # Master label axes (authoritative, unfiltered)
    hourly_master_records = query_usage_by_hour(
        db_path, days=days, exclude_classifications=exclude_classifications
    )
    daily_master_records = query_usage_by_day(
        db_path, days=days, exclude_classifications=exclude_classifications
    )

    hourly_labels = sorted(list({r["timestamp_hour"] for r in hourly_master_records}))
    daily_labels = sorted(list({r["day"] for r in daily_master_records}))

    classifications = ["awake", "dark_wake_only", "sleep_then_full_wake"]

    hourly_layers = {}
    daily_layers = {}
    for cls in classifications:
        hr_recs = query_usage_by_hour(
            db_path,
            days=days,
            exclude_classifications=exclude_classifications,
            only_classification=cls,
        )
        dy_recs = query_usage_by_day(
            db_path,
            days=days,
            exclude_classifications=exclude_classifications,
            only_classification=cls,
        )
        # Force inclusion of an "Other Apps" series if the master (unfiltered) records contain any below-top-apps traffic
        hourly_layers[cls] = {
            "datasets": align_layer(
                hourly_labels,
                hr_recs,
                "timestamp_hour",
                force_include_other=any(
                    r["app_name"] not in top_apps and (r["total_bytes"] or 0) > 0
                    for r in hourly_master_records
                ),
            )
        }
        daily_layers[cls] = {
            "datasets": align_layer(
                daily_labels,
                dy_recs,
                "day",
                force_include_other=any(
                    r["app_name"] not in top_apps and (r["total_bytes"] or 0) > 0
                    for r in daily_master_records
                ),
            )
        }

    views_data = {
        "5m": build_view_dataset(records_5m, "timestamp_5m", top_apps, colors),
        "hourly": {"labels": hourly_labels, "layers": hourly_layers},
        "daily": {"labels": daily_labels, "layers": daily_layers},
    }

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Checkboxes that should start unchecked (driven by --exclude at generation time)
    # The full viewsData is still embedded so the user can toggle them back on.
    cls_initially_excluded = exclude_classifications or []

    html_content = HTML_TEMPLATE.replace("__GENERATED_TIME__", now_str)
    html_content = html_content.replace("__TOTAL_TRANSFER__", format_bytes(grand_total))
    html_content = html_content.replace("__TOTAL_IN__", format_bytes(grand_in))
    html_content = html_content.replace("__TOTAL_OUT__", format_bytes(grand_out))
    html_content = html_content.replace("__ACTIVE_APPS__", str(len(totals)))
    html_content = html_content.replace("__DAYS__", str(days))
    html_content = html_content.replace("__TABLE_ROWS__", table_html)
    html_content = html_content.replace("__VIEWS_DATA_JSON__", json.dumps(views_data))
    html_content = html_content.replace(
        "__CLS_INITIALLY_EXCLUDED__", json.dumps(cls_initially_excluded)
    )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return output_path


def main():
    """Parse CLI flags and write the dashboard HTML file."""
    cfg = load_config()
    parser = argparse.ArgumentParser(description="Generate HTML network usage dashboard")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database file")
    parser.add_argument(
        "--days",
        type=int,
        default=cfg["default_report_days"],
        help=f"Number of past days to include (default: {cfg['default_report_days']})",
    )
    parser.add_argument("--out", type=str, default=DEFAULT_HTML_PATH, help="Output HTML file path")
    parser.add_argument(
        "--open", action="store_true", help="Open generated HTML file in default browser"
    )
    parser.add_argument(
        "--exclude",
        type=str,
        default=None,
        metavar="CLASS",
        help="Comma-separated gap classifications to exclude from the embedded data: dark_wake_only, sleep_then_full_wake. The corresponding dashboard checkboxes will start unchecked (but can be toggled back on interactively).",
    )
    args = parser.parse_args()

    # Parse --exclude into a list; validate values
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

    db_path = get_db_path(args.db)
    path = generate_html_report(
        db_path,
        days=args.days,
        output_path=args.out,
        exclude_classifications=exclude_classifications,
    )
    print(f"HTML dashboard generated: file://{path}")

    if args.open:
        webbrowser.open(f"file://{path}")


if __name__ == "__main__":
    main()
