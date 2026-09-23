# 0006 — Application Support is a true standalone install; Python is resolved once at install time

Date: 2026-09-23

## Context

0005 left an open question about the six files copied into `~/Library/Application Support/NetTally/`:
only `collector.py` + its three sibling imports were load-bearing there; `report.py` /
`html_generator.py` were carried along inert. Separately, the Python runtime was resolved two
inconsistent, undeclared ways — `/usr/bin/python3` hardcoded in the LaunchAgent plist, `env
python3` in the CLI wrapper — and never version-checked. `install.sh` even resolved `python3`
via PATH for one thing (config query) and hardcoded `/usr/bin/python3` in the generated plist.
Apple's /usr/bin/python3 only exists with the Xcode Command Line Tools installed, so a plain
user hitting this would get a GUI "Developer Tools not found" dialog the first time the daemon
ran. Distribution to other people (unzip a source release → `./install.sh`) needs the installed
copy to survive deleting the source folder and a single, pinned interpreter.

## Decision

The Application Support copy becomes the standalone runtime, and Python is pinned at install
time:

- `install.sh` copies everything launchable into Application Support: collector/db/config/
  app_folder/report/html_generator, `templates/` (the dashboard template is resolved relative
  to html_generator), the `nettally` wrapper, `install.sh`/`uninstall.sh`, and the plist
  template. `install.sh` symlinks `nettally` onto PATH via `~/.local/bin` (created, and added
  to the shell profile's `$PATH` if needed, when it or `~/bin` is not already on PATH); the
  wrapper resolves symlinks first, so `report`/`html`/`run-once` run the deployed copies. Deleting the source folder
  afterwards is fine, and `nettally install` from the deployed copy acts as a repair/
  reconfigure command.
- Python is resolved once, at install time. `install.sh` takes the first `python3` on PATH
  (guarding `/usr/bin/python3` with `xcode-select -p` before running it, so the CLT dialog
  never appears), requires `3.9 <= version < 4.0`, fails fast with a friendly message, writes
  `$APP_DIR/python_path`, and substitutes the path into the LaunchAgent plist. The tracked
  `com.nettally.daemon.plist` is now the authoritative template (`__HOME__`, `__PYTHON__`,
  `__THROTTLE_INTERVAL__`) consumed by install.sh — replacing the inline heredoc.
- The CLI wrapper reads the same `python_path`, so daemon and CLI share one interpreter once
  installed; pre-install it falls back to `env python3`.

## Consequences

- Editing any deployed file — including `report.py` / `html_generator.py` / `templates/` — now
  requires re-running `install.sh` for the deployed CLI and daemon to pick it up. The
  "did you re-install?" trap from 0005 is uniform across all files instead of half-and-half.
- The workspace stays the source of truth for development: `./nettally` there reads the
  workspace copies directly, so edit → run is unchanged for the developer.
- The checked-in plist must stay a template; install.sh is the only writer of the
  `~/Library/LaunchAgents` copy.
- Resolves 0005's open question: the Application Support copy is now genuinely self-contained
  (option B in 0005), rather than carrying dead code.