# 0004 — Propagate dark-wake state across on-time polls

Date: 2026-08-22 (commit d76c3dc)

## Context

Buckets were found tagged "awake" during a period the Mac was verifiably fully asleep the
whole time. Root cause: the gap detector (0001) only runs when a poll is late
(`time_diff > gap_threshold`). Two situations never trip it even during genuine, continuous
dark-wake: (1) a lone on-time poll sandwiched between two separate sub-threshold gaps, and (2)
a long dark-wake stretch (e.g. an extended Power Nap sync) where the collector never actually
misses a beat, so no individual gap ever exceeds the threshold despite the system staying
dark-waked for hours. A poll in either situation passes `gap_classification=None` by default,
silently defaulting to "awake."

`pmset -g systemstate` was investigated as a possible cheaper alternative to log-scanning.
Rejected: it's a point-in-time snapshot with no documented field distinguishing dark-wake from
full wake, and even if it had one, it structurally can't see a wake that both started and ended
between two 30-second polls — only the log-based approach (0001) has the necessary history.

## Decision

Track `last_classification` across the collector's main loop, parallel to `last_poll_epoch`.
When a poll arrives on time (no threshold-exceeding gap) but the previous poll was
`dark_wake_only`, check `pmset -g log` for a genuine `Wake` event since the last poll:
- No `Wake` found → propagate `dark_wake_only` onto this poll too (chain continues).
- `Wake` found → reclassify this poll as `sleep_then_full_wake` (chain broken by a real
  wake-up) — deliberately not just "awake," to keep the "first bucket after a wake" convention
  consistent with how a directly-detected gap ending in a real wake is already classified.

This check uses `pmset -g log | tail -n 200` rather than a full unbounded read, since — unlike
the rare/reactive gap-detector call — this one can run every poll for as long as a dark-wake
chain continues, and `pmset -g log` has no native output-limiting flag (see 0001).

## Consequences

- Historical data collected before this fix can have the same bug baked in; a one-off script
  (`fix_dark_wake_propagation_oneoff.py`, not part of the app, see AGENTS.md) replays the same
  logic retroactively against `usage_5m`, using a combination of a fresh `pmset -g log` read and
  the already-recorded `power_events` table. It explicitly reports (rather than guesses) when a
  historical gap is too old for either source to still cover.
- The `gaps` table is intentionally *not* written to by the new per-poll check — it's reserved
  for actually-detected gaps (a missed poll), and writing a row for every on-time poll during a
  long dark-wake chain would misrepresent normal polling as a gap.
