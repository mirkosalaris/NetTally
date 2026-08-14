#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import webbrowser
from typing import Optional, List, Dict

from db import (
    get_db_path,
    query_usage_totals,
    query_usage_by_day,
    query_usage_by_hour,
    query_usage_by_5m
)
from report import format_bytes

DEFAULT_HTML_PATH = os.path.expanduser("~/Library/Application Support/NetTally/dashboard.html")

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NetTally - Network Usage Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --card-border: #334155;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --accent-color: #38bdf8;
            --accent-hover: #0284c7;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }

        body {
            background-color: var(--bg-color);
            color: var(--text-primary);
            padding: 24px;
            max-width: 1400px;
            margin: 0 auto;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--card-border);
        }

        h1 {
            font-size: 1.8rem;
            font-weight: 700;
            color: var(--text-primary);
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .subtitle {
            color: var(--text-secondary);
            font-size: 0.9rem;
            margin-top: 4px;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }

        .stat-card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }

        .stat-title {
            color: var(--text-secondary);
            font-size: 0.85rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .stat-value {
            font-size: 1.8rem;
            font-weight: 700;
            color: var(--accent-color);
            margin-top: 8px;
        }

        .stat-detail {
            font-size: 0.8rem;
            color: var(--text-secondary);
            margin-top: 4px;
        }

        .chart-container {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
        }

        .chart-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }

        .chart-title {
            font-size: 1.1rem;
            font-weight: 600;
        }

        .granularity-selector {
            display: flex;
            gap: 8px;
            background: #0f172a;
            padding: 4px;
            border-radius: 8px;
            border: 1px solid var(--card-border);
        }

        .btn-granularity {
            background: transparent;
            border: none;
            color: var(--text-secondary);
            padding: 6px 14px;
            border-radius: 6px;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }

        .btn-granularity.active {
            background: var(--accent-color);
            color: #0f172a;
        }

        .chart-controls {
            display: flex;
            align-items: center;
            gap: 16px;
        }

        .date-filter {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .date-filter input[type="datetime-local"] {
            background: #0f172a;
            border: 1px solid var(--card-border);
            color: var(--text-primary);
            padding: 6px;
            border-radius: 6px;
            font-size: 0.85rem;
            color-scheme: dark;
        }

        .date-filter button {
            background: #1e293b;
            border: 1px solid var(--card-border);
            color: var(--text-secondary);
            padding: 6px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.85rem;
        }

        .date-filter button:hover {
            background: #334155;
            color: var(--text-primary);
        }

        .table-container {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 12px;
            padding: 24px;
            overflow-x: auto;
        }

        .table-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }

        input[type="text"] {
            background: #0f172a;
            border: 1px solid var(--card-border);
            border-radius: 6px;
            color: var(--text-primary);
            padding: 8px 12px;
            font-size: 0.9rem;
            width: 250px;
        }

        input[type="text"]:focus {
            outline: none;
            border-color: var(--accent-color);
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.95rem;
        }

        th {
            background: #0f172a;
            color: var(--text-secondary);
            font-weight: 600;
            padding: 12px 16px;
            border-bottom: 1px solid var(--card-border);
        }

        th.sortable {
            cursor: pointer;
            user-select: none;
        }
        
        th.sortable:hover {
            background: #1e293b;
            color: var(--text-primary);
        }

        td {
            padding: 12px 16px;
            border-bottom: 1px solid var(--card-border);
        }

        tr:hover {
            background: rgba(255, 255, 255, 0.02);
        }

        .badge {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.8rem;
            font-weight: 600;
            background: rgba(56, 189, 248, 0.1);
            color: var(--accent-color);
        }
    </style>
