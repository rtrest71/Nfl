#!/bin/bash
#
# Double-click this ONCE. After that your Mac checks your team by itself and
# tells you when there is something to do - a lineup change, or a trade offer
# waiting. It stays quiet when there is nothing.
#
# When it runs:
#   Sunday   10:00 am  - before the early games, while you can still act
#   Sunday    6:00 pm  - before the late games
#   Tuesday   9:00 am  - after waivers, when trades tend to arrive
#   Thursday  4:00 pm  - before Thursday night football
#
# If the Mac is asleep at one of those times, macOS runs it as soon as it wakes.
#
# To undo it, double-click uninstall_weekly_nudge.command.

cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"
LABEL="com.rtrestini.fantasynudge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo
echo "  Setting up the weekly check on your fantasy team."
echo "  Folder: $HERE"
echo

if [ "$(uname)" != "Darwin" ]; then
    echo "  This installer is for macOS. On another system, run"
    echo "      python3 weekly_nudge.py"
    echo "  from cron or your system's scheduler instead."
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
    <string>$HERE/weekly_nudge.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$HERE</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>0</integer>
          <key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>0</integer>
          <key>Hour</key><integer>18</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>2</integer>
          <key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>4</integer>
          <key>Hour</key><integer>16</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>$HERE/logs/nudge.log</string>
  <key>StandardErrorPath</key>
  <string>$HERE/logs/nudge.log</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST" 2>/dev/null

echo "  Running it once now, so you can see what it will look like..."
echo
"$PYTHON" "$HERE/weekly_nudge.py" --always
STATUS=$?
echo

if [ $STATUS -le 1 ]; then
    echo "  DONE. It will check your team four times a week from now on,"
    echo "  and only interrupt you when there is something to change."
    echo
    echo "  The latest brief is always in: $HERE/cache/brief.txt"
    echo "  Log file: $HERE/logs/nudge.log"
else
    echo "  Installed, but it could not read your team just now."
    echo "  Make sure you are online and have run build_data.py at least once."
fi

echo
echo "  Press any key to close this window."
read -r -n 1
