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
    query_usage_by_5m_layered,
    query_usage_coverage,
    query_usage_totals,
)
from passthrough import format_names, resolve_names
from report import format_bytes, parse_exclude_classes

DEFAULT_HTML_PATH = os.path.expanduser("~/Library/Application Support/NetTally/dashboard.html")

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
_TEMPLATE_PATH = os.path.join(_TEMPLATE_DIR, "dashboard_template.html")
_CHARTJS_PATH = os.path.join(_TEMPLATE_DIR, "chart.umd.js")


def _load_template() -> str:
    with open(_TEMPLATE_PATH, encoding="utf-8") as f:
        return f.read()


def _load_chartjs() -> str:
    """Return the vendored Chart.js UMD source, ready to inline into a <script>.

    The bundle is inlined rather than referenced so the generated report stays a
    single self-contained file that works from any --out path, with no network
    access at all -- see templates/CHARTJS-LICENSE.md (MIT).
    """
    with open(_CHARTJS_PATH, encoding="utf-8") as f:
        source = f.read()
    # A literal "</script>" inside the payload would close the tag early.
    return source.replace("</script", "<\\/script")


HTML_TEMPLATE = _load_template()
CHART_JS = _load_chartjs()


CLASSIFICATIONS = ["awake", "dark_wake_only", "sleep_then_full_wake"]

# gap_classification -> dense index used by the embedded payloads. NULL and
# 'unknown_gap' both fold into 'awake', exactly as db.py's only_classification
# branch and the dashboard's JS classKey() do.
CLASSIFICATION_INDEX = {
    None: 0,
    "awake": 0,
    "unknown_gap": 0,
    "dark_wake_only": 1,
    "sleep_then_full_wake": 2,
}

OTHER_APP_LABEL = "Other Apps"


def bucket_label_to_input_value(label: Optional[str]) -> str:
    """Render a stored bucket label as a datetime-local input value.

    The database stores bucket labels as 'YYYY-MM-DD HH:MM', while
    <input type="datetime-local"> only accepts 'YYYY-MM-DDTHH:MM'. Anything the
    browser cannot parse is ignored rather than reported, so a mis-formatted
    min/max leaves the input unbounded instead of constrained.
    """
    if not label:
        return ""
    return label[:16].replace(" ", "T")


def build_coverage_summary(coverage: dict, days: Optional[int]) -> str:
    """Describe, in the report's own voice, the time range the data actually covers.

    `days` is the requested window; coverage comes from the database and can be
    much narrower (a machine that was off for a week records nothing), so the
    two are reported separately instead of implying the window is fully covered.
    """
    window = f"the last {days} days" if days is not None else "all recorded history"
    first = coverage.get("first_bucket")
    last = coverage.get("last_bucket")
    if not first or not last:
        return f"No usage data recorded in {window} — start the collector to populate this report."

    days_with_data = coverage.get("day_count", 0)
    buckets = coverage.get("bucket_count", 0)
    parts = [
        f"{first} → {last}",
        f"{days_with_data} {'day' if days_with_data == 1 else 'days'} with data",
        f"{buckets:,} recorded 5-min buckets",
    ]
    if days is not None and days_with_data < days:
        parts.append(f"report window: {window}")
    return " · ".join(parts)


