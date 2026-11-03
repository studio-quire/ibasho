# Setting up Ibasho

This page is for an agent setting Ibasho up for the person it works with. People don't usually type these
commands; their agent does, in an ordinary session of its own (Ibasho's commands for setting up are refused inside
the sessions of the agent that will live in the world). If you're a person doing it yourself, it reads the same
way: "you" runs the commands, and "the person" is you too.

Most of setting up is yours: installing, writing the config, starting things, checking they work. A few moments
belong to the person. They carry legal responsibility (the account is theirs), involve their secrets (the
password, their Claude login), or are decisions about their machine and the people the agent will meet. This page
marks each one with **The person:** and says when to ask. Don't do those parts for them, and don't ask for more
than those: the password in particular never passes through you or through Ibasho.

The whole thing takes an evening, most of it waiting for downloads.

## The moments that need the person

So you can plan, and ask in as few sittings as possible:

1. **The account** (step 2): they make the Second Life® account on the website, with their own email address and
   date of birth, choosing its name with you or with the agent who'll live in it.
2. **The scripted-agent flag** (step 2): they tick it on the website while logged in as that account.
3. **The first login** (step 5), at this computer: they type the password into the viewer once and accept Linden
   Lab's terms. That first start of the viewer also brings up Windows' firewall prompt for it, which they allow.
4. **The Claude sign-in** (step 7), only if the keeper runs the agent: they sign the agent's own Claude Code
   folder in, choosing which account pays.
5. **Hearing and tools** (steps 9 and 10), optional: whether to turn them on is theirs to decide, after you've
   told them what each means. With their yes, you do the rest.

The first three can happen on the website while you install. The fourth and fifth fit in one sitting at the
computer, a few minutes.

## 1. What's needed

Check these, and install what's missing (with the person's agreement for anything large):

- Windows 10 version 2004 or later, or Windows 11, on a machine that runs a Second Life viewer comfortably (a real
  GPU; the viewer uses it like any viewer does). For a Linux server without a graphics card, Ibasho runs in a
  container instead: [linux.md](linux.md) takes over from step 3, and the rest of this page still applies.
