# AGENTS.md — NetTally project context

Read this before making changes. It exists so an AI coding tool (or a future human, including
past-you) doesn't have to re-derive the design from scratch or re-litigate settled decisions.
For the "why" behind a specific past decision, check `docs/decisions/` before proposing a
different approach — if you think a past decision was wrong, say so explicitly and explain why,
rather than silently reversing it.

## What this is

NetTally is a solo/hobby, local-only macOS tool that polls `nettop` every 30s, buckets
per-app byte deltas into 5-minute SQLite rows, and reports/visualizes usage by 5-min/hour/day.
It also classifies gaps caused by sleep/dark-wake so "a huge byte spike right after wake" isn't
misread as one instant of real traffic. The data pipeline is entirely on-device: usage data
lives in a local SQLite database, is never uploaded, and there is no telemetry. The source code
itself is version-controlled and hosted on GitHub (origin), and commits are pushed there.

## Critical gotcha: there are TWO live copies of the Python files

- `./nettally report` / `./nettally html` / `./nettally run-once` run the `.py` files sitting
  in **this workspace** (the `nettally` wrapper resolves paths relative to its own location).
- The always-on background collector does **not** run from here. `install.sh` copies
  `collector.py`, `db.py`, `config.py`, `app_folder.py` (and, currently, `report.py` /
  `html_generator.py`, though nothing actually executes those two from there — see
  `docs/decisions/0005-app-support-copy-scope.md`) into
  `~/Library/Application Support/NetTally/`, and the LaunchAgent plist
  (`com.nettally.daemon.plist`) hardcodes that path as `collector.py`'s location.
- **Consequence: after editing `collector.py`, `db.py`, `config.py`, or `app_folder.py`, you
  must re-run `./install.sh` for the running daemon to pick up the change.** Restarting the
  LaunchAgent without re-running `install.sh` just relaunches the old copy. Editing
  `report.py` / `html_generator.py` needs no reinstall — the CLI always reads the workspace
  copy directly.
- The database (`usage.db`, default path `~/Library/Application Support/NetTally/usage.db`,
  overridable via `--db`) is a single shared file regardless of which copy of the code touched
  it — there's no duplicate-database confusion, only duplicate-*code* confusion.

## Architecture at a glance

```
nettop (30s poll) → collector.py → usage_5m (SQLite, 5-min buckets, per app)
                                  ↳ power_events / gaps (pmset -g log derived, for sleep/wake classification)
report.py / html_generator.py → read usage_5m via db.py's query_usage_by_{5m,hour,day} → CLI / HTML dashboard
```

- `usage_5m` PK is `(timestamp_5m, app_name)`. A bucket can receive several polls before it
  rolls over; `record_usage_deltas()` does `gap_classification = excluded.gap_classification`
  on conflict, i.e. **whichever poll last touches a given (bucket, app) pair wins the
  classification for that row.** This last-write-wins behavior was the root mechanism behind
  the "isolated buckets wrongly tagged awake" bug (see decision 0004) — keep it in mind before
  changing how gap_classification is written or aggregated.
- `gap_classification` is a closed set of values: `NULL` (awake), `'dark_wake_only'`,
  `'sleep_then_full_wake'`, `'unknown_gap'`. `'unknown_gap'` is deliberately folded into the
  `'awake'` bucket everywhere it's filtered (db.py and the dashboard JS) — don't reintroduce a
  4th visible category without updating both places.
- `query_usage_by_hour(db_path, ...)` and `query_usage_by_day(db_path, ...)` signatures and
  return shape are a deliberate, explicit constraint from `report.py`'s existing callers — the
  classification-aware rollups (decision 0003) were built as an *additional* layered query path
  specifically so these two functions didn't need to change. Don't change their signature or
  default (unfiltered) behavior without checking every caller.

## Before you consider a change done

1. Run the tests: `python3 -m unittest discover -s tests` (repo root), or `make test`. All of
   them, not just the ones near your change — several past bugs here were dashboard JS bugs with
   zero test coverage until they were found manually; don't assume "tests pass" means "no
   regression" if you touched `html_generator.py`'s embedded JS.
2. Run `make lint` (ruff) and `make typecheck` (mypy; config in `pyproject.toml`), and run
   `make format` before you commit so the diff stays annular-format-clean. If your change
   produces a lint/type finding you're confident is a false positive, say so explicitly instead
   of silently growing `ignore` lists or adding `# type: ignore` (both are kept minimal
   deliberately).
3. If you touched `collector.py` / `db.py` / `config.py` / `app_folder.py`: tell the user (or
   run, if you can) `./install.sh` again — see the gotcha above.
4. If you touched anything sleep/wake/gap-classification related, verify against real data if
   at all possible (a crafted repro or a real `usage.db` snapshot), not just unit tests with
   mocked `pmset` output. This codebase's nastiest bugs were all "looks right in isolation, but
   the real device log timing broke the assumption."

## Decision log

`docs/decisions/` has one short file per non-obvious design call, in `Context / Decision /
Consequences` form. Skim the filenames before proposing a redesign of gap classification, the
rollup/filter architecture, or the install/copy split — there's a good chance the alternative
you're about to suggest was already considered and rejected, with the reason written down.

### Writing a new decision record

Add one when you made a call with a real alternative — something a later session might
plausibly re-propose differently — not for routine bug fixes, which commit messages and tests
already cover. Rule of thumb: if justifying "why not the more obvious approach" would take more
than a sentence, it earns a record.

- File: `docs/decisions/NNNN-kebab-case-title.md`, next sequential number. No separate template
  file — the first 5 existing entries are the style reference (e.g. 0001-0005).
- Write it in the same change as the decision, not as a follow-up. Deferred documentation
  doesn't happen.
- Date it; cite the commit hash only if you already know it (e.g. writing the record right
  after committing the code) — don't hold up a commit just to learn its own hash.
- If a past decision gets reversed, don't edit the old record's Decision section. Add a new one
  and note in both files which supersedes which — the abandoned approach and why it didn't work
  is often the most useful part of the trail.
