#!/usr/bin/env python3
"""A Model Context Protocol server for your fantasy team.

This is how your own assistant gets to *ask* rather than be told. Point an
MCP-speaking client at this file and it gains five tools:

    fantasy_lineup      who to start this week, and what to change in Sleeper
    fantasy_roster      every player you own, scored
    fantasy_offers      trades other managers have sent you, already judged
    fantasy_check_trade score a trade you are thinking about
    fantasy_brief       all of the above as one block of plain text

Speaks JSON-RPC 2.0 over stdin/stdout, in the standard MCP framing: one JSON
message per line. Nothing outside the Python standard library, and no server
to keep running - the client starts it when it needs it.

To register it with a Claude-based client, add to its MCP config:

    {"mcpServers": {"fantasy": {"command": "python3",
                                "args": ["/full/path/to/mcp_server.py"]}}}

Read-only by design. It will never accept a trade, set a lineup or drop a
player on your behalf - it tells you what to do, and you do it in Sleeper.
"""

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import assistant_api  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "fantasy-assistant", "version": "1.0.0"}

WEEK_ARG = {
    "type": "integer",
    "description": "NFL week number. Omit for the current week, which is "
                   "almost always what you want.",
}
FRESH_ARG = {
    "type": "boolean",
    "description": "Skip the two-minute cache and re-read Sleeper. Use when "
                   "the user says something just changed.",
}

TOOLS = [
    {
        "name": "fantasy_lineup",
        "description":
            "The best legal starting lineup this roster can field this week, "
            "and the exact START/BENCH changes to make in the Sleeper app. "
            "Use this for any 'who do I start', 'is my lineup right', or "
            "'should I play X over Y' question. Points are computed from raw "
            "projected stats under this league's own scoring (full PPR, "
            "4-point passing touchdowns), not a generic PPR column. Players "
            "who cannot play are excluded outright rather than benched.",
        "inputSchema": {"type": "object",
                        "properties": {"week": WEEK_ARG, "fresh": FRESH_ARG}},
    },
    {
        "name": "fantasy_roster",
        "description":
            "Every player on the roster with his projected points, best "
            "first, plus injury status. Use this for 'who do I have', 'who "
            "should I drop', or when you need the whole picture before "
            "answering something else. Makes no lineup decisions.",
        "inputSchema": {"type": "object",
                        "properties": {"week": WEEK_ARG, "fresh": FRESH_ARG}},
    },
    {
        "name": "fantasy_offers",
        "description":
            "Trade offers other managers have actually sent, read straight "
            "out of Sleeper and already scored by what each would do to the "
            "starting lineup. Use this for 'has anyone offered me anything', "
            "'any trades waiting', or as a proactive check. Read-only: "
            "accepting or rejecting still happens in the Sleeper app.",
        "inputSchema": {"type": "object",
                        "properties": {"week": WEEK_ARG, "fresh": FRESH_ARG}},
    },
    {
        "name": "fantasy_check_trade",
        "description":
            "Score a hypothetical trade - one the user is considering, or one "
            "discussed outside Sleeper. Judged purely on the change to the "
            "projected starting lineup, so a trade that only improves the "
            "bench scores near zero, which is the honest answer. Also warns "
            "about roster limits and starting slots the trade would leave "
            "unfillable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "give": {"type": "array", "items": {"type": "string"},
                         "description": "Players the user would send, by name."},
                "get": {"type": "array", "items": {"type": "string"},
                        "description": "Players the user would receive, by name."},
                "week": WEEK_ARG, "fresh": FRESH_ARG,
            },
            "required": ["give", "get"],
        },
    },
    {
        "name": "fantasy_brief",
        "description":
            "Everything at once as plain text: the league rules that change "
            "the advice, the lineup to start, the changes to make, anyone who "
            "cannot play, and any waiting trade offers. The one call to make "
            "for a weekly check-in or an open-ended 'how's my team'.",
        "inputSchema": {"type": "object",
                        "properties": {"week": WEEK_ARG, "fresh": FRESH_ARG}},
    },
]


def call_tool(name, arguments):
    """Run one tool. Returns (text, is_error)."""
    week = arguments.get("week")
    week = int(week) if week else None
    fresh = bool(arguments.get("fresh"))

    try:
        if name == "fantasy_lineup":
            return _dump(assistant_api.get_lineup(week, fresh)), False
        if name == "fantasy_roster":
            return _dump(assistant_api.get_roster(week, fresh)), False
        if name == "fantasy_offers":
            return _dump(assistant_api.get_offers(week, fresh)), False
        if name == "fantasy_check_trade":
            return _dump(assistant_api.check_trade(
                arguments.get("give") or [], arguments.get("get") or [],
                week, fresh)), False
        if name == "fantasy_brief":
            return assistant_api.get_brief(week, fresh), False
    except assistant_api.ApiError as exc:
        # A real, actionable condition - say what it is rather than raising.
        return ("Cannot answer right now: %s" % exc), True
    except Exception as exc:  # noqa: BLE001
        return ("The fantasy tool failed: %s: %s"
                % (type(exc).__name__, exc)), True
    return ("No such tool: %s" % name), True


def _dump(payload):
    return json.dumps(payload, indent=2, default=str)


def handle(message):
    """Handle one JSON-RPC message. Returns a response dict, or None."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION,
                  "capabilities": {"tools": {}},
                  "serverInfo": SERVER_INFO}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        text, is_error = call_tool(params.get("name"),
                                   params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}],
                  "isError": is_error}
    elif method == "ping":
        result = {}
    elif method and method.startswith("notifications/"):
        return None                      # notifications are never answered
    else:
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601,
                          "message": "Method not found: %s" % method}}

    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        try:
            response = handle(message)
        except Exception:  # noqa: BLE001 - a crash here kills the client's tools
            traceback.print_exc(file=sys.stderr)
            response = {"jsonrpc": "2.0", "id": message.get("id"),
                        "error": {"code": -32603, "message": "Internal error"}}
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
