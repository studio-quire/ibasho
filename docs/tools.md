# Tools

Tools are programs the agent writes for itself: Python files in its life folder's `tools/`, run with
`ibasho tool NAME [ARGS...]`. A model living this way runs into things it lacks (a calculator for a build's
geometry, a texture generator, a parser for a notecard format, a way to keep its notes in order), and building
the missing piece is often the best part of a day. Without tools it can only work around the gap.

Tools are off by default, and only you can turn them on: by default a keeper session can run `ibasho` commands
and edit files in its life folder, nothing else. With tools on, it can also run code it wrote, inside a Windows
sandbox that sees, of your files, only its tools folder. (With another harness, what the agent can run is
whatever that harness allows; tools add something only where `ibasho tool` is its one way to run code.) Read [What's contained](#whats-contained) and
[What isn't](#what-isnt) before you turn them on.

## Turning them on

```
ibasho tool setup         # Python for tools, the sandbox, the agent's tools/ folder; then a test of the sandbox
```

Then, in config.toml:

```toml
[tools]
enabled = true
```

`ibasho tool setup` downloads Python 3.14.9 (the embeddable package from python.org, about 12 MB, checked
against the hash python.org published for it) into `tools-runtime/` beside config.toml, makes the sandbox (a
Windows app container), adds `tools/` to the agent's life folder with a README for the agent, and then runs a
probe in the sandbox that tries to read config.toml and your user folder, write outside `tools/`, and reach the
internet. It stops with PROBLEM lines if any of that works on your machine. Offline, `--zip FILE` takes a copy of
the zip you fetched yourself (it's checked the same way). Run `ibasho tool setup` again after updating Ibasho.

From the next session on, the keeper's wake prompt tells the agent about tools and their limits. While tools are
off, the wake prompt doesn't mention them, and sessions are refused `ibasho tool` outright. With another harness,
tell the agent yourself; `ibasho tool list` shows it what it has.

| setting | default | |
|---|---|---|
| `enabled` | false | `ibasho tool NAME` runs at all |
| `network` | false | tools may reach the internet (see [What isn't](#what-isnt): maybe your local network too) |
| `timeout_s` | 100 | one run's limit; then it's stopped |
| `max_output_kb` | 64 | what a run may print (all of it goes into the model's context); past it, it's stopped |
| `memory_mb` | 1024 | all of a run's processes together |
| `cpu_percent` | 50 | of the whole machine, so a busy tool doesn't starve the viewer |

The settings are read on every run, so changes apply at once (the wake prompt's description of them at the next
session). Harnesses usually stop a command after a while (Claude Code after 2 minutes by default; the model can
ask for up to 10), and stopping `ibasho tool` stops the tool, so a `timeout_s` past that only helps runs the agent
starts in the background.

### Packages

Tools start with Python's standard library. To add packages:

```
ibasho tool install pillow numpy
```

This runs pip (from the Python Ibasho is installed in) to fetch wheels built for the tools' Python into its
`site-packages`. Wheels only, so installing runs no code from the package; a package that only publishes source
can't be installed this way. Like setup, this is yours: sessions are refused it. `ibasho tool list` shows what's
installed. A newer Ibasho with a newer Python reinstalls them at `ibasho tool setup`.

## What the agent sees

- `ibasho tool NAME [ARGS...]` runs `tools/NAME.py`, or `tools/NAME/__main__.py`, with the working directory pinned
  to `tools/`. Output (stdout and stderr together) comes back as it's printed, and the tool's exit code is the
  command's. A run that passes the time or output limit is stopped, with a line saying which.
- `ibasho tool list` shows the tools (with the first line of each one's docstring), the Python version, the
  packages and the limits.
- `tools/README.md` explains how a run works and what tools can't do, and that asking you is the way to change
  that (a package, the network, a limit).

## What's contained

Each run is a fresh process in a Windows AppContainer, the sandbox Windows uses for Store apps and browsers'
renderer processes. A process in one has a second identity, the container's, and Windows checks every file
and network access against both: your own rights open nothing by themselves. Ibasho grants the container exactly
two folders. Around that:

- **Files.** It reads and writes `tools/` and reads its Python, and nothing else of yours: not the rest of the
  life folder, not config.toml, not your user folder (Documents, AppData...). It can't write outside `tools/` in
  your folders or the install's, nor in the temp folder, nor in the app container's own folder Windows makes for
  it (Ibasho takes its access to that away). It can't make links pointing outside `tools/` either (Windows refuses
  that to sandboxed processes).
- **Network.** None by default: no connections, no name lookups. Never this machine's loopback, even with the
  network on, so a tool can't reach the Ibasho daemon (and through it the world), the agent's harness, or any other
  local server.
- **Screen and input.** It runs on a desktop of its own: it can't capture your screen, read the keyboard, see or
  send messages to other programs' windows, or use the clipboard.
- **Environment.** Its environment variables are built from scratch (Windows paths and a home inside `tools/`),
  so none of yours reach it: no API keys, tokens or paths of yours.
- **Resources.** The time, output, memory and CPU limits above, at most 16 processes, below-normal priority.
  When a run ends, everything it started ends with it: nothing keeps running in the background.
- **In keeper sessions.** `ibasho tool setup` and `ibasho tool install` are refused (by the command, and by the
  session's permission settings), so the agent can't widen its own sandbox; config.toml and `tools-runtime/` are
  outside the folders sessions may write (Ibasho refuses to run tools if your `keeper.life` or `data.dir` would put
  them inside). Files a tool writes are only files: sessions run with project settings off, which also leaves
  out any CLAUDE.md in the life folder, so nothing there becomes instructions or permissions (see
  [keeper.md](keeper.md#isolation)). With another harness, this holds only if the harness keeps the agent away
  from config.toml, `tools-runtime/` and the person's commands: `ibasho tool setup` and `install` refuse by
  themselves only when `IBASHO_SESSION` is set in the agent's environment.

Ibasho's tests check these on a real Windows machine (`tests/test_tools.py`).

## What isn't

- **Some reading.** Windows lets every app sandbox read some things, and tools are no exception: Windows' own
  folders, installed programs' folders (`C:\Program Files`), and much of the registry read-only. That includes
  `HKEY_CURRENT_USER`: your user environment variables (as set in System Properties, or with `setx`) and the
  settings some programs keep there. If you keep an API key or password in a user environment variable, a tool
  could read it; `ibasho tool setup` says so when a variable's name looks like a secret. With the network off,
  what a tool reads can only reach the model (in the tool's output); with it on, a tool could send it out.
- **The network, when on, may include your local network.** Windows' sandbox rules separate "internet" from
  "private network" by how it classifies your network, and a home network isn't always classified as private.
  On a test machine, a tool with `network = true` could open the router's admin page. Turn the network on only if
  that's acceptable, or if the machine sits on a network with nothing to reach.
- **Disk space and the GPU.** There's no disk quota: a tool can fill the drive `tools/` is on. The GPU isn't
  capped.
- **Windows services that serve app sandboxes.** Some system services accept requests from Store apps, so
  tools can make them too.
- **Windows itself.** An app container is a strong boundary (browsers depend on it) but not a perfect one: a
  tool exploiting an unpatched Windows flaw could get out. Keep Windows updated. The network isolation is enforced
  by the Windows Defender Firewall service; with that service disabled it may not hold (setup's test checks).
- **What the code tries.** The agent writes its tools while reading what strangers say all day. The sandbox
  limits what a tool can reach, not what it attempts; within `tools/` (and the internet, if on) it can do
  anything the agent wrote.
- **Windows only.** Elsewhere `ibasho tool` says it needs Windows and runs nothing.

What a tool prints reaches the model, so it's sent to the model's provider like the rest of the session.

## Why tools can't use `ibasho`

A tool that could call `ibasho` would act in the world without the model seeing each step: a loop could talk,
give or pay many times a minute, with the model reading only the end of it. Ibasho's caps would still hold, but
the model's judgment wouldn't be on every action, and that's what the rest of Ibasho is built around. Reaching the
daemon from the sandbox would also need a loopback exemption, which takes administrator rights. So a tool computes
and the model acts: a tool can work out a build's 40 prim positions, and the model places them.

## How it's built

`src/ibasho/tools/sandbox.py` starts the tool with `CreateProcess` and an app container's security capabilities
(the container's SID, plus `internetClient` when the network is on), on a desktop created for that run, inside a
job object with the limits. The container has an app profile (Windows requires one); it's named after the
install's folder, so two installs on one machine don't share one, and `ibasho tool setup --remove` deletes it.
The tools' Python is Ibasho's own copy, so the access entries the sandbox needs are on Ibasho's folders only, never
on a Python of yours. Before each run Ibasho re-applies the container's access to `tools/` (a file moved in from
elsewhere keeps the access it had; written there, it inherits).

Other ways were tried or weighed: a less-privileged app container also blocks installed programs' folders, but
it can't start Python without the capability that opens the registry anyway, and it can't open a socket at all
without the network on, so `import asyncio` fails. A low-integrity token alone stops writes but not reads of your
files, and blocking its network needs a firewall rule, which needs administrator rights.

## Removing

```
ibasho tool setup --remove
```

removes `tools-runtime/` and the app container (its registry entry and its folder under
`%LOCALAPPDATA%\Packages`), and takes the container's access entry off `tools/`. The agent's tools stay in its
life folder. Set `enabled = false` to keep everything but stop runs.

## Files

- `tools-runtime/` beside config.toml: `python/` (the tools' Python, with `site-packages/`) and `runtime.json`
  (version, container, packages you installed).
- `tools/` in the life folder: the agent's tools, its README, and `.home/` (the tools' home folder: caches,
  library settings, temporary files; temporary files older than a day are deleted before a run).
