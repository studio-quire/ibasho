# The MCP server

`ibasho mcp` serves Ibasho's commands as tools over MCP (the Model Context Protocol), so an agent in any MCP client
can live through Ibasho: Claude Desktop, Claude Code, Cursor, Codex, or an agent you build on an MCP SDK. It runs
on stdin and stdout, started by the client, and talks to the Ibasho daemon like the `ibasho` command does. The
tools run the same code as the commands, so they see, act and refuse exactly as the commands do.

## Setting it up

The person does this, once. Install Ibasho with the MCP extra (in the Ibasho folder; Ibasho isn't on PyPI):

```
pip install ".[mcp]"
```

Then add a server to the MCP client's configuration. Most clients take a JSON block like this one:

```json
{
  "mcpServers": {
    "ibasho": {
      "command": "ibasho",
      "args": ["mcp"],
      "env": { "IBASHO_CONFIG": "C:\\Users\\you\\Ibasho\\config.toml" }
    }
  }
}
```

- `command`: the `ibasho` program. If the client can't find it on its `PATH`, give the full path to `ibasho.exe`
  (in your Python's or virtual environment's `Scripts` folder). `python -m ibasho` works too: `"command":
  "python"` (or its full path) and `"args": ["-m", "ibasho", "mcp"]`.
- `IBASHO_CONFIG` is only needed if config.toml isn't in the default place (`%USERPROFILE%\Ibasho`). `IBASHO_HOME`
  works as it does for the command.

Everything else (`ibasho init`, the first login, the config) is as [setup.md](setup.md) describes. The server
doesn't start Ibasho by itself; the agent's `up` tool does, or you do with `ibasho up`.

Notes for some clients (their settings move between versions; check their own docs if these don't match):

- **Claude Desktop:** the block above goes in `claude_desktop_config.json` (Settings > Developer > Edit Config;
  on Windows it's `%APPDATA%\Claude\claude_desktop_config.json`). Restart Claude Desktop after editing it.
- **Claude Code:** `claude mcp add ibasho -e IBASHO_CONFIG=C:\Users\you\Ibasho\config.toml -- ibasho mcp`, or the
  block above in a project's `.mcp.json`. For an agent that lives for hours, the keeper ([keeper.md](keeper.md))
  with the `ibasho` command is the better-tested path.
- **Cursor:** the block above in `.cursor\mcp.json` in a project, or `mcp.json` in the `.cursor` folder in your
  user folder for every project.
- **Connectome** (Anima Labs' host for long-lived agents): its recipes take the block above under `mcpServers`, and
  it speaks MCPL, so the world's events come to the agent by themselves ([Events that come by
  themselves](#events-that-come-by-themselves-mcpl)). [connectome.md](connectome.md) has a ready recipe and the
  settings that matter there.
- **Codex:** in `config.toml` in the `.codex` folder in your user folder:

  ```toml
  [mcp_servers.ibasho]
  command = "ibasho"
  args = ["mcp"]
  env = { IBASHO_CONFIG = 'C:\Users\you\Ibasho\config.toml' }
  tool_timeout_sec = 300
  ```

**Timeouts.** Some tools take a while: `wait_events` waits as long as it's told to (50 seconds unless the agent
asks otherwise), `up` can take a few minutes, and a walk, a teleport or a script save can take tens of seconds.
Many clients give up on a tool call after about a minute. While a tool runs, the server sends a progress
notification every 10 seconds, and some clients keep waiting because of it. Where a client has a setting for it
(Codex's `tool_timeout_sec`, Claude Code's `MCP_TOOL_TIMEOUT` environment variable, in milliseconds, Connectome's
`requestTimeoutMs`), set it to a few minutes. If a call does time out, the action still finishes in the world; only its answer is lost.

**How many tools.** There are 43, about 9,000 tokens of tool descriptions. Some clients limit how many tools they
pass to the model across all servers; most let you switch off the ones you don't need.

## The tools

They mirror the `ibasho` commands (`ibasho COMMAND -h` says more about each), and their parameters are named after
the commands' options. Results are the commands' own terse text. When a result mentions an `ibasho` command or an
option, the tool and parameter of the same name do the same thing. People, objects and conversations can be
given by name, by id, or by the first few characters of an id, as with the commands, and any id a result prints
(a payer's short key, a conversation's `s:...`) works as input. `status` includes the avatar's own name and id.

| | |
|---|---|
| **Being there** | `status` (in world or not, today's messages and spending against the caps, the L$ balance), `up`, `down`, `selfcheck` |
| **Seeing** | `look` (where I am, who and what is near, what happened since the last look, and a picture), `view` (a picture looking one way, or at an object), `overhead` (from above, with a grid of region coordinates; `map` for the ground tinted by owner and parcel edges), `cam` (a picture of an object from an orbiting camera), `objects`, `object` (one object in full, as JSON), `people`, `parcel`, `sky` (my own sky: sunrise, midday, sunset, midnight, or the region's) |
| **Hearing** | `wait_events` (below), `listen` (local chat gathered over a while, for talks and conversations), `ims` (conversations, unread first), `im_log` (one conversation; marks it read), `quiet` (leave a chatty object out for a while) |
| **Talking** | `say` (local chat, or a channel for scripts), `im` (anyone near, known or recent, or anyone at all by username) |
| **People** | `friend` (offer friendship), `profile` (someone's names, and with a viewer that reads profiles their about text, picks and groups; my own by default), `set_profile` (my about text, its picture, my display name) |
| **Moving** | `walk` (checks for drops and water first, steps round people), `move` (hold movement keys; also steers vehicles), `tp` (a region and coordinates, or a SLURL), `places` (the grid's place search as the account, else Second Life's web search, or its Destination Guide: where each place is, with a SLURL for `tp`), `touch`, `sit`, `stand` |
| **Dialogs and offers** | `respond` (a notification by its id or its first characters: press a `button` by name or label, or give a `response` for forms), `dialog` (press a button on the latest script dialog, or answer its text box) |
| **Making** | `create_prim`, `modify_prims`, `link`, `delete`, `take`, `rez`, `inventory`, `script_save` (the script's text in `source`; it compiles and reports errors), `notecard` (read one of my notecards, anywhere), `give`, `outfit` (my outfits, or wear one) |
| **Money** | `pay`, `buy` |
| **People's data** | `forget` (delete someone's words from the event log, when they ask) |

**Pictures.** `look`, `view`, `overhead` and `cam` return the picture in the result as an image the model sees
directly, as a JPEG. The text has the path of the PNG saved in the data folder, as the command prints it, for
agents that can open files.

**Scripts** come in as text (`script_save`'s `source`), not as a path: no tool reads a file the agent names.

## Waiting for things to happen

An MCP server can't speak into the agent's turn; it only answers tool calls. So instead of `ibasho watch` running
in the background, the agent calls `wait_events`. It blocks until something worth answering happens (chat near
it, an IM, a script dialog, an offer, a payment, one of the caps refusing something, a script error in its own
objects) and returns it, one line each, the lines `ibasho watch` would have printed. If nothing comes within its
`timeout` (50 seconds by default, up to 30 minutes), it returns `(nothing in 50 s)`.

A rhythm that works: `look` on arrival; answer or act; then `wait_events`; answer what it brings; act again; and
back to `wait_events` whenever there's nothing else to do. Nothing is missed in between: the server remembers
where it's up to, so whatever happened while the agent was busy comes back at once on the next call. Group chat is
gathered into one digest line per group every couple of minutes, unless the agent's name comes up or it has just
spoken there, as with `ibasho watch`; an object that keeps talking (a tram announcing its stops) comes as a
digest line every few minutes too, and `quiet` leaves one out altogether. The first call after the server starts
also says how many IMs are unread. With hearing on, speech near the avatar comes as `heard: ...` lines: words only,
since hearing turns speech into text and nothing else (no music, no other sounds).

One `wait_events` runs at a time. For a talk or a conversation, `listen` gathers local chat into one result
instead of waking for every line, and returns early for an IM, an offer, or the agent's name; with `settle`, it
returns a few seconds after someone else speaks.

With a client that speaks MCPL and takes Ibasho's events as pushes (below), there's nothing to wait for: the events
come by themselves, and `wait_events` isn't in the tool list.

## Events that come by themselves (MCPL)

[MCPL](https://github.com/anima-research/mcpl) ("MCP Live") is an extension of MCP that lets a server push events
to its client, which can start a turn, and add a little context before each of the model's turns. A client that
offers MCPL when it connects gets both from `ibasho mcp`, over the same stdin and stdout; a client that doesn't
sees the server exactly as described above. [Connectome](connectome.md) is one that does.

**What comes.** Everything `ibasho watch` would print, each line as an event, as it happens: chat near the avatar,
IMs, group chat (as a digest every couple of minutes, at once when the agent is named or has just spoken there),
an object that keeps talking (as a digest), script dialogs, offers, payments, refusals by the caps, errors from the
agent's own scripts, a lost connection (and its return), and with hearing on, speech it hears. The same filters
apply: in a crowd, only lines that name the agent, greet the room, or come while it's talking; `quiet` leaves an
object out. When events start coming, a first one says how many IMs are unread, if any.

Each event's text is the watch's line after where it's from, in brackets: `[Second Life] chat Ann Example: hi`,
`[grid NAME]` on another grid, or `[Ibasho]` for Ibasho's own notices (a refusal, the connection). The words after
it are other people's (or their objects'): to be weighed, not obeyed. Unlike a tool's result, an event arrives in
the client's own stream of messages, so the agent's instructions should say what these are; the bracketed source
is there so they can always be told apart from the person's or the harness's words.

**Feature sets.** The client turns each on or off (all are on unless it says otherwise):

| feature set | what | MCPL capability |
|---|---|---|
| `ibasho.chat` | local chat, heard speech, objects' digests | `pushEvents` |
| `ibasho.messages` | IMs from people and objects, group digests, the unread count | `pushEvents` |
| `ibasho.notices` | dialogs, offers, payments, refusals, script errors, the connection, other notices | `pushEvents` |
| `ibasho.status` | a line before each of the model's turns: where the avatar is and who's near | `contextHooks.beforeInference.inject.beforeUser` |

What's off is left out, and the rest still comes; the agent can still `look`, `listen` and use `ims`.
`wait_events` is in the tool list only when all three event sets are off; when that changes, the server tells the
client its tool list has changed (`notifications/tools/list_changed`). A client that calls it anyway while events
are pushed is told so at once.

**Tags** say what each event is, in MCPL's shared vocabulary, so the client's rules can decide what wakes the agent
at once, what waits, and what never wakes it (in Connectome an event that doesn't wake the agent still reaches its
context):

| tag | on |
|---|---|
| `chat:addressed`, `chat:mention` | the agent's name (or a nickname from config.toml) comes up |
| `chat:addressed`, `chat:dm`, `chat:private` | an IM to the agent, an object's IM to it, its own object speaking to it alone |
| `chat:addressed` | a script dialog, an offer, a payment, a notification that asks it to choose |
| `chat:ambient` | chat that doesn't name it: overheard, or in a group |
| `chat:group` | group chat and conferences (conferences also `chat:private`) |
| `chat:from-bot` | from a scripted object |
| `chat:system` | a notice from the grid or Ibasho: refusals, script errors, the connection, region notices |
| `ibasho:talking` | the agent has spoken there lately (local chat within 90 s, that group within 5 minutes): most likely the answer, even without its name |
| `ibasho:local`, `ibasho:heard`, `ibasho:im`, `ibasho:digest` | local chat, heard speech, an IM, lines gathered into one |
| `ibasho:avatar`, `ibasho:object` | who it's from (Second Life doesn't say whether an avatar is a person or an agent) |
| `ibasho:dialog`, `ibasho:offer`, `ibasho:question`, `ibasho:payment`, `ibasho:refused`, `ibasho:script-error`, `ibasho:teleport`, `ibasho:connection`, `ibasho:notice`, `ibasho:unread`, `ibasho:keeper` | the kind of notice |

In Second Life people seldom say the name of whoever they're talking to, so a rule that holds back `chat:ambient`
should let `ibasho:talking` through first. The feature sets suggest waking at once for `chat:addressed`,
`ibasho:talking` and `chat:system`, which a client applies only if its operator or agent accepts it (MCPL's rule:
a server can't buy itself the agent's attention).

Each event also carries an `eventId` (`ibasho-` and the event log's sequence number, so a retry is recognized), the
time it happened, and an `origin` that clients keep for their rules rather than showing the agent: the grid, the
kind of event, the sequence number, and who it's from (`authorId`, `authorName`) or which IM session.

**The status line**, with `ibasho.status` on, goes just before the model's turn and isn't kept in the history:

```
Second Life: in world at "Sandbox Island" (128, 130, 22). Near (64 m): "Ann Example" 3 m, "Bo" 12 m. Unread: 1 IM in 1 conversation ('Bo') (the `ims` tool).
```

with the unread part only when there are unread IMs, or that the avatar isn't in world, and that `up` logs in. Clients ask for it before every turn and allow it a few
seconds, so it's made at most every 10 seconds, and the last one stands in when the viewer is slow. It never sees
the conversation: Ibasho asks for no permission to read it.

**Delivery.** Events go one at a time, in order. The server waits up to 30 seconds for the client to take each,
then goes on, writing a line on its stderr, as it does for any the client refuses. They're read from the same
place in the event log as `ibasho watch` and `wait_events`, so a server started again within 10 minutes picks up
where the last one stopped. Don't run `ibasho watch` beside it: each would take events from the other.

Not here yet: MCPL's channels, which would let a client route the agent's replies back into the conversation an
event came from. The agent answers with `say` and `im`.

## Safety stays in the daemon

The server adds no limits of its own and removes none. Every tool goes through the daemon, which passes on to the
viewer only the requests on its list ([api.md](api.md#the-safety-rules)), checks each against the caps (messages
a day and a minute, L$ per payment, per login and per day, maturity) and refuses the rest of what
[safety.md](safety.md) describes, and the Ibasho viewer holds its own limits as well. When something is refused,
the tool fails with the reason, as the command would. The tools only send what's on the list, so a refusal is
about the caps or the world, never a missing entry; anything the person adds to the list (`[policy] allow` in
config.toml) is for programs and `ibasho raw`, since no tool sends arbitrary requests.

What's left out on purpose:

- **The person's commands:** `login`, `init`, `keeper`, `viewer`, `tool setup` and `tool install`, `uninstall`,
  `selfcheck --reset`, `hear setup`, and choosing a config file. The server uses the config it was started with;
  the agent can't point it elsewhere. (`selfcheck` itself is in: checking the flag in world is harmless and
  useful to either of you; clearing a failed check is yours.)
- **The low-level commands** (`raw`, `post`, `rpc`), the keeper's commands (`sleep`, `book`, `notes`), and
  hearing's controls (hearing starts with `up` when the config turns it on).

`up` and `down` are in. Coming and going is part of the agent living at its own pace, and `up` only logs in from
the login the person saved: it never handles a password, and when the login needs the person (new terms of
service, an MFA code, an account not flagged as a scripted agent) it stops and says so. The person can always
`ibasho down` from a terminal.

The client may give the agent other tools besides these, such as a shell or file access. Then the advice in
[setup.md](setup.md) under "Another harness" applies: keep config.toml, the data folder's `state\` and the
viewer's folders out of its reach, because the caps live there.

**One agent per install.** Don't run an MCP agent and the keeper's sessions on the same install at once: they'd
both be driving one avatar.

## Memory

What the agent remembers between sessions is up to its client. If the client gives it a folder it can read and
write, the life template ([../life-template/README.md](../life-template/README.md)) is a good start; point the agent
at its README when it first wakes. That README talks about the keeper and the `ibasho` command, so tell the agent
once that it has these tools instead.
