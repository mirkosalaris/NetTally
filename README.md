# NetTally

A lightweight, local-only background daemon and CLI utility for macOS that records per-application, per-day network transfer bytes (sent and received).

---

## Features

- **5-Minute Block Granularity**: Tracks network bytes in 5-minute intervals — pinpoint which app caused a spike at 14:15 vs 14:20.
- **Multi-Resolution Reporting**: Roll up data on demand to hourly or daily totals without losing the underlying detail.
- **Per-App & Per-Day Granularity**: Aggregates network bytes by application name and local calendar day (`YYYY-MM-DD`).
- **Zero Ongoing Cost & Local-Only**: Operates 100% locally with zero cloud dependencies, network uploads, or external telemetry. The HTML dashboard vendors its charting library, so viewing a report makes no network requests at all.
- **Indefinite Retention**: Stores transfer history in a local SQLite database (`~/Library/Application Support/NetTally/usage.db`).
- **No Sudo Required**: Uses macOS `nettop` in non-elevated user mode and runs seamlessly as a background `LaunchAgent`.
- **Smart Process Folding**: Groups helper processes (e.g., `Google Chrome Helper`, `Claude Helper`, `Code Helper`) into clean app names using configurable rules in `app_map.json`.
- **Sleep/Dark-Wake Classification**: Retroactively classifies polling gaps caused by sleep or dark-wake via `pmset` logs, so post-wake byte spikes aren't misread as a single instant of real traffic, and labels 5-minute buckets accordingly.
- **Pausing**: Stop recording on demand — indefinitely or with a timeout (`nettally pause --for 2h`). A timed pause resumes by itself, even across a reboot.
- **Pass-Through Apps**: Flag VPN clients (or anything else whose bytes mirror other apps') with `nettally passthrough add`, and reports stop counting the double-counted traffic.
- **CLI & Interactive HTML Reports**: Query history via terminal tables, CSV, JSON, or launch a multi-resolution interactive HTML chart dashboard.

---

## Installation & Teardown

### Installation

To install and register the background LaunchAgent service:

```bash
./install.sh
```

Requires **Python 3.9+** (macOS system Python with the Xcode Command Line Tools installed, or
any Homebrew / python.org Python). `install.sh` detects an interpreter, version-checks it,
pins it for the background service and the CLI, and fails fast with a friendly message if
none is available.

This will:
1. Copy scripts to `~/Library/Application Support/NetTally/` (a fully standalone runtime).
2. Generate and load `~/Library/LaunchAgents/com.nettally.daemon.plist`.
3. Start the background collector daemon (runs automatically on login/reboot).
4. Symlink `nettally` to `~/.local/bin/nettally`, pointing at the standalone copy, and adds
   `~/.local/bin` to `$PATH` in your shell profile (`~/.zshrc` for zsh, `~/.bash_profile` for
   bash) if it isn't already there — open a new terminal afterwards so `nettally` is available.
   You can delete the source folder after install; `nettally` and the daemon keep working from
   Application Support.

### Upgrading / Reconfiguring

Re-run `./install.sh` after pulling a new version, or after a macOS
or Homebrew Python upgrade — it re-copies the code, re-pins the Python interpreter, and
regenerates the LaunchAgent plist. The source tree is the only place this can be run from:
the copy in Application Support is the runtime, not an upgrade path.

#### Configuration files

`config.json` and `app_map.json` are yours to edit, and `install.sh` will not overwrite
them silently. On a re-install each one is compared against the repo copy, and if it has
diverged you are asked whether to replace it — answering yes first writes a timestamped
`config.json.bak-YYYYMMDD-HHMMSS` (or `app_map.json.bak-…`) beside it, so nothing is lost.
Answering no, or hitting enter, keeps your file. When there is no terminal attached
(you pipe the output, or run it from a script) your file is kept and the installer prints
the non-interactive route instead: `rm` the file and re-run `./install.sh`, which then seeds
it from the repo.

Where each file is read from at runtime:

| File | Read from, in order | Notes |
| --- | --- | --- |
| `config.json` | `~/Library/Application Support/NetTally/config.json` | No fallback to the repo copy |
| `app_map.json` | explicit `--config` → Application Support → the copy next to `app_folder.py` | The last is only a fallback for a missing Application Support file |

Two consequences worth knowing before you edit:

- **The repo copies are install-time seeds, except for `app_map.json`.** Editing
  `config.json` in the source tree changes nothing until you re-run `./install.sh` and
  accept the replacement. Editing `app_map.json` in the source tree *does* affect
  `./nettally` run from that tree, but never the installed daemon, which reads
  Application Support.
- **New settings appear on their own; changed defaults do not.** `load_config()` merges
  your file over the `DEFAULTS` dict in `config.py`, so a setting added by an upgrade takes
  effect immediately. But if a setting is already present in your `config.json`, your value
  wins forever — bumping the default in `config.py` will not change it. Re-run
  `./install.sh` and accept the replacement to pick up new defaults, or edit the value in
  Application Support by hand.

Only `collector` accepts `--config`. `report` and `html` always read the Application
Support `config.json`, so pointing the collector at an alternative file also means its
`app_map.json` is the only other consumer of that flag.

### Status Check

To check if the daemon is active and view recent logs:

```bash
./nettally status
```

It also reports whether tracking is currently paused.

#### Logs

The collector writes its own log, so there is one file to read:

| File | Contents |
| --- | --- |
| `~/Library/Logs/NetTally/collector.log` | Everything, INFO and up. Rotated at 5 MB, keeping 3 backups (`collector.log.1` … `.3`) |
| `~/Library/Logs/NetTally/collector.err.log` | Normally empty. Only output produced *before* the collector could open its own log — an import error, a bad interpreter, an unwritable log directory |

```bash
tail -f ~/Library/Logs/NetTally/collector.log
```

Rotation is why the log is capped rather than growing forever: a launchd-redirected
stream never rotates, and the collector logs a few lines per poll indefinitely, which
measured out at roughly 375 KB/day.

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

### Excluding Sleep/Dark-Wake Traffic

`report` and `html` both accept `--exclude`, a comma-separated list of gap
classifications to leave out of the totals:

```bash
# only traffic that happened while you were actually awake
./nettally report --exclude dark_wake_only,sleep_then_full_wake

# drop just the post-wake catch-up bursts
./nettally report --exclude sleep_then_full_wake
```

The only valid values are `dark_wake_only` and `sleep_then_full_wake`; anything else is
rejected with an error listing the valid ones. `unknown_gap` is deliberately *not*
accepted — it is folded into the awake bucket everywhere it is aggregated, so excluding it
would be a no-op that looks like it worked.

For the dashboard, `--exclude` only decides which checkboxes start **unchecked**. All
three layers are always embedded in the file, so you can toggle any of them back on
interactively:

```bash
./nettally html --exclude sleep_then_full_wake
```

### Interactive HTML Dashboard
```bash
./nettally html
```

Opens a browser with a Chart.js dashboard featuring three granularity views:
- **5-Min** — per-app stacked bar chart across 5-minute intervals
- **Hourly** — per-app stacked bar chart across hours
- **Daily** — per-app stacked bar chart across days

On-chart controls, all of them live (nothing requires a regeneration):
- **Date range** — two `datetime-local` inputs plus **Apply** / **Reset**, which filter
  every granularity view to the selected window.
- **Granularity** — 5-Min / Hourly / Daily buttons switch the dataset in place.
- **Three classification checkboxes** — *Awake*, *Dark-wake gaps*, and *Post-wake
  catch-up bursts*. Unchecking one removes that layer from the bars, and the per-bar
  classification is recomputed against what is actually still displayed: a bar is only
  marked as exclusively dark-wake once the awake bytes are unchecked too, otherwise a
  mixed hour would keep looking awake no matter what you filtered.
- **Show pass-through apps** — present only when a registered pass-through app has
  traffic in the window. The rows are embedded in the payload but hidden by default, so
  the stat cards, the bars and the table start out matching the CLI reports; checking
  the box reveals them in place, and **Reset** puts the box back to hidden.

Two visual cues carry the classification, so a gap is identifiable without cross-referencing
the database:
- **Diagonal stripes** mark bars whose traffic is entirely `dark_wake_only`, drawn in the
  app's own colour so they stay legible against a stacked bar.
- **Tooltips annotate** each bar: `⚠ dark-wake gap` for `dark_wake_only` and
  `↑ post-wake burst` for `sleep_then_full_wake`. A tooltip is only annotated when the bar
  is exclusively that class, matching the stripe logic.

---

## Pausing Tracking

To stop recording without uninstalling the daemon:

```bash
# pause indefinitely, until you say otherwise
./nettally pause

# resume
./nettally resume
```

Or pause for a fixed time and let it resume itself:

```bash
./nettally pause --for 2h
```

`--for` accepts a bare number of seconds or a number with an `s`/`m`/`h`/`d` unit, and
units can be combined: `90`, `90s`, `45m`, `2h`, `1h30m`, `1d`. Garbage (`5x`, `2h!`,
`1.5h`) is rejected rather than reinterpreted.

Pausing again while paused replaces the previous deadline, so you can extend or shorten a
pause without resuming first.

```bash
./nettally pause --for 15m
./nettally pause --for 2h     # "Already paused (resumes in 15m); replacing it..."
```

`./nettally status` shows the current state, including how long is left on a timed pause:

```
=== NetTally Service Status ===
-  0  com.nettally.daemon
Tracking is PAUSED (resumes in 1h 47m).
```

### What a pause does and does not record

While paused, the collector does not call `nettop` at all and writes nothing to the
database — no `usage_5m` rows are created for that window. The pause is a **hole in the
data**.

Two further consequences worth knowing about:

- **The traffic that happens during a pause is not counted**, and cannot be recovered
  afterwards. That is the point of pausing.
- **At most one polling interval of usage is lost at each boundary.** Per-process byte
  counters are cumulative, so the collector drops its baselines when the pause starts and
  the first poll back is a pure re-baselining sample recording zero bytes. Without that,
  resuming would attribute the whole window's traffic to a single 5-minute bucket and the
  existing gap classifier would label it awake — a large fake spike.

A pause is also *not* treated as sleep: because the collector keeps advancing its poll
timer while idle, resuming does not trigger `pmset` gap classification, and no bucket is
tagged `dark_wake_only` or `unknown_gap`.

### Timed pauses survive a reboot

There is no background timer process. The deadline is an epoch timestamp stored in
`~/Library/Application Support/NetTally/pause_state.json`, and the collector's own polling
loop is what notices it has passed. So if the machine is powered off for longer than the
pause, tracking resumes on the collector's first cycle after it comes back.

An indefinite pause survives a reboot too — only `nettally resume` lifts it.

### Scope

Pausing is global: it stops recording for every app, and there is no per-app pause. The
pause state is a machine-level control, not a property of any one database, so it applies
whichever database the collector is pointed at.

---

## Pass-Through Apps (VPNs and other tunnel clients)

A VPN client double-counts. nettop attributes every tunnelled byte both to the app that
generated it *and* to the VPN process that carries the same payload out of the machine, so
totals that include the VPN roughly double the real traffic — a tunnel that carries most of
the traffic can show up as nearly half of everything ever recorded, almost exactly the sum of
all the other apps.

NetTally cannot tell on its own which processes behave this way, so you register them:

```bash
./nettally passthrough add PanGPS      # folds to "GlobalProtect VPN" via app_map.json
./nettally passthrough add "GlobalProtect VPN"   # same entry, other spelling
./nettally passthrough list
./nettally passthrough remove "GlobalProtect VPN"
```

`add` accepts the name exactly as reports display it, or a raw `nettop` process name —
either way it is folded through `app_map.json` first, and one registry entry covers every
process that folds into that name. A name that was never recorded is still registered (you
may be pre-registering an app before its first run), with a warning and a close-match
suggestion if the database has something similar.

The registry is a small JSON file (`~/Library/Application Support/NetTally/passthrough.json`),
hand-editable, and it survives `nettally uninstall` — like `usage.db`, it describes the data a
reinstall keeps. Only `nettally uninstall --purge` deletes it. `./nettally status` lists what
is registered.

**Exclusion happens when you report, not when the collector records.** The raw rows stay in
`usage.db` untouched, so registering a pass-through app is *retroactive* (the next report
corrects all of recorded history) and *reversible* (`remove` brings the numbers back in
full):

```bash
./nettally report                        # pass-through apps excluded, with a footer note
./nettally report --include-passthrough  # ...and back again for this run
```

Every table report names what it hid (`[Pass-through apps excluded: ...]`), so a "missing"
app is never silently missing; csv and json put that note on stderr so stdout stays
machine-readable. Raw SQL against `usage.db` still sees everything — only the reports filter.

The HTML dashboard follows the same rule with a twist: its rows are embedded in the
payload (the toggle could not reveal what was never sent), but every server-rendered
figure — stat cards, table rows, the active-app count — describes the *hidden* default,
and a **Show pass-through apps** checkbox in the filter bar flips them back on for one
look. A pass-through app also keeps its own chart series instead of folding into
"Other Apps", so revealing it shows exactly its bytes.

---

## System Architecture

```
~/Library/Application Support/NetTally/
  ├── usage.db          # SQLite DB with usage_5m, process_state, power_events, and gaps tables
  ├── app_map.json      # Process canonicalization & folding configuration
  ├── config.json       # Polling / reporting configuration
  ├── python_path       # Python interpreter pinned at install time
  ├── pause_state.json  # Present only while tracking is paused (see "Pausing Tracking")
  ├── passthrough.json  # Pass-through apps excluded from reports (see "Pass-Through Apps")
  ├── collector.py      # Background nettop poller & delta engine
  ├── db.py             # SQLite database interface with 5m/hourly/daily query helpers
  ├── app_folder.py     # Helper process canonicalization logic
  ├── report.py         # CLI text reporting
  ├── pause.py          # Pause state file + `pause` / `resume` / `state` commands
  ├── html_generator.py # Multi-resolution HTML dashboard builder
  ├── templates/        # HTML dashboard template
  ├── nettally          # CLI wrapper (symlinked onto PATH as `nettally`)
  ├── install.sh        # Installer — re-run this from the source tree to upgrade
  └── uninstall.sh      # `nettally uninstall`

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
    gap_classification TEXT,      -- NULL (awake) | 'dark_wake_only' | 'sleep_then_full_wake' | 'unknown_gap'
    PRIMARY KEY (timestamp_5m, app_name)
);

CREATE TABLE process_state (
    pid             INTEGER NOT NULL,
    process_name    TEXT NOT NULL,
    last_bytes_in   INTEGER NOT NULL,   -- cumulative counter at the last poll
    last_bytes_out  INTEGER NOT NULL,
    last_seen_epoch REAL NOT NULL,      -- epoch seconds of the last poll
    PRIMARY KEY (pid, process_name)
);

CREATE TABLE power_events (
    ts_epoch   REAL NOT NULL,     -- event timestamp, epoch seconds
    event_type TEXT NOT NULL,     -- 'Sleep' | 'Wake' | 'DarkWake'
    reason_raw TEXT,              -- raw pmset reason string, when the log gave one
    PRIMARY KEY (ts_epoch, event_type)
);

CREATE TABLE gaps (
    start_epoch    REAL NOT NULL,
    end_epoch      REAL NOT NULL,
    classification TEXT NOT NULL,
    PRIMARY KEY (start_epoch, end_epoch)
);
```

`gap_classification` is a closed set: `NULL` means the bucket reflects ordinary awake
traffic; `'dark_wake_only'`, `'sleep_then_full_wake'`, and `'unknown_gap'` mark buckets
written by the poll immediately after a sleep/dark-wake gap. Reports can filter those
classes out with `--exclude`; `'unknown_gap'` is counted as awake everywhere it's
aggregated. Databases created before gap classification existed get the column added by
an `ALTER TABLE ... ADD COLUMN gap_classification TEXT` on startup, so no migration step
is needed.

`process_state` is a scratch table: it holds the last cumulative byte counter seen per
process so the next poll can compute a delta, and stale rows are pruned on a timer
(`process_state_prune_interval_seconds` / `process_state_max_age_seconds` in
`config.json`). `power_events` and `gaps` are append-only audit trails of the `pmset -g
log` parsing; nothing currently reads them back, so you can safely delete those two
tables' rows without affecting any report.

### How Data Collection Works

1. **Polling**: Every 30 seconds, `collector.py` executes `nettop -P -L 1 -x -J bytes_in,bytes_out`.
2. **Delta Calculation**: Computes per-process byte deltas against stored `process_state` baselines. Handles process restarts and PID reuse gracefully.
3. **App Folding**: Maps raw process identifiers (e.g. `Google Chrome H.1535`) to canonical app names via `app_map.json`.
4. **5-Minute Upsert**: Accumulates deltas via UPSERT into the current 5-minute bucket in `usage_5m`.
5. **Rollups**: Hourly and daily aggregations are computed on-the-fly at query time via SQL.
6. **Gap Classification**: If a poll arrives late (sleep/dark-wake), the collector parses
   `pmset -g log` for Sleep/Wake/DarkWake events spanning the gap, records them, stores a
   verdict in the `gaps` table, and tags the post-gap 5-minute bucket with that
   classification so reports can exclude it.

---

## Testing

Run unit tests to verify gap classification, app folding, SQLite persistence, and
day-boundary handling:

```bash
python3 -m unittest discover -s tests
```

or via the Makefile:

```bash
make test      # unit tests
make lint      # ruff (checks + formatting)
make typecheck # mypy
make format    # ruff format
```

---

## Third-Party Code

| Component | Version | License | Where |
| --- | --- | --- | --- |
| [Chart.js](https://www.chartjs.org) | 4.4.7 | MIT | `templates/chart.umd.js`, inlined into every generated dashboard |

The vendored bundle is the official `dist/chart.umd.js` build with its `sourceMappingURL`
comment removed (the `.map` is not vendored, so leaving the pointer would make devtools
request a file that does not exist). Its license text is kept verbatim alongside it in
[`templates/CHARTJS-LICENSE.md`](templates/CHARTJS-LICENSE.md). To upgrade, replace the
bundle with a new official build, keep the version banner, and update the test that
asserts the pinned version.

---

## Known Limitations

- **Best-Effort Accounting**: Byte totals reflect counters reported by `nettop` and Activity Monitor, which are best-effort system estimations.
- **Sleep/Dark-Wake Gaps**: Network traffic measured immediately after waking up from sleep/dark-wake contains byte deltas that accumulated throughout the entire sleeping/idle period. NetTally retrospectively classifies these gaps using `pmset` logs, but the sub-gap timing of exact bytes (e.g. down to the minute of a specific background sync) remains unknown.
- **Truncated Process Names**: On some macOS builds, `nettop` truncates process names longer than ~16 characters (e.g. `Google Chrome H.` instead of `Google Chrome Helper`). Custom mappings are provided in `app_map.json` to handle these cleanups seamlessly.
- **macOS Only**: Uses `nettop`, `launchd`, and `launchctl` — inherently macOS-specific.
