#!/usr/bin/env bash
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
APP_DIR="$HOME/Library/Application Support/NetTally"
LOG_DIR="$HOME/Library/Logs/NetTally"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_NAME="com.nettally.daemon.plist"
PLIST_DEST="$LAUNCH_AGENTS_DIR/$PLIST_NAME"
PLIST_LABEL="com.nettally.daemon"

echo "== Installing NetTally =="

# 1. Create target directories
echo "Creating application directories..."
mkdir -p "$APP_DIR"
mkdir -p "$LOG_DIR"
mkdir -p "$LAUNCH_AGENTS_DIR"

# 2. Copy application files
echo "Copying scripts to $APP_DIR..."
cp "$SCRIPT_DIR/collector.py" "$APP_DIR/"
cp "$SCRIPT_DIR/db.py" "$APP_DIR/"
cp "$SCRIPT_DIR/config.py" "$APP_DIR/"
cp "$SCRIPT_DIR/app_folder.py" "$APP_DIR/"
cp "$SCRIPT_DIR/report.py" "$APP_DIR/"
cp "$SCRIPT_DIR/html_generator.py" "$APP_DIR/"
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

# 3. Generate LaunchAgent plist with user home directory and dynamic interval.
#    Delegate the comment-stripping + parsing to config.py (the single source
#    of truth for config.json semantics) instead of duplicating the regex.
echo "Generating LaunchAgent plist..."
POLLING_INTERVAL=$(python3 "$SCRIPT_DIR/config.py" --get polling_interval_seconds 2>/dev/null || echo 30)

cat <<EOF > "$PLIST_DEST"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$APP_DIR/collector.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>$POLLING_INTERVAL</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/collector.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/collector.err.log</string>
</dict>
</plist>
EOF

# 4. Load LaunchAgent into launchctl (unload first if already registered)
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

# 5. Create user bin symlink if standard path exists
if [ -d "$HOME/.local/bin" ]; then
    ln -sf "$SCRIPT_DIR/nettally" "$HOME/.local/bin/nettally"
    echo "Symlinked CLI command to $HOME/.local/bin/nettally"
elif [ -d "$HOME/bin" ]; then
    ln -sf "$SCRIPT_DIR/nettally" "$HOME/bin/nettally"
    echo "Symlinked CLI command to $HOME/bin/nettally"
fi

echo ""
echo "SUCCESS: NetTally installed and running!"
echo "Status check: launchctl list | grep nettally"
echo "View report:  $SCRIPT_DIR/nettally report"
echo "View HTML:    $SCRIPT_DIR/nettally html"
