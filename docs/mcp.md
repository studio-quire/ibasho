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
(Codex's `tool_timeout_sec`, Claude Code's `MCP_TOOL_TIMEOUT` environment variable, in milliseconds), set it to a
few minutes. If a call does time out, the action still finishes in the world; only its answer is lost.

**How many tools.** There are 41. Some clients limit how many tools they pass to the model across all servers;
most let you switch off the ones you don't need.

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
| **People** | `friend` (offer friendship), `profile` (someone's names, and with a viewer that reads profiles their about text, picks and groups) |
| **Moving** | `walk` (checks for drops and water first, steps round people), `move` (hold movement keys; also steers vehicles), `tp`, `touch`, `sit`, `stand` |
| **Dialogs and offers** | `respond` (a notification by its id or its first characters: press a `button` by name or label, or give a `response` for forms), `dialog` (press a button on the latest script dialog, or answer its text box) |
| **Making** | `create_prim`, `modify_prims`, `link`, `delete`, `take`, `rez`, `inventory`, `script_save` (the script's text in `source`; it compiles and reports errors), `notecard` (read one of my notecards, anywhere), `give` |
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