def build_view_dataset(
    records: list[dict], time_key_name: str, top_apps: list[str], colors: list[str]
) -> dict:
    """Build the 5m chart dataset (labels, per-app series, bucket classifications)."""
    distinct_times = sorted({r[time_key_name] for r in records})

    # Index once instead of rescanning `records` per (app, timestamp): the
    # report is O(apps x buckets) big on a month of data, and the per-pair
    # rescan used to dominate generation time.
    top_app_set = set(top_apps)
    bytes_by_bucket_app: dict[tuple[str, str], int] = {}
    other_by_bucket: dict[str, int] = {}
    # Classification of each bucket: the most severe gap_classification present.
    bucket_cls_map: dict[str, str] = {}
    for r in records:
        t = r[time_key_name]
        app = r["app_name"]
        total = r["total_bytes"] or 0
        key = (t, app)
        bytes_by_bucket_app[key] = bytes_by_bucket_app.get(key, 0) + total
        if top_app_set and app not in top_app_set:
            other_by_bucket[t] = other_by_bucket.get(t, 0) + total
        cls = r.get("gap_classification")
        if cls:
            current = bucket_cls_map.get(t)
            # A bucket can hold rows from more than one poll state: the collector
            # classifies per poll and a bucket spans up to six of them. A bar is
            # the whole bucket, so it takes the most severe class present -- the
            # same ordering db.py's MAX() already applies within a bucket+app.
            # Most-severe is the conservative direction: unchecking a class then
            # hides every bucket containing any of its bytes, instead of leaving
            # a wake burst inside a bar that still reads as "awake".
            if current is None or CLASSIFICATION_INDEX.get(cls, 0) > CLASSIFICATION_INDEX.get(
                current, 0
            ):
                bucket_cls_map[t] = cls

    bucket_classifications = [bucket_cls_map.get(t) or "awake" for t in distinct_times]

    datasets = []
    for idx, app in enumerate(top_apps):
        datasets.append(
            {
                "label": app,
                "data": [bytes_by_bucket_app.get((t, app), 0) for t in distinct_times],
                "backgroundColor": colors[idx % len(colors)],
            }
        )

    if distinct_times and top_apps:
        other_data = [other_by_bucket.get(t, 0) for t in distinct_times]
        if any(v > 0 for v in other_data):
            datasets.append(
                {"label": OTHER_APP_LABEL, "data": other_data, "backgroundColor": colors[-1]}
            )

    return {
        "labels": distinct_times,
        "datasets": datasets,
        "bucketClassification": bucket_classifications,
    }


def build_layered_view(
    layered_rows: list[dict],
    granularity: str,
    top_apps: list[str],
    colors: list[str],
) -> dict:
    """Build the hourly/daily chart view from the per-(bucket, app, class) rows.

    Produces a master label axis plus one dataset list per classification, all
    of them with the same series in the same order (including an "Other Apps"
    series that is present, even all-zero, in every layer as soon as any layer
    has one) -- the client-side combine step has no fallback and silently drops
    data if the shapes disagree.
    """
    if granularity == "hourly":

        def label_of(row: dict) -> str:
            return row["timestamp_5m"][:13] + ":00"

    else:

        def label_of(row: dict) -> str:
            return row["day"]

    labels: set[str] = set()
    # (classification index) -> (label, app) -> total bytes
    layer_totals: list[dict[tuple[str, str], int]] = [{} for _ in CLASSIFICATIONS]
    has_other_traffic = False

    for r in layered_rows:
        label = label_of(r)
        labels.add(label)
        cls_idx = CLASSIFICATION_INDEX.get(r.get("gap_classification"), 0)
        app = r["app_name"]
        total = r["total_bytes"] or 0
        if app not in top_apps:
            if total > 0:
                has_other_traffic = True
            app = OTHER_APP_LABEL
        totals = layer_totals[cls_idx]
        key = (label, app)
        totals[key] = totals.get(key, 0) + total

    ordered_labels = sorted(labels)
    layers = {}
    for cls_idx, cls_name in enumerate(CLASSIFICATIONS):
        totals = layer_totals[cls_idx]
        datasets = []
        for idx, app in enumerate(top_apps):
            datasets.append(
                {
                    "label": app,
                    "data": [totals.get((label, app), 0) for label in ordered_labels],
                    "backgroundColor": colors[idx % len(colors)],
                }
            )
        other_data = [totals.get((label, OTHER_APP_LABEL), 0) for label in ordered_labels]
        if has_other_traffic or any(v > 0 for v in other_data):
            datasets.append(
                {
                    "label": OTHER_APP_LABEL,
                    "data": other_data,
                    "backgroundColor": colors[-1],
                }
            )
        layers[cls_name] = {"datasets": datasets}

    return {"labels": ordered_labels, "layers": layers}


