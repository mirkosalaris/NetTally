# 0007 — Pass-through apps are excluded at report time, not collection time

Date: 2026-10-07

## Context

Take "GlobalProtect VPN" as the canonical example: per-5-minute inspection shows its bytes
are almost exactly the sum of every other app's — the classic signature of a VPN tunnel
double-counting: nettop attributes each payload both to the app that generated it and to the
VPN client that carries the same bytes out over the tunnel. A tunnel that carries most of the
traffic can account for nearly half of everything recorded. The two raw processes
(`PanGPS`, `GlobalProtect`) both fold to "GlobalProtect VPN" via `app_map.json`, so the
folded name is the thing reports must hide.

Automatic detection was considered and set aside: the collector would have to notice that one
app's bytes consistently mirror the rest of the machine's (per bucket, per direction, with
thresholds and lag), which is heuristic, architecture-dependent, and easy to get wrong in both
directions. It may be tackled later, but it needs careful planning; for now the user says
which apps are pass-throughs.

## Decision

Register pass-through apps by hand in `passthrough.json` (beside `config.json` in Application
Support), managed by `nettally passthrough add|remove|list`, and **exclude them when a report
is generated**, never at collection time:

- The collector and `db.py` stay ignorant of the registry — `usage_5m` keeps every byte
  exactly as recorded. The audit trail stays complete and the hot polling loop gains no new
  configuration to read.
- Exclusion is retroactive: registering an app corrects all recorded history on the next
  report, not just future rows.
- Exclusion is reversible: `passthrough remove` restores the full numbers in full, and
  `report --include-passthrough` opts back in for one run (the HTML dashboard embeds the rows
  behind a toggle that is off by default).
- Matching is case-insensitive against the already-folded `app_name` reports display, so
  `add PanGPS` and `add "GlobalProtect VPN"` land on the same registry entry.
- A footer/stderr note names what was hidden, so a "missing" app in a report is never
  silently missing.

Alternatives considered:

- **Collection-time skip** (don't record the app at all): loses the raw data, so a wrong or
  later-revised registration is unrecoverable, and the collector would need to re-read the
  registry while polling.
- **A `pass_through` column on `usage_5m`**: conflates configuration with measurement, still
  requires something to write the flag, and makes the report-time toggle story harder (the
  rows themselves would carry the mark).
- **`db.py` filtering inside the queries**: couples every consumer of the query API to the
  registry and makes "show everything" a second code path through `db.py`.

## Consequences

- Reports (`report`, and the HTML dashboard) read the registry on every run; a test suite
  patches `passthrough.DEFAULT_REGISTRY_PATH` so a real registry on a dev machine can't make
  report output machine-dependent.
- `passthrough.json` is configuration that pairs with the preserved data, so a plain
  `uninstall` keeps it (like `usage.db`); only `--purge` deletes it.
- The file is hand-editable and tolerated as missing/corrupt (reads as an empty registry,
  with a logged warning), because a broken registry must never break a report.
- `collector.py` and `db.py` deliberately grow no knowledge of this file. If automatic
  detection ever lands, it should propose registry entries for the user to confirm, not
  silently change what is recorded.
