#!/usr/bin/env python3
"""The Sunday-morning nudge: tell me what to change before kickoff.

Writes the week's brief to ``cache/brief.txt`` (and ``cache/brief.json``) and,
on a Mac, raises a notification if there is actually something to do. Silence
is the point: it says nothing when your lineup is already right and nobody has
offered you a trade.

Run it by hand:

    python3 weekly_nudge.py                # notify only if something changed
    python3 weekly_nudge.py --always       # notify regardless
    python3 weekly_nudge.py --print        # just print the brief

Run it every week by itself: ``./install_weekly_nudge.command``.

Exit status is 0 when there is something worth your attention and 1 when there
is not, so another program - your own assistant, say - can use it as a check
without parsing anything.
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import assistant_api
import config


def gather():
    """The week's news. Returns (payload, headline, worth_interrupting_for)."""
    lineup = assistant_api.get_lineup(fresh=True)
    try:
        offers = assistant_api.get_offers()["offers"]
    except assistant_api.ApiError:
        offers = []

    changes = lineup["changes"]
    cannot = lineup["cannot_play"]

    parts = []
    if changes:
        parts.append("%d lineup change%s worth %s points"
                     % (len(changes), "" if len(changes) == 1 else "s",
                        lineup["points_gained_by_changes"]))
    if offers:
        parts.append("%d trade offer%s waiting"
                     % (len(offers), "" if len(offers) == 1 else "s"))
    if cannot and not changes:
        # Worth saying even with nothing to swap: it means a hole in the lineup.
        parts.append("%d player%s cannot play"
                     % (len(cannot), "" if len(cannot) == 1 else "s"))

    headline = ("Week %s: %s" % (lineup["week"], ", ".join(parts))
                if parts else "Week %s: nothing to change." % lineup["week"])

    payload = {
        "week": lineup["week"],
        "headline": headline,
        "needs_attention": bool(parts),
        "lineup": lineup,
        "offers": offers,
    }
    return payload, headline, bool(parts)


def notify(title, message):
    """A desktop notification on a Mac; a no-op anywhere else."""
    if sys.platform != "darwin":
        return False
    script = ('display notification %s with title %s sound name "Ping"'
              % (json.dumps(message), json.dumps(title)))
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=20)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def main():
    parser = argparse.ArgumentParser(description="Weekly fantasy nudge")
    parser.add_argument("--always", action="store_true",
                        help="Notify even when there is nothing to change.")
    parser.add_argument("--print", dest="show", action="store_true",
                        help="Print the brief instead of notifying.")
    parser.add_argument("--quiet", action="store_true",
                        help="Write the files, raise no notification.")
    args = parser.parse_args()

    try:
        payload, headline, attention = gather()
    except assistant_api.ApiError as exc:
        print("Cannot build this week's brief: %s" % exc, file=sys.stderr)
        return 2

    text = assistant_api.get_brief()
    os.makedirs(config.CACHE_DIR, exist_ok=True)
    with open(os.path.join(config.CACHE_DIR, "brief.txt"), "w",
              encoding="utf-8") as handle:
        handle.write(text)
    with open(os.path.join(config.CACHE_DIR, "brief.json"), "w",
              encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)

    if args.show:
        print(text)
    elif not args.quiet and (attention or args.always):
        detail = []
        for change in payload["lineup"]["changes"][:3]:
            detail.append("%s %s" % (change["action"].title(), change["name"]))
        for offer in payload["offers"][:2]:
            detail.append("Trade: %s" % offer["verdict"])
        notify(headline, " · ".join(detail) or "Open localhost:8000")
        print(headline)
    else:
        print(headline)

    return 0 if attention else 1


if __name__ == "__main__":
    sys.exit(main())