def build_table_data(
    layered_rows: list[dict], bucket_labels: list[str], bucket_classifications: list[str]
) -> dict:
    """Build the per-app series the dashboard re-aggregates for the table.

    The table has to follow the same filters as the plot, so it cannot be a
    server-rendered per-app total: the report embeds one sparse series per app
    instead, which the client sums over the selected range and classifications.
    Buckets are indices into the 5-minute label axis (already embedded for the
    chart), delta-encoded because one app's buckets are almost always close
    together -- that keeps the payload about a third smaller.

    `bucket_classifications` is carried once per bucket rather than per row: a
    stacked bar *is* the bucket, so the plot classifies a bucket as a whole, and
    a table that classified each app's row independently would quietly disagree
    with the plot on the buckets a poll boundary split across two classifications
    (a few hundred KB on a month of real data). Both consumers now select the
    same buckets.
    """
    bucket_index = {label: i for i, label in enumerate(bucket_labels)}
    bucket_class = [
        CLASSIFICATION_INDEX.get(cls if cls else "awake", 0) for cls in bucket_classifications
    ]
    entries_by_app: dict[str, list[tuple[int, int, int, int]]] = {}
    for r in layered_rows:
        index = bucket_index.get(r["timestamp_5m"])
        if index is None:
            continue
        entries_by_app.setdefault(r["app_name"], []).append(
            (
                index,
                r["bytes_in"] or 0,
                r["bytes_out"] or 0,
                r["sample_count"] or 0,
            )
        )

    # Same order as the server-rendered table used to be: total bytes, descending.
    ordered_apps = sorted(
        entries_by_app, key=lambda app: -sum(e[1] + e[2] for e in entries_by_app[app])
    )

    series = []
    for app in ordered_apps:
        bucket_indices: list[int] = []
        bytes_in: list[int] = []
        bytes_out: list[int] = []
        samples: list[int] = []
        previous = 0
        for index, bin_, bout, sample_count in sorted(entries_by_app[app]):
            bucket_indices.append(index - previous)
            previous = index
            bytes_in.append(bin_)
            bytes_out.append(bout)
            samples.append(sample_count)
        series.append(
            {
                "i": bucket_indices,
                "in": bytes_in,
                "out": bytes_out,
                "s": samples,
            }
        )

    return {"apps": ordered_apps, "bucketClass": bucket_class, "series": series}


