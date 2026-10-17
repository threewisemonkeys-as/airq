#!/bin/sh
# Install (or reinstall) the collector + dashboard server as launchd user agents.
# Uninstall: ./install.sh uninstall
set -e
umask 077
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3)"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA" "$DIR/data"
chmod 700 "$DIR/data"

for name in collect server; do
  launchctl bootout "gui/$(id -u)/com.airq.$name" 2>/dev/null || true
  rm -f "$LA/com.airq.$name.plist"
done
[ "$1" = "uninstall" ] && { echo "airq agents removed (data kept in $DIR/data)"; exit 0; }

cat > "$LA/com.airq.collect.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.airq.collect</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/collect.py</string></array>
  <key>StartInterval</key><integer>60</integer>
  <key>Umask</key><integer>63</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$DIR/data/collect.log</string>
  <key>StandardOutPath</key><string>$DIR/data/collect.log</string>
</dict></plist>
PLIST

cat > "$LA/com.airq.server.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.airq.server</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/server.py</string></array>
  <key>KeepAlive</key><true/>
  <key>Umask</key><integer>63</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>$DIR/data/server.log</string>
  <key>StandardOutPath</key><string>$DIR/data/server.log</string>
</dict></plist>
PLIST

for name in collect server; do
  chmod 600 "$LA/com.airq.$name.plist"
  launchctl bootstrap "gui/$(id -u)" "$LA/com.airq.$name.plist"
done
echo "airq installed: collecting every minute; dashboard at http://localhost:8765"
