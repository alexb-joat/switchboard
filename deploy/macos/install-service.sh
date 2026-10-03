#!/usr/bin/env sh
# Run Switchboard at login as a launchd agent (no root needed).
# Usage: ./deploy/macos/install-service.sh        Remove: ./deploy/macos/install-service.sh --remove
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="io.github.switchboard.router"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"; echo "removed"; exit 0
fi

[ -x "$REPO/.venv/bin/python" ] || { echo "create the venv first (see README)"; exit 1; }
[ -f "$REPO/config.yaml" ] || { echo "create config.yaml first (see README)"; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"
umask 077
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REPO/.venv/bin/python</string>
    <string>-m</string><string>switchboard</string>
    <string>--config</string><string>config.yaml</string>
    <string>serve</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8787</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>Umask</key><integer>63</integer>
  <key>StandardOutPath</key><string>$REPO/logs/server.log</string>
  <key>StandardErrorPath</key><string>$REPO/logs/server.log</string>
</dict>
</plist>
PLIST
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "running. logs: $REPO/logs/server.log"
