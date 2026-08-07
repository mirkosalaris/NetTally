#!/usr/bin/env bash
set -e

APP_DIR="$HOME/Library/Application Support/NetTally"
LOG_DIR="$HOME/Library/Logs/NetTally"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_NAME="com.nettally.daemon.plist"
PLIST_DEST="$LAUNCH_AGENTS_DIR/$PLIST_NAME"
PLIST_LABEL="com.nettally.daemon"

echo "== Uninstalling NetTally =="

# 1. Unload LaunchAgent
USER_ID=$(id -u)
echo "Stopping LaunchAgent service..."
launchctl bootout "gui/$USER_ID/$PLIST_LABEL" 2>/dev/null || launchctl unload "$PLIST_DEST" 2>/dev/null || true

# 2. Remove plist
if [ -f "$PLIST_DEST" ]; then
    echo "Removing LaunchAgent plist..."
    rm -f "$PLIST_DEST"
fi

# 3. Remove symlink
rm -f "$HOME/.local/bin/nettally" "$HOME/bin/nettally" 2>/dev/null || true

# 4. Cleanup application files & logs (preserve database by default unless --purge is passed)
if [ "$1" == "--purge" ]; then
    echo "Purging all data, database, and logs..."
    rm -rf "$APP_DIR"
    rm -rf "$LOG_DIR"
else
    echo "Removing application code and logs (preserving database at $APP_DIR/usage.db)..."
    rm -f "$APP_DIR/collector.py" "$APP_DIR/db.py" "$APP_DIR/app_folder.py" "$APP_DIR/report.py" "$APP_DIR/html_generator.py"
    rm -rf "$LOG_DIR"
    echo "(Tip: Pass --purge to uninstall.sh if you also wish to delete usage.db)"
fi

echo ""
echo "SUCCESS: NetTally uninstalled."
