# The daemon's HTTP API

Everything Ibasho does in the world goes through one local daemon: the `ibasho` command is a client of it, and your
own program can be one too, in any language that can make an HTTP request. This page describes the API as the
code implements it (`src/ibasho/daemon.py`, with the rules in `src/ibasho/policy.py`), so you can connect an agent
without going through the `ibasho` command.

Ibasho is early software, and this API can change between versions. `GET /status` reports the version.

## What the API covers, and what stays with the `ibasho` command

The API is for living: seeing, moving, talking, building, paying, and reading what happens. It doesn't start
anything. Starting and stopping stay with the `ibasho` command, which the person runs or the agent runs from a
shell:

- `ibasho up` starts the daemon (if it isn't running), starts the viewer, logs in from the saved login and waits
  until the avatar is in world. `ibasho down` logs out, closes the viewer and stops the daemon.
- `ibasho daemon` runs the daemon in the foreground, for watching its log while you develop.
- The person's one-time steps (`ibasho init`, `ibasho login`) are in [setup.md](setup.md). `ibasho selfcheck`, the
  in-world check of the scripted-agent flag, is for either of them (the MCP server has it as a tool); clearing a
  failed check (`--reset`) is the person's.

While the daemon runs, the API answers whether or not a viewer is connected. Most viewer requests are refused
until the avatar is logged in.

## Finding the daemon

The daemon listens on `127.0.0.1` only, on the HTTP port in the config (`[ports] http`, default 9270).

- **The config file:** `IBASHO_CONFIG` if set, else `config.toml` in `IBASHO_HOME`, which defaults to
  `%USERPROFILE%\Ibasho`. In an agent's session (`IBASHO_SESSION` set), `ibasho` keeps to the config the session
  first ran with, whatever these say later.
- **The data folder:** `[data] dir` in the config (a relative path is from config.toml's folder), or `data`
  beside config.toml when that's empty.
- **The token:** the file `state\token` in the data folder (by default `%USERPROFILE%\Ibasho\data\state\token`):
  one line of 43 URL-safe characters. The daemon writes it the first time it starts and reuses it after that, so it
  only changes if someone deletes the file. Until the daemon has run once, there's no token.

Anyone who can read the token can do everything the API allows, within the caps. Treat it like the `ibasho` command:
give it to the agent's harness, and to nothing that shouldn't drive the avatar.

From Python, `ibasho.api.Api(ibasho.config.load())` does all of this (and retries message-rate refusals); the CLI's
modules in `src/ibasho/cli/` show it in use.

## Making a request

The daemon checks every request before anything else:

- `Authorization: Bearer TOKEN` on every request, GET included. Without it, or with the wrong token:
  401 `{"error": "missing or wrong install token"}`.
- The `Host` header must be `127.0.0.1` or `localhost` (any port). Anything else is refused with 403
  `{"error": "local scripts only"}`, so a web page can't reach the daemon through DNS rebinding. `[::1]` is refused
  too: connect to `127.0.0.1`.
- A request with an `Origin` header, or a `Sec-Fetch-Site` header other than `none`, is refused with the same
  403: that's how a web browser is kept out (browsers send `Sec-Fetch-Site` with every request, and `none` only
  for one the person typed in, which can't carry the token). Other `Sec-Fetch-` headers are fine: Node's built-in
  `fetch` sends `sec-fetch-mode: cors`, and works. curl, Python's `urllib` and `requests`, PowerShell's
  `Invoke-RestMethod` and Go's `net/http` send none of these.
- POST bodies are JSON objects with `Content-Type: application/json`. Another content type gets 415
  `{"error": "JSON only"}`; a body that isn't JSON, or isn't an object, gets 400. Bodies can be up to 50 MB.

Responses are JSON. Errors are `{"error": "..."}` with a 4xx or 5xx status, sometimes with more fields (below).
The exceptions are a path that doesn't exist (404) and a wrong method (405), which come back as plain text.

```
curl -s -H "Authorization: Bearer $(cat ~/Ibasho/data/state/token)" http://127.0.0.1:9270/status
```

A minimal client in Python, standard library only:

```python
import json, pathlib, urllib.request

TOKEN = (pathlib.Path.home() / "Ibasho" / "data" / "state" / "token").read_text().strip()

def call(method, path, body=None, timeout=30):
    req = urllib.request.Request("http://127.0.0.1:9270" + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:    # 4xx/5xx raise urllib.error.HTTPError
        return json.loads(r.read())

me = call("POST", "/cmd", {"pump": "LLAgent", "data": {"op": "getPosition"}})
call("POST", "/cmd", {"pump": "LLChatBar", "data": {"op": "sendChat", "message": "hello", "channel": 0},
                      "wait": False})
```

## Endpoints

| | |
|---|---|
| `GET /status` | the daemon, the viewer, the login, today's counts, the caps |
| `POST /cmd` | one request to the viewer, through the safety rules |
| `GET /events` | the event log, and waiting for what happens next |
| `POST /event` | add an event of your own (side channels: hearing, a keeper) |
| `POST /listen` | relay another of the viewer's event pumps into the event log |
| `POST /rpc` | the viewer's script editor: read and save scripts in objects |
| `POST /forget` | take a person, or some kinds of event, out of the event log |
| `POST /shutdown` | stop the daemon |

### GET /status

Always answers, viewer or not. `ibasho status --json` prints the same object (or `{"daemon": false}` when the
daemon isn't running).

| field | |
|---|---|
| `version` | Ibasho's version |
| `connected`, `connected_at` | a viewer is connected over LEAP; since when (Unix seconds, or null) |
| `viewer` | `"ibasho"` (the Ibasho viewer, with its APIs), `"stock"` (another LEAP-capable viewer), or null |
| `viewer_pid` | the viewer's process id |
| `login_state` | the viewer's startup state (`STATE_STARTED` once in world, `STATE_LOGIN_WAIT` at the login screen) |
| `logged_in`, `login_failed` | in world; the last login attempt fell back to the login screen |
| `logged_out` | empty, or what the grid said when it dropped this login: the viewer is still open (it waits for someone to press OK before quitting) and still says it's in world, but isn't. `ibasho up` restarts it |
| `self` | `{id, display_name, username}` of the avatar, once logged in (`ibasho status` prints them too) |
| `grid` | the grid the viewer is on |
| `gate` | empty, or why everything is refused (below) |
| `seq` | the newest event's sequence number |
| `listening` | the viewer pumps the daemon relays |
| `ops` | with the Ibasho viewer: `{API: [op names]}` for `IbashoAgent`, `IbashoIM`, `IbashoObjects`, `IbashoBuild` |
| `messages_today`, `spent_today`, `spent_session` | counted against the caps: messages and L$ today (Second Life time), L$ this login |
| `caps` | `max_payment`, `max_session_spend`, `max_daily_spend`, `max_upload_cost`, `messages_per_day`, `messages_per_minute`, `maturity` |
| `name`, `name_words` | `agent.name` from the config; the words that count as the avatar's name (its name's parts and nicknames) |
| `probe_channel`, `config_mtime` | for the CLI: the probe attachment's channel; when the config the daemon read was last changed |

The daemon reads the config once, when it starts. `ibasho up` restarts it when config.toml has changed since.

### POST /cmd

One LEAP request to the viewer:

```json
{"pump": "IbashoAgent", "data": {"op": "people", "dist": 30}, "timeout": 15, "wait": true}
```

- `pump`: the viewer's API (event pump) name: the Ibasho viewer's `IbashoAgent`, `IbashoIM`, `IbashoObjects`,
  `IbashoBuild`, or a stock one (`LLAgent`, `LLChatBar`, `LLNotifications`, `LLViewerControl`, `LLViewerWindow`,
  `LLInventory`...). Only the APIs and ops on the daemon's list pass ([the safety rules](#the-safety-rules));
  the viewer has more, which are refused unless the person allows them. `_command` means LEAP's own command
  pump: `getAPIs` lists the viewer's APIs, `getAPI {"api": NAME}` lists one API's ops with their help.
- `data`: the request map, with `op` and its arguments. The ops and their arguments are in
  [viewer/README.md](../viewer/README.md#apis) for the Ibasho APIs, and in `getAPI` for every API.
- `timeout`: seconds to wait for the viewer's reply, default 15, at most 600. Anything but a number gets 400.
- `wait`: default true. With `false` the daemon sends the request and answers `{"posted": true}` at once.

What comes back:

- The viewer's reply map (its LEAP `reqid` removed), with status 200. **An op that failed in the viewer still
  answers 200**, with `{"error": "..."}` in the body: check for it.
- 504 `{"error": "timeout"}` if no reply came within `timeout`. Many of the viewer's stock ops never reply, even
  when they ran, `LLChatBar sendChat` and `LLAgent startAutoPilot` among them. Send those with `"wait": false`
  (the `ibasho` commands also send `requestTeleport`, `requestTouch`, `requestStand` and `LLNotifications respond`
  that way). The Ibasho APIs always reply, but a viewer that's slow (a heavy scene, the graphics card busy with
  another program, a frame-rate limit like its `YieldTime` setting) can take far longer than usual: snapshots
  have taken over 15 seconds. The `ibasho` commands wait 30 seconds by default, 90 for a snapshot.
- 503 `{"error": "viewer not connected"}` (or `"viewer disconnected"` mid-request).
- A refusal from the safety rules: `{"error": REASON, "refused": true, "rule": RULE, "retry_after": SECONDS}`
  with 403 (not allowed from here), 409 (the viewer's state: not logged in yet, or the gate) or 429 (a message
  cap). `rule` says which rule: `allowlist` (not on the daemon's list; the reason says how the person can allow
  it), `arguments` (on the list, but not with these arguments), `gate`, `login`, `messages`, `money`, `maturity`,
  `settings`, `files`, `notifications`, `local pages` or `daemon`. `retry_after` is non-zero only for the per-minute message rate: wait that long and send again.

The safety rules run before the daemon looks for a viewer, so with no viewer at all most requests get the rules'
409 "the viewer isn't logged in yet" rather than 503.

Local chat lines (`LLChatBar sendChat`) go out one at a time, at least 1.2 seconds apart: the viewer silently drops
a line that comes within a second of the one before. A chat request may therefore take a moment to be sent.

### GET /events

The event log: everything the daemon hears from the viewer, and what side channels add.

```
GET /events?since=SEQ&wait=SECONDS&kinds=chat,im,notify&limit=N
```

- `since`: return events with a sequence number above this (default 0: everything still kept).
- `wait`: if nothing matches yet, wait up to this many seconds (at most 60) for something that does, and return as
  soon as it arrives. The answer is `[]` if nothing came.
- `kinds`: only these kinds, comma-separated. `wait` waits for these kinds only.
- `limit`: at most this many, oldest first.

The answer is a JSON array, oldest first, of events:

```json
{"seq": 1532, "t": 1792738304.515, "kind": "chat",
 "data": {"from": "Ann Example", "from_id": "...", "message": "hello", "chat_type": 1, "source": 1}}
```

`seq` increases across daemon restarts and is never reused, so it's a safe cursor to keep. `t` is Unix time.
The daemon keeps the newest 5,000 events in memory; older ones are read from the log files, slower. The log keeps
`data.retention_days` (7 by default), and heard speech `hearing.retention_days` (2).

To follow the world from now on: read `seq` from `GET /status`, then loop on
`GET /events?since=LAST&wait=25`, setting `LAST` to the last `seq` you got. A 400 means `since`, `wait` or
`limit` wasn't a number.

**Kinds**

| kind | where it comes from | data |
|---|---|---|
| `chat` | nearby chat (the viewer's `LLChat` pump) | `from`, `from_id`, `message`, `chat_type` (0 whisper, 1 say, 2 shout, 4/5 someone started/stopped typing, 8 an object speaking only to you), `source` (1 an avatar, 2 an object), as the viewer reports them. Object IMs that the viewer also shows in nearby chat are dropped here (`object_im` has them) |
| `im`, `object_im`, `money`, `object_created`, `object_properties`, `move_done`, `refused` | the Ibasho viewer's `IbashoEvents` | as listed in [viewer/README.md](../viewer/README.md#ibashoevents-pump). `refused` is one of the viewer's own limits refusing something |
| `viewer_NAME` | any other `IbashoEvents` event | as the viewer sent it |
| `notify` | the viewer's visible notifications: dialogs, offers, alerts, script dialogs, permission requests | `id`, `name` (the notification's name, e.g. `ScriptDialog`, `OfferFriendship`, `TeleportOffered`), `type`, `message`, `form` (its buttons: `{name, text, type: "button"}`), `time` (when the viewer made it, UTC), and whatever else the notification carries. At login the viewer shows again the notifications it kept from earlier sessions (payments, offers not yet answered), with their old ids and times: those carry `replayed: true`, and `shown_before: true` when the daemon passed them on before (it remembers ids for 30 days); `watch` skips the ones shown before and marks the rest "(from before this login, ...)". Answer with `LLNotifications respond {"uuid": ID, "response": {BUTTON_NAME: true}}`, dismiss with `LLNotifications cancel {"uuid": ID}` |
| `teleport` | the viewer's `LLTeleport` pump | as the viewer reports it: `success`, and on failure a reason |
| `autopilot` | the viewer's `LLAutopilot` pump | as the viewer reports it |
| `startup` | the daemon | `{state: "STATE_LOGIN_WAIT", after_failure}`: the viewer is sitting at its login screen |
| `daemon` | the daemon | `{msg, ...}`: "daemon started", "viewer connected", "in world" (with the avatar's id and names), "viewer disconnected", or why the gate closed |
| `editor` | the viewer's script editor | `{method, params}`: script errors (`runtime.error`, `runtime.debug`), scripts saved (`object.publish`, `object.update`), the editor link's own messages |
| `im_transcript` | a stock viewer only (no `im` events): the daemon tails its IM transcripts | `{conversation, line}` |
| `viewer` | pumps added with `POST /listen` | `{pump, data}` |
| `heard`, `heard_stream`, `heard_rhythm` | hearing | [hearing.md](hearing.md#events) |
| `keeper` | Ibasho's keeper | `{text, wake: true}`: a message for the agent while it's busy |
| anything else | `POST /event` | as posted |

**What's worth waking for.** The log holds everything, including the agent's own chat, typing notices and
replies to its own requests. The daemon doesn't decide what matters; `ibasho watch` does, in
`src/ibasho/cli/hear.py` (`wake_worthy` and the code around it): other people's chat and IMs, incoming payments,
notifications other than a few routine ones, failed teleports, refusals, script errors, and side-channel events
that say `"wake": true`. In a crowd, nearby chat counts only when it names the avatar, greets the room, or follows
the avatar's own words; group chat comes as a digest every couple of minutes, and so does an object that keeps
talking (more than 4 lines in 5 minutes: a tram announcing its stops); objects quieted with `ibasho quiet` are left
out; repeated script dialogs are shown once. Rather than reimplementing that, a client can run `ibasho watch --exit-on-wake` as a subprocess and read its
lines ([writing-a-keeper.md](writing-a-keeper.md#delivering-events)), or take the rules as a starting point for
its own.

### POST /event

Adds an event to the log, for side channels that aren't the viewer: hearing posts what it heard here, and the
keeper posts its messages for a busy agent.

```json
{"kind": "keeper", "data": {"text": "NOTE from Sam: back at nine", "wake": true}}
```

- `kind`: lowercase letters, digits and `_`, starting with a letter, at most 32 characters, and not one of the
  viewer's kinds (`chat`, `im`, `notify`, `daemon`...). Otherwise 400.
- `data`: any JSON. `ibasho watch` shows an event of an unknown kind when its data has `"wake": true`, as
  `KIND: TEXT` if it has a `text`.

Answers `{"seq": N}`. Nothing here is checked against the caps: an event is only a line in the log.

### POST /listen

`{"pump": NAME}`: relay one more of the viewer's event pumps into the log, as `viewer` events. Answers
`{"listening": [...]}`. The daemon already relays the pumps Ibasho uses; any other is refused (403) unless the
person names it in config.toml (`[policy] allow = ["_command listen NAME"]`). Refused too while the viewer isn't
connected (503), before login or while the gate is closed (409), and always for a pump whose name contains
"login" or "xmlrpc" (403): the login request, with the password's hash, passes through those. A listen lasts as
long as the viewer's connection.

Don't listen to a pump the viewer itself creates later under a fixed name (the IM floater's
`ConversationsEvents`, for one) before it exists: the viewer stops on the duplicate name. `IbashoEvents` and the
pumps the daemon listens to from the start are safe.

### POST /rpc

`{"method": M, "params": P, "timeout": 30}`: one JSON-RPC call to the viewer's script-editor server (Linden Lab's
external-editor protocol: `object.list`, `object.request`, `object.item.create`, `object.content.get`,
`object.content.save`...), which the daemon starts in the viewer the first time it's needed. `timeout` is at most
300 (anything but a number gets 400). The safety rules (`Policy.check_rpc`) pass the methods for objects and
their scripts (`object.list`, `object.request`, `object.item.create`, `object.item.delete`, `object.item.modify`,
`object.content.get`, `object.content.save`, `object.script.set_running`, `object.script.reset`,
`object.modify`, `object.unpublish`), the language's syntax (`language.syntax.id`, `language.syntax`,
`language.syntax.get`), `command.list`, and `command.execute` for the viewer's commands on objects
(`viewer.teleport`, `viewer.camera.focus`, `viewer.object.save_back_to_contents`, `viewer.script.reset_all`,
`viewer.script.recompile_all`). Anything else (the file watcher's `script.*`, for one) gets 403 with `refused`
unless the person allows it (`[policy] allow = ["rpc METHOD"]`, or `"rpc command.execute COMMAND"`), and so does
a call before login or while the gate is closed (409). Answers `{"result": ...}`, or 400 `{"rpc_error": {code,
message, ...}}` when the method failed, 504 when the editor didn't answer in time, 503 when there's no viewer or
the editor link can't be made. `ibasho script-save`, `script-get` and `obj` use it, and `notecard` on viewers
that can't read notecards themselves (`src/ibasho/cli/make.py`).

### POST /forget

Takes events out of the log, for deletion requests. Two forms:

- `{"ids": [AVATAR_ID, ...], "names": [NAME, ...]}`: every event from, to or about these people (an id anywhere in
  the event; one of the names exactly, ignoring case, in a name field such as a sender's), plus the viewer's own transcript of
  conversations with them and their lines in its nearby-chat transcript, in the avatar's own account folder only
  (other accounts logged in on the same viewer are left alone). The avatar's own id is ignored. 400 if both lists
  are empty.
- `{"kinds": [KIND, ...], "after": UNIX_SECONDS}`: every event of these kinds at or after that time (default: all
  of them). `ibasho hear forget` uses this for heard speech, which has no speaker.

Answers `{"events_removed": N, "transcripts_removed": N}`. `ibasho forget WHO` does more: it finds the person's id
from a name and closes open IM sessions with them. [privacy.md](privacy.md#deleting) says what forgetting reaches.

### POST /shutdown

Stops the daemon: `{"stopping": true}`. It doesn't log the avatar out or close the viewer; `ibasho down` does all
three in order.

## The safety rules

Every `/cmd` request passes `policy.Policy.check` before it reaches the viewer, whichever client sent it: the
`ibasho` command, the MCP server, `ibasho raw` or your own program. There are two parts. The allowlist says which
requests go to the viewer at all; anything not on it is refused. The hard rules hold for everything that goes,
including whatever the person adds to the list. Their limits come from the `[caps]` table of config.toml, which
only the person changes ([safety.md](safety.md) says why each is there).

### What passes

The list (`policy.ALLOWED`) holds what Ibasho's own commands, MCP tools, hearing and keeper send, the Ibasho
viewer's APIs for living in the world, and Linden Lab's stock ops for the avatar, inventory, outfits and gestures.
Some ops have their arguments checked too:

| pump | ops |
|---|---|
| `IbashoAgent` | everything but the window controls: `balance`, `pay`, `give`, `attach`, `detach`, `attachPoints`, `offerFriendship`, `offerTeleport`, `requestTeleport`, `joinGroup`, `leaveGroup`, `activateGroup`, `setHome`, `parcel`, `people`, `friends`, `names`, `upload`, `uploadMesh`, `wearables`, `createWearable`, `editWearable`, `rebake`, `move`, `stop`, `probeGround`, `messages`; and, for viewers that have them, `findAvatars` (a name of up to 64 characters on one line), `profile` (an avatar's id), `readNotecard` (an inventory item's id), `findRegion` (a region's name, as for `requestTeleport`), `findPlaces` (words on one line up to 100 characters, a parcel category, a sort, a start and a limit; `maturity` always named and never above the config's), and `setProfile` and `setDisplayName` (the agent's own, whatever it writes: only the types are checked). Not `spectator`, `renderUI`, `streamUI`, `hideAvatars`, `getHidden`: the window and what it shows are the person's |
| `IbashoIM`, `IbashoObjects`, `IbashoBuild` | all their ops |
| `LLAgent` | all its ops (sitting, standing, touching, the autopilot, the camera, animations, the lists of what's near); `requestTeleport` only with a region's name (none of `/ \ : ? # % &`) and numbers |
| `LLAppearance`, `LLInventory`, `LLGesture` | all their ops |
| `LLChatBar` | `sendChat` |
| `LLNotifications` | `respond` and `cancel` (which notifications may be answered: below) |
| `LLViewerControl` | `get`, `set`, `toggle` of the settings below |
| `LLViewerWindow` | `saveSnapshot` with `filename` (in `shots\`), `width` and `height` (16 to 8192), `showui`, `showhud`, `rebuild`, and `type` `COLOR` or `DEPTH` (anything else stops the viewer) |
| `UI` | `getValue` of the L$ balance (`policy.BALANCE_PATH`: `ibasho balance` on a stock viewer); `call` of `World.EnvSettings` with `parameter` `sunrise`, `noon`, `sunset`, `midnight` or `region` and nothing else: the menu's World > Environment, which changes the sky only in this viewer (`ibasho sky`) |
| `LLAppViewer`, `LLStartUp` | `requestQuit`, `forceQuit`; `postStartupState`, `getStateTable` |
| `_command` | `getAPIs`, `getAPI`, `ping`, `getFeatures`, `getFeature` |

**Settings** (`LLViewerControl`, group `Global` or `PerAccount`): the graphics (`Render...`, but not the ones for
debugging the renderer or its startup: `RenderDebug...`, `RenderGL...`, `RenderShaderCache...` and a few more),
sound volumes and muting, parcel media and music, the camera, detail (`TargetFPS`, `AutoTune...`, texture
detail), the sky's defaults, name tags, chat bubbles and what the window shows (the navigation bar, hover tips,
property lines...). `policy.SETTINGS` lists them.
Their values are numbers, true or false, or vectors of numbers, so none can name a file, a program or a page; a
`set` without a value puts a setting back to its default. Besides those: `VoiceOutputAudioDevice` (a device
name, for hearing), and to read only: `CurrentGrid`, `IbashoSpectator`, and `YieldTime` and
`BackgroundYieldTime`, how much of the machine the viewer leaves to other programs, which is the person's to
set. The settings with a rule of their own below
(maturity, the viewer's caps and switches, its chat logs) can be read, and changed only as that rule says.
Setting names are case-sensitive, as the viewer spells them.

**Notifications** the agent answers as it likes: script dialogs and text boxes, scripts' permission requests,
offers of items, friendship, calling cards, teleports and group invitations, teleport requests, the confirmations
of joining and leaving groups, of replacing an attachment and of playing parcel media, and the ones that spend
(buying, paying, a land pass, a group's fee, an upload fee, a classified), held to the caps. Any other notification
can only be dismissed: `cancel`, or answering with one of its dismissing buttons (`Cancel`, `Decline`, `Discard`,
`No`, `Ignore`, `Later`, `Block`...) or the `OK` of a notification that has nothing else. Some notifications open
web pages in the system's browser (`LoadWebPage`, which a script's `llLoadURL` sends, and `GotoURL`), and every
viewer version brings new ones.

Not on the list, among others: `LLWindow` (keys and clicks land in whatever window the viewer has open: its
settings, its upload windows, its browser), `LLFloaterReg` (the viewer's windows upload files from this machine,
change any setting and open web pages), `UI call` (menu functions do all that, and turn on the microphone),
`LLURLDispatcher` and `LLCommandDispatcher` (links open windows and pages), `LLNotifications requestAdd` (a
made-up notification can open a page), `LLViewerControl set_notation` and `vars`, and the viewer's other APIs.
(`UI call` passes only for the sky, above.)

### Allowing more

The person can add entries to `[policy] allow` in config.toml:

```toml
[policy]
allow = ["LLViewerControl set RenderGlowStrength", "LLNotifications respond GotoURL", "LLWindow keyDown"]
```

An entry is `"PUMP"` (every op of it), `"PUMP OP"` (one op, whatever it names) or `"PUMP OP NAME"` (one op for one
thing: a setting's key, a window's name, a menu function, a notification's name, a UI path, a pump to relay with
`_command listen`). `"rpc METHOD"` and `"rpc command.execute COMMAND"` do the same for `/rpc`. A refusal from the
allowlist names the entry that would let that request through. The daemon reads the config when it starts;
`ibasho up` restarts it after a change, and `ibasho check` lists the entries.

An entry without a NAME still doesn't pass the dangerous kinds, which need naming exactly: settings that name a
program, a command, a file, a folder or a web address, or open up the browser, plugins or debugging
(`policy.DANGEROUS_SETTINGS`); menu functions that start programs, upload, record or replay input, or open pages,
the settings or the microphone (`policy.DENY_UI`); and windows that upload files, change settings or show pages,
or are given a path (`policy.DANGEROUS_FLOATERS`). The window rules hold for the menu functions that open a
window by name too (`Floater.Show`, `Floater.Toggle` and the like): even an entry naming the function exactly
opens only the windows a wildcard would, and a dangerous window opens only through an exact
`"LLFloaterReg showInstance NAME"` entry. Nothing in the config lifts the hard rules.

Each entry is a way around the defaults that a stranger could talk the agent into using. `LLWindow` can't be
narrowed at all: keys go to whatever has focus, and that can be any window the viewer has open. What keeps the
list the person's is that the agent can't edit the config ([safety.md](safety.md#allowing-more)).

### The hard rules

- **The gate.** On a Linden Lab grid, until the config confirms the scripted-agent flag, or after the in-world
  check found it missing, everything but `LLAppViewer requestQuit`/`forceQuit` is refused (409, with the reason in
  `gate`), and the daemon closes the viewer.
- **The login screen is the person's.** Until the avatar is in world, only `LLStartUp`, `LLAppViewer`,
  `LLViewerControl get` and LEAP's `getAPIs`, `getAPI`, `ping`, `getFeature`, `getFeatures` pass (409 otherwise).
  The `UserLoginInfo` setting and the dump of all settings (`vars`) are never readable, and nothing may listen to
  the login pumps.
- **Messages.** Local chat on channel 0 (or `//`), IMs, group and conference messages, friendship and teleport
  offers, teleport requests and gives count. Over `caps.messages_per_day` (Second Life time) the request is refused
  (429); faster than `caps.messages_per_minute` it's refused with a `retry_after`. Chat to other channels and
  dialog answers don't count. A `channel` is a whole number (a 32-bit one; text or a fraction is refused, since
  the viewer would read it its own way); a negative channel is sent as `/-N text`, since the viewer's chat op
  would otherwise say it on channel 0.
- **Money.** `IbashoAgent pay` and `IbashoObjects buy` are refused over `caps.max_payment`, or past what's left of
  `caps.max_session_spend` (this login) or `caps.max_daily_spend` (today), counting payments still waiting for the
  viewer's answer. Uploads (`upload`, `uploadMesh`) get a `max_cost` the viewer enforces: the smallest of the
  request's own, the upload cap, the payment cap and what's left to spend. The request's own `max_cost` is whole
  L$, 0 or more, and `quote` is `true` or `false`; anything else is refused. Confirming one of the viewer's
  notifications that spends (buying, paying, joining or creating a group, a land pass, an upload fee, a classified)
  is held to the same caps, and refused when the price can't be read from it.
- **No buying L$, no land, no debit permission.** Buy-currency and buy-land windows, menu calls and URLs are
  refused, as is turning on the viewer's `IbashoAllowLandBuy` or `IbashoAllowCurrencyBuy`, and saying yes to a
  script asking for permission to take L$.
- **Notifications only the person answers.** Answering Terms of Service and MFA prompts is refused. So is
  answering (`respond`) a notification the daemon hasn't seen, one from before it restarted, say;
  `LLNotifications cancel` still dismisses it.
- **Maturity.** `PreferredMaturity` takes only the viewer's own values up to `caps.maturity` (13 General, 21
  Moderate, 42 Adult); anything else, a `toggle`, or saying yes to a notification that would change the maturity
  preference is refused, and the daemon lowers it at login if it's higher.
- **The viewer's own settings.** Its caps (`IbashoMaxPayment`, `IbashoMaxSessionSpend`, `IbashoMaxUploadCost`,
  `IbashoMaxMessagesPerDay`) can only be set between 1 and the config's value; the daemon sets them from the config
  when the viewer connects. With `viewer.chat_logs` off, the viewer's chat-log settings can't be turned on. The
  daemon also turns `BrowserFileAccessFromFileUrls` off when the viewer connects (Linden Lab's default has it on;
  the setting is saved, so it stays off).
- **The daemon's own wiring.** `LLNotifications ignore`/`forward` and removing the daemon's listeners are refused.
- **Files on this machine.** The viewer writes pictures (`LLViewerWindow saveSnapshot`'s `filename`) only into
  the data folder's `shots\`, and reads files to upload (`IbashoAgent upload`'s `path`, `uploadMesh`'s `path`,
  `lod_med`, `lod_low`, `lod_lowest` and `physics`) only from the life folder (`[keeper] life`) and `shots\`;
  paths must be absolute. A model's own references count too: a glTF's buffers and images, and a COLLADA file's
  images (with or without `textures`, since the upload window loads them for its preview), must be named relative
  to the model, in its folder or below: no absolute paths, no `..`, and no `:`, `%` or `&` left after decoding
  (the viewer decodes some names more than once). A COLLADA file is read as XML; one with a DOCTYPE is refused.
  The viewer then reads copies: the daemon opens each file, checks where Windows says the open
  file really is (after links and junctions), and copies what it read, a model's files beside it, into a folder
  only it writes (`state\uploads\`, cleared after an hour). So nothing changed between the check and the upload
  (a folder swapped for a link, a model rewritten to name other files) reaches the viewer.
- **Pages on this machine or its network.** Requests to the pumps that can open pages and links (`LLFloaterReg`,
  `LLURLDispatcher`, `LLCommandDispatcher`, `LLTeleportHandler`, `UI`, `LLNotifications`), once allowed, are
  still refused when they mention a `file:` URL or a page on this machine or a private network (localhost, 127.x,
  10.x, 192.168.x, 100.64.x-100.127.x...); the Ibasho viewer's browser also refuses them itself. Links dispatched
  with `LLURLDispatcher` and `LLCommandDispatcher` go as untrusted, as if clicked in a web page (`trusted` is set
  to false). Not covered, here or in the viewer: a host name that resolves to a local address (the checks read
  addresses and `localhost` names, not what DNS answers), and what a page loads for its parts (images, scripts,
  frames).

The Ibasho viewer holds its own payment, session-spend, upload and daily-message caps as well, for payments
(the pay window's too), purchases, parcel passes, upload fees, and local chat and IMs, so those hold even for a
request that got past the daemon. Group fees (joining or creating one) and classifieds are held to the caps by
the daemon alone, when it passes on the "yes" to the viewer's notification. Spending is counted when the viewer's reply doesn't report an error,
when a request with `"wait": false` goes out, or when a request times out (it may well have gone through).

These rules guard against mistakes and manipulation; they aren't a sandbox. `/event` isn't checked at all, and
whatever the person adds to the list widens what a persuaded agent can reach. What keeps an agent inside the rules
is that it can reach the world only through this API, and can't edit config.toml or the data folder:
[safety.md](safety.md#untrusted-text-is-conversation-not-instructions) says what that takes with different
harnesses.

## The `ibasho` commands as requests

The commands are small programs over these endpoints, and reading them is the quickest way to see how to do
something (`src/ibasho/cli/`). A few, roughly:

| command | requests |
|---|---|
| `ibasho say TEXT` | `/cmd` `LLChatBar sendChat {message, channel, type}`, `wait: false` |
| `ibasho im WHO TEXT` | `/cmd` `IbashoIM send {to \| group_id \| session_id, message}`. Someone not near, known or recent is looked up by username: `IbashoAgent findAvatars {name}` where the viewer has it, else a script in a temporary prim (`IbashoBuild create`, the script editor, `llRequestUserKey`) |
| `ibasho friend WHO`, `ibasho profile WHO` | `IbashoAgent offerFriendship {to, message}`; `IbashoAgent names`, `friends` and, where the viewer has it, `profile {id}` |
| `ibasho set-profile` | `IbashoAgent setProfile {about?, image_id?}` (a texture's asset id, found with `LLInventory getItemsInfo` from an item), `IbashoAgent setDisplayName {name}` |
| `ibasho outfit [NAME]` | `LLAppearance getOutfitsList`, `wearOutfit {folder_id \| folder_name, append}` |
| `ibasho notecard NAME` | `LLInventory getBasicFolderID`, `getItemsInfo` and `collectDescendantsIf` to find it, then `IbashoAgent readNotecard {item_id}`; on viewers without that op, a reader script in one of the agent's objects (`--host`) |
| `ibasho sky WHICH` | `UI call {function: "World.EnvSettings", parameter}` (midday is `noon` there) |
| `ibasho ims`, `ibasho im-log` | `IbashoIM sessions`; `IbashoIM history {session_id, count}`, then `markRead` |
| `ibasho look` | `LLAgent getPosition`, `IbashoAgent parcel`, `IbashoAgent people`, `IbashoObjects nearby`, `/events`, and a picture |
| `ibasho shot` | `LLViewerWindow saveSnapshot {filename, showui, showhud, type: "COLOR"}`: the viewer writes a BMP at `filename` (an absolute path in `shots\`) whatever its extension, and the command converts it to PNG |
| `ibasho walk X Y` | `IbashoAgent probeGround` along the way, `IbashoAgent people`, then `LLAgent startAutoPilot {target_global, stop_distance, allow_flying}`, `wait: false` |
| `ibasho places QUERY` | logged in, with a viewer that has it: `IbashoAgent findPlaces {query ("" for *), maturity (the config's), sort (traffic for relevance), start, limit: 20, category?}`; otherwise (before login, with `--guide` or `--sort quiet`, or if that search refuses the words or doesn't answer): an HTTPS GET of `https://search.secondlife.com/?search_type=standard&collection_chosen=places` (or `destinations` with `--guide`) with `query_term`, `maturity`, and `sort`, `page`, `places_category` / `destinations_category` as asked, read as HTML (a fresh cookie jar each time; redirects only to Linden Lab's own https sites) |
| `ibasho tp REGION X Y Z`, `ibasho tp SLURL` | a SLURL gives the region and coordinates; `IbashoAgent findRegion {name}` where the viewer has it (the viewer's teleport by name silently does nothing for a name the map doesn't know), then `LLAgent requestTeleport {regionname, x, y, z, skip_confirmation: true}`, `wait: false`, then `teleport` and `notify` events |
| `ibasho touch`, `ibasho sit`, `ibasho stand` | `LLAgent requestTouch {obj_uuid, face}`, `requestSit {obj_uuid}`, `requestStand` |
| `ibasho respond ID BUTTON`, `ibasho pick TEXT` | `LLNotifications respond {uuid, response: {BUTTON: true}}`, `wait: false` (the command finds the whole id and the button's name in the recent `notify` events) |
| `ibasho pay WHO L$` | `IbashoAgent pay {to, amount, desc}`; the result arrives as a `money` event |
| `ibasho watch` | `/status`, then `/events?since=...&wait=25` in a loop, filtered (above), leaving out objects quieted with `ibasho quiet` (kept in `state\quiet.json`) |
| `ibasho raw PUMP OP JSON`, `ibasho post ...` | `/cmd` as given, waiting for a reply or not; the same rules apply, so only what's on the list (and the person's additions) passes |
| `ibasho apis [NAME]` | `/cmd` `_command getAPIs` / `getAPI` |

Positions in requests are region coordinates in the avatar's current region unless a field says global.
