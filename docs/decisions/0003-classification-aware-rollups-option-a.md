# 0003 — Classification-aware Hourly/Daily rollups: precomputed layers (Option A)

Date: 2026-08-19 (commit 247c1d6)

## Context

The dashboard needed Hourly/Daily charts where the user can toggle awake / dark_wake_only /
sleep_then_full_wake on and off, same as the 5-min view already allowed. Four designs were
considered:

- **A.** Precompute one full query result per classification ("layer") in Python at report
  generation time; sum layers client-side in JS depending on which checkboxes are on.
- **B.** Ship all 5-minute rows to the client and let JS re-aggregate to hour/day on the fly,
  filtering by classification in JS.
- **C.** A single SQL query per granularity returning classification as an extra dimension,
  pivoted client-side.
- **D.** Re-query the server (no server in this app — would mean shelling out to Python again)
  per checkbox change.

## Decision

Option A. Explicitly chosen over B despite B's apparent simplicity, because: this project's
test coverage lives in Python (`tests/`), not JS, so keeping the filtering/aggregation logic in
Python keeps it testable; and it avoids touching `query_usage_by_hour()` / `query_usage_by_day()`
at all — they keep their existing signature and default (unfiltered) behavior, used as-is by
`report.py`. The added complexity of three precomputed layers instead of one query lives
entirely in `html_generator.py`'s Python-side report generation, not in the DB query layer.

## Consequences

- `align_layer()` must produce the same dataset count/order/length across all three layers
  (`awake`, `dark_wake_only`, `sleep_then_full_wake`) for a given granularity, including an
  "Other Apps" series that must appear (even as an all-zero series) in every layer if it
  appears in any — the combine step in JS has no reference-layer fallback logic and will
  silently drop data if the layers' shapes don't match. (This was bug #2 in the initial
  implementation, since fixed.)
- `unknown_gap` must be folded into the `awake` layer in both the Python query
  (`only_classification == 'awake'` branch in db.py) and the JS `classKey()` — it's easy to fix
  one and miss the other. (This was bug #1, since fixed.)
- `--exclude` must be threaded into the per-layer queries too, not just the unfiltered/5m ones.
  (This was bug #3, since fixed.)
- If a 4th classification value or a new UI filter dimension is ever added, expect to touch
  `align_layer()`, `classKey()`/`checkedCls` handling, and the per-layer query loop together —
  they're currently three separate places that all have to agree, by convention, not by shared
  code.
