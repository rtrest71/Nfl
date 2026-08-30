#!/bin/bash
#
# Stops the assistant starting automatically. Double-click to undo
# install_autostart.command. The app itself is untouched - you can still run
# ./start.command by hand whenever you want.

LABEL="com.rtrestini.draftassistant"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo
if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    echo "  Automatic start removed. The assistant is stopped."
    echo "  To run it by hand:  double-click start.command"
else
    echo "  Automatic start was not set up. Nothing to remove."
fi
echo
echo "  Press any key to close."
read -r -n 1
