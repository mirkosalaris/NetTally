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
from config import load_config

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

        .cls-filters {
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }

        .cls-filter-label {
            display: flex;
            align-items: center;
            gap: 5px;
            font-size: 0.8rem;
            color: var(--text-secondary);
            cursor: pointer;
            user-select: none;
        }

        .cls-filter-label input[type="checkbox"] {
            accent-color: var(--accent-color);
            width: 14px;
            height: 14px;
            cursor: pointer;
        }

        .cls-filter-label:hover {
            color: var(--text-primary);
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
        <div class="chart-header" style="flex-wrap: wrap; gap: 12px;">
            <div class="chart-title">Transfer Timeline Breakdown</div>
            <div class="chart-controls" style="flex-wrap: wrap; gap: 12px;">
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
                <div class="cls-filters" id="clsFilters">
                    <label class="cls-filter-label">
                        <input type="checkbox" id="chk-awake" checked onchange="applyFilter()"> Awake
                    </label>
                    <label class="cls-filter-label">
                        <input type="checkbox" id="chk-dark-wake" checked onchange="applyFilter()"> Dark-wake gaps
                    </label>
                    <label class="cls-filter-label">
                        <input type="checkbox" id="chk-sleep-wake" checked onchange="applyFilter()"> Post-wake catch-up bursts
                    </label>
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
        const initiallyExcluded = __CLS_INITIALLY_EXCLUDED__;

        const ctx = document.getElementById('usageChart').getContext('2d');

        // ── Stripe pattern cache for dark_wake_only bars ──────────────────────
        const stripePatternCache = {};
        function makeStripePattern(baseColor) {
            const patCanvas = document.createElement('canvas');
            patCanvas.width = 8;
            patCanvas.height = 8;
            const pctx = patCanvas.getContext('2d');
            // Dark muted background
            pctx.fillStyle = 'rgba(0,0,0,0.35)';
            pctx.fillRect(0, 0, 8, 8);
            // Diagonal stripes in the dataset's color, semi-transparent
            pctx.strokeStyle = baseColor;
            pctx.globalAlpha = 0.55;
            pctx.lineWidth = 2;
            pctx.beginPath();
            pctx.moveTo(0, 8); pctx.lineTo(8, 0);
            pctx.moveTo(-4, 4); pctx.lineTo(4, -4);
            pctx.moveTo(4, 12); pctx.lineTo(12, 4);
            pctx.stroke();
            return ctx.createPattern(patCanvas, 'repeat');
        }
        function getStripePattern(color) {
            if (!stripePatternCache[color]) {
                stripePatternCache[color] = makeStripePattern(color);
            }
            return stripePatternCache[color];
        }

        // ── Classification helpers ────────────────────────────────────────────
        // Stores the classifications of bars currently rendered (post-filter).
        // Used by the tooltip callback to annotate dark_wake_only bars.
        let currentClassifications = [];

        function classKey(cls) {
            // Fold both NULL/undefined and explicit 'unknown_gap' into 'awake'
            if (cls === 'unknown_gap' || cls == null) return 'awake';
            return cls;
        }

        function getCheckedClassifications() {
            const checked = new Set();
            if (document.getElementById('chk-awake').checked)      checked.add('awake');
            if (document.getElementById('chk-dark-wake').checked)  checked.add('dark_wake_only');
            if (document.getElementById('chk-sleep-wake').checked) checked.add('sleep_then_full_wake');
            return checked;
        }

        // Build combined dataset for hourly/daily by summing selected layers elementwise.
        function buildCombinedDataForGranularity(granularity, checkedCls) {
            const original = viewsData[granularity];
            if (!original) return null;
            if (granularity === '5m') return original;

            const labels = original.labels || [];
            const layers = original.layers || {};

            // choose a reference datasets list to copy labels/colors
            const refLayer = layers['awake'] || Object.values(layers)[0];
            if (!refLayer) return { labels: [], datasets: [], bucketClassification: [] };

            const ndatasets = refLayer.datasets.length;
            const datasets = [];
            for (let j = 0; j < ndatasets; j++) {
                const refDs = refLayer.datasets[j] || { label: 'Unknown', data: [], backgroundColor: '#94a3b8' };
                const combinedData = labels.map((_, i) => {
                    let sum = 0;
                    for (const layerName of Object.keys(layers)) {
                        if (!checkedCls.has(layerName)) continue;
                        const lds = layers[layerName].datasets[j];
                        if (lds && Array.isArray(lds.data)) sum += lds.data[i] || 0;
                    }
                    return sum;
                });
                datasets.push({ label: refDs.label, data: combinedData, backgroundColor: refDs.backgroundColor });
            }

            // Per-dataset classifications (for stripe/tooltip) are computed in applyFilter(),
            // where checkbox state is available.
            return { labels, datasets, layers };
        }

        // Apply per-bar stripe pattern or solid color based on per-dataset classifications
        function coloredDatasets(perDatasetClassifications, baseDatasets) {
            return baseDatasets.map((ds, datasetIndex) => {
                const base = ds.backgroundColor; // single color string from viewsData
                const perBar = perDatasetClassifications.map(clsArr => {
                    const cls = clsArr && clsArr[datasetIndex];
                    return cls === 'dark_wake_only' ? getStripePattern(base) : base;
                });
                return { ...ds, backgroundColor: perBar };
            });
        }

        // ── Chart init (starts empty; applyFilter() renders first frame) ──────
        let currentChart = new Chart(ctx, {
            type: 'bar',
            data: { labels: [], datasets: [] },
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
                            callback: function(value) { return formatBytesJS(value); }
                        }
                    }
                },
                plugins: {
                    legend: { labels: { color: '#f8fafc' } },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                let label = context.dataset.label + ': ' + formatBytesJS(context.raw);
                                const perLabel = currentClassifications[context.dataIndex] || [];
                                const cls = perLabel[context.datasetIndex];
                                if (cls === 'dark_wake_only') {
                                    label += '  ⚠ dark-wake gap';
                                } else if (cls === 'sleep_then_full_wake') {
                                    label += '  ↑ post-wake burst';
                                }
                                return label;
                            }
                        }
                    }
                }
            }
        });

        let currentGranularity = '5m';

        window.onload = function() {
            // Apply initial excluded state from --exclude CLI flag
            if (initiallyExcluded.includes('dark_wake_only')) {
                document.getElementById('chk-dark-wake').checked = false;
            }
            if (initiallyExcluded.includes('sleep_then_full_wake')) {
                document.getElementById('chk-sleep-wake').checked = false;
            }
            initDateInputs();
            applyFilter();
        };

        function initDateInputs() {
            const labels = viewsData[currentGranularity]?.labels;
            if (labels && labels.length > 0) {
                document.getElementById('startDate').value = formatForInput(labels[0]);
                document.getElementById('endDate').value   = formatForInput(labels[labels.length - 1]);
            }
        }

        function formatForInput(labelStr) {
            if (labelStr.length === 10) return labelStr + 'T00:00';
            return labelStr.replace(' ', 'T');
        }

        // ── Unified filter: date-range ∩ classification ───────────────────────
        function applyFilter() {
            const startVal = document.getElementById('startDate').value;
            const endVal   = document.getElementById('endDate').value;
            const checkedCls = getCheckedClassifications();
            const originalData = viewsData[currentGranularity];
            if (!originalData) return;

            if (currentGranularity === '5m') {
                const filteredLabels = [];
                const filteredCls    = [];
                const filteredDatasets = originalData.datasets.map(ds => ({ ...ds, data: [] }));

                for (let i = 0; i < originalData.labels.length; i++) {
                    const label = originalData.labels[i];
                    const cls   = classKey(originalData.bucketClassification?.[i]);

                    // Date-range filter (always pass if inputs are empty)
                    let dateOk = true;
                    if (startVal && endVal) {
                        let cmpLabel = label;
                        let cmpStart = startVal.replace('T', ' ');
                        let cmpEnd   = endVal.replace('T', ' ');
                        if (currentGranularity === 'daily') {
                            cmpLabel = label.substring(0, 10);
                            cmpStart = cmpStart.substring(0, 10);
                            cmpEnd   = cmpEnd.substring(0, 10);
                        }
                        dateOk = cmpLabel >= cmpStart && cmpLabel <= cmpEnd;
                    }

                    // Classification filter: per-row classification must be checked
                    const clsOk = checkedCls.has(cls);

                    if (dateOk && clsOk) {
                        filteredLabels.push(label);
                        filteredCls.push(cls);
                        for (let j = 0; j < originalData.datasets.length; j++) {
                            filteredDatasets[j].data.push(originalData.datasets[j].data[i]);
                        }
                    }
                }

                currentClassifications = filteredCls.map(c => [c]); // normalize to per-dataset shape
                currentChart.data = {
                    labels: filteredLabels,
                    datasets: coloredDatasets(currentClassifications, filteredDatasets)
                };
                currentChart.update();
                return;
            }

            // Hourly / Daily: sum selected layers client-side (labels are master-aligned)
            const combined = buildCombinedDataForGranularity(currentGranularity, checkedCls);
            const labels = combined.labels || [];
            const layers = combined.layers || {};
            const combinedDatasets = combined.datasets || [];

            // Compute per-label per-dataset classifications for stripe/tooltip decisions
            // Stripe/tooltip classification must reflect what's actually summed into the
            // visible bar, not the raw per-layer values regardless of checkbox state.
            // A layer's contribution only counts here if its checkbox is checked --
            // otherwise a hour that mixes e.g. awake + dark-wake traffic would keep
            // "seeing" the unchecked awake bytes and never render as exclusively dark-wake.
            const perDatasetClassifications = labels.map((_, i) => {
                const arr = [];
                for (let j = 0; j < combinedDatasets.length; j++) {
                    const awakeRaw = (layers['awake'] && layers['awake'].datasets[j] && layers['awake'].datasets[j].data[i]) || 0;
                    const darkRaw  = (layers['dark_wake_only'] && layers['dark_wake_only'].datasets[j] && layers['dark_wake_only'].datasets[j].data[i]) || 0;
                    const sleepRaw = (layers['sleep_then_full_wake'] && layers['sleep_then_full_wake'].datasets[j] && layers['sleep_then_full_wake'].datasets[j].data[i]) || 0;

                    const awakeVal = checkedCls.has('awake') ? awakeRaw : 0;
                    const darkVal  = checkedCls.has('dark_wake_only') ? darkRaw : 0;
                    const sleepVal = checkedCls.has('sleep_then_full_wake') ? sleepRaw : 0;

                    let cls = null;
                    if (darkVal > 0 && awakeVal === 0 && sleepVal === 0) cls = 'dark_wake_only';
                    else if (sleepVal > 0 && awakeVal === 0 && darkVal === 0) cls = 'sleep_then_full_wake';
                    else if (awakeVal > 0) cls = 'awake';
                    arr.push(cls);
                }
                return arr;
            });

            // Date-range filtering (keep labels but zero-pad dataset values outside range)
            const filteredLabels = [];
            const filteredDatasets = combinedDatasets.map(ds => ({ ...ds, data: [] }));
            const filteredClassifications = [];

            for (let i = 0; i < labels.length; i++) {
                const label = labels[i];
                let dateOk = true;
                if (startVal && endVal) {
                    let cmpLabel = label;
                    let cmpStart = startVal.replace('T', ' ');
                    let cmpEnd   = endVal.replace('T', ' ');
                    if (currentGranularity === 'daily') {
                        cmpLabel = label.substring(0, 10);
                        cmpStart = cmpStart.substring(0, 10);
                        cmpEnd   = cmpEnd.substring(0, 10);
                    }
                    dateOk = cmpLabel >= cmpStart && cmpLabel <= cmpEnd;
                }

                if (dateOk) {
                    filteredLabels.push(label);
                    filteredClassifications.push(perDatasetClassifications[i]);
                    for (let j = 0; j < combinedDatasets.length; j++) {
                        filteredDatasets[j].data.push(combinedDatasets[j].data[i] || 0);
                    }
                }
            }

            currentClassifications = filteredClassifications;
            currentChart.data = {
                labels: filteredLabels,
                datasets: coloredDatasets(filteredClassifications, filteredDatasets)
            };
            currentChart.update();
        }

        function resetFilter() {
            initDateInputs();
            // Re-check all boxes on reset
            document.getElementById('chk-awake').checked      = true;
            document.getElementById('chk-dark-wake').checked  = true;
            document.getElementById('chk-sleep-wake').checked = true;
            applyFilter();
        }

        function switchGranularity(viewKey, btnElement) {
            document.querySelectorAll('.btn-granularity').forEach(btn => btn.classList.remove('active'));
            btnElement.classList.add('active');
            currentGranularity = viewKey;
            if (viewsData[viewKey]) {
                applyFilter();
            }
        }

        // ── Utilities ─────────────────────────────────────────────────────────
        function formatBytesJS(bytes) {
            if (bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
        }

        function filterTable() {
            const input  = document.getElementById('searchInput');
            const filter = input.value.toLowerCase();
            const table  = document.getElementById('usageTable');
            const tr     = table.getElementsByTagName('tr');
            for (let i = 1; i < tr.length; i++) {
                const td = tr[i].getElementsByTagName('td')[0];
                if (td) {
                    const txtValue = td.textContent || td.innerText;
                    tr[i].style.display = txtValue.toLowerCase().indexOf(filter) > -1 ? '' : 'none';
                }
            }
        }

        let currentSortCol = -1;
        let sortAscending  = true;

        function sortTable(colIndex, isRaw) {
            const table = document.getElementById('usageTable');
            const tbody = table.getElementsByTagName('tbody')[0];
            const rows  = Array.from(tbody.getElementsByTagName('tr'));
            if (rows.length === 1 && rows[0].cells.length === 1) return;

            if (currentSortCol === colIndex) {
                sortAscending = !sortAscending;
            } else {
                sortAscending  = true;
                currentSortCol = colIndex;
            }

            rows.sort((a, b) => {
                let cellA = a.getElementsByTagName('td')[colIndex];
                let cellB = b.getElementsByTagName('td')[colIndex];
                let valA, valB;
                if (isRaw) {
                    valA = parseInt(cellA.getAttribute('data-raw') || 0, 10);
                    valB = parseInt(cellB.getAttribute('data-raw') || 0, 10);
                } else if (colIndex === 4) {
                    valA = parseInt(cellA.textContent || cellA.innerText, 10);
                    valB = parseInt(cellB.textContent || cellB.innerText, 10);
                } else {
                    valA = (cellA.textContent || cellA.innerText).toLowerCase();
                    valB = (cellB.textContent || cellB.innerText).toLowerCase();
                }
                if (valA < valB) return sortAscending ? -1 : 1;
                if (valA > valB) return sortAscending ?  1 : -1;
                return 0;
            });

            const ths = table.getElementsByTagName('th');
            for (let i = 0; i < ths.length; i++) {
                ths[i].innerHTML = ths[i].innerHTML.replace(/ [▲▼]/, '');
                if (i === colIndex) ths[i].innerHTML += sortAscending ? ' ▲' : ' ▼';
            }
            rows.forEach(row => tbody.appendChild(row));
        }
    </script>
</body>
</html>
"""

def build_view_dataset(records: List[Dict], time_key_name: str, top_apps: List[str], colors: List[str]) -> Dict:
    distinct_times = sorted(list(set(r[time_key_name] for r in records)))

    # Build per-bucket classification map: use MAX(gap_classification) already computed
    # by the SQL query. All rows for the same timestamp share the same classification,
    # so the first non-None value wins. NULL/None → 'awake'.
    bucket_cls_map: Dict[str, str] = {}
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
        "datasets": datasets,
        "bucketClassification": bucket_classifications
    }

def generate_html_report(db_path: str, days: Optional[int] = None, output_path: str = DEFAULT_HTML_PATH, exclude_classifications: Optional[List[str]] = None) -> str:
    cfg = load_config()
    if days is None:
        days = cfg["default_report_days"]

    totals = query_usage_totals(db_path, days=days, exclude_classifications=exclude_classifications)
    daily_records = query_usage_by_day(db_path, days=days, exclude_classifications=exclude_classifications)
    hourly_records = query_usage_by_hour(db_path, days=days, exclude_classifications=exclude_classifications)
    records_5m = query_usage_by_5m(db_path, days=days, exclude_classifications=exclude_classifications)

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

    top_apps_limit = cfg.get("html_top_apps_limit", 8)
    top_apps = [r["app_name"] for r in totals[:top_apps_limit]] if totals else []
    colors = [
        '#38bdf8', '#818cf8', '#c084fc', '#f472b6',
        '#fb7185', '#34d399', '#fbbf24', '#a3e635', '#94a3b8'
    ]

    # Build layered views for hourly/daily: master aligned labels + per-classification layers
    def align_layer(master_labels, layer_records, time_key_name, force_include_other: bool = False):
        datasets = []
        for idx, app in enumerate(top_apps):
            color = colors[idx % len(colors)]
            data = []
            for t in master_labels:
                bytes_sum = sum(r["total_bytes"] for r in layer_records if r[time_key_name] == t and r["app_name"] == app)
                data.append(bytes_sum)
            datasets.append({
                "label": app,
                "data": data,
                "backgroundColor": color
            })

        # Other Apps
        other_data = []
        for t in master_labels:
            bytes_sum = sum(r["total_bytes"] for r in layer_records if r[time_key_name] == t and r["app_name"] not in top_apps)
            other_data.append(bytes_sum)
        if force_include_other or any(v > 0 for v in other_data):
            datasets.append({
                "label": "Other Apps",
                "data": other_data,
                "backgroundColor": colors[-1]
            })

        return datasets

    # Master label axes (authoritative, unfiltered)
    hourly_master_records = query_usage_by_hour(db_path, days=days, exclude_classifications=exclude_classifications)
    daily_master_records = query_usage_by_day(db_path, days=days, exclude_classifications=exclude_classifications)

    hourly_labels = sorted(list({r["timestamp_hour"] for r in hourly_master_records}))
    daily_labels = sorted(list({r["day"] for r in daily_master_records}))

    classifications = ["awake", "dark_wake_only", "sleep_then_full_wake"]

    hourly_layers = {}
    daily_layers = {}
    for cls in classifications:
        hr_recs = query_usage_by_hour(db_path, days=days, exclude_classifications=exclude_classifications, only_classification=cls)
        dy_recs = query_usage_by_day(db_path, days=days, exclude_classifications=exclude_classifications, only_classification=cls)
        # Force inclusion of an "Other Apps" series if the master (unfiltered) records contain any below-top-apps traffic
        hourly_layers[cls] = {"datasets": align_layer(hourly_labels, hr_recs, "timestamp_hour", force_include_other=any(r["app_name"] not in top_apps and (r["total_bytes"] or 0) > 0 for r in hourly_master_records))}
        daily_layers[cls] = {"datasets": align_layer(daily_labels, dy_recs, "day", force_include_other=any(r["app_name"] not in top_apps and (r["total_bytes"] or 0) > 0 for r in daily_master_records))}

    views_data = {
        "5m": build_view_dataset(records_5m, "timestamp_5m", top_apps, colors),
        "hourly": {
            "labels": hourly_labels,
            "layers": hourly_layers
        },
        "daily": {
            "labels": daily_labels,
            "layers": daily_layers
        }
    }

    import datetime
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
    html_content = html_content.replace("__CLS_INITIALLY_EXCLUDED__", json.dumps(cls_initially_excluded))

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    return output_path

def main():
    cfg = load_config()
    parser = argparse.ArgumentParser(description="Generate HTML network usage dashboard")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database file")
    parser.add_argument("--days", type=int, default=cfg["default_report_days"], help=f"Number of past days to include (default: {cfg['default_report_days']})")
    parser.add_argument("--out", type=str, default=DEFAULT_HTML_PATH, help="Output HTML file path")
    parser.add_argument("--open", action="store_true", help="Open generated HTML file in default browser")
    parser.add_argument(
        "--exclude", type=str, default=None,
        metavar="CLASS",
        help="Comma-separated gap classifications to exclude from the embedded data: dark_wake_only, sleep_then_full_wake. The corresponding dashboard checkboxes will start unchecked (but can be toggled back on interactively)."
    )
    args = parser.parse_args()

    # Parse --exclude into a list; validate values
    valid_classes = {"dark_wake_only", "sleep_then_full_wake"}
    exclude_classifications: Optional[List[str]] = None
    if args.exclude:
        parsed = [c.strip() for c in args.exclude.split(",") if c.strip()]
        invalid = [c for c in parsed if c not in valid_classes]
        if invalid:
            parser.error(f"Unknown classification(s): {', '.join(invalid)}. Valid values: {', '.join(sorted(valid_classes))}")
        exclude_classifications = parsed if parsed else None

    db_path = get_db_path(args.db)
    path = generate_html_report(db_path, days=args.days, output_path=args.out, exclude_classifications=exclude_classifications)
    print(f"HTML dashboard generated: file://{path}")

    if args.open:
        webbrowser.open(f"file://{path}")

if __name__ == "__main__":
    main()

