# 0001 — Classify sleep/dark-wake gaps using `pmset -g log`

Date: 2026-08-16 (commit b227270)

## Context

`nettop` byte counters keep accumulating across sleep/dark-wake. A big byte delta reported
right after a gap could be real traffic from one instant, or bytes that trickled in slowly
over hours of background dark-wake syncing — these look identical in the raw counters, but
should probably be treated differently when reporting "usage."

## Decision

When a poll's gap since the last poll exceeds `interval * gap_threshold_multiplier` (default
3x), parse `pmset -g log` for `Sleep` / `Wake` / `DarkWake` lines whose timestamp falls in that
window (with a margin, since log timestamps and poll timestamps aren't perfectly aligned).
Classify the gap as `dark_wake_only`, `sleep_then_full_wake`, or `unknown_gap` (no usable
events found), and store both the raw events (`power_events` table) and the gap verdict
(`gaps` table) for later inspection, plus tag the `usage_5m` row via `gap_classification`.

## Consequences

- Classification quality is bounded by whatever `pmset -g log` still retains — it does not
  keep forever, so very old gaps in the DB may become unverifiable later (relevant if writing
  a retroactive fix script, see 0004).
- `pmset -g log` has no native flag to limit its own output — anything that needs to keep this
  cheap on a frequent/repeated basis has to pipe through `tail` itself (see 0004, which needed
  this for a per-poll check rather than a rare per-gap one).
- This only fires reactively, on a poll that's actually late. It structurally cannot see a
  dark-wake state that persists across polls that all arrive on time — that blind spot is a
  separate, later bug (0004), not something this decision tried to solve.