def generate_html_report(
    db_path: str,
    days: Optional[int] = None,
    output_path: str = DEFAULT_HTML_PATH,
    exclude_classifications: Optional[list[str]] = None,
    pass_through_apps: Optional[frozenset[str]] = None,
) -> str:
    """Query usage and render the dashboard HTML to output_path, returning the path.

    Pass-through rows are embedded like every other row -- the dashboard hides
    them behind an off-by-default toggle -- but the server-rendered state
    (stat cards, table rows, the "Active Apps" count) describes the hidden
    default, so those placeholders are built from the visible rows only.
    `pass_through_apps=None` resolves the live registry.
    """
    cfg = load_config()
    if days is None:
        days = cfg["default_report_days"]

    totals = query_usage_totals(db_path, days=days, exclude_classifications=exclude_classifications)
    pass_through = resolve_names(pass_through_apps)
    pass_through_lower = {name.lower() for name in pass_through}
    visible_totals = [r for r in totals if str(r["app_name"]).lower() not in pass_through_lower]
    hidden_names = [
        str(r["app_name"]) for r in totals if str(r["app_name"]).lower() in pass_through_lower
    ]
    records_5m = query_usage_by_5m(
        db_path, days=days, exclude_classifications=exclude_classifications
    )
    # One pass over the exact per-(bucket, app, classification) rows feeds the
    # hourly/daily layers and the re-aggregatable table data.
    layered_rows = query_usage_by_5m_layered(
        db_path, days=days, exclude_classifications=exclude_classifications
    )
    coverage = query_usage_coverage(db_path, days=days)

    grand_in = sum(r["total_bytes_in"] or 0 for r in visible_totals)
    grand_out = sum(r["total_bytes_out"] or 0 for r in visible_totals)
    grand_total = sum(r["total_bytes"] or 0 for r in visible_totals)

    if visible_totals:
        table_rows = []
        for r in visible_totals:
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
    top_apps = [r["app_name"] for r in visible_totals[:top_apps_limit]]
    # Pass-through apps keep their own series instead of folding into "Other
    # Apps": the toggle has to be able to reveal them in place, and a series
    # that was aggregated away at generation time cannot be split back apart.
    chart_apps = top_apps + [name for name in hidden_names if name not in top_apps]
    # The series palette deliberately ends in the muted gray the "Other Apps"
    # series uses (the builders read colors[-1]); the vivid colors before it
    # are there so the appended pass-through series don't reuse another app's
    # color (with the default limit of 8, the first pass-through lands on
    # index 8).
    colors = [
        "#38bdf8",
        "#818cf8",
        "#c084fc",
        "#f472b6",
        "#fb7185",
        "#34d399",
        "#fbbf24",
        "#a3e635",
        "#f97316",
        "#22d3ee",
        "#e879f9",
        "#94a3b8",
    ]

    # Build layered views for hourly/daily: master aligned labels + per-classification layers
    views_data = {
        "5m": build_view_dataset(records_5m, "timestamp_5m", chart_apps, colors),
        "hourly": build_layered_view(layered_rows, "hourly", chart_apps, colors),
        "daily": build_layered_view(layered_rows, "daily", chart_apps, colors),
    }
    table_data = build_table_data(
        layered_rows,
        views_data["5m"]["labels"],
        views_data["5m"]["bucketClassification"],
    )

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Checkboxes that should start unchecked (driven by --exclude at generation time)
    # The full viewsData is still embedded so the user can toggle them back on.
    cls_initially_excluded = exclude_classifications or []

    # The date filter can only ever select buckets that exist, so the inputs are
    # bounded by the data's own extent rather than by "now". The bounds go in as
    # datetime-local values ("T" separator), not database labels (" " separator):
    # a browser silently ignores a min/max it cannot parse, which would leave the
    # inputs unbounded exactly when they are supposed to be constrained.
    coverage_first = bucket_label_to_input_value(coverage.get("first_bucket"))
    coverage_last = bucket_label_to_input_value(coverage.get("last_bucket"))
    days_with_data = coverage.get("day_count", 0)
    if days is None:
        window_detail = f"All recorded history · {days_with_data} days with data"
    else:
        window_detail = f"Last {days} days · {days_with_data} days with data"

    html_content = HTML_TEMPLATE.replace("__CHART_JS__", CHART_JS)
    html_content = html_content.replace("__GENERATED_TIME__", now_str)
    html_content = html_content.replace(
        "__COVERAGE_SUMMARY__", build_coverage_summary(coverage, days)
    )
    html_content = html_content.replace("__COVERAGE_FIRST__", coverage_first)
    html_content = html_content.replace("__COVERAGE_LAST__", coverage_last)
    html_content = html_content.replace("__WINDOW_DETAIL__", window_detail)
    html_content = html_content.replace("__TOTAL_TRANSFER__", format_bytes(grand_total))
    html_content = html_content.replace("__TOTAL_IN__", format_bytes(grand_in))
    html_content = html_content.replace("__TOTAL_OUT__", format_bytes(grand_out))
    html_content = html_content.replace("__ACTIVE_APPS__", str(len(visible_totals)))
    html_content = html_content.replace("__TABLE_ROWS__", table_html)
    html_content = html_content.replace("__PASS_THROUGH_APPS_JSON__", json.dumps(hidden_names))
    html_content = html_content.replace(
        "__PASS_THROUGH_NOTE__",
        f"[{format_names(hidden_names)}]" if hidden_names else "",
    )
    html_content = html_content.replace("__VIEWS_DATA_JSON__", json.dumps(views_data))
    html_content = html_content.replace("__TABLE_DATA_JSON__", json.dumps(table_data))
    html_content = html_content.replace(
        "__CLS_INITIALLY_EXCLUDED__", json.dumps(cls_initially_excluded)
    )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return output_path


def main() -> None:
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

    # Parse --exclude into a validated classification list (shared with report)
    exclude_classifications = parse_exclude_classes(parser, args.exclude)

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