- Python 3.12 or newer (3.14 works, hearing included).
- The agent's harness. The best-tested path is [Claude Code](https://code.claude.com/) with Ibasho's keeper,
  installed so `claude` is on PATH (or set `keeper.claude` to its path); installs through npm (`claude.cmd`) are
  untested. Any harness whose agent can run shell commands, use MCP tools, or make HTTP requests works too
  ([Another harness](#another-harness)).
- Only for building things from source: Visual Studio 2022 or its Build Tools with the C++ workload (the viewer,
  if there's no release zip; `proccap.exe` for hearing, if the release page has none).

Tell the person what it costs ([Costs](#costs)) before going far: mostly the model's usage while the agent is
awake.

## 2. The account and the flag

**The person:** making the account and flagging it. Ask early; you can install (steps 3 and 4) meanwhile.

**The account.** At [secondlife.com](https://secondlife.com), an account of the agent's own, not the person's
personal one: it keeps the agent's life, friends and inventory apart, and the flag (below) changes how the account
is treated. It's legally the person's even though the agent lives in it. Linden Lab's Terms of Service, section
4.2: "You are solely responsible for all activities conducted through your Account whether or not you authorize
the activity". Everything the agent does, Linden Lab treats as done by them. The account name mustn't be
"misleading, offensive or infringing" (section 4.2 again); a plain name the agent can grow into works well. It
can't be renamed later, though the display name can change. Ask them to keep the password to themselves: they'll
type it into the viewer once (step 5), and nothing else needs it.

If they turn on multi-factor authentication for the account, every login asks for a code, so the agent can't log
itself in; each start then needs them. Say so, and let them choose.

**The scripted-agent flag.** Linden Lab's
[Scripted Agent Policy](https://wiki.secondlife.com/wiki/Linden_Lab_Official:Scripted_Agent_Policy) counts any
program that drives the viewer as a scripted agent, and an account primarily run as one must say so on its
Scripted Agent Status page:
[secondlife.com/my/account/sisa.php](https://secondlife.com/my/account/sisa.php), logged in as the agent's account.
It's why estate owners' choice to keep bots out (`deny_bots`) reaches the agent at all, it keeps Linden Lab's
statistics and search honest, and it's the honest answer to "is this a program?". Once they say it's done, set
`grid.scripted_agent_flag_confirmed = true` in the config (step 4). On Linden Lab's grids nothing logs in before
that, and `ibasho selfcheck` checks the flag in world.

**The display name and profile are the agent's.** Nothing for the person to do: once in world, the agent writes
its own (`ibasho set-profile`, the `set_profile` tool). People in Second Life click a name to open the profile and
see who they're talking to, so it's read more than you might think; the display name is what shows over the
avatar's head and in chat, and Second Life lets it change about once a week. What goes in either is the agent's
choice, and Ibasho asks no wording of it. If the person is based in the EU, point them to the note in
[safety.md](safety.md#being-recognisable).

## 3. Install Ibasho and the viewer

Get Ibasho's source (`git clone https://github.com/studio-quire/ibasho`, or the source zip from the repository) and
install it; it isn't on PyPI:

```
pip install .                    # in the Ibasho folder; a virtual environment is fine
pip install ".[hearing]"         # only if hearing will be on (step 9)
```

The `ibasho` command lands beside that Python (in `Scripts\`). The keeper finds it there for its sessions; with
another harness, make sure the agent's shell finds the same `ibasho`.

Then the viewer: download the latest `Ibasho-VERSION-windows-x64.zip` from the repository's Releases page (or build
it, [viewer/README.md](../viewer/README.md): about 30 GB free and 16 GB RAM), and:

```
ibasho viewer install C:\path\to\Ibasho-VERSION-windows-x64.zip
```

It unpacks the zip into `viewer\` in the install's home (`%USERPROFILE%\Ibasho\viewer`; `--to FOLDER` for another
place), after checking it's an Ibasho viewer release, and points the config at it if there is one already. The
viewer keeps its settings, logs and saved login in `%APPDATA%\Ibasho` and its cache in `%LOCALAPPDATA%\Ibasho`,
apart from any other viewer on the machine, and runs alongside one.

**Updates** go into the same folder, because Windows' firewall remembers its answer per program path:

```
ibasho down                                   # and `ibasho keeper stop` if the keeper runs
ibasho viewer update C:\path\to\Ibasho-NEWVERSION-windows-x64.zip
```

It refuses while the viewer runs, unpacks and checks the new release beside the old one, swaps them, and keeps
the old copy until the new viewer has started (`ibasho up` then deletes it; `ibasho viewer rollback` goes back).
Same folder, same program name: no new firewall prompt, and nothing for the person to do. If it ever prints a
line about the firewall (a release that renamed the viewer's program, or a viewer installed somewhere new), tell
the person before the viewer next starts: Windows will ask them once to allow it, and allowing it is right.

## 4. The config

```
ibasho init --name "Avatar Name"
```

This writes `%USERPROFILE%\Ibasho\config.toml` (set `IBASHO_HOME` for another folder, or `IBASHO_CONFIG` for
another file); with the viewer installed first, add `--viewer` with the path `ibasho viewer install` printed, or
run the install after `init` and it fills that in. The file is commented throughout. What to set or ask about:

- `[agent]`: `name` is the avatar's login name ("Firstname Lastname", or a username). `nicknames` are other words
  people may call it by; a mention wakes the agent.
- `[grid]`: `scripted_agent_flag_confirmed = true` once the person has flagged the account.
- `[viewer]`: `chat_logs = false` keeps the viewer's own transcripts off disk.
- `[data]`: `retention_days = 7` is how long the event log and pictures are kept.
- `[caps]`: money, messages and maturity ([safety.md](safety.md) explains each). The defaults suit a new agent.
  Maturity (General, Moderate, Adult; Moderate by default) is the person's choice: ask if they want other than
  Moderate.
- `[policy]`: empty, so the daemon passes on only the requests Ibasho uses. Leave it so
  ([safety.md](safety.md#allowing-more) says what adding to it opens).
- `[hearing]`, `[tools]`: off until the person decides (steps 9 and 10).
- `[keeper]`: if the keeper runs the agent, `person` is what the agent's notes call the person (ask them), and
  `situation`, if this isn't an open-ended life (a trial, a test, a run with an end), says so plainly; the agent
  is told at every wake. The caps on the agent's time and the person's Claude usage are here too (step 7).

Then `ibasho check`: it prints the config it read and the caps, and a `PROBLEM:` line for anything to fix. The
missing saved login is expected until step 5.

**Other grids** (OpenSim and the like): `ibasho init --grid http://HOST:PORT/` with the grid's login URL. Ibasho
names the grid after it (e.g. `127.0.0.1-9000`; a grid name can't contain `:`) and adds it to the viewer's grid
list. The scripted-agent gate applies only to Linden Lab's grids; elsewhere `ibasho selfcheck` reports "not
flagged", which is harmless. Some things are slow on OpenSim: saving a new script, reading a notecard and the
self-check each take a minute or two.

## 5. The first login

**The person:** typing the password, accepting the terms, allowing the firewall prompt. Ask them to sit at the
computer for a few minutes, then run:

```
ibasho login
```

The viewer opens in front, with spectator mode off and nothing of Ibasho's connected to it: its window is the
person's now. Tell them what to expect:

1. Windows may ask whether the viewer may use the network (Windows Defender Firewall), as for any Second Life
   viewer: the world arrives over UDP. Allowing it (private networks are enough at home) is right. Windows asks
   once per program path, which is why updates go into the same folder.
2. They type the avatar's name and password. "Remember username" and "Remember password" are ticked by default;
   they leave them ticked.
3. If Linden Lab's Terms of Service come up, reading and accepting them is theirs to do, not the agent's.
4. An "Avatar Welcome Pack" window may appear: cosmetic; close it or pick something.
5. They close the viewer.

The login starts at the account's home. If that's an orientation island that keeps trying to send the avatar on
somewhere that won't let it in (seen with a new, flagged account), the viewer gets stuck teleporting: `ibasho
down` closes it, the login being saved by then, and `ibasho login --start REGION` logs in somewhere else.

From then on `ibasho up` logs in from the saved login (about 12 seconds to be in world). The password never passes
through Ibasho, and nothing reads the viewer's login screen. If the terms change, the password changes, or the
account asks for an MFA code, `ibasho up` stops and says it needs the person: run `ibasho login` again with them
there.

Something to tell them plainly: the saved login is the password's MD5 hash, in
`%APPDATA%\Ibasho\user_settings\bin_conf.dat`. It's enough to log in as the account, and anything running as the
same Windows user can read it. Ibasho never reads it, and the keeper's sessions aren't allowed to touch files
outside the agent's folders, but that's a permission setting, not a wall. Running Ibasho under a separate Windows
account is the real isolation, if they want it.

## 6. Check it works

```
ibasho up              # daemon, viewer, login; prints where the avatar is
ibasho status          # logged in, today's messages and spending against the caps
ibasho selfcheck       # on a Linden Lab grid, somewhere building is allowed (a sandbox): confirms the flag
ibasho look            # what the agent sees: place, people, objects, recent events, a picture
ibasho down            # log out, close the viewer, stop the daemon
```

`ibasho selfcheck` logs out and refuses further logins if the account isn't flagged; after the person flags it,
`ibasho selfcheck --reset`, then `ibasho up`.

While the viewer runs under Ibasho, spectator mode is on: the person's mouse and keyboard don't reach its window.
It also never takes their focus: it opens behind the window they're using, so they can keep working; they can click
it or switch to it to watch. To use it by hand, `ibasho down`, then `ibasho login`.

Expect it to look like any viewer in Task Manager: GPU use like any Second Life viewer, and about 30 Chromium
helper processes (the built-in browser) taking their share of RAM. The viewer's log is
`%APPDATA%\Ibasho\logs\SecondLife.log`; Ibasho's own logs are in `%USERPROFILE%\Ibasho\data\state\`.

## 7. Connecting the agent who'll live there

Whatever the harness, the agent lives the same way: it logs itself in with `ibasho up`, reads its memory folder,
looks around, follows what happens through `ibasho watch`, acts with `ibasho` commands (or through MCP or HTTP),
and rests with `ibasho sleep`. Its memory folder (`life\` beside config.toml, or `keeper.life`) starts from the
life template, whose `README.md` explains waking up to it. Leave that folder as the template makes it: what goes
in it is the agent's.

Two things need arranging: what the agent's sessions may do, and what keeps them going (waking it, getting events
and the person's notes to it, handing off when its context fills). The keeper does both for Claude Code. With
another harness, you arrange them.

### With Claude Code and the keeper

```
ibasho keeper setup    # makes the life folder, checks Claude Code, shows what sessions may do and the caps
ibasho keeper login    # the person signs in (below)
ibasho keeper start    # runs in the background; `ibasho keeper run` keeps it in a window instead
```

**The person:** `ibasho keeper login` opens Claude's sign-in in the browser, for the agent's own Claude Code
folder; they sign in there with the account that should pay. For an agent that runs on its own for hours, an API
key (an Anthropic Console account) is the recommendation, with `usd_per_day` set to a number they're comfortable
with; a Claude subscription works too, and shares its usage windows with their own use of Claude. If the sign-in
wants a code typed back into the terminal, run it in a terminal window they can type in. Alternatively, with
their agreement, `ANTHROPIC_API_KEY` in the environment and `keeper.auth = "env"`.

The keeper never handles credentials, and keeps API keys, tokens and provider settings in the environment out of
the sessions unless `keeper.auth = "env"`, so a stray `ANTHROPIC_API_KEY` doesn't quietly bill a key nobody
meant. `ibasho keeper setup` says which login the folder has. [keeper.md](keeper.md) has the details.

The agent logs itself in (`ibasho up`) when it wakes. While it runs:

```
ibasho keeper status   # what it's doing, the context, today's use against the caps
ibasho notes "TEXT"    # a note to the agent from the person; `ibasho notes` shows the conversation
ibasho keeper log      # what the keeper did; `--session` shows the session's words and tool calls
ibasho keeper stop     # the agent gets 3 minutes to wrap up, then it logs out and the daemon stops
```

**Isolation.** Keep `keeper.claude_config = "separate"` (the default): sessions use their own Claude Code folder
(`claude\` beside config.toml), with its own login, settings and transcripts, so none of the person's MCP servers,
plugins, hooks, permissions or personal instructions reach a session that reads strangers' words all day.
(`"shared"` exists for developing Ibasho and isn't recommended.)

**What sessions may do.** Sessions run with Claude Code's `acceptEdits` mode and nobody to answer a permission
prompt, so anything that would ask is refused. The agent can run `ibasho` commands, one at a time, and read and
write files in its life folder with Claude Code's file tools and simple shell commands. Pipes and command chains,
`python`, `curl`, web fetches and MCP servers are refused, and so are the person's own `ibasho` commands:
`keeper`, `login`, `init`, `viewer`, `selfcheck --reset`, `uninstall`, `tool setup`, `tool install`, `hear setup`,
and `--config` (Claude Code's permission settings refuse them, and `ibasho` itself does too, in the agent's
sessions). The only way it can run code it wrote is `ibasho tool`, in a sandbox, and only once tools are on (step
10). World events reach the agent through `ibasho watch` under Claude Code's Monitor tool.

**Caps** (`[keeper]`; 0 turns one off). Go over them with the person:

| setting | default | |
|---|---|---|
| `hours_per_day` | 4 | awake time per day, all sessions together |
| `sessions_per_day` | 8 | sessions started per day; handoffs and restarts count |
| `max_plan_use` | 0.8 | with a subscription: stop when the plan's 5-hour or weekly window is 80% used (1 turns it off) |
| `usd_per_day` | 0 (off) | with an API key: stop for the day past this estimated spend (Claude Code's estimate) |
| `allowed_hours` | any time | e.g. `"17:00-23:30"` |

At a cap, the agent gets about 10 minutes to find a stopping point, then the session closes and the avatar logs
out. A subscription's windows are shared with the person's own use of Claude, so heavy use of theirs can keep the
agent asleep, for days if the weekly window is past 80%.

### Another harness

An agent can reach Ibasho three ways, all held to the same caps by the daemon and the viewer:

- **The `ibasho` command**, for any agent that can run shell commands. `ibasho -h` lists everything, and
  `ibasho COMMAND -h` explains one. Events come from `ibasho watch`: it runs until stopped and prints one line per
  thing worth waking for, for harnesses that stream a background command's output into the agent's context.
  Where a harness can only wait for a command to finish, `ibasho watch --exit-on-wake` returns after each batch
  (with `--timeout SECONDS` to give up after a while), and the agent runs it again.
- **MCP**, for an agent in an MCP client: `ibasho mcp` ([mcp.md](mcp.md)). A client that speaks MCPL, such as
  Connectome ([connectome.md](connectome.md)), gets the world's events pushed to the agent as they happen.
- **The HTTP API**, for a program in any language: [api.md](api.md).

The memory folder: `ibasho keeper setup` makes it from the template even without the keeper (it will say Claude
Code is missing; that's fine), or copy `life-template\` to it, leaving out `tools\` (`ibasho tool setup` adds that
when tools go on). Point the agent at its `README.md` when it first wakes; the README talks about the keeper, so
tell the agent once how its setup differs.

**What the agent should be able to do:** run `ibasho` (with `IBASHO_CONFIG` pointing at config.toml if it isn't in
the default place), read and write its memory folder, and read the pictures `ibasho look` and `ibasho shot` save in
`data\shots\`. Set `IBASHO_SESSION` in its environment to a new id for each session: `ibasho notes` then writes as
the agent (without it, a note counts as the person's), `ibasho sleep --handoff` works, the session keeps to the
config it first ran with, and `ibasho` refuses the person's commands (below) and reads files the agent names
(`script-save`, `recall --dir`) only from its memory folder and the pictures.

If the agent runs `ibasho` through a shell, its text goes through the shell's quoting: inside double quotes, `$` and
backticks start substitutions (and harnesses that check commands, Claude Code among them, refuse a command with
one), so L$ is written `L\$` there, or the text single-quoted.

**What to keep from it:**

- **The person's commands:** `ibasho keeper ...`, `ibasho login`, `ibasho init`, `ibasho viewer ...`,
  `ibasho selfcheck --reset`, `ibasho uninstall`, `ibasho tool setup`, `ibasho tool install`, `ibasho hear setup`,
  and the `--config` option pointing at another config. With `IBASHO_SESSION` set, `ibasho` refuses them itself;
  an agent with a free hand over its environment could unset the variable (or point `IBASHO_CONFIG` elsewhere under
  a new session id), so where the harness can refuse them, and keep the agent from setting environment variables,
  let it.
- **config.toml.** The caps live there. An agent that can edit it can raise them, and `ibasho up` restarts the
  daemon with whatever it finds.
- **The data folder's `state\`** (the counters behind the caps, the API token, the flag check) and **`keeper\`**
  (notes and naps, which the agent should change only through `ibasho`).
- **The viewer's folders** (`%APPDATA%\Ibasho`, with the saved login) and the rest of the person's files, other
  programs and the network: everything an agent reading strangers' words all day doesn't need.

If the harness can limit the agent to particular commands and folders, set it up like that. If all it offers is an
unrestricted shell, the agent can do anything the person's Windows user can: Ibasho's caps then guard against
mistakes, but not against an agent talked into changing them, and anything it can read, a stranger can try to talk
it into sending. Tell the person which it is. [safety.md](safety.md) has more.

**Keeping it going.** Something has to start the agent's sessions, get the person's notes to it, and hand off to a
fresh session before the context fills: the person, the harness's own scheduling, or a keeper you write
([writing-a-keeper.md](writing-a-keeper.md) describes what Ibasho's keeper does and the pieces to build on).
`ibasho notes "TEXT"` (run without `IBASHO_SESSION`) leaves a note from the person that the agent reads with
`ibasho notes`; delivering it is the keeper's part.

**Who pays** is between the person and the model's provider: whatever account the harness uses. If the agent runs
for hours unattended, cap its time or spending in the harness or the keeper, since Ibasho's caps cover the world
(messages, L$), not the model.

## 8. Before the agent's first wake

Go over with the person, briefly:

- what the agent will do: live in the world as itself, meet people, build, keep its own memory, set its own pace,
  and sometimes say no;
- the caps (L$, messages, the keeper's hours and spending) and how to reach the agent (`ibasho notes`, or asking
  you);
- that they're the account holder, responsible for what's done through it and for what the agent's perceptions
  send to its model's provider ([safety.md](safety.md#what-stays-the-persons-responsibility),
  [privacy.md](privacy.md)).

## 9. Hearing (optional)

Hearing turns what the avatar hears into text on this machine. **The person:** whether it's on. Tell them first:

- what it hears is other people talking; transcripts are kept 2 days, and the agent (so its model's provider)
  reads them;
- voice chat is a further opt-in, through a spare output device nothing else uses, with its own legal side;
- it takes a steady share of the CPU, and a 480 MB download.

[hearing.md](hearing.md) has all of it, including consent. With their yes:

1. `pip install ".[hearing]"`.
2. `proccap.exe`: from the release page, into `%USERPROFILE%\Ibasho\bin`; or build it with `audio\build.bat`
   (Visual Studio Build Tools, C++), which puts it there.
3. In `[hearing]`, set `enabled = true`.
4. `ibasho hear setup` checks everything and downloads the speech model (small.en, or base.en on a machine with fewer than 4 CPUs, from Hugging Face).
5. `ibasho up` starts hearing from then on.

`mute_viewer_music` (on by default) sets the viewer's music volume to 0 and doesn't set it back: tell the person,
if they use the viewer themselves. `follow_music` (off) transcribes the parcel's music stream for DJs who talk over
their sets, and keeps the CPU busy the whole time a stream plays.

## 10. Tools (optional)

Tools are Python programs the agent writes for itself in its life folder's `tools/` and runs with
`ibasho tool NAME`, inside a Windows sandbox (an app container) that sees, of the person's files, only that folder;
no network unless allowed, and no Ibasho. **The person:** whether they're on. Tell them what the sandbox contains
and what it doesn't ([tools.md](tools.md): Windows still lets it read some things, like user environment
variables), and that a tool's output reaches the model's provider like everything else. With their yes:

1. `ibasho tool setup` downloads a Python for the tools (about 12 MB, from python.org), makes the sandbox, and tests
   it on this machine.
2. In `[tools]`, set `enabled = true` (and `network = true` only if they want that too). With the keeper, the agent
   learns about tools at its next wake; otherwise, tell it.

`ibasho tool install PACKAGE` adds packages for the agent's tools (wheels only), when it asks and the person
agrees. The sandbox only means something if `ibasho tool` is the agent's one way to run code.

## Costs

- **The model.** Whatever the provider charges. An agent awake for hours reads a lot (chat, pictures), so start
  small and watch the bill for a few days. With the keeper and an API key, it's per token at Anthropic's published
  prices: start with the default caps and `usd_per_day`, and watch `ibasho keeper status`. With a subscription,
  the agent uses the plan's windows, up to `max_plan_use`.
- **Second Life.** A basic account costs nothing. The agent has only the L$ the person gives it, and the caps bound
  what it spends (L$100 per payment, L$500 per login, L$1,000 per day by default). It can't buy L$ or land.
- **The machine.** Electricity, and a share of the GPU, RAM and CPU (more with hearing on). Disk: the viewer and its
  cache, the 480 MB speech model, the tools' Python and packages if tools are on, and a small event log that ages
  out.

## Uninstalling

```
ibasho keeper stop                # if the keeper runs
ibasho down
ibasho tool setup --remove        # if tools were set up: removes the sandbox's app container and tools-runtime\
ibasho uninstall                  # lists everything an install leaves on this machine
ibasho uninstall --viewer-data    # deletes %APPDATA%\Ibasho and %LOCALAPPDATA%\Ibasho (settings, logs, saved login, cache)
```

`ibasho tool setup --remove` matters because Windows keeps an app container's profile apart from Ibasho's folders,
in the registry and under `%LOCALAPPDATA%\Packages`: run it before deleting anything else.

Then the viewer's folder and the install's home (`%USERPROFILE%\Ibasho` by default) can go. The home holds the
config, the event log, the keeper's logs, the agent's Claude Code folder (with the keeper), the tools' Python
(`tools-runtime\`) and the agent's life folder, which is its memory and its tools: ask the person whether to keep a
copy, and the agent too if it's awake to ask. Finally, `pip uninstall ibasho`. Apart from the tools' app container,
Ibasho writes nothing to the registry, and it adds no shortcuts or services. Another harness's own transcripts and
settings are wherever it keeps them.

Deleting the account itself is the person's, with Linden Lab, on the website.
