# 0005 — Application Support copy: what's actually load-bearing

Date: 2026-08-22 (observation, no code change made)

## Context

`install.sh` copies six files into `~/Library/Application Support/NetTally/`: `collector.py`,
`db.py`, `config.py`, `app_folder.py`, `report.py`, `html_generator.py`. It was unclear whether
all six are actually needed there, versus just carried along by habit.

## Decision (documented as-is, not yet changed)

Only `collector.py` and its three sibling imports (`db.py`, `config.py`, `app_folder.py`) are
load-bearing in that directory: the LaunchAgent plist points `ProgramArguments` at
`$APP_DIR/collector.py`, and Python resolves `collector.py`'s `from db import ...` etc. against
its own running directory, so those three must sit alongside it.

`report.py` and `html_generator.py` are copied too, but nothing currently executes them from
that location — the `nettally` CLI wrapper always resolves paths to wherever the `nettally`
script itself lives (the workspace), never to Application Support. Their copy there is inert:
it can go stale without anything noticing or breaking, because nothing reads it.

Left as-is for now rather than removed, since it may be intentional insurance (a fully
self-contained Application Support install, independent of the workspace folder living inside a
synced cloud-storage location) even though nothing currently exercises it that way.

## Consequences

- If you ever add a code path that runs `report.py`/`html_generator.py` from
  `$APP_DIR` directly (bypassing the `nettally` wrapper), remember its copy there can be stale —
  same "did you rerun `install.sh`" trap as `collector.py`, just currently dormant.
- If/when this gets cleaned up, the two options are: stop copying `report.py` /
  `html_generator.py` in `install.sh` (since nothing reads the copy), or start actually using
  the Application Support copy as a true standalone install and document that intent. Either is
  fine; leaving it ambiguous is what caused the "why do we copy these?" question that prompted
  this note.
