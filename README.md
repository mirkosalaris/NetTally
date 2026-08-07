# NetTally

A lightweight, local-only background daemon and CLI utility for macOS that records per-application, per-day network transfer bytes (sent and received).

---

## Features

- **Per-App & Per-Day Granularity**: Aggregates network bytes by application name and local calendar day (`YYYY-MM-DD`).
- **Zero Ongoing Cost & Local-Only**: Operates 100% locally with zero cloud dependencies, network uploads, or external telemetry.
- **Indefinite Retention**: Stores transfer history in a local SQLite database (`~/Library/Application Support/NetTally/usage.db`).
- **No Sudo Required**: Uses macOS `nettop` in non-elevated user mode and runs seamlessly as a background `LaunchAgent`.
- **Smart Process Folding**: Groups helper processes (e.g., `Google Chrome Helper`, `Claude Helper`, `Code Helper`) into clean app names using configurable rules in `app_map.json`.
- **CLI & Rich HTML Reports**: Query history via terminal tables, CSV, JSON, or launch an interactive HTML chart dashboard.

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

### 1. Summary Report (Default 30 Days)
```bash
./nettally report
./nettally report --days 7
./nettally report --all
```

### 2. Daily Breakdown Report
```bash
./nettally report --by-day
```

### 3. Drill-Down by Specific App
```bash
./nettally report --app "Google Chrome"
```

### 4. Machine-Readable Export (CSV or JSON)
```bash
./nettally report --format csv
./nettally report --format json
```

### 5. Interactive HTML Dashboard
```bash
./nettally html
```
Generates a standalone HTML dashboard with interactive Chart.js stacked bar charts and opens it in your default browser.

---

## System Architecture

```
~/Library/Application Support/NetTally/
  ├── usage.db         # SQLite database storing usage_daily & process_state
  ├── app_map.json     # Process canonicalization & folding configuration
  ├── collector.py     # Background nettop poller & delta engine
  ├── db.py            # SQLite database interface & schema migrations
  ├── app_folder.py    # Helper process canonicalization logic
  ├── report.py        # CLI text reporting
  └── html_generator.py # Standalone HTML dashboard builder

~/Library/LaunchAgents/
  └── com.nettally.daemon.plist
```

### How Data Collection Works

1. **Polling**: Every 30 seconds, `collector.py` executes `nettop -P -L 1 -x -J bytes_in,bytes_out`.
2. **Delta Calculation**: Compares cumulative byte counters against `process_state` in SQLite. If a process restarts or PID resets, a fresh baseline is established.
3. **App Folding**: Maps raw process identifiers (e.g. `Google Chrome H.1535`) to clean names (`Google Chrome`) using `app_map.json`.
4. **Daily Upsert**: Accumulates deltas into `usage_daily` table under local date (`YYYY-MM-DD`).

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
