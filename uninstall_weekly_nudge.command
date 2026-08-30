#!/bin/bash
#
# Stops the weekly check on your team. Double-click to undo
# install_weekly_nudge.command. Nothing else is touched - the assistant itself
# keeps running, and you can still run weekly_nudge.py by hand.

LABEL="com.rtrestini.fantasynudge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo
if [ -f "$PLIST" ]; then
    launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    echo "  Weekly check removed. Nothing will notify you from now on."
    echo "  To check by hand:  python3 weekly_nudge.py --print"
else
    echo "  The weekly check was not set up. Nothing to remove."
fi
echo
echo "  Press any key to close."
read -r -n 1
