# Connecting this to Hobbs

Three ways in, depending on which half of Hobbs is asking. Pick whichever
fits; they all read the same numbers, so they always agree.

| You want | Use |
|---|---|
| Hobbs to call it as a **tool** (Claude side) | `mcp_server.py` |
| Hobbs to fetch it over **HTTP** (Grok side, or anything else) | `/api/v1/*` |
| Hobbs to be **told**, without asking | `weekly_nudge.py` |

Everything is read-only. Nothing here can set a lineup, accept a trade or drop
a player — it tells you what to do, and you do it in the Sleeper app. That is
deliberate: an automated accept on a bad offer is not a bug you get to undo.

---

## 1. As tools — MCP

`mcp_server.py` is a Model Context Protocol server. Give an MCP-speaking
client this config and Hobbs gains five tools:

```json
{
  "mcpServers": {
    "fantasy": {
      "command": "python3",
      "args": ["/Users/YOU/Nfl/mcp_server.py"]
    }
  }
}
```

Use the real full path to the folder. Nothing needs to be running first — the
client starts the server when it needs it, and it reads Sleeper itself.

| Tool | Ask it when |
|---|---|
| `fantasy_lineup` | "who do I start", "is my lineup right", "should I play X over Y" |
| `fantasy_roster` | "who do I have", "who should I drop" |
| `fantasy_offers` | "has anyone offered me a trade" |
| `fantasy_check_trade` | "is this trade good" — takes `give` and `get`, by name |
| `fantasy_brief` | "how's my team" — everything at once, as text |

Every tool takes an optional `week` (defaults to the current one) and `fresh`
(skip the two-minute cache and re-read Sleeper).

Check it works before wiring it up:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 mcp_server.py
```

## 2. Over HTTP — `/api/v1`

While `app.py` is running (it starts at login if you installed
`install_autostart.command`):

```
GET  /api/v1/lineup     the best legal lineup, and what to change in Sleeper
GET  /api/v1/roster     every player you own, scored, best first
GET  /api/v1/offers     trades sent to you, already judged
GET  /api/v1/rules      the league rules that change the advice
GET  /api/v1/brief      all of it as plain text
POST /api/v1/trade      {"give": ["Name"], "get": ["Name"]}
```

Add `?week=7` for a different week, `?fresh=1` to bypass the cache. Examples:

```bash
curl localhost:8000/api/v1/brief
curl localhost:8000/api/v1/lineup | python3 -m json.tool
curl -X POST localhost:8000/api/v1/trade \
     -H 'Content-Type: application/json' \
     -d '{"give": ["Brock Bowers"], "get": ["Bijan Robinson"]}'
```

`/api/v1` is a contract and will not change under you. `/api/state` is
whatever the web page needs today — don't build on that one.

### What comes back

Every response carries a `rules` block. That is not padding. An assistant that
does not know this is a **one-flex, full-PPR, four-point-passing-touchdown**
league will confidently hand you advice written for a different league, and it
will sound just as certain.

`fantasy_lineup` / `/api/v1/lineup`:

```json
{
  "ok": true,
  "week": 5,
  "projected_total": 128.4,
  "starters":  [{"slot": "RB", "name": "...", "projected_points": 18.2, ...}],
  "bench":     [...],
  "cannot_play": [{"name": "...", "reason": "IR"}],
  "changes":   [{"action": "START", "name": "...", "projected_points": 14.1}],
  "points_gained_by_changes": 9.3,
  "rules": {...},
  "caveat": "..."
}
```

`changes` is the answer to "what do I do right now". An empty list means the
lineup already set in Sleeper is the best one available — say so and stop.

`fantasy_check_trade` and `fantasy_offers` score a trade by **the change to
your starting lineup**, and nothing else. A trade that makes your bench better
scores near zero, because it is worth near zero. Verdicts:

| `points_change` | verdict |
|---|---|
| ≥ +8 | ACCEPT |
| +3 to +8 | LEAN ACCEPT |
| −3 to +3 | TOO CLOSE TO CALL |
| −8 to −3 | LEAN REJECT |
| < −8 | REJECT |

`warnings` is where roster-limit and unfillable-slot problems show up. Read it
before repeating the verdict.

## 3. Told without asking — the weekly nudge

```bash
./install_weekly_nudge.command      # double-click it once
```

After that the Mac checks by itself, four times a week — Sunday 10am and 6pm,
Tuesday 9am after waivers, Thursday 4pm — and raises a notification **only
when there is something to do**. Silence is the point. A notification every
week is noise you learn to ignore, and then you miss the one that mattered.

Each run also writes:

* `cache/brief.txt` — the whole brief as text
* `cache/brief.json` — the same thing structured, with `needs_attention`

So Hobbs can watch that file instead of polling anything. The exit status is
`0` when something needs attention and `1` when nothing does, which is enough
on its own for a shell check.

By hand:

```bash
python3 weekly_nudge.py --print     # just show me
python3 weekly_nudge.py             # notify if there's news
python3 weekly_nudge.py --always    # notify regardless
```

Undo it with `./uninstall_weekly_nudge.command`.

---

## What Hobbs should know about the numbers

Say this once in Hobbs's own instructions and the advice gets noticeably
better:

> Projected points come from raw projected stats scored under this league's
> exact rules — full PPR, four points for a passing touchdown, one flex — not
> from a generic "PPR points" column. Trades and lineups are judged only by
> the change to the projected starting lineup. The projections cannot see this
> week's weather, a Friday practice report, or a snap-count change from last
> night. Those are worth checking on a close call, and are the one place an
> outside opinion beats this tool.

And what it cannot do: it will not accept a trade, set a lineup, add or drop
anyone. Every answer ends at "here is what to do in Sleeper."
