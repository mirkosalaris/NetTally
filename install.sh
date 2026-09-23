#!/usr/bin/env bash
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
APP_DIR="$HOME/Library/Application Support/NetTally"
LOG_DIR="$HOME/Library/Logs/NetTally"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_TEMPLATE="$SCRIPT_DIR/com.nettally.daemon.plist"
PLIST_NAME="com.nettally.daemon.plist"
PLIST_DEST="$LAUNCH_AGENTS_DIR/$PLIST_NAME"
PLIST_LABEL="com.nettally.daemon"

echo "== Installing NetTally =="

# --- Resolve the Python runtime ----------------------------------------------
# NetTally requires Python >= 3.9, < 4.0 (see `requires-python` in pyproject.toml).
# The interpreter is resolved ONCE, here, and recorded in $APP_DIR/python_path,
# baked into the LaunchAgent plist, and picked up by the CLI wrapper — so the
# daemon and the CLI always agree on which Python they run.
#
# The /usr/bin/python3 system stub is guarded with `xcode-select -p` BEFORE it
# is executed: running the stub without the Xcode Command Line Tools pops a GUI
# "Developer Tools not found" dialog, which an install must never do.
PYTHON="$(command -v python3 2>/dev/null || true)"

python_version_ok() {
    [ -n "$PYTHON" ] || return 1
    "$PYTHON" -c 'import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] < (4,) else 1)' 2>/dev/null
}

if [ -n "$PYTHON" ] && [ "$PYTHON" != "/usr/bin/python3" ]; then
    # A non-system python3 on PATH (Homebrew, python.org, pyenv, ...): fine, if new enough.
    if ! python_version_ok; then
        echo "ERROR: $PYTHON is too old for NetTally (need Python >= 3.9, < 4)." >&2
        exit 1
    fi
elif [ -n "$PYTHON" ] && xcode-select -p >/dev/null 2>&1; then
    # System /usr/bin/python3 with the Command Line Tools present: safe to run.
    if ! python_version_ok; then
        echo "ERROR: the system Python ($PYTHON) is too old for NetTally (need >= 3.9)." >&2
        exit 1
    fi
else
    echo "ERROR: no usable Python found." >&2
    echo "" >&2
    echo "NetTally requires Python >= 3.9, < 4.0. Install the Xcode Command Line Tools" >&2
    echo "(xcode-select --install) or a current Python (brew install python), then" >&2
    echo "re-run ./install.sh." >&2
    exit 1
fi

echo "Using Python: $PYTHON ($("$PYTHON" --version 2>&1))"

# 1. Create target directories
echo "Creating application directories..."
mkdir -p "$APP_DIR"
mkdir -p "$LOG_DIR"
mkdir -p "$LAUNCH_AGENTS_DIR"

# 2. Copy application files. Application Support now holds a fully standalone
#    runtime: the daemon runs collector.py from here, the `nettally` CLI on PATH
#    runs from here, and report/html read templates from here. See
#    docs/decisions/0006-standalone-install-python-resolution.md.
echo "Copying scripts to $APP_DIR..."
cp "$SCRIPT_DIR/collector.py" "$APP_DIR/"
cp "$SCRIPT_DIR/db.py" "$APP_DIR/"
cp "$SCRIPT_DIR/config.py" "$APP_DIR/"
cp "$SCRIPT_DIR/app_folder.py" "$APP_DIR/"
cp "$SCRIPT_DIR/report.py" "$APP_DIR/"
cp "$SCRIPT_DIR/html_generator.py" "$APP_DIR/"
cp "$SCRIPT_DIR/nettally" "$APP_DIR/"
cp "$SCRIPT_DIR/install.sh" "$APP_DIR/"
cp "$SCRIPT_DIR/uninstall.sh" "$APP_DIR/"
cp "$SCRIPT_DIR/com.nettally.daemon.plist" "$APP_DIR/"
mkdir -p "$APP_DIR/templates"
cp "$SCRIPT_DIR/templates/dashboard_template.html" "$APP_DIR/templates/"
chmod +x "$APP_DIR/nettally" "$APP_DIR/install.sh" "$APP_DIR/uninstall.sh"
if [ ! -f "$APP_DIR/app_map.json" ]; then
    cp "$SCRIPT_DIR/app_map.json" "$APP_DIR/"
else
    echo "Existing app_map.json found in Application Support; keeping user customizations."
fi
if [ ! -f "$APP_DIR/config.json" ]; then
    cp "$SCRIPT_DIR/config.json" "$APP_DIR/"
else
    echo "Existing config.json found in Application Support; keeping user customizations."
fi

# 3. Record the resolved interpreter for the CLI wrapper and the plist.
printf '%s\n' "$PYTHON" > "$APP_DIR/python_path"

# 4. Read the polling interval from config.py — the single source of truth for
#    config.json semantics (comment-stripping + defaults live there, not here).
POLLING_INTERVAL=$("$PYTHON" "$SCRIPT_DIR/config.py" --get polling_interval_seconds 2>/dev/null || echo 30)

# 5. Generate the LaunchAgent plist from the checked-in template. The template
#    keeps __HOME__ / __PYTHON__ / __THROTTLE_INTERVAL__ placeholders; this is
#    the only place that rewrites them into real, install-specific values.
echo "Generating LaunchAgent plist from template..."
escape_sed_repl() { printf '%s' "$1" | sed -e 's/[&\\]/\\&/g'; }
sed -e "s|__PYTHON__|$(escape_sed_repl "$PYTHON")|g" \
    -e "s|__HOME__|$(escape_sed_repl "$HOME")|g" \
    -e "s|__THROTTLE_INTERVAL__|$(escape_sed_repl "$POLLING_INTERVAL")|g" \
    "$PLIST_TEMPLATE" > "$PLIST_DEST"

# 6. Load LaunchAgent into launchctl (unload first if already registered)
echo "Registering and starting LaunchAgent..."
USER_ID=$(id -u)

# Try to unload/bootout any existing instance (ignore errors — may not be loaded yet)
launchctl bootout "gui/$USER_ID/$PLIST_LABEL" 2>/dev/null || true

# Small delay to let launchd settle after bootout
sleep 0.5

# Bootstrap the service; fall back to legacy load on older macOS
if launchctl bootstrap "gui/$USER_ID" "$PLIST_DEST" 2>/dev/null; then
    echo "Service registered via launchctl bootstrap."
else
    echo "bootstrap unavailable; using launchctl load..."
    launchctl load -w "$PLIST_DEST" 2>/dev/null || true
fi

# 7. Create user bin symlink if a standard path exists.
#    Points at the deployed wrapper in Application Support, not at a source
#    folder that the user might delete later.
for BIN_DIR in "$HOME/.local/bin" "$HOME/bin"; do
    if [ -d "$BIN_DIR" ]; then
        ln -sf "$APP_DIR/nettally" "$BIN_DIR/nettally"
        echo "Symlinked CLI command to $BIN_DIR/nettally (standalone copy in Application Support)"
        break
    fi
done

echo ""
echo "SUCCESS: NetTally installed and running!"
echo "Status check:   launchctl list | grep nettally"
echo "Status summary: nettally status"
echo "View report:    nettally report"
echo "View HTML:      nettally html"
echo "Python pinned:  $PYTHON"