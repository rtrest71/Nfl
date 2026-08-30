#!/bin/bash
#
# Double-click this ONCE. After that the assistant starts by itself every time
# you log in, and stays running. You will never need the terminal again -
# just open http://localhost:8000 in a browser.
#
# To undo it, double-click uninstall_autostart.command.

cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"
LABEL="com.rtrestini.draftassistant"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo
echo "  Setting up the Fantasy assistant to start automatically."
echo "  Folder: $HERE"
echo

if [ "$(uname)" != "Darwin" ]; then
    echo "  This installer is for macOS. On another system, run ./start.command"
    echo "  yourself, or add it to your startup programs."
    echo "  Press any key to close."
    read -r -n 1
    exit 1
fi

PYTHON="$(command -v python3 || command -v python)"
if [ -z "$PYTHON" ]; then
    echo "  Python is not installed. Install it from python.org and try again."
    echo "  Press any key to close."
    read -r -n 1
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$HERE/logs"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$HERE/app.py</string>
    <string>--no-browser</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$HERE</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$HERE/logs/assistant.log</string>
  <key>StandardErrorPath</key>
  <string>$HERE/logs/assistant.log</string>
</dict>
</plist>
PLISTEOF

# Reload cleanly whether or not it was already installed.
launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST" 2>/dev/null

sleep 3
if curl -s --max-time 5 "http://localhost:8000/api/health" >/dev/null 2>&1; then
    echo "  DONE. The assistant is running now and will start on every login."
    echo
    echo "     Open this, and bookmark it:   http://localhost:8000"
    echo
    echo "  It keeps itself running - macOS restarts it if it ever stops."
    echo "  Log file: $HERE/logs/assistant.log"
    open "http://localhost:8000" 2>/dev/null
else
    echo "  Installed, but it is not answering on port 8000 yet."
    echo "  Give it a few seconds and open http://localhost:8000"
    echo "  If it never comes up, check $HERE/logs/assistant.log"
fi

echo
echo "  Press any key to close this window."
read -r -n 1
