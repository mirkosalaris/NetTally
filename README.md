# NetTally

A lightweight, local-only background daemon and CLI utility for macOS that records per-application, per-day network transfer bytes (sent and received).

---

## Features

- **5-Minute Block Granularity**: Tracks network bytes in 5-minute intervals — pinpoint which app caused a spike at 14:15 vs 14:20.
- **Multi-Resolution Reporting**: Roll up data on demand to hourly or daily totals without losing the underlying detail.
- **Per-App & Per-Day Granularity**: Aggregates network bytes by application name and local calendar day (`YYYY-MM-DD`).
- **Zero Ongoing Cost & Local-Only**: Operates 100% locally with zero cloud dependencies, network uploads, or external telemetry.
- **Indefinite Retention**: Stores transfer history in a local SQLite database (`~/Library/Application Support/NetTally/usage.db`).
- **No Sudo Required**: Uses macOS `nettop` in non-elevated user mode and runs seamlessly as a background `LaunchAgent`.
- **Smart Process Folding**: Groups helper processes (e.g., `Google Chrome Helper`, `Claude Helper`, `Code Helper`) into clean app names using configurable rules in `app_map.json`.
- **CLI & Interactive HTML Reports**: Query history via terminal tables, CSV, JSON, or launch a multi-resolution interactive HTML chart dashboard.

---

## Installation & Teardown

### Installation

To install and register the background LaunchAgent service:

```bash
./install.sh
```

This will:
1. Copy scripts to `~/Library/Application Support/NetTally/`.
2. Generate and load `~/Library/LaunchAgents/com.nettally.daemon.plist`.
3. Start background collector daemon (runs automatically on login/reboot).
4. Symlink `./nettally` to `~/.local/bin/nettally` (if available).

### Status Check

To check if the daemon is active and view recent logs:

```bash
./nettally status
```

### Uninstallation

To stop and remove the background service (preserving your data):

```bash
./uninstall.sh
```

To purge all data including `usage.db`:

```bash
./uninstall.sh --purge
```

---

## Usage

Use the `./nettally` CLI command to query and visualize data:

### Summary Totals (default: last 30 days)
```bash
./nettally report
./nettally report --days 7
./nettally report --all
./nettally report --today
```

### 5-Minute Block Breakdown
```bash
./nettally report --by-5m
./nettally report --today --by-5m
./nettally report --app "Google Chrome" --by-5m
```

### Hourly Breakdown
```bash
./nettally report --by-hour
./nettally report --today --by-hour
```

### Daily Breakdown
```bash
./nettally report --by-day
```

### Filter by App
```bash
./nettally report --app "Google Chrome"
./nettally report --app "Chrome" --by-5m
```

### Machine-Readable Export (CSV or JSON)
```bash
./nettally report --format csv
./nettally report --by-5m --format json
```

### Interactive HTML Dashboard
```bash
./nettally html
```

Opens a browser with a Chart.js dashboard featuring three granularity views:
- **5-Min** — per-app stacked bar chart across 5-minute intervals
- **Hourly** — per-app stacked bar chart across hours
- **Daily** — per-app stacked bar chart across days

---

## System Architecture

```
~/Library/Application Support/NetTally/
  ├── usage.db          # SQLite DB with usage_5m and process_state tables
  ├── app_map.json      # Process canonicalization & folding configuration
  ├── collector.py      # Background nettop poller & delta engine
  ├── db.py             # SQLite database interface with 5m/hourly/daily query helpers
  ├── app_folder.py     # Helper process canonicalization logic
  ├── report.py         # CLI text reporting
  └── html_generator.py # Multi-resolution HTML dashboard builder

~/Library/LaunchAgents/
  └── com.nettally.daemon.plist
```

### Database Schema

```sql
CREATE TABLE usage_5m (
    timestamp_5m TEXT NOT NULL,   -- 'YYYY-MM-DD HH:MM' (rounded to 5-min boundary)
    day          TEXT NOT NULL,   -- 'YYYY-MM-DD' (cached for fast rollups)
    app_name     TEXT NOT NULL,
    bytes_in     INTEGER NOT NULL DEFAULT 0,
    bytes_out    INTEGER NOT NULL DEFAULT 0,
    sample_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (timestamp_5m, app_name)
);
```

### How Data Collection Works

1. **Polling**: Every 30 seconds, `collector.py` executes `nettop -P -L 1 -x -J bytes_in,bytes_out`.
2. **Delta Calculation**: Computes per-process byte deltas against stored `process_state` baselines. Handles process restarts and PID reuse gracefully.
3. **App Folding**: Maps raw process identifiers (e.g. `Google Chrome H.1535`) to canonical app names via `app_map.json`.
4. **5-Minute Upsert**: Accumulates deltas via UPSERT into the current 5-minute bucket in `usage_5m`.
5. **Rollups**: Hourly and daily aggregations are computed on-the-fly at query time via SQL.

---

## Testing

Run unit tests to verify app folding, SQLite persistence, and day-boundary handling:

```bash
python3 -m unittest discover tests
```

---

## Known Limitations

- **Best-Effort Accounting**: Byte totals reflect counters reported by `nettop` and Activity Monitor, which are best-effort system estimations.
- **Truncated Process Names**: On some macOS builds, `nettop` truncates process names longer than ~16 characters (e.g. `Google Chrome H.` instead of `Google Chrome Helper`). Custom mappings are provided in `app_map.json` to handle these cleanups seamlessly.
- **macOS Only**: Uses `nettop`, `launchd`, and `launchctl` — inherently macOS-specific.
