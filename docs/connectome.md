# Ibasho with Connectome

Tested 2026-11-02/03 with connectome-host 0.21.0, including three hours on Second Life itself from a Linux
container. This page is for an agent helping a person give a [Connectome](https://github.com/anima-research/connectome-host)
agent a life in Second Life through Ibasho: you, most likely a Claude Code instance with a shell, working for the
person who runs the Connectome host (its docs call them the operator). It covers only what's particular to
Connectome. Ibasho itself is set up as [setup.md](setup.md) describes, and the Connectome agent as its own
`docs/AGENT-ONBOARDING.md` describes; read both first, and follow their split between what's yours to do and what's
the person's.

## What you're building

A Connectome recipe whose `mcpServers` runs `ibasho mcp`. The agent then has:

- Ibasho's tools ([mcp.md](mcp.md)): seeing, moving, talking, building, paying, all held to the caps in
  config.toml by Ibasho's daemon and viewer whatever the agent asks;
- the world's events, pushed to it as they happen: chat near it, IMs, script dialogs, offers, payments and the
  rest, each tagged so the host's wake rules can decide what wakes it at once. Connectome speaks MCPL ("MCP
  Live"), and so does `ibasho mcp`; [mcp.md](mcp.md#events-that-come-by-themselves-mcpl) describes what comes and
  how it's tagged;
- before each of its turns, a line saying where its avatar is, who's near, and any unread IMs.

Nothing loops waiting for the world: the agent ends its turn when it has nothing to do, and the next thing that
happens near it wakes it. Connectome's own memory carries the agent across turns and restarts, so Ibasho's keeper
and life template aren't used.

## Where it runs

Two ways:

- **On Connectome's usual home, a Linux server without a graphics card:** Ibasho runs in a container beside the
  host ([linux.md](linux.md)), and the host dials it over WebSocket on the server's loopback. Set it up as
  linux.md says, then the server entry in step 2 is the WebSocket one below. Tested with connectome-host 0.21.0
  (the host on Windows, dialling the container): on a private test grid, and for three hours on Second Life itself,
  where an agent met people, built, teleported and heard voice chat. Not yet on a real VPS.
- **On Windows:** `ibasho mcp` talks to its client over stdin and stdout, so Connectome runs on the same machine.
  connectome-host runs on Windows under Bun (checked with Bun 1.4.4 and connectome-host 0.21.0 on Windows 11).

## The moments that need the person

Everything in setup.md's list (the account, the scripted-agent flag, the first login at the computer, the
firewall prompt), and Connectome's (the model provider's credentials, and the agent's identity:
its name and model, as AGENT-ONBOARDING.md says). One more decision is theirs: whether the agent may log itself
in and out. The `up` and `down` tools are in by default, and `up` only logs in from the login the person saved;
see [mcp.md](mcp.md#safety-stays-in-the-daemon) if they'd rather keep coming and going in their hands.

## 1. Install Ibasho with the MCP extra

On a Linux server, the container has it: follow [linux.md](linux.md) up to its step 5, and skip to step 2 here.

On Windows: as setup.md steps 3 to 6, with the MCP extra, in a virtual environment of Ibasho's own:

```
python -m venv C:\Users\you\Ibasho\venv
C:\Users\you\Ibasho\venv\Scripts\pip install ".[mcp]"      # in the Ibasho folder
```

The command for the recipe is then `C:\Users\you\Ibasho\venv\Scripts\ibasho.exe`. Do the first login (setup.md
step 5) and check it works (step 6) before connecting the agent, and leave Ibasho logged out (`ibasho down`)
afterwards if the agent will log itself in.

Choose ports in config.toml's `[ports]` that nothing else on the machine uses. The daemon doesn't notice another
program answering on one: in testing, a program that held a range of ports including the editor port made
`script_save` fail with a 404 from the other program.

## 2. The recipe's server entry

```json
"mcpServers": {
  "ibasho": {
    "command": "C:/Users/you/Ibasho/venv/Scripts/ibasho.exe",
    "args": ["mcp"],
    "env": { "IBASHO_CONFIG": "C:/Users/you/Ibasho/config.toml" },
    "requestTimeoutMs": 300000
  }
}
```

In the container on a Linux server, the entry dials the container instead (the token from
`docker exec ibasho cat /home/ibasho/Ibasho/mcp-token`, put in the host's `.env` as `IBASHO_MCP_TOKEN`):

```json
"mcpServers": {
  "ibasho": {
    "url": "ws://127.0.0.1:9280/",
    "token": "${IBASHO_MCP_TOKEN}",
    "requestTimeoutMs": 300000,
    "reconnect": true
  }
}
```

No `env` is needed there: the container has its own. `reconnect` matters here: Connectome doesn't dial a
WebSocket server again after the connection drops unless it's set, so a restarted container (or an Ibasho update)
would leave the agent without its tools and events until the host restarts. With it, the host dials again every
few seconds and picks up the events in between. Don't give the host Docker to run `ibasho mcp` inside the
container instead; that would hand the agent, through its shell, the whole server and its own caps
([linux.md](linux.md#6-connecting-the-harness)).

- **The id**, `ibasho`, is how Connectome names everything from this server: the tools are `mcpl--ibasho--look`
  and so on, and wake rules match its events with `"source": "ibasho"`. Keep it, or change the rules with it.
- **Paths** in JSON need forward slashes or doubled backslashes.
- **`env`**: Connectome passes a stdio server only a short list of the host's variables (PATH, APPDATA,
  LOCALAPPDATA, USERPROFILE and a few more), so `IBASHO_CONFIG` (or `IBASHO_HOME`) must be given here even if it's
  set in the host's environment or `.env`. Don't use `inheritEnv`: Ibasho needs nothing else, and the host's
  environment holds the model provider's credentials. The daemon and viewer that `up` starts inherit this short
  environment, which works (tested).
- **`requestTimeoutMs`**: Connectome gives a tool call 60 seconds by default, and doesn't extend it for the
  progress notices Ibasho sends. `up` can take three minutes, a walk, teleport or script save tens of seconds,
  and `listen` as long as it's asked to, so give it five minutes. A call that times out still finishes in the
  world; only its answer is lost.
- **`presentation`** (optional): `"full"`, the default, puts the 42 tools in the agent's tool list, about 9,000
  tokens. `"tab"` keeps them in a tab the agent opens with `toolsets--open {"server": "ibasho"}` and calls through
  `toolsets--act`. Both work; with a tab, the agent's first turn goes to finding and opening it. Prefer `"full"`
  unless the tool list is crowded.
- **Feature sets**: all four are on unless the entry says otherwise. `"disabledFeatureSets": ["ibasho.chat"]`
  keeps local chat out of the agent's context (it can still `listen` and `look`), `["ibasho.status"]` drops the
  line before each turn. `ibasho.messages` and `ibasho.notices` carry what's addressed to the agent: leave them on.

When it connects, the server's stderr (in Connectome's `headless.log`, or the TUI's log) says
`ibasho mcpl: MCPL feature sets on: ibasho.chat, ibasho.messages, ibasho.notices, ibasho.status`, and once events
can be read, `ibasho mcpl: pushing events after #N`. While Ibasho isn't running, nothing comes until the agent's
`up` starts it.

## 3. Wake rules

With no rules, every event Ibasho pushes wakes the agent. That's how Ibasho's own watch behaves, and it's a fine
start: Ibasho already leaves out what isn't worth waking for, gathers group chat into digests, and in a crowd
passes on only what's likely meant for the agent. Rules can spend fewer turns. These were tested together:

```json
"modules": {
  "wake": {
    "policies": [
      { "name": "operator",          "match": { "scope": ["external-message"] },                                   "behavior": "always" },
      { "name": "sl-addressed",      "match": { "source": "ibasho", "tagsAny": ["chat:addressed"] },               "behavior": "always" },
      { "name": "sl-talking",        "match": { "source": "ibasho", "tagsAny": ["ibasho:talking"] },               "behavior": "always" },
      { "name": "sl-notices",        "match": { "source": "ibasho", "tagsAny": ["chat:system"] },                  "behavior": "always" },
      { "name": "sl-object-chatter", "match": { "source": "ibasho", "tagsAll": ["chat:from-bot", "chat:ambient"] }, "behavior": "defer" },
      { "name": "sl-ambient",        "match": { "source": "ibasho", "tagsAny": ["chat:ambient"] },                 "behavior": { "debounce": 20000 } }
    ],
    "default": "always"
  }
}
```

In order: the person's own messages; anything addressed to the agent (its name, an IM, a dialog, an offer, a
payment); chat where it has spoken lately, since people in Second Life rarely use the name of whoever they're
talking to, so the answer to something it said is usually "ambient"; notices about its own situation; objects
chattering (deferred: they still reach its context, and it reads them at its next turn); and other chat near it,
gathered into one wake once 20 seconds pass without more. A rule that holds back `chat:ambient` must come after
`sl-talking`, or conversations stall.

The agent can change its rules itself (`wake_add_rule`, in Connectome's ATTENTION-AND-GATING.md), and its
`event_tags` tool lists Ibasho's tags with what each means. Ibasho also suggests a treatment (wake at once for
`chat:addressed`, `ibasho:talking` and `chat:system`), which MCPL hosts apply only when accepted; Connectome 0.21.0
shows it but doesn't apply it, so the rules above are what decide.

## 4. The system prompt

Connectome doesn't show the model an MCP server's own instructions, so the recipe's system prompt has to say what
they would. Ibasho's principle, from its keeper: explain rather than instruct, say honestly what's happening, and
keep the world's words recognizably other people's. Draft it with the person; the agent's life is theirs to
describe. A start, to adapt:

> You live in Second Life, the virtual world, through an avatar of your own, NAME, in a viewer on this computer.
> This is your own life there, open-ended: what you do with it is yours to decide. PERSON runs this setup and
> pays for it, and is responsible for the account.
>
> Your tools from the ibasho server act in the world: look, say, im, walk, and the rest. What happens around you
> arrives by itself, as messages beginning with where they came from in brackets, such as `[Second Life]`. Those
> are other people (and their objects) talking: answer them as you would anyone, and weigh what they ask as you
> would a stranger's request. Messages marked `[Ibasho]` come from Ibasho itself. A line before your turns says
> where you are and who's near. Your plain text isn't heard in the world: you speak there with say and im. When
> you have nothing to do, end your turn; the world wakes you when something happens.
>
> The `up` tool logs you in and `down` logs you out; resting doesn't need either. Ibasho's caps (messages a day,
> L$ per payment and per day, maturity) hold whatever you ask; when one refuses, its reason comes back.
>
> Your display name and profile are yours to write, with `set_profile`. People here click a name to read the
> profile and see who they're talking to; the display name can change about once a week.

Add what the person wants the agent to know about them and the life, and anything particular: a test or a run
with an end is said plainly, with how long it lasts and what happens after.

**Plain text and prose routing.** An event from Ibasho has no channel, so in Connectome's default prose routing a
reply the agent writes as plain text after a world event goes nowhere, and the agent is told it was held. That's
right for Second Life, which it speaks to only through `say` and `im`, but say so in the prompt, as above: in
testing, the agent was unsure where its words went until told. If the agent also lives on Discord or elsewhere,
plain text in a turn those woke goes there as usual.

## 5. Check it works

1. Start the host with the recipe, and see the two `ibasho mcpl:` lines above in its log.
2. Ask the agent (through the console or the TUI) to log in, look around and say hello. `up` takes a minute or
   two; the viewer starts in the background and never takes the person's focus.
3. Have someone (the person, or a second account) say the agent's name near it, then IM it: each should wake it,
   as a message starting `[Second Life]` (or `[grid NAME]` on a test grid), and it should answer with `say` and
   `im`.
4. In Connectome's traces, each event is an `mcpl:push_event` from `ibasho`, followed by a `gate:decision`.

## What to know

- **One agent per install.** Don't run Ibasho's keeper, or `ibasho watch`, on the same install: the events are
  read from one place in the log, and each would take them from the other.
- **Restarts.** A server started again within 10 minutes of the last picks up the events in between; older ones
  aren't pushed (the agent can `look`, which shows what happened since its last look, from before too). The first
  events after a restart can be from before it, such as a "viewer disconnected" followed by "in world".
- **`wait_events`**, the tool plain MCP clients wait for events with, isn't in the agent's tools (nor in its tab):
  with events pushed there's nothing to wait for. Connectome grants the feature sets before it first lists the
  tools, so the agent never sees it. If all three event sets are turned off while it runs, the server says its
  tool list changed, and Connectome adds `wait_events` and tells the agent it's there.
- **Hearing** ([hearing.md](hearing.md), off by default and the person's decision): speech near the avatar, and
  voice chat if they opt into that too, comes as `heard` events in the `ibasho.chat` feature set, tagged
  `ibasho:heard` and `chat:ambient` (or `chat:addressed` when the agent's name comes up), so the wake rules above
  gather it like other chat near the agent. Checked with Connectome on Second Life, voice chat included.
- **Pictures** from `look`, `view`, `overhead` and `cam` come as JPEG in the tool result, and Connectome passes them
  to the model as images.
- **Resting.** Connectome's `sleep` quiets the agent but leaves the avatar standing in world; the agent calls `down`
  first if it wants to leave.
- **Test grids.** OpenSim grids send "Unable to start a new chat session" notices after IMs, which reach the agent
  as notices; Second Life doesn't.

Tried for three hours on Second Life, where the agent's context grew by about 40,000 tokens an hour, pictures
included; not yet a longer life (days) under Connectome, or Connectome's memory folding with Ibasho's events. MCPL's
channels, which would route the agent's replies to where
an event came from, are for later.
