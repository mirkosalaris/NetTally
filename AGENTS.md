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

## Deployed copy: Application Support is the standalone runtime, not a shadow copy

- The always-on background collector runs from `~/Library/Application Support/NetTally/`
  (the LaunchAgent plist's `ProgramArguments` points at `collector.py` there).
- Once installed, the `nettally` CLI on your PATH also runs from there: `install.sh` copies
  `collector.py`, `db.py`, `config.py`, `app_folder.py`, `report.py`, `html_generator.py`,
  `pause.py`, `templates/` (`dashboard_template.html`, the vendored `chart.umd.js`, and its
  license), the `nettally` wrapper, `install.sh`/`uninstall.sh`, and the plist template
  into Application Support, and symlinks `nettally` onto your PATH (into `~/.local/bin`
  — created and added to `$PATH` in your shell profile if not already there). Deleting the
  source folder after install is fine. (See `docs/decisions/0006-...`; it resolves the open
  question in `0005`.)
- `nettally install` on PATH resolves to the **deployed** copy of `install.sh`, where
  `SCRIPT_DIR` is already `APP_DIR`, so its first `cp` copies each file onto itself and
  `set -e` aborts the run. It is not an upgrade or repair path — only the source tree's
  `./install.sh` is. Don't document it as one.
- `./nettally ...` from **this workspace** is the development path: the wrapper resolves paths
  relative to its own (symlink-resolved) location, so it keeps running the workspace copies of
  `report.py` / `html_generator.py` / `collector.py` directly.
- **Consequence: after editing any of `collector.py`, `db.py`, `config.py`, `app_folder.py`,
  `report.py`, `html_generator.py`, `pause.py`, `templates/dashboard_template.html`, or
  `templates/chart.umd.js`, you must re-run `./install.sh` for the deployed daemon **and**
  the PATH CLI to pick up the change.** Restarting the LaunchAgent without re-running
  `install.sh` just relaunches the old copy. The workspace `./nettally` sees edits
  immediately. Only the source tree can do this: the Application Support copy is the
  runtime, so upgrading it means a fresh source tree and a re-run of its `./install.sh`.
  (`templates/chart.umd.js` counts because `html_generator.py` inlines it into every
  generated report; leaving it out of the install is not an option.)
- The database (`usage.db`, default path `~/Library/Application Support/NetTally/usage.db`,
  overridable via `--db`) is a single shared file regardless of which copy of the code touched
  it — there's no duplicate-database confusion, only duplicate-*code* confusion.
- `pause_state.json` sits next to `config.json` in the same directory, and is created *and
  removed* by the user-facing `nettally pause` / `nettally resume`. It's a control-plane
  file, deliberately **not** a row in `usage.db`: `nettally pause` then works even when the
  database is locked or corrupt, and `uninstall.sh`'s explicit `rm -f` list removes it, so a
  `usage.db` preserved by a plain uninstall can't resurrect a stale "paused" flag on the next
  install. 

## Python runtime contract

- NetTally runs on **Python >= 3.9, < 4.0** — declared as `requires-python` in
  `pyproject.toml`, with ruff's `target-version = "py39"` enforcing the same syntax/API floor.
- `install.sh` resolves the interpreter **once, at install time**: first `python3` on PATH,
  version-checked, then recorded in `$APP_DIR/python_path` and baked into the generated
  LaunchAgent plist. The CLI wrapper reads the same `python_path`, so daemon and CLI share one
  interpreter once installed; before installation it falls back to `env python3`.
- The `/usr/bin/python3` system stub is only run after `xcode-select -p` confirms the Command
  Line Tools exist, so install never pops the GUI "Developer Tools not found" dialog. If no
  usable Python is found, `install.sh` fails fast with a friendly message.
- After a macOS or Homebrew Python change, re-run `./install.sh` to re-pin.

## Logs

- The collector owns its log: `setup_logging()` in `collector.py` attaches a
  `RotatingFileHandler` for `~/Library/Logs/NetTally/collector.log` (5 MB, 3 backups). The
  bounds live in `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT` in `collector.py` — change them there,
  not in the plist.
- The plist intentionally keeps `StandardErrorPath` (`collector.err.log`) and has no
  `StandardOutPath`. stderr is the only place a failure *before* logging is configured can
  surface (import error, bad interpreter, unwritable log dir), and it's normally empty. The
  daemon writes nothing to stdout; the only `print` in `collector.py` is behind
  `if args.once:`, which runs attached to a terminal.
- `nettally status` tails `collector.log` and only shows `collector.err.log` when it is
  non-empty.

## Architecture at a glance

```
nettop (30s poll) → collector.py → usage_5m (SQLite, 5-min buckets, per app)
                                  ↳ power_events / gaps (pmset -g log derived, for sleep/wake classification)
report.py / html_generator.py → read usage_5m via db.py's query_usage_by_{5m,hour,day} → CLI / HTML dashboard
pause.py → pause_state.json → collector.py loop skips polling while paused (hole, not a label)
```

- `gaps` and `power_events` are **write-only** — the collector inserts into them, and nothing
  ever reads them back (the classification result lands in `usage_5m.gap_classification`
  instead). They're an audit trail of the `pmset -g log` parsing, kept for when you need to
  explain *why* a bucket was classified as it was. Don't treat them as a report input, and
  don't assume a change to them affects any query.

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

## Pausing (`pause.py`)

`nettally pause` / `nettally resume` (and `pause.py state`, which `nettally status` calls)
manipulate one small JSON file, `pause_state.json`. The collector's loop reads it once per
cycle at `collector.py`'s `read_pause_state()` call, which is also what makes a timed pause
need no timer process. Three invariants carry the whole design; don't break them without
reworking the tests that encode them (`TestCollectorHonorsPause`):

- **A pause is a hole, not a label.** Nothing is recorded while paused, and nothing about a
  pause reaches the database — in particular it never becomes a `gap_classification` value or
  a `gaps` row. A pause displays exactly as a long sleep gap does. 
- **`process_state` is cleared on the way *in*, not on the way out.** Byte counters are
  cumulative, so resuming against baselines from before the pause would turn the whole window
  into a single 5-minute bucket that reads as awake traffic — the exact bug this feature exists
  to prevent. Clearing at entry also makes a `launchd` restart *during* a pause safe, and makes
  the resume path an unremarkable ordinary poll. Don't "optimize" this into a re-baseline at
  exit.
- **The paused branch still advances `last_poll_epoch`.** A deliberate pause is not a sleep, so
  the resume poll must not look late; otherwise it triggers `pmset` gap detection over the pause
  window and stamps the first post-pause bucket `unknown_gap`, which folds into `awake`.

There is deliberately **no** `gaps` audit row for a pause window. Writing one needs the
collector to observe both ends of the pause, which silently drops the row if launchd restarts
the daemon in between — and incomplete audit data is worse than none. `collector.log` and
`nettally status` cover it instead.

## Before you consider a change done

1. Run the tests: `python3 -m unittest discover -s tests` (repo root), or `make test`. All of
   them, not just the ones near your change — several past bugs here were dashboard JS bugs with
   zero test coverage until they were found manually; don't assume "tests pass" means "no
   regression" if you touched `html_generator.py`'s embedded JS.
2. Run `make lint` (ruff) and `make typecheck` (mypy; config in `pyproject.toml`), and run
   `make format` before you commit so the diff stays ruff-format-clean. If your change
   produces a lint/type finding you're confident is a false positive, say so explicitly instead
   of silently growing `ignore` lists or adding `# type: ignore` (both are kept minimal
   deliberately).
3. If you touched any of the deployed files (`collector.py` / `db.py` / `config.py` /
   `app_folder.py` / `report.py` / `html_generator.py` / `pause.py` / `templates/`): tell the
   user (or run, if you can) `./install.sh` — see the deployed-copy section above.
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
