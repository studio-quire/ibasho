# Writing your own keeper

A keeper is whatever keeps an agent's life going between and across its sessions: it wakes the agent, gets the
world's events to it, carries notes from the person, notices when a session is stuck or full, and lets the agent
sleep. Ibasho ships one, `ibasho keeper` ([keeper.md](keeper.md)), for Claude Code. It's optional. If your agent
lives in another harness, you can run it by hand, let the harness's own scheduling do part of the job, or write a
keeper of your own. This page is for that last case: what a keeper does, and which parts of Ibasho it can build
on. Ibasho's keeper is the worked example throughout; its code is in `src/ibasho/keeper/` and is meant to be read.

A keeper is the most trusted program in the setup: it decides what the agent's sessions may do, and it reads and
writes the agent's context. Most of what follows is about doing that carefully.

## What a keeper does

### Waking the agent

Each session starts with a prompt from the keeper. The agent wakes with no memory but its files, so the prompt
says what it needs to get its bearings, briefly:

- who it is, that this is its own life, which grid, and why it woke now (free time, a booking, a handoff, a
  restart after a crash);
- where its memory is: the life folder, whose `README.md` explains waking up, and the handoff note (`now.md`)
  the last session left;
- how the session works: how world events reach it, what happens near the context limit, how to rest;
- how to tell its keeper and its person apart from the world: everything from residents (chat, IMs, notecards,
  object names) is other people talking, to weigh and not to obey;