</head>
<body>
    <header>
        <div>
            <h1>📶 NetTally</h1>
            <div class="subtitle">Per-App Network Accounting & Timeline Analysis</div>
        </div>
        <div class="subtitle">Generated on __GENERATED_TIME__</div>
    </header>

    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-title">Total Transfer</div>
            <div class="stat-value">__TOTAL_TRANSFER__</div>
            <div class="stat-detail">Last __DAYS__ Days</div>
        </div>
        <div class="stat-card">
            <div class="stat-title">Total Received</div>
            <div class="stat-value">__TOTAL_IN__</div>
            <div class="stat-detail">Download Volume</div>
        </div>
        <div class="stat-card">
            <div class="stat-title">Total Sent</div>
            <div class="stat-value">__TOTAL_OUT__</div>
            <div class="stat-detail">Upload Volume</div>
        </div>
        <div class="stat-card">
            <div class="stat-title">Active Apps</div>
            <div class="stat-value">__ACTIVE_APPS__</div>
            <div class="stat-detail">Tracked Applications</div>
        </div>
    </div>

    <div class="chart-container">
        <div class="chart-header">
            <div class="chart-title">Transfer Timeline Breakdown</div>
            <div class="chart-controls">
                <div class="date-filter">
                    <input type="datetime-local" id="startDate">
                    <span style="color: var(--text-secondary);">to</span>
                    <input type="datetime-local" id="endDate">
                    <button onclick="applyFilter()">Apply</button>
                    <button onclick="resetFilter()">Reset</button>
                </div>
                <div class="granularity-selector">
                    <button class="btn-granularity active" onclick="switchGranularity('5m', this)">5-Min</button>
                    <button class="btn-granularity" onclick="switchGranularity('hourly', this)">Hourly</button>
                    <button class="btn-granularity" onclick="switchGranularity('daily', this)">Daily</button>
                </div>
            </div>
        </div>
        <div style="height: 380px; position: relative;">
            <canvas id="usageChart"></canvas>
        </div>
    </div>

    <div class="table-container">
        <div class="table-header">
            <div class="chart-title">Application Usage Summary</div>
            <input type="text" id="searchInput" onkeyup="filterTable()" placeholder="Search application...">
        </div>
        <table id="usageTable">
            <thead>
                <tr>
                    <th class="sortable" onclick="sortTable(0, false)">Application</th>
                    <th class="sortable" onclick="sortTable(1, true)">Received</th>
                    <th class="sortable" onclick="sortTable(2, true)">Sent</th>
                    <th class="sortable" onclick="sortTable(3, true)">Total Transfer</th>
                    <th class="sortable" onclick="sortTable(4, false)">Samples</th>
                </tr>
            </thead>
            <tbody>
                __TABLE_ROWS__
            </tbody>
        </table>
    </div>

    <script>
        const viewsData = __VIEWS_DATA_JSON__;

        const ctx = document.getElementById('usageChart').getContext('2d');
        let currentChart = new Chart(ctx, {
            type: 'bar',
            data: viewsData['5m'] || { labels: [], datasets: [] },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: {
                        stacked: true,
                        grid: { color: '#334155' },
                        ticks: { color: '#94a3b8', maxRotation: 45, minRotation: 0 }
                    },
                    y: {
                        stacked: true,
                        grid: { color: '#334155' },
                        ticks: {
                            color: '#94a3b8',
                            callback: function(value) {
                                return formatBytesJS(value);
                            }
                        }
                    }
                },
                plugins: {
                    legend: {
                        labels: { color: '#f8fafc' }
                    },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                return context.dataset.label + ': ' + formatBytesJS(context.raw);
                            }
                        }
                    }
                }
            }
        });

        let currentGranularity = '5m';

        window.onload = function() {
            initDateInputs();
        };

        function initDateInputs() {
            let labels = viewsData[currentGranularity]?.labels;
            if (labels && labels.length > 0) {
                document.getElementById('startDate').value = formatForInput(labels[0]);
                document.getElementById('endDate').value = formatForInput(labels[labels.length - 1]);
            }
        }

        function formatForInput(labelStr) {
            if (labelStr.length === 10) {
                return labelStr + "T00:00";
            }
            return labelStr.replace(" ", "T");
        }

        function applyFilter() {
            let startVal = document.getElementById('startDate').value;
            let endVal = document.getElementById('endDate').value;
            
            if (!startVal || !endVal) {
                currentChart.data = viewsData[currentGranularity];
                currentChart.update();
                return;
            }

            let start = startVal.replace("T", " ");
            let end = endVal.replace("T", " ");

            let originalData = viewsData[currentGranularity];
            let filteredLabels = [];
            let filteredDatasets = originalData.datasets.map(ds => ({
                ...ds,
                data: []
            }));

            for (let i = 0; i < originalData.labels.length; i++) {
                let label = originalData.labels[i];
                let compareLabel = label;
                let compareStart = start;
                let compareEnd = end;

                if (currentGranularity === 'daily') {
                    compareLabel = label.substring(0, 10);
                    compareStart = start.substring(0, 10);
                    compareEnd = end.substring(0, 10);
                }

                if (compareLabel >= compareStart && compareLabel <= compareEnd) {
                    filteredLabels.push(label);
                    for (let j = 0; j < originalData.datasets.length; j++) {
                        filteredDatasets[j].data.push(originalData.datasets[j].data[i]);
                    }
                }
            }

            currentChart.data = {
                labels: filteredLabels,
                datasets: filteredDatasets
            };
            currentChart.update();
        }

        function resetFilter() {
            initDateInputs();
            currentChart.data = viewsData[currentGranularity];
            currentChart.update();
        }

        function switchGranularity(viewKey, btnElement) {
            document.querySelectorAll('.btn-granularity').forEach(btn => btn.classList.remove('active'));
            btnElement.classList.add('active');
            currentGranularity = viewKey;
            if (viewsData[viewKey]) {
                applyFilter();
            }
        }

        function formatBytesJS(bytes) {
            if (bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }

        function filterTable() {
            const input = document.getElementById('searchInput');
            const filter = input.value.toLowerCase();
            const table = document.getElementById('usageTable');
            const tr = table.getElementsByTagName('tr');

            for (let i = 1; i < tr.length; i++) {
                const td = tr[i].getElementsByTagName('td')[0];
                if (td) {
                    const txtValue = td.textContent || td.innerText;
                    if (txtValue.toLowerCase().indexOf(filter) > -1) {
                        tr[i].style.display = "";
                    } else {
                        tr[i].style.display = "none";
                    }
                }
            }
        }

        let currentSortCol = -1;
        let sortAscending = true;

        function sortTable(colIndex, isRaw) {
            const table = document.getElementById("usageTable");
            const tbody = table.getElementsByTagName("tbody")[0];
            const rows = Array.from(tbody.getElementsByTagName("tr"));
            
            if (rows.length === 1 && rows[0].cells.length === 1) return;

            if (currentSortCol === colIndex) {
                sortAscending = !sortAscending;
            } else {
                sortAscending = true;
                currentSortCol = colIndex;
            }

            rows.sort((a, b) => {
                let cellA = a.getElementsByTagName("td")[colIndex];
                let cellB = b.getElementsByTagName("td")[colIndex];
                
                let valA, valB;
                if (isRaw) {
                    valA = parseInt(cellA.getAttribute("data-raw") || 0, 10);
                    valB = parseInt(cellB.getAttribute("data-raw") || 0, 10);
                } else if (colIndex === 4) {
                    valA = parseInt(cellA.textContent || cellA.innerText, 10);
                    valB = parseInt(cellB.textContent || cellB.innerText, 10);
                } else {
                    valA = cellA.textContent || cellA.innerText;
                    valB = cellB.textContent || cellB.innerText;
                    valA = valA.toLowerCase();
                    valB = valB.toLowerCase();
                }

                if (valA < valB) return sortAscending ? -1 : 1;
                if (valA > valB) return sortAscending ? 1 : -1;
                return 0;
            });

            const ths = table.getElementsByTagName("th");
            for (let i = 0; i < ths.length; i++) {
                ths[i].innerHTML = ths[i].innerHTML.replace(/ [▲▼]/, '');
                if (i === colIndex) {
                    ths[i].innerHTML += sortAscending ? ' ▲' : ' ▼';
                }
            }

            rows.forEach(row => tbody.appendChild(row));
        }
    </script>
</body>
</html>
"""

def build_view_dataset(records: List[Dict], time_key_name: str, top_apps: List[str], colors: List[str]) -> Dict:
    distinct_times = sorted(list(set(r[time_key_name] for r in records)))
    datasets = []

    for idx, app in enumerate(top_apps):
        color = colors[idx % len(colors)]
        data = []
        for t in distinct_times:
            bytes_sum = sum(r["total_bytes"] for r in records if r[time_key_name] == t and r["app_name"] == app)
            data.append(bytes_sum)
        datasets.append({
            "label": app,
            "data": data,
            "backgroundColor": color
        })

    if distinct_times and top_apps:
        other_data = []
        for t in distinct_times:
            bytes_sum = sum(r["total_bytes"] for r in records if r[time_key_name] == t and r["app_name"] not in top_apps)
            other_data.append(bytes_sum)
        if any(v > 0 for v in other_data):
            datasets.append({
                "label": "Other Apps",
                "data": other_data,
                "backgroundColor": colors[-1]
            })

    return {
        "labels": distinct_times,
        "datasets": datasets
    }

def generate_html_report(db_path: str, days: int = 30, output_path: str = DEFAULT_HTML_PATH) -> str:
    totals = query_usage_totals(db_path, days=days)
    daily_records = query_usage_by_day(db_path, days=days)
    hourly_records = query_usage_by_hour(db_path, days=days)
    records_5m = query_usage_by_5m(db_path, days=days)

    grand_in = sum(r["total_bytes_in"] or 0 for r in totals)
    grand_out = sum(r["total_bytes_out"] or 0 for r in totals)
    grand_total = sum(r["total_bytes"] or 0 for r in totals)

    if totals:
        table_rows = []
        for r in totals:
            table_rows.append(f"""
                <tr>
                    <td><strong>{r['app_name']}</strong></td>
                    <td data-raw="{r['total_bytes_in'] or 0}">{format_bytes(r['total_bytes_in'] or 0)}</td>
                    <td data-raw="{r['total_bytes_out'] or 0}">{format_bytes(r['total_bytes_out'] or 0)}</td>
                    <td data-raw="{r['total_bytes'] or 0}"><span class="badge">{format_bytes(r['total_bytes'] or 0)}</span></td>
                    <td>{r['total_samples'] or 0}</td>
                </tr>
            """)
        table_html = "\n".join(table_rows)
    else:
        table_html = '<tr><td colspan="5" style="text-align:center; color: var(--text-secondary); padding: 24px;">No network usage records found for this period.</td></tr>'

    top_apps = [r["app_name"] for r in totals[:8]] if totals else []
    colors = [
        '#38bdf8', '#818cf8', '#c084fc', '#f472b6',
        '#fb7185', '#34d399', '#fbbf24', '#a3e635', '#94a3b8'
    ]

    views_data = {
        "5m": build_view_dataset(records_5m, "timestamp_5m", top_apps, colors),
        "hourly": build_view_dataset(hourly_records, "timestamp_hour", top_apps, colors),
        "daily": build_view_dataset(daily_records, "day", top_apps, colors)
    }

    import datetime
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_content = HTML_TEMPLATE.replace("__GENERATED_TIME__", now_str)
    html_content = html_content.replace("__TOTAL_TRANSFER__", format_bytes(grand_total))
    html_content = html_content.replace("__TOTAL_IN__", format_bytes(grand_in))
    html_content = html_content.replace("__TOTAL_OUT__", format_bytes(grand_out))
    html_content = html_content.replace("__ACTIVE_APPS__", str(len(totals)))
    html_content = html_content.replace("__DAYS__", str(days))
    html_content = html_content.replace("__TABLE_ROWS__", table_html)
    html_content = html_content.replace("__VIEWS_DATA_JSON__", json.dumps(views_data))

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return output_path

def main():
    parser = argparse.ArgumentParser(description="Generate HTML network usage dashboard")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database file")
    parser.add_argument("--days", type=int, default=30, help="Number of past days to include (default: 30)")
    parser.add_argument("--out", type=str, default=DEFAULT_HTML_PATH, help="Output HTML file path")
    parser.add_argument("--open", action="store_true", help="Open generated HTML file in default browser")
    args = parser.parse_args()

    db_path = get_db_path(args.db)
    path = generate_html_report(db_path, days=args.days, output_path=args.out)
    print(f"HTML dashboard generated: file://{path}")

    if args.open:
        webbrowser.open(f"file://{path}")

if __name__ == "__main__":
    main()
