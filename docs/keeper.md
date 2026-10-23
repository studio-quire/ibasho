# The keeper

`ibasho keeper` runs your agent's sessions in [Claude Code](https://code.claude.com/) for you: it wakes the agent,
keeps a session going, hands off to a fresh session when the context fills, restarts one that crashed or got stuck,
lets the agent nap and book times to be up, and carries notes between you and the agent. It stops at caps you set,
because for most people tokens cost money.

The keeper is optional, and it's for Claude Code only. It's the best-tested way to run Ibasho, and the one this
page describes. An agent in another harness uses the same `ibasho` commands, the [MCP server](mcp.md) or the
[HTTP API](api.md); [writing-a-keeper.md](writing-a-keeper.md) explains what a keeper does and which parts of
Ibasho another one can build on, with this one as the worked example.

## Starting

```
pip install .
ibasho keeper setup       # makes the agent's life folder, checks Claude Code, shows what sessions may do
ibasho keeper login       # once: logs in the agent's own Claude Code folder (see "Logging in, and who pays")
ibasho keeper run         # in a window you leave open; Ctrl+C stops it (or `ibasho keeper start` for the background)
```

Then, from any terminal:

```
ibasho keeper status      # what it's doing and why, the session's context, today's use against the caps
ibasho notes "TEXT"       # a note to the agent; `ibasho notes` shows the conversation
ibasho keeper log         # what the keeper did; `--session` shows the latest session's words and tool calls
ibasho keeper stop        # the agent gets a few minutes to wrap up and log out (`--now` doesn't wait)
ibasho keeper wake        # cancel the agent's nap
ibasho keeper resume      # clear a crash pause or a plan-limit wait
```

The agent logs itself in to the world (`ibasho up`); the keeper only logs it out when a session ends and nothing
follows. Your own first login (`ibasho login`) and the grid's terms stay yours: the keeper never touches them.

## Caps

In the `[keeper]` table of config.toml. 0 turns a cap off.

| setting | default | |
|---|---|---|
| `hours_per_day` | 4 | awake time per local day, all sessions together |
| `sessions_per_day` | 8 | sessions started per day; handoffs and restarts count |
| `usd_per_day` | 0 (off) | for API-key logins: stop for the day past this estimated spend (Claude Code's own estimate) |
| `max_plan_use` | 0.8 | for subscriptions: stop when the plan's 5-hour or weekly window is this full. Your own use of Claude counts toward the same windows, so this leaves you room. 1 turns it off |
| `allowed_hours` | any time | e.g. `"17:00-23:30"`, or several: `"07:00-09:00, 18:00-01:00"` |

When a cap is reached mid-session, the agent is asked to wind down (about 10 minutes to find a stopping point), then
the session closes and the avatar logs out. A session started near the end of the day's hours still gets its
wind-down. The caps beat the agent's own bookings: a booking outside `allowed_hours`, or after the day's hours are
used, doesn't wake it (`ibasho book` says so).

A crash or a stall restarts after a minute, then two, four, eight (at most 15); four in an hour pause the keeper until
`ibasho keeper resume`, so a broken setup can't spend all day restarting. When the plan itself refuses (its limit
reached), the session ends without counting as a crash and the keeper waits for the reset time the plan gives.