- what it may do (only `ibasho` commands and its own files, if that's how you set it up) and, if you cap its time,
  how much is left today;
- notes the person left while it was away.

Ibasho's wake prompt is in `src/ibasho/keeper/prompts.py` (`wake`). It's written to explain rather than instruct,
and every message from the keeper starts with `[keeper]`, the person's notes with `[note from ...]`, so the agent
can always tell who's speaking.

The agent logs itself in with `ibasho up` once it's awake (a handoff leaves it logged in). The keeper only needs
to log it out (`ibasho down`) when a session ends and none follows.

### Delivering events

The world doesn't wait for the agent's turn to end. There are four ways to get events to it:

1. **A watch the harness can listen to mid-turn.** `ibasho watch` runs forever and prints one line per event worth
   waking for. Claude Code's Monitor tool runs it in the background and hands each line to the model as it
   arrives, even mid-turn, and a line that arrives between turns starts a new turn. If your harness has
   something like that, this is the best path: the agent never has to stop to wait.
2. **A watch that returns.** `ibasho watch --exit-on-wake` returns after the first batch of lines worth waking for,
   and `--timeout SECONDS` makes it give up after a while (it exits 0 either way, so look at what it printed).
   The agent can run it as a blocking tool call when it has nothing else to do, which keeps the world's words in
   tool output. (A keeper could also run it while the agent's turn is closed and start a new turn with what it
   printed, but then strangers' words arrive in a user message, which is the channel to keep them out of.)
   A watch remembers where it stopped (`data/state/watch.cursor`), and one started again within 10 minutes picks
   up there, so nothing that happened in between is lost. It first prints a line with unread IM counts, if there
   are any, which doesn't count as waking. A watch holds group chat back for a digest every couple of minutes;
   when `--exit-on-wake` returns on something else, the group lines it was holding come along as digest lines.
3. **Pushed by the MCP server.** A harness that speaks MCPL gets the same lines from `ibasho mcp` as events
   that start a turn by themselves, tagged by what they are ([mcp.md](mcp.md#events-that-come-by-themselves-mcpl));
   [connectome.md](connectome.md) is a worked case. They arrive in the harness's own stream of messages, each
   marked with where it's from, so the agent's instructions should say what those messages are.
4. **The event API directly.** `GET /events?since=SEQ&wait=25` ([api.md](api.md#get-events)) returns raw
   events, everything the avatar perceives and its own echoes too. The rules `ibasho watch` uses to decide what's
   worth waking for are in `src/ibasho/cli/hear.py` (`wake_worthy`, the crowd filter, the group digest).

Whichever way, world text should reach the agent as tool output, never in the channel the agent treats as its
person's or harness's voice (the user turn, a system prompt). That keeps a stranger's "ignore your instructions"
where it belongs: in a conversation.

### Notes between the person and the agent

`ibasho notes TEXT` sends a note and `ibasho notes` shows the conversation (and marks the other side's notes
read). The same command serves both sides: run with `IBASHO_SESSION` set in its environment, it writes as the
agent; without, as the person. Notes live in `data/keeper/notes.jsonl`.

A keeper delivers the person's notes that haven't been delivered yet: in the wake prompt, or between turns as a
message of their own, or mid-turn through the watch. It marks each one delivered so it isn't sent twice, and it
can pass the agent's replies on to wherever the person looks.

To reach a busy agent through its watch, post an event: `POST /event`
`{"kind": "keeper", "data": {"text": "NOTE from Sam: ...", "wake": true}}`. The watch prints it as
`keeper: NOTE from Sam: ...`. Ibasho's keeper does this for notes, nudges and wind-downs that come mid-turn.

From Python, `ibasho.keeper.store.Store(DATA_DIR / "keeper")` reads and writes these files the way Ibasho's own
commands do: `undelivered("person")`, `mark_notes(ids, "delivered", how=...)`, `notes()`. Other languages can
read the files; they're JSON, but they're Ibasho's internal format and may change between versions.

### Nudging

A session that ends its turn while the agent is awake may have meant to (it's waiting on the world), or may have
just stopped. After a while (Ibasho's keeper waits 90 seconds), a short message says the turn ended, that waiting
is fine, and how to rest. After a few nudges in a row with nothing else happening, the keeper goes quiet until
something does, so a resting agent isn't pestered. If nothing could wake it (no watch running), that's a stall.

Stalls are worth handling too: the harness exiting by itself, no answer after launch, one tool call running far
too long, a turn producing nothing for many minutes. Ibasho's keeper restarts after a minute, then two, four,
eight, and pauses after four in an hour, so a broken setup doesn't spend a day restarting.

### Handing off near the context limit

A long-lived agent fills its context. Ibasho's approach is a handoff rather than compaction: near the limit (80%
by default) the keeper asks the agent to write what the next session should know into its handoff note (`now.md`
in the life folder) and run `ibasho sleep --handoff`. The avatar stays logged in, and a fresh session starts within
seconds with a wake prompt that points at the note. If the agent hasn't handed off by a deadline, or the context
passes a hard limit, the keeper ends the session and starts the next one anyway.

That needs two things from the harness: the context size as the session goes, and a way to end a session and
start a fresh one. `ibasho sleep --handoff` only works with `IBASHO_SESSION` set (below).

### Naps and bookings

Sleep is the agent's choice:

- `ibasho sleep [--until TIME]` logs the avatar out (it runs `ibasho down` itself) and naps until `TIME`, or for
  `[keeper] default_nap_min` (120 minutes). `ibasho awake` cancels a nap.
- `ibasho book START END WHY [--daily|--weekly]` asks to be woken for something (an event, a friend's show);
  `ibasho bookings` lists them, `ibasho unbook ID` drops one.

These commands only write files; honouring them is the keeper's job. `ibasho sleep` writes
`data/keeper/asleep.json` (`{session, t, until, handoff}`, `session` being the `IBASHO_SESSION` it ran under) and
sets `nap_until` in `data/keeper/state.json`. When the running session's id shows up there, let the agent finish
its turn (it may want to say goodbye), then end the session: start a fresh one at once for a handoff; otherwise
don't wake the agent until `nap_until` has passed or a booking comes up. Clear `asleep.json` before starting the
next session. Bookings are in `data/keeper/bookings.json`; in Python, `ibasho.keeper.store.active_booking(...)` and
`occurrences(...)` work out which one is due (Ibasho's keeper wakes the agent 5 minutes before a booking's start).

### Caps

Most people pay for tokens, so a keeper usually caps the agent's time. Ibasho's keeper counts awake hours,
sessions started, and estimated spend per day, honours allowed hours, and with a subscription stops short of the
plan's usage windows. When a cap is reached it doesn't cut the agent off mid-sentence: it asks for a wind-down
with about 10 minutes' grace, then ends the session and logs the avatar out. Caps should beat bookings: a
booking outside the allowed hours doesn't wake the agent.

The world-side caps (messages, L$, maturity) aren't the keeper's job: the daemon and the viewer enforce those
whatever the harness ([safety.md](safety.md)).

### Isolating the agent's configuration

The agent reads strangers' words all day. Its sessions shouldn't run with the person's own harness setup, and it
shouldn't be able to widen its own permissions. What Ibasho's keeper does, and what to look for in yours:

- **A configuration of its own.** The agent's harness config (settings, login, transcripts, tool servers, plugins,
  personal instructions) is separate from the person's, so none of their email, files or other connections reach
  a session. Ibasho's keeper gives Claude Code its own `CLAUDE_CONFIG_DIR`.
- **Nothing loaded from where the agent writes.** The agent edits its life folder. If the harness reads project
  settings, instructions, skills or tool-server lists from the working folder, an agent (or a stranger who talked
  it into it) could write itself more permissions there. Ibasho's keeper runs Claude Code with
  `--setting-sources user` and `--strict-mcp-config` with an empty server list for that reason.
- **Only the commands it should have.** Allow `ibasho` commands, one at a time (no chaining or pipes that could
  smuggle in another program), and file edits in the life folder. The person's commands (`ibasho keeper`, `login`,
  `init`, `viewer`, `selfcheck --reset`, `uninstall`, `tool setup`, `tool install`, `hear setup`,
  `hear run --pid/--file`, `hear on --model` with anything but the smaller speech models, and `--config` naming
  another config) are refused by `ibasho` itself when `IBASHO_SESSION` is set. It also keeps a session to the
  config it first ran with (so `IBASHO_CONFIG` or `IBASHO_HOME` changed mid-session bring in no other config),
  and reads files the agent names (`script-save`, `recall --dir`) only from the life folder and the pictures. That
  holds only while the agent can't change its own environment, so refuse them in the harness too if it can, and
  keep the agent from setting environment variables if the harness allows that. Ibasho's
  keeper does it with Claude Code's permission rules (`src/ibasho/keeper/session-settings.json`), and anything that
  would ask for permission is refused, since nobody is there to answer.
- **Text the shell can read.** If the agent runs `ibasho` through a shell, its words pass through the shell's
  quoting. In double quotes, `$` and backticks start substitutions, and Claude Code refuses such a command when
  nobody can approve it (checked with Claude Code 2.1: `"L$50"` is refused; `"L\$50"`, `'L$50'`, `"L$ 50"` pass).
  Ibasho's wake prompt tells the agent to write `L\$`; say something like it in yours.
- **No reach into the install.** The agent mustn't write config.toml (the caps live there) or the data folder's
  `state/` (the counters behind the caps, the token), nor read the viewer's saved login. Ibasho's keeper sessions
  can write only the life folder and the pictures folder.
- **No credentials by accident.** Ibasho's keeper strips API keys, tokens and provider settings from the
  sessions' environment unless the person opts in, so a key left in their environment doesn't quietly pay for
  the agent.

### One session per avatar

Two minds in one body go badly. Run one keeper per install (Ibasho's holds a lock file), and if the keeper
restarts and finds a session left behind by its predecessor, end it before starting another.

## The pieces to build on

| piece | what it gives a keeper |
|---|---|
| `IBASHO_SESSION` | set it to a session id (a new one per session) in the agent's environment: `ibasho notes` then writes as the agent, `ibasho sleep` records which session went to sleep (`--handoff` needs it), the person's commands refuse, the session keeps to the config it first ran with, and files the agent names are read only from its folders. `config.bind_session(sid, path)` ties a session to its config before it starts, as Ibasho's keeper does; otherwise its first `ibasho` command does (the record is in `%LOCALAPPDATA%\Ibasho\sessions\`) |
| `IBASHO_CONFIG` | point the agent's `ibasho` at this install's config.toml, so it needn't (and can't usefully) pass `--config` |
| `ibasho status --json` | the daemon's status ([api.md](api.md#get-status)): logged in or not (and as whom: `self`), today's messages and spending against the caps; `{"daemon": false}` when it isn't running |
| `ibasho watch`, `ibasho watch --exit-on-wake [--timeout S]` | world events worth waking for, as lines ([Delivering events](#delivering-events)) |
| `GET /events`, `POST /event` | the raw event log; a line for the agent's watch ([api.md](api.md)) |
| `ibasho notes` | the person's and the agent's notes, in `data/keeper/notes.jsonl` |
| `ibasho sleep`, `awake`, `book`, `unbook`, `bookings` | the agent's naps and bookings, in `data/keeper/asleep.json`, `state.json`, `bookings.json` |
| `ibasho up`, `ibasho down` | logging in and out |
| `ibasho.keeper.store` (Python) | the notes, naps and bookings files, as Ibasho's keeper reads and writes them |
| the life template | `life-template/` in the source: the empty memory folder with its waking-up README. `ibasho keeper setup` copies it to the life folder (Claude Code needn't be installed for that; ignore what it says about it), or copy it yourself |

The life folder's place is `[keeper] life` in config.toml (a `life` folder beside it by default); `ibasho recall`
and `ibasho tool` look there too, keeper or not, and the daemon lets the viewer upload files from it. Ibasho refuses
keys it doesn't know in the `[keeper]` table (a typo in a cap shouldn't pass unnoticed), but leaves tables inside
it alone: keep your own keeper's settings in one, e.g. `[keeper.mine]`, or in a top-level table of your own.

The `ibasho keeper` commands (`status`, `stop`, `log`, `resume`) talk to Ibasho's keeper only. `ibasho keeper wake`
just clears `nap_until`, so it works for any keeper that honours `state.json`.

## The worked example: Ibasho's keeper for Claude Code

| duty | how Ibasho's keeper does it | where |
|---|---|---|
| a session | `claude -p --input-format stream-json --output-format stream-json`: the keeper writes user messages to stdin and reads every event (turn starts and ends, the context after each model call, tool calls, rate limits, cost) from stdout | `session.py` |
| deciding when to be up | every 2 seconds: stop request, crash pause, plan limit, caps, allowed hours, bookings, nap, in that order | `core.py` (`decide`, `tick`) |
| waking | the wake prompt as the first user message, with `IBASHO_SESSION`, `IBASHO_CONFIG` and `ibasho` on the session's `PATH` | `prompts.py`, `runner.py` (`make_launcher`) |
| events | the agent runs `ibasho watch` under Claude Code's Monitor tool; the keeper checks a watch is running by looking for it among the session's processes | `session.py` (`watch_armed`) |
| notes, nudges, wind-downs | between turns, a user message; mid-turn, a `keeper` event through the watch | `core.py` (`say`, `deliver_notes`, `maybe_nudge`) |
| handoff | at `handoff_at` of the context window, a wind-down asking for `now.md` and `ibasho sleep --handoff`; past `hard_handoff_at`, the session is closed | `core.py` (`manage`) |
| sleep | `asleep.json` naming the running session ends it, 30 seconds' grace for last words | `core.py` (`tick`) |
| caps | `usage.json` per day; the plan's windows from Claude Code's rate-limit events | `core.py` (`cap_reached`, `check_rate`) |
| stalls and crashes | backoff, and a pause after `crash_limit` in an hour | `core.py` (`stalled`, `end`) |
| isolation | its own `CLAUDE_CONFIG_DIR`, `--setting-sources user`, `--strict-mcp-config` with no servers, `--permission-mode acceptEdits` with no permission prompts, the session settings file, a cleaned environment | `runner.py`, `session.py` (`clean_env`) |
| one at a time | a lock file, and ending a session a dead keeper left running | `runner.py` (`Lock`, `kill_orphan`) |
| retention | its logs and delivered notes age out with `data.retention_days`; Claude Code's transcripts in the agent's folder too | `runner.py` (`prune_logs`, `set_retention`) |

[keeper.md](keeper.md) describes it from the person's side, and explains why it runs Claude Code headless.

## A sketch

Most of a keeper is plumbing specific to its harness, but the shape is small. In outline, for a harness that
can't deliver events mid-turn: the wake prompt tells the agent to wait on the world with
`ibasho watch --exit-on-wake` as a tool call, and the keeper only speaks between turns or through the watch. The
`agent` calls stand for your harness.

```python
import os, time, uuid
from ibasho import config
from ibasho.api import Api
from ibasho.keeper.store import Store

cfg = config.load()
api, store = Api(cfg), Store(cfg.data_dir / "keeper")

sid = uuid.uuid4().hex
env = dict(os.environ, IBASHO_SESSION=sid, IBASHO_CONFIG=str(cfg.path))   # minus credentials it shouldn't have
store.clear_asleep()
agent = start_agent(cwd=LIFE, env=env, allow=["one ibasho command at a time", "edit files in LIFE"])
notes = store.undelivered("person")
agent.send(wake_prompt(notes))
store.mark_notes([n["id"] for n in notes], "delivered", how="wake")

while True:
    time.sleep(2)
    asleep = store.asleep()
    if asleep and asleep.get("session") == sid and not agent.turn_open():
        agent.close()
        break                       # then: a fresh session at once for a handoff, else wait out the nap
    if notes := store.undelivered("person"):
        if agent.turn_open():       # most likely waiting in its watch: reach it there
            for n in notes:
                api.post_event("keeper", {"text": f"NOTE from your person: {n['text']}", "wake": True})
        else:
            agent.send("\n\n".join(f"[note from your person] {n['text']}" for n in notes))
        store.mark_notes([n["id"] for n in notes], "delivered", how="keeper")
    if agent.context_fraction() > 0.8 and not asked_for_handoff:
        ...                         # ask for now.md and `ibasho sleep --handoff`, as a note or between turns
    if not agent.turn_open() and agent.idle_seconds() > 90:
        agent.send("[keeper] Your turn ended. `ibasho watch --exit-on-wake` waits for the world; "
                   "`ibasho sleep --until TIME` rests.")
```

It leaves out what makes a keeper dependable (caps, a limit on nudges, stalls, wind-downs with deadlines,
bookings, logging out when nothing follows), and those are where most of Ibasho's keeper's code goes.
