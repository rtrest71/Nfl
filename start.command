#!/bin/bash
#
# Draft-day launcher. Double-click this file in Finder, or run ./start.command
#
# It does three things a bare "python3 app.py" does not:
#
#   1. Restarts the app automatically if it ever stops, so a stray Ctrl-C or a
#      crash mid-draft costs you a couple of seconds instead of your pick.
#   2. Keeps the Mac awake (caffeinate) so the machine does not sleep between
#      picks and drop the live feed.
#   3. Opens the browser once, on the first launch only, rather than piling up
#      a new tab on every restart.
#
# To stop it for real: press Ctrl-C, or just close this window.

cd "$(dirname "$0")" || exit 1

trap 'echo; echo "  Stopped. Close this window or run ./start.command to restart."; exit 0' INT TERM

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "  Python is not installed. Install it from python.org, then try again."
    echo "  Press any key to close."
    read -r -n 1
    exit 1
fi

# caffeinate exists on macOS only; fall back to running the app directly.
KEEPAWAKE=""
if command -v caffeinate >/dev/null 2>&1; then
    KEEPAWAKE="caffeinate -i"
    echo "  Keeping this Mac awake for the draft."
fi

FIRST_RUN=1
while true; do
    if [ "$FIRST_RUN" = "1" ]; then
        $KEEPAWAKE "$PYTHON" app.py "$@"
        FIRST_RUN=0
    else
        # Do not reopen the browser on a restart - the tab is already there.
        $KEEPAWAKE "$PYTHON" app.py --no-browser "$@"
    fi

    echo
    echo "  ------------------------------------------------------------"
    echo "  The app stopped. Restarting in 3 seconds."
    echo "  Press Ctrl-C now if you meant to quit."
    echo "  ------------------------------------------------------------"
    sleep 3
done