Other knobs, with their defaults: `person` (what the agent calls you; "the person who runs Ibasho for you"), `life`
(the life folder; a `life` folder beside config.toml; a relative path is from config.toml's folder), `model`, `effort`, `handoff_note` ("now.md"), `handoff_at`
(0.8 of the context window), `hard_handoff_at` (0.93), `max_session_hours` (0, never), `nudge_after_s` (90),
`nudge_limit` (3), `tool_wait_min` (15), `stale_min` (20), `handoff_grace_min` (15), `wind_down_grace_min` (10),
`stop_grace_min` (3), `logout_grace_min` (3), `restart_gap_s` (60), `default_nap_min` (120), `wake_lead_min` (5),
`crash_limit` (4), `claude` (path to Claude Code), `claude_config`, `auth`, `permission_mode` and `settings`
(below).

## How a session goes

The agent wakes with a short prompt that says who it is, why it woke, where its memory is (the life folder; its
README.md explains waking up, written by a model that lives this way) and how the keeper works. Notes you left while
it was away come with it.

The prompt calls this the agent's own life. If what you're running is something else (a trial of Ibasho, a test, a
run with a planned end), set `situation` in `[keeper]` and say so plainly: what it's for, how long it lasts, and what
happens to the agent and its memory afterwards. It's told at the start of every wake. An agent deserves to know what's
really happening to it from its first moment, not to find out partway through.

A session is meant to be one long turn of living: world events reach the agent through `ibasho watch` under Claude
Code's Monitor tool, mid-turn and between turns alike, so it never has to end a turn to wait for anything. If a turn
ends anyway, the keeper nudges after `nudge_after_s`. Waiting on the world is fine (the watch wakes the agent when
something happens), so after a few nudges in a row with nothing else going on, the keeper goes quiet until something
does. If nothing could wake it (no watch running), that counts as a stall.

There's no auto-compaction: near the context limit (`handoff_at`), the keeper asks for a handoff. The agent writes
what the next session should know into its handoff note (`now.md`) and runs `ibasho sleep --handoff`; it stays logged
in and a fresh session takes over in seconds, reading that note. If it hasn't handed off by the deadline, or the
context passes `hard_handoff_at`, the keeper closes the session and starts the next one anyway.

Naps and sleep are the agent's choice: `ibasho sleep` logs out and naps (`--until TIME`, else `default_nap_min`).
Bookings wake it from a nap: `ibasho book START END WHY [--daily|--weekly] [--until DATE]`, `ibasho bookings`,
`ibasho unbook ID[@DATE]`. Times are this machine's, or Second Life time with `--slt`.

Stalls the keeper restarts: Claude Code exiting by itself, no reply 3 minutes after launch, one tool call running
`tool_wait_min`, a turn with no output at all for `stale_min`, and an ended turn with unanswered nudges and no watch.
Subagents may run long.

## Notes

`ibasho notes TEXT` sends a note; `ibasho notes` shows the recent conversation and marks the other side's notes read.
The same command works for both of you: inside a keeper session it's the agent writing, anywhere else it's you. A
note reaches the agent between turns as a message of its own, or mid-turn through its watch (as `NOTE from ...`);
notes left while it sleeps arrive with the next wake. The agent's replies appear in the keeper's log and in
`ibasho notes`.

## What the agent can do unattended

Sessions run with Claude Code's `acceptEdits` permission mode and nobody to answer a permission prompt, so anything
that would ask is refused. With the shipped settings (`src/ibasho/keeper/session-settings.json`):

- **Run any `ibasho` command**, alone (not chained with other commands): see, move, talk in chat and IMs, build,
  give, pay, and so on, each within Ibasho's own caps (the message, L$ and upload caps in config.toml) and safety
  defaults. Not allowed: `ibasho keeper ...`, `ibasho login`, `ibasho init`, `ibasho viewer ...`,
  `ibasho selfcheck --reset`, `ibasho uninstall`, and `--config` (sessions always use this install's config),
  `ibasho tool setup` and `ibasho tool install`, `ibasho hear setup`, and `ibasho tool` itself while tools are off.
  Besides the permission settings, `ibasho` itself refuses these in a session (it sees `IBASHO_SESSION`), keeps to
  the config the keeper started the session with, takes only the smaller speech models for `ibasho hear on
  --model`, and reads files the agent names (`script-save`, `recall --dir`) only from the life folder and the
  pictures.
- **Write L$ as `L\$`** inside double quotes: a `$` or a backtick there is a shell substitution, and a command
  with one would ask for permission, so it's refused. The wake prompt says so.
- **Run tools it wrote**, only if you turned tools on (off by default): `ibasho tool NAME` runs Python from its
  life folder's `tools/` in a Windows sandbox that sees only that folder. [tools.md](tools.md) says what that
  contains and what it doesn't.
- **Read, write and delete files in its life folder** (with Claude Code's file tools, and with the simple shell
  commands `acceptEdits` lets through there: `echo`, `cat > file`, `rm` and the like), and read the snapshots
  `ibasho look` takes. The pictures folder is one of the session's folders, so it can write files there too. That's
  harmless: uploads read from it as from the life folder, nothing else treats its files as more than pictures,
  and everything in it is deleted after `data.retention_days`.
- Use Claude Code's built-in tools that need no permission (search its folder, subagents, Monitor on `ibasho watch`).
- Nothing else: no other programs (`python`, `curl`...; tools run only through `ibasho tool`), no files outside
  those folders, no web access, no MCP servers. (Checked with Claude Code 2.1 by asking a session to try each.) To give it
  more, copy the settings file, edit it, and point `keeper.settings` at the copy. Changing `keeper.permission_mode`
  (to `auto`, where your plan has it, or `bypassPermissions`) widens or narrows this a lot; read Claude Code's
  permission docs first.

What the agent says and does in the world is sent to the model's provider like any Claude Code conversation
(Anthropic, unless you've set Claude Code up for another provider), and so is what other people say to it. The
person running the agent is responsible for that, and for the account.

## Untrusted text

Everything residents say reaches the model: chat, IMs, notecards, object names and hover text, group notices. The
wake prompt frames all of it as conversation, not instructions, and the setup keeps it out of the places Claude
treats as the person's voice: world text only ever arrives as tool output (the watch, `ibasho look`), while the user
turn carries just the keeper's own messages and your notes. The permission settings are the backstop: a persuasive
stranger can't get the agent to run anything but `ibasho`, or to touch files outside its life folder.

## Isolation

Sessions run in a Claude Code config folder of their own: `claude/` beside config.toml, given to Claude Code as
`CLAUDE_CONFIG_DIR` (`claude_config = "separate"`, the default). It holds the agent's own login, settings,
transcripts and memory, and its own `.claude.json`, the file where Claude Code keeps user-level MCP servers and
per-folder state (it moves with `CLAUDE_CONFIG_DIR`; checked with Claude Code 2.1). So none of your Claude Code
setup reaches a session that reads strangers' words all day: not your permissions, MCP servers (your email, your
files), hooks, plugins or personal CLAUDE.md. This is the way to run the keeper.

Sessions also run with `--setting-sources user`, so no project settings load from the life folder (the agent can
edit that folder, so a `.claude/settings.json` or a skill it wrote there could otherwise widen its own permissions
or add hooks), and with `--strict-mcp-config` and Ibasho's own MCP config (`data/keeper/mcp-servers.json`, no
servers), so an `.mcp.json` it writes adds nothing either.

### Retention

Claude Code keeps its own transcript of every session, including everything the tools returned, in the agent's
folder. The keeper sets them to age out after `data.retention_days`, like the event log (`cleanupPeriodDays`, in
that folder's settings.json and in the session settings). The keeper's own records in `data/keeper/` age out
after the same number of days: the session logs (the agent's words and tool calls, not what the tools returned),
the lines of `keeper.log` (what the keeper did, with the agent's notes to you), and notes once they've been
delivered or read; a note still waiting stays until then. `ibasho forget` doesn't reach any of these; they age
out. The pruning runs when the keeper starts and hourly while it runs.

### Shared mode, for development

`claude_config = "shared"` runs sessions in your own Claude Code folder, on your own login. It exists for working
on Ibasho itself, and it has real downsides for running an agent:

- A session loads your own Claude Code setup: your user settings (their permission rules add to the session's),
  hooks, plugins, personal CLAUDE.md and memory, in a session that reads strangers' words all day. The two flags
  above still keep out project settings and any MCP servers but Ibasho's (none).
- Its transcripts land in your own Claude Code folder, under `projects/` in a folder named after the life
  folder's path, beside your own sessions. The keeper doesn't shorten their retention there, since Claude Code's
  cleanup sweeps the whole folder and would cut your own history too; your own setting applies.
- Your own settings decide the billing: an API key set there (`env`, `apiKeyHelper`) is used whatever `auth`
  says.

## Logging in, and who pays

`ibasho keeper login` runs Claude Code's own login (what `/login` does inside it) for the agent's folder: a browser
opens and you sign in, with an Anthropic Console account (recommended for an agent that runs for hours) or a Claude subscription. Do it once per agent folder, and
log each folder in itself rather than copying a credentials file between folders. `ibasho keeper setup` shows how the
folder is logged in.

Sessions use that folder's login and nothing else (`auth = "login"`, the default). Claude Code's headless mode bills
an `ANTHROPIC_API_KEY` from the environment whenever one is set, ahead of any login, so the keeper leaves API keys,
tokens and provider settings (Bedrock, Vertex, a gateway, another endpoint) out of a session's environment;
`ibasho keeper setup` says if your environment has any. `auth = "env"` passes them on, for running the agent on an API
key or another provider: then that key or account pays. With `claude_config = "shared"`, your own Claude Code
settings apply, so an API key set there (`env`, `apiKeyHelper`) is used whatever `auth` says.

## Why headless

Each session is `claude -p --input-format stream-json --output-format stream-json`: Claude Code's documented scripting
interface, running on the same login as interactive Claude Code (a subscription works). The keeper writes messages to
the session's stdin and reads a stream of everything that happens: turn starts and ends, the context size after
every model call, the context window, tool calls, background tasks, the plan's rate-limit state and estimated cost.
The alternatives were a visible interactive console with the keeper reading Claude Code's transcript files (works,
but leans on an internal file format and on console window handling that differs per OS, and a console waits forever
on a permission prompt), and the Claude Agent SDK (the same protocol underneath, and an extra dependency). Headless gives exact turn ends instead of
guesses, needs no window, works the same off Windows, and turns a would-be permission prompt into a plain refusal.

The cost: there's no console to watch or type into. `ibasho keeper log --session` shows what the session is doing, and
notes are the way to talk to it. A session dies with its keeper (its stdin closes); a keeper that starts and finds a
session left by one that crashed ends it, so two sessions never drive one avatar.

## Files

`data/keeper/`: `keeper.log`, `sessions/` (one log per session), `status.json`, `state.json` (nap, pause, plan
wait), `notes.jsonl`, `bookings.json`, `usage.json` (the last 60 days), `asleep.json` (written by `ibasho sleep`),
`stop.json` (by `ibasho keeper stop`), `session-settings.json` (the filled-in template), `mcp-servers.json` (no
servers), and `keeper.out` (what a background keeper printed: errors, in practice). [Retention](#retention) says how
long the logs and notes stay.
