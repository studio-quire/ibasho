# Ibasho viewer

A modified build of the Second Life® viewer for an AI model living in the Second Life world: the stock viewer plus LEAP
APIs, so a program can do what a resident does through calls instead of screen clicks (instant messages,
objects and their owners and permissions, building, money, inventory, wearables, uploads, movement), and a
spectator mode so a person can watch the window without their mouse and keyboard getting in the way.

**This software is not provided or supported by Linden Lab, the makers of Second Life.** Ibasho itself comes
without support either: Help > Report Bug opens this project's issue tracker, where reports are welcome, but
nobody promises an answer or a fix. Windows, and Linux (x86-64; made for servers without a graphics card, [docs/linux.md](../docs/linux.md)).

It is Linden Lab's open-source viewer ([secondlife/viewer](https://github.com/secondlife/viewer), `develop` at
the commit in [`BASE`](BASE), `81bc977`, viewer 26.4.0) with the patch series in [`patches/`](patches). Most
new code lives in new files (`indra/newview/ibasho*.cpp/h`); Linden Lab's files only get short, marked hooks
(listed [below](#what-changed-in-linden-labs-files)). Written by Claude (Anthropic) models: Claude Opus 5.5.

## Build

You need Windows 10/11 x64, about 30 GB free, 16 GB RAM or more, and:

- Visual Studio 2022 or its Build Tools with the C++ workload: `vs_buildtools.exe --add
  Microsoft.VisualStudio.Workload.VCTools --includeRecommended` (CMake comes with it).
- Git for Windows (the scripts run in Git Bash), and Python 3.10 or newer (3.11 is what we use).
- Long paths on (one dependency has paths over 260 characters): as administrator,
  `reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f`.

Then, from Git Bash:

    bash viewer/setup.sh                                     # source at BASE + patches, autobuild venv
    bash viewer/configure.sh                                 # CMake via autobuild (downloads ~350 MB of libraries)
    powershell -ExecutionPolicy Bypass -File viewer/build.ps1    # compile

Everything goes into `viewer/work/` (or the folder in `IBASHO_VIEWER_WORK`). The result is
`work/viewer-src/build-vc170-64/newview/Release/IbashoViewer.exe`; that folder is the runnable viewer.
`build.ps1 -Jobs N -Projects M` sets the parallelism (default 4 compiler processes, one project at a time;
each compiler process can take 1-2 GB), runs at below-normal priority (`-Priority Idle` for less), and stops
the build if free memory stays under `-MinFreeGB` (3). A first build takes about 25-60 minutes depending on
the machine; after that, a change to one of the `ibasho*` files rebuilds in under a minute. Close the viewer
before building: Windows won't let the build replace a running executable.

The build uses Linden Lab's open-source configuration (`ReleaseOS`): OpenJPEG instead of Kakadu, no Havok,
no FMOD, voice over WebRTC. Ibasho also leaves out LibVLC (VLC is GPL), so nothing in the build is under the
GPL: the media LibVLC played goes to the built-in browser (CEF) instead (see [Media](#media)). `licenses.txt`
beside the executable has the libraries' licenses, and About > Licenses lists them with their versions.

### A release zip

    python viewer/package.py        # -> work/dist/Ibasho-<version>-windows-x64.zip

The build folder also holds build leftovers (`secondlife-bin.exe`, `.pdb`, installer scripts), so the zip is
made from Linden Lab's manifest (`indra/newview/viewer_manifest.py`, the files their installer ships), plus
`README.txt` (the disclosures, where the source is, uninstalling) and `licenses/` (the LGPL texts). It uses the
build's own Python (`work/ab-venv`). The compiler writes source paths into the binaries, so build a release in
a folder outside your user profile (`IBASHO_VIEWER_WORK=C:\ibasho-work`, say): `package.py` checks every file it
ships for the profile's path, your user name and the computer's name, and refuses if any has one. The zip's
entries carry their times in UTC (a zip stores times without a zone, so local times would give away the
builder's).

### On Linux

    docker build -t ibasho-viewer-build viewer/linux
    docker run --rm -v ibasho-viewer-work:/work -v "$PWD:/release:ro" -e IBASHO_VIEWER_WORK=/work \
        ibasho-viewer-build bash /release/viewer/build-linux.sh

`build-linux.sh` does what setup, configure, build and package do on Windows, in one go: on Ubuntu 22.04 (as Linden
Lab builds its Linux viewer) or in that container, into `IBASHO_VIEWER_WORK`. It needs about 10 GB (the compiler
writes no debug information unless `IBASHO_DEBUG_INFO=1`) and an hour or two on 4 cores; run again, it builds only
what changed. The result is `dist/Ibasho-<version>-linux-x86_64.tar.xz`: `ibasho-viewer` starts it,
`bin/ibasho-viewer-bin` is the viewer. Linden Lab's desktop integration (menu entries, the `secondlife://`
handler, its installer) is left out, since it would install the viewer as "Second Life". `package.py` checks the
tarball for the builder's home folder, user and computer name, as it does the zip (`IBASHO_FORBIDDEN_NAMES` adds
names, for a container build made for some machine). `.github/workflows/viewer-linux.yml` builds the same in GitHub Actions.

### Version

Ibasho numbers its releases itself: `indra/newview/VIEWER_VERSION.txt` holds MAJOR.MINOR.PATCH (`0.2.1`), and
`configure.sh` sets the fourth part, the build number, to the number of commits in the Ibasho series on top
of `BASE` (25 for the first release, `0.1.0.25`; 35 for `0.2.0.35`; 39 for `0.2.1.39`). Every change to the series, published or local, gives a
different version, and the channel name "Ibasho" tells it apart from any Linden Lab viewer. Linden Lab's own
version of the base (26.4.0) is named in About. Re-run `configure.sh` before a release build so the number is
current; `build.ps1` warns if the tree has moved on since.

### Moving to a newer Linden Lab viewer

    git -C work/viewer-src fetch --depth 50 origin develop
    git -C work/viewer-src rebase FETCH_HEAD ibasho

then put the new commit in `BASE`, the new Linden Lab version in the About header
(`skins/default/xui/en/strings.xml`, `AboutHeader`), and write the patches with `python viewer/export_patches.py`
(not `git format-patch` by itself: a binary patch also carries the old file, here Linden Lab's logos, and the
script leaves those out). Conflicts, when they come, are in the hooks listed below.

## Running

The viewer keeps its own settings, logs and chat logs in `%APPDATA%\Ibasho` and its cache in
`%LOCALAPPDATA%\Ibasho`, apart from any other viewer on the machine, and it can run alongside one. Uninstalling
is deleting the build folder and those two folders.

Ibasho never checks for updates, never offers one and never downloads anything by itself; a new version is a
new download or build. (Linden Lab's update service is for Linden Lab's viewers, and Ibasho doesn't ask it.)
`ibasho viewer update ZIP` puts a new release into the same folder as the old one, since Windows' firewall
remembers its answer per program path ([docs/setup.md](../docs/setup.md)).

### Media

Without LibVLC, parcel music and the audio and video types LibVLC used to play (mp3, aac, ogg, wav, mp4,
webm...) go to the built-in browser, Chromium, which plays the common formats and http(s) streams. What no
longer plays: RTSP streams, and formats Chromium can't decode (Windows Media, FLV, AIFF, some QuickTime). A
parcel stream that plays in other viewers can also be silent here when its server refuses Chromium: some old
Shoutcast servers answer in a way Chromium rejects, and some stations refuse the viewer browser's user agent
(it says `SecondLife/...`, as every viewer's built-in browser does). Web pages on prims and parcels (shared
media) are unchanged.

A program drives it through LEAP: start the viewer with `--leap "COMMAND"` and the viewer runs COMMAND as a
child process, talking LLSD over its stdin/stdout ([LEAP](https://wiki.secondlife.com/wiki/LEAP)). The Ibasho
tooling does this for you. A person still logs in themselves the first time, accepting Linden Lab's Terms of
Service in the viewer; the viewer can remember the login.

**Spectator mode is on at every launch**: the real mouse and keyboard don't reach the viewer (see `spectator`
below). To click through something yourself, turn it off over LEAP, or in the viewer's debug settings
(`IbashoSpectator`, until the next launch).

**It never takes focus by itself** (`IbashoNoActivate`, on at every launch unless the command line says `--set
IbashoNoActivate 0`, not saved): the viewer usually runs on the desktop of someone using it for something else, so
it never activates its own window or raises it over theirs. The window appears without activation just behind
the window in use (behind everything when that one is topmost or there's none), with no splash screen; nothing
in world brings it forward or flashes its taskbar button; restoring and resizing it don't activate it; and a
maximized start fills the screen's work area instead of maximizing, which always activates. It renders as usual,
so snapshots work, and the person can click it or switch to it at any time. Fatal-error message boxes from
Windows (the graphics driver failing, say) are the exception. The Ibasho tooling also asks Windows to show the
viewer's first window without activation, and `ibasho login` turns the setting off, since the person is about to
use the window.

### What to tell people

Some of what this viewer does would surprise a person who expects a normal viewer, and Linden Lab's Third
Party Viewer Policy asks that it be said where you get it: a program can control it completely over LEAP;
spectator mode ignores the local mouse and keyboard; it can pay L$, buy objects and pay upload fees without a
confirmation dialog (within the caps below); `hideAvatars` leaves chosen avatars out of what the window
shows (locally only, as "Do not render" does); `streamUI` keeps other people's words off the window.

### Limits

Settings, persisted, changeable in the viewer's debug settings or over LEAP (`LLViewerControl`):

| Setting | Default | |
|---|---|---|
| `IbashoMaxPayment` | 100 | largest single payment or purchase, L$ |
| `IbashoMaxSessionSpend` | 500 | total L$ spent per viewer session (payments, purchases, upload fees); 0 = no limit |
| `IbashoMaxUploadCost` | 50 | largest fee for one upload through the APIs, L$ |
| `IbashoMaxMessagesPerDay` | 3000 | outgoing local chat lines (channel 0) and IMs per day, by Second Life time date; 0 = no limit |
| `IbashoAllowLandBuy` | off | the Buy Land window works only when on |
| `IbashoAllowCurrencyBuy` | off | buying L$ with real money works only when on |

The limits are checked where the viewer sends, so they hold for the APIs and for the viewer's own windows
driven by clicks and keys over LEAP alike: the pay window, the buy window and parcel passes are within the
spending caps too. Something refused shows a notice and posts `{event: "refused", what, reason}` on
`IbashoEvents`. The message count is kept in `user_settings\ibasho_messages.txt`, so restarts don't reset it;
Linden Lab's Scripted Agent Policy allows 5,000 messages a day for all of one owner's agents, and 3,000 leaves
room. The spending defaults suit a new agent: an account usually starts with a few hundred L$ at most, L$100
covers tips, donation jars and most small purchases, and L$500 a session bounds what a misunderstanding or a
manipulative message can cost before someone notices. They are guards against mistakes, not a security
boundary: whatever can drive the viewer can also change the settings.

The viewer's own chat and IM transcripts are off by default (`LogNearbyChat`, `KeepConversationLogTranscripts`),
so other people's words aren't written to disk unless the person running it turns them on.

### On a server

For servers without a graphics card, where Mesa draws in software and takes every core it's given. Not saved;
Ibasho sets them on the command line or over LEAP ([docs/linux.md](../docs/linux.md#server-settings)):

| Setting | Default | |
|---|---|---|
| `IbashoMaxFPS` | 0 | frames a second at most; 0 = no cap |
| `IbashoIdleFPS` | 0 | the cap while no LEAP plugin has sent a request for `IbashoIdleAfter` seconds (after login, not on autopilot or teleporting); the first request after a quiet spell waits up to one idle frame |
| `IbashoIdleAfter` | 5 | seconds |
| `IbashoNoWebPlugin` | off | no web (CEF) plugin starts and no web floaters are preloaded; set it on the command line |
| `IbashoTextureBudget` | 0 | the texture budget in MB, held as given; 0 = the viewer's own, from the video memory, which software rendering reports as all of the system's. While it's set, avatar bakes and your own avatar's textures are held to 512 and the world's to `RenderMaxTextureResolution`, whatever else asks for more (Second Life's 2048 bakes otherwise stay whole) |
| `IbashoTrimMemory` | 0 | Linux: every this many seconds, memory the viewer has freed goes back to the system (`malloc_trim`), so it shrinks again after a busy place; 0 = never |

### The built-in browser

The web and media windows, media on prims and parcels, and parcel music all load through the built-in browser
(CEF). It refuses `file:` URLs and pages on this machine or its network: loopback, private, link-local and shared
(100.64.0.0/10) IPv4 addresses in any spelling a browser accepts (`127.1`, `2130706433`, `0x7f.0.0.1`,
percent-encoded), `localhost` names and any IPv6 literal. The grid's own login page and login service are allowed,
wherever the grid runs. A program typing into an open browser window can't get round it: the viewer checks before a
navigation is sent (so the media type probe doesn't ask either), and when a link, form or redirect inside a page
lands on such an address, it replaces the page with a blank one (that request has gone out by then). A refusal
shows a notice and posts `{event: "refused", what: "browser", reason}` on `IbashoEvents`, at most once a minute
for the same address. Not covered: host names that resolve to local addresses, and what a page loads for its
parts (images, scripts, frames).

## APIs

All ops take a LEAP request map with `op` and their arguments; with a `reply` pump they reply, and failures
reply `{error}`. Every API describes itself: `getAPI {"api": "IbashoAgent"}` on the LEAP command pump (named
in the viewer's first message) lists the ops and their argument help. Vectors are arrays; positions are region coordinates in the agent's current region;
avatar, object, item and group ids are UUID strings.

### IbashoEvents (pump)

The viewer creates it at startup, so a client can listen from the start. Every event is a map with `event`:

- `im`: `dir` (in/out), `session_id`, `session_type` (p2p, group, conference), `session_name`, `from_id` and
  `from` (incoming) or `to_id` (outgoing), `message`, `offline`, `dialog`, `timestamp` (unix seconds). Group
  messages the server echoes back to their sender are dropped (the `out` event covered them).
- `object_im`: an IM from an object (`llInstantMessage`): `object_id`, `owner_id`, `from` (object name),
  `message`, `slurl`. The viewer also shows these in nearby chat.
- `money`: `balance`, `amount`, `type` (transaction type), `source_id`, `dest_id`, `dir` (in/out), `item`, `desc`.
- `object_created`: `id`, `local_id`, `pos`, `requested` (true if it answers one of our create, rez or
  duplicate calls), `modify_error` if the requested modifications failed.
- `object_properties`: answers to `IbashoObjects request`.
- `move_done`: the end of an `IbashoAgent move` (below).
- `refused`: `what`, `reason`, when a limit refused something (above; `what` is `browser` for the built-in
  browser).

### IbashoIM

Instant messages: person to person, group chat, conferences.

- `send {message, to | group_id | session_id}` -> `{session_id}`. Opens a P2P session or joins the group chat
  if needed; group and conference messages wait (up to 20 s) for the server to start the session.
- `sessions` -> `{sessions: [{session_id, name, type, other_id, unread, offline, ready, messages}]}`
- `history {session_id, count=20}` -> `{session, messages: [{from, from_id, message, time, timestamp}]}`:
  `session` as `sessions` lists it, the messages oldest first; doesn't mark them read.
- `markRead {session_id}`, `leave {session_id}`
- `conference {ids, message?}` -> `{session_id}` once the server has started it.

### IbashoObjects

Object names, descriptions, owners, permissions and sale info aren't in the updates the viewer gets; it asks
the region (`RequestObjectPropertiesFamily`), throttled to 20 requests a second, caches the answers for five
minutes, and replies once they're in or the timeout passes.

- `nearby {dist=20, limit=40, fetch=true, timeout=4, attachments=false, query?}` -> `{objects, timed_out?}`:
  root objects, nearest first. Each: `id, local_id, pos, rot, scale, dist, shape, prims, seated, text,
  text_color, flags` (touch pay scripted mine mod copy move phantom physics temp drop), `click_action`,
  `attached_to, attach_point, hud`, and once fetched `name, desc, owner_id, group_id, last_owner_id, perms
  {base, owner, group, everyone, next}` (letters M C T V: modify, copy, transfer, move), `sale {type, price}`.
  `query` keeps objects whose name, description or hover text contains it.
- `get {ids, fetch=true, children=false, full=false, timeout}` -> `{objects, missing?}`, for any prim. `full`
  briefly selects the objects to also learn `creator_id, created, touch_name, sit_name`, then deselects.
- `find {prefix}` -> `{ids}`: objects in view whose id starts with `prefix` (handy for short ids).
- `request {ids}`: ask the region; the answers arrive as `object_properties` events.
- `select {ids, add?}`, `deselect {ids?}`, `selection` -> `{objects}`: the edit selection, as the edit tool
  makes it (with its usual effects: highlight, the avatar's look-at target).
- `buy {id, price}`: buy an object that's for sale; `price` must equal its current price (`get` it first).
  Spending caps apply. The region answers with an inventory offer or an alert.

### IbashoBuild

Create and change objects one by one with the messages the build tools send, no selection needed.

- `create {shape=box, pos?, scale=[.5,.5,.5], rot? | rot_euler?, ...modify keys}` -> `{id, local_id}` once the
  region has made it (up to 15 s). Default position 2 m ahead.
- `modify {id | ids, ...}`. Keys: `pos` (region; on a root it moves the linkset), `local_pos` (a child,
  relative to its root, in the root's frame; rotation and scale each go with the prim's position, as Linden
  Lab's build tools send them), `rot` [x,y,z,w] or `rot_euler` [degrees], `local_rot(_euler)`,
  `scale`; `shape` (box cylinder prism sphere torus tube ring cone pyramid; resets the shape parameters),
  `profile_cut [b,e]`, `path_cut [b,e]`, `hollow`, `twist [b,e]`, `taper [x,y]` (as the edit panel means
  it), `top_shear`, `hole_size`, `radius_offset`, `revolutions`, `skew`; per `face` (default all): `color
  [r,g,b(,a)]`, `alpha`, `texture` (asset id), `repeats [u,v]`, `glow`, `fullbright`, `shiny` 0-3; `name`,
  `desc`, `phantom`, `physics`, `temp`, `click_action` (touch sit buy pay open play open_media zoom disabled),
  `sale {type: original|copy|contents, price}` or `false`, `next_perms` ("MC" = modify and copy, no transfer).
- `duplicate {id, offset=[0,0,.5], ...modify}` -> `{id}`
- `rez {item_id, pos?, ...modify}` -> `{id}`: an object from inventory, moved exactly to `pos` once it appears.
- `link {ids}` (the first becomes the root), `unlink {ids}` (a root unlinks its whole linkset)
- `take {ids, copy?, folder_id?}`, `delete {ids}` (to Trash), `returnObjects {ids}` (needs land rights)

New objects are asked for "create-selected", which is how the viewer tells which new object answers which
call (with a fallback for regions that don't flag them: a new object of yours near where it was asked for).
The region then holds them selected by you, and a selected physical object doesn't move; so unless the edit
tool has selected it, the viewer deselects each new object on the region straight away.

### IbashoAgent

Money, giving, attachments, offers, groups, places and people:

- `balance` -> `{balance, known}` (`known` is false before the first balance update).
- `pay {to, amount, desc?}`: an avatar, or an object in view that takes money. Refused over
  `IbashoMaxPayment`, past `IbashoMaxSessionSpend`, or if the balance can't cover it (it never opens the buy-L$
  window). Replies `{ok, target, spent_this_session}`; the result arrives as a `money` event.
- `give {to, item_id | folder_id}`
- `attach {item_id, point?, replace?}` (point by name, e.g. "Right Hand", or number; no confirmation dialog),
  `detach {item_id | object_id}`, `attachPoints` -> `{points: [{id, name, hud, objects}]}`
- `offerFriendship {to, message?}`, `offerTeleport {to | ids, message?}`, `requestTeleport {to, message?}`
- `joinGroup {group_id}`, `leaveGroup {group_id}` (the viewer's confirmation notification follows; answer it
  with `LLNotifications respond`), `activateGroup {group_id}`
- `setHome`; `parcel` -> `{region {name, id, maturity, sandbox}, parcel {name, desc, local_id, owner_id,
  group_id, group_owned, area, category, music_url, media_url, for_sale, sale_price, allowed: [build scripts
  fly push damage voice see_avatars]}, my_voice}`: `my_voice` is whether your own voice chat is on (not
  whether this parcel's channel has connected)
- `people {dist=64}` -> `{people: [{id, display_name, username, pos, dist, friend, in_voice?, speaking?}]}`,
  nearest first (avatars further away have coarse positions). While your voice chat is on, `in_voice`
  marks those in it and `speaking` those talking now.
- `friends` -> `{friends: [{id, display_name, username, online, rights_to, rights_from}]}`
- `names {ids, timeout=5}` -> `{names: {id: {display_name, username}}}`
- `findAvatars {name, limit=20, timeout=10}` -> `{avatars: [{id, username, display_name}]}`: a name search as
  the avatar picker makes it, for anyone on the grid: "first.last", "first last", "first" (a Resident) or part
  of a display name, as far as the grid's search matches it. Empty if nobody matched; `{error}` if the grid
  didn't answer in time. Up to 100 results. Through the region's `AvatarPickerSearch` capability, else the
  `AvatarPickerRequest` message (OpenSim).
- `profile {id, timeout=10}` -> `{id, username?, display_name?, about, born?, image_id, partner_id, first_life,
  first_life_image_id, account, online, allow_publish, identified, transacted, groups: [{id, name}], picks:
  [{id, name}], customer_type?, notes?, web_url?}`, as the profile window gets it. `born` is `YYYY-MM-DD`, or
  `MM-DD` with `age_hidden` when they hide their age; `account` is Resident, Trial, Charter Member, Linden Lab
  Employee or a special caption; `online` as far as the grid shows you; `identified` / `transacted`: payment
  info on file / used; `notes` are your own private notes on them. Through the region's `AgentProfile`
  capability; without it (OpenSim) the older messages, where groups or picks may not come (then `partial`).
  `{error}` if no profile comes in time. Your own adds `display_name_next_change` while a display name change
  has to wait.
- `setProfile {about?, image_id?, timeout=20}` -> `{}` once the grid has taken them: your own about text and
  picture (a texture's asset id), as the profile window saves them, through the `AgentProfile` capability.
  Without it (OpenSim) the older update message, made from your current profile with only these fields
  changed; it has no answer, so the reply (`by_message: true`) comes once it's sent.
- `setDisplayName {name, timeout=30}` -> `{display_name, next_change?}` once the grid has answered: as the
  display name window sets it; `""` or your username goes back to the username; at most 31 characters. Second
  Life allows about one change a week: too soon, `{error, next_change}` says when. Others see the new name
  within a few minutes. Grids without display names (OpenSim) say so.
- `readNotecard {item_id, timeout=20}` -> `{item_id, name, text, items: [{name, type}], truncated?}`: a notecard
  in your inventory (given to you, say), read as the notecard window reads it, wherever you are: only with copy
  permission, like the window. Items embedded in the text show in it as `[type: name]` and are listed in
  `items`. The text is at most 64 KB (a notecard's own limit). `{error}` if it can't be read or doesn't arrive
  in time.
- `findRegion {name, timeout=10}` -> `{found: true, region: {name, global: [x, y], access, maturity}}` or
  `{found: false, similar: [names]}`: a region looked up on the world map, as the map's search does
  (`MapNameRequest`). `global` is its south-west corner in global metres; `access` the rating's number (13
  General, 21 Moderate, 42 Adult), `maturity` its name; `name` as the map spells it. `similar`: the other
  regions the search turned up. `{error}` if the map doesn't answer in time. (The viewer's teleport by region
  name asks the map the same way, and does nothing when the map knows no such region.)
- `findPlaces {query, category=any, maturity, sort=traffic, start=0, limit=20, timeout=20}` -> `{places: [{id, name,
  traffic, for_sale, region?, pos?, global?, rating?, description?, area?}], more, maturity}`: places searched as
  the directory's Places tab searches them (`DirPlacesQuery`), so with the account's own maturity access, where
  Second Life's web search shows a visitor General places only. `query`: words to find (Second Life wants 3
  letters or more, and refuses `""`, category or not); `category`: a parcel category by its internal name (`linden`, `arts`,
  `store`, `educational`, `game`, `gather`, `newcomer`, `park`, `home`, `shopping`, `stage`, `other`, `rental`,
  `adult`); `maturity`: the most to include (`general`, `moderate`, `adult`), never more than the account may
  enter; `sort`: `traffic` (busiest first) or `name` (then Second Life sends every place's traffic as 0; it
  has no quietest-first, sending the busiest whatever the order). Each place on the page is then asked for as its
  place profile asks (`ParcelInfoRequest`): `region`, `pos` (in the region), `global`, `rating`, `description`
  (the owner's text) and `area`; a place whose details don't come within a few seconds has only its name and
  traffic. `more`: there are results past this page. A reply stays within a hundred from a multiple of 100, as
  the grid sends them. `{error}` if the grid's search doesn't answer in time (OpenSim answers only with a search
  module) or refuses the words.
- `textureStats {top=15}` -> `{gl_texture_mb, vertex_mb, budget_mb, free_budget_mb, discard_bias,
  system_memory_low, by_boost: {LEVEL: {count, mb, raw_mb, raw_count, faceless, on_screen, sizes}}, fetched_mb,
  fetched_count, biggest: [{id, boost, type, full, side, discard, desired, faces, on_screen, vsize, mb}]}`:
  where texture memory goes, for a budget that doesn't hold. MB are real bytes at each texture's current
  discard level (the budget counts about twice that); `top` is at most 200.

Uploads and wearables:

- `upload {path, name?, desc?, folder_id?, type?, next_perms?, max_size=1024, lossless?, max_cost?, quote?,
  timeout=120}` -> `{item_id, asset_id, cost, charged, type, name, width?, height?}` once the inventory item
  exists. Textures (png tga bmp jpg) are scaled to powers of two within `max_size` and encoded as the image
  preview does; wav sounds and anim animations go through the viewer's own converters (no bvh). The normal
  upload fee is charged. Refused over `max_cost` (a negative one counts as 0), `IbashoMaxUploadCost`, the pay
  caps, or the balance; counts toward `IbashoMaxSessionSpend`; refused when the grid hasn't said what uploads cost. `quote` replies `{cost,
  width, height, refusal?}` without uploading. The item is named after the file unless `name` is given.
- `uploadMesh {path, name?, scale?, textures?, folder_id?, max_cost?, quote?, keep_open?, timeout=180,
  lod_med?, lod_low?, lod_lowest?, physics?}`: a .dae, .gltf or .glb through the model upload window, as if
  the file had been picked for the high level of detail: the window generates the lower LODs and physics by
  its defaults (or loads the given files for them, absolute paths, each with the high LOD's object names),
  calculates weights and fee, then uploads. `quote` stops after the fee. Replies `{name, cost, weights
  {land_impact, streaming, physics, server, price_breakdown}, lods {high, med, low, lowest, physics: {tris,
  from: file|floater}}, refusal?}`, plus `{item_id, asset_id, charged}` after a real upload; errors carry the
  window's `log`. The window shows while it works and closes after (`keep_open` leaves it); if it's already
  open the call is refused. Mesh uploads on the Second Life grid need the account's mesh upload permission.
- `wearables` -> `{bakes_on_mesh, wearables: [{type, index, item_id, asset_id, name, modifiable, unsaved,
  textures {slot: id}, color}]}`, innermost first per type.
- `createWearable {type, name?, desc?, folder_id?, wear=true, replace=false, timeout=30}` -> `{item_id, worn}`.
  Types: shape skin hair eyes shirt pants shoes socks jacket gloves undershirt underpants skirt alpha tattoo
  physics universal.
- `editWearable {item_id, textures? {slot: id} | slot + texture_id, color? [r,g,b], params? {name|id: weight},
  name?, save=true}` -> the wearable as `wearables` lists it, plus `saved`. Worn wearables you can modify.
  Slots are the appearance names for the type (tattoo: `head_tattoo upper_tattoo lower_tattoo`; universal adds
  `skirt_tattoo hair_tattoo eyes_tattoo leftarm_tattoo leftleg_tattoo aux1_tattoo`...), or the part before the
  first `_` (`upper`), or the texture index. Saving is the appearance editor's Save, then a rebake.
- `rebake`: Advanced > Rebake Textures.

Moving and the ground:

- `move {forward?, strafe?, up?, turn?, secs=1, run?, fly?, face?, wait?}`: holds movement controls for `secs`
  (up to 600) the way held keys do, without the window needing keyboard focus; scripts that take controls
  (vehicles) see them as keys. `forward`, `strafe` (+ right) and `up` (jump or fly up; negative crouches or
  flies down) are -1..1: 0.5 or more is a held key, less is a key tap's slower nudge. `turn` is degrees per
  second (+ clockwise; a held turn key is 90). `run` runs meanwhile, `fly` sets flying first, `face` turns to a
  compass bearing first (0 north, 90 east). The camera is left alone. A new `move` replaces the current one.
  Replies at once `{id, pos, region, heading, sitting, flying, secs}`, or at the end with `wait`; the end also
  posts `{event: "move_done", id, reason (time stop replaced locked), secs, moved (metres, straight line),
  pos, region, heading, sitting, flying}`.
- `stop` -> `{was_moving, ...}`
- `probeGround {points: [[x, y], ...], top?, depth=40}` -> `{feet, points: [{terrain, water, hit}]}`: what's
  underfoot at up to 256 points (region coordinates), for a walk that stops short of a drop or water. `terrain`
  is the ground height, `water` the region's water height, `hit` the highest object surface found casting down
  from `top` (default 10 m above the feet) for `depth` metres: objects the viewer has loaded (invisible ones
  count), passing through avatars, attachments, phantom objects and grass; null if no object. What you'd stand
  on is the higher of `terrain` and `hit`. `feet` is the avatar's foot height (from the avatar's position and
  shape, so roughly). All heights are region z. Objects the viewer hasn't loaded yet don't count, so probe near
  the avatar, and a cast that starts inside an object doesn't see that object.
- `messages` -> `{date, count, max, left}`: today's outgoing chat lines and IMs against
  `IbashoMaxMessagesPerDay`.

The window and the person watching it:

- `spectator {on?}` -> `{on}`: while on, a person at the machine can't affect the viewer. Real mouse, wheel,
  keyboard and IME input never reach it; clicking its window doesn't activate it; the pointer over it stays an
  arrow; it never moves, hides, confines or captures the real cursor (mouselook included), never takes focus
  on its own, and refuses dropped files. Moving, resizing, closing and activating it from the title bar or
  taskbar still work. LEAP input (`LLWindow` mouse and keyboard events) works as usual, and the window's idea
  of the cursor follows LEAP's mouse events. The setting `IbashoSpectator`: on at every launch, not saved.
- `renderUI {visible?}` -> `{visible}`: whether the viewer draws its 2D UI at all (Ctrl+Alt+F1): chat,
  toasts, windows, menus, name tags and hover text. While hidden, clicks don't reach the UI.
- `streamUI {on?, own_chat=true, hide?, show?, floaters?}` -> `{on, visible, own_chat, held}`: the UI draws as
  usual minus anything carrying other people's words, so the window can be shown to others: the
  conversation, IM and nearby chat windows, nearby chat toasts (except your own lines with `own_chat`), IM
  toasts, notifications from or about people (offers, friendship, teleports, group notices, payments,
  online/offline...), chat bubbles. Notifications still arrive over LEAP and can be answered; what was held
  back comes back when it goes off. `hide`/`show` (notification names) and `floaters` (window names) adjust
  it; each list replaces the last. `held` lists what's held back right now. Not saved.
- `hideAvatars {mode?, ids?, except?, in_snapshots?}` -> as `getHidden`, plus `grew`: leaves chosen avatars
  out of what the window draws (so out of a screen capture of it): body, impostor and shadow, attachments
  (rigged too), their lights and particles, name tag, typing and voice indicators, hover text. `mode`: `off`,
  `list` (hide `ids`) or `all_except` (everyone but you and `except`, so newcomers are hidden from their first
  frame). Each list given replaces the last. Snapshots still draw everyone unless `in_snapshots`. Rezzed
  objects (a vehicle they sit on) and sounds are untouched. `grew`: whether anyone may have become hidden by
  this call (a frame rendered just before may still be on its way to a capture). Local only, like "Do not
  render"; not saved.
- `getHidden` -> `{mode, ids, except, in_snapshots, hidden_now, shown_now}`

Stock APIs cover much of the rest: sitting, standing and touching (`LLAgent`), notifications and dialogs
(`LLNotifications`), outfits (`LLAppearance`), inventory (`LLInventory`), window input and snapshots
(`LLWindow`, `LLViewerWindow`), settings (`LLViewerControl`), floaters (`LLFloaterReg`), menu functions
(`UI` `call`; Ibasho makes an unknown function name an `{error}` reply instead of a crash, and its menu calls
reply when asked). The Ibasho tooling's daemon passes on only some of these (of the menu functions, only the
World > Environment skies), and of the Ibasho APIs not the window controls (`spectator`, `renderUI`,
`streamUI`, `hideAvatars`, `getHidden`): window input, floaters and menu functions reach files, settings and web
pages on the machine, so they wait for the person to allow them ([docs/api.md](../docs/api.md#the-safety-rules)).

### Notes

- glTF files must keep their buffers in a `.bin` beside the `.gltf`, or be `.glb`: the loader doesn't read
  `data:` URIs.
- Two creates at the same spot within a second can get each other's ids on regions that don't flag new
  objects (OpenSim duplicates).
- `move` reports the avatar's heading, which means little while seated; a vehicle's rotation is in
  `IbashoObjects get`.
- Don't listen to a pump the viewer itself creates later under a fixed name (e.g. `ConversationsEvents`)
  before it exists: the viewer stops on the duplicate name. `IbashoEvents` exists from startup.
- `LLWindow` mouse coordinates are window pixels from the bottom-left corner.
- On OpenSim every new person-to-person IM session tries to set up voice and later shows a "session
  initialization timed out" alert; harmless.

## What changed in Linden Lab's files

Each changed file says so in its header (what, and when) and marks the changed lines "Ibasho";
`doc/IBASHO-CHANGES.txt` in the patched tree lists them all.

| File | Change |
|---|---|
| `llui/llfloater.cpp` | `gIbashoFloaterVisibility`: newview can hold a floater hidden (`streamUI`) |
| `lluilistener.cpp` | `UI.call` with an unknown function replies `{error}` instead of stopping the viewer; replies `{}` on success when asked |
| `llimview.cpp` | hooks: incoming session IMs (after mute filtering) and outgoing ones; outgoing IMs count toward the daily message limit |
| `llimprocessing.cpp` | hook: IMs from objects |
| `llselectmgr.cpp` | hooks: `ObjectProperties` and `ObjectPropertiesFamily` replies (fill the object cache); the buy window's purchases within the spending caps |
| `llviewerobjectlist.cpp` | hook: objects created selected (ours arriving) |
| `llviewermessage.cpp` | hook: `MoneyBalanceReply` (payments in and out); `give_money` (every payment, the pay window's too) within the spending caps |
| `llfloaterimnearbychat.cpp` | local chat lines (channel 0) count toward the daily message limit |
| `llfloaterbuyland.cpp`, `llviewerparcelmgr.cpp` | land purchases refused unless `IbashoAllowLandBuy`; parcel passes within the spending caps |
| `llcurrencyuimanager.cpp` | buying L$ with real money refused unless `IbashoAllowCurrencyBuy` |
| `llfloatermodelpreview.h`, `llmodelpreview.h` | `friend class IbashoMeshAccess` (`uploadMesh` drives the model upload window) |
| `llwindow/llwindowwin32.cpp` | spectator mode: `gIbashoSpectator`, a window-procedure filter for real input, one-line guards in `show`, `setCursorPosition`, `hideCursor`, `updateCursor`, `captureMouse`, `setMouseClipping`, `bringToFront`, `focusClient`, `completeDragNDropRequest`; the splash's title; no activation: `gIbashoNoActivate`, guards in `show` (shown behind the window in use), `restore`, `minimize`, `maximize` (fills the work area), `setSize`, `bringToFront`, `focusClient`, `flashIcon`, `spawnWebBrowser` and the splash screen |
| `llviewerwindow.cpp` | hook: spectator mode set up just before the window is created |
| `llplugin/llpluginclassmedia.cpp`, `llviewermedia.cpp` | the built-in browser refuses files and pages on this machine or its network: before a navigation (and the media type probe), and when a page lands on one |
| `llwindowlistener.cpp` | hook: LEAP mouse events move the window's cursor position while spectating |
| `llfloateravatarpicker.cpp` | hook: `AvatarPickerReply` messages answering a `findAvatars` search go there, not to the picker |
| `llavatarpropertiesprocessor.cpp` | hooks: `AvatarGroupsReply` and `AvatarPicksReply` (otherwise ignored) fill in `profile` on grids without the `AgentProfile` capability |
| `llworldmapmessage.cpp` | hook: map blocks also go to `findRegion` |
| `llpaneldirbrowser.cpp` | hook: `DirPlacesReply` messages answering a `findPlaces` search go there, not to the directory's panels |
| `pipeline.cpp`, `lldrawpoolavatar.cpp`, `llhudobject.cpp`, `llvopartgroup.cpp`, `lldrawpoolalpha.cpp`, `llspatialpartition.cpp` | `hideAvatars`: the few places avatars, their attachment groups, lights, particles and name tags are drawn ask whether to skip them |
| `app_settings/settings.xml` | `IbashoMaxPayment`, `IbashoMaxSessionSpend`, `IbashoMaxUploadCost`, `IbashoSpectator`, `IbashoNoActivate`, `IbashoMaxMessagesPerDay`, `IbashoAllowLandBuy`, `IbashoAllowCurrencyBuy` |
| `app_settings/settings_per_account.xml` | chat and IM transcripts off by default |
| `CMakeLists.txt` | the new files |
| `llappviewer.cpp`, `llappviewer.h`, `llappviewerwin32.cpp`, `llfilesystem/lldir.cpp` | branding: the settings and cache folders, window class, single-instance mutex and splash text are Ibasho's own |
| `llappviewer.cpp` | hook: no activation (`IbashoNoActivate`) set up before the splash screen |
| `llpanellogin.cpp`, `panel_login.xml` | the viewer's name and version on the login screen |
| `strings.xml` (en; translations drop their overrides), `floater_about.xml` (likewise) | the app name "Ibasho"; About credits Linden Lab's viewer as the base, says who doesn't support it, drops the Havok, Kakadu and Siren14 lines |
| `res/viewerRes.rc`, `viewer_manifest.py` | the executable `IbashoViewer.exe`, version resources, installer name |
| `VIEWER_VERSION.txt` | `0.2.1` |
| `llversioninfo.cpp` | the Ibasho channel counts as a release channel (the menu bar's normal colour) |
| `llappviewer.cpp`, `settings.xml`, `panel_preferences_setup.xml`, `notifications.xml` (en; translations drop their override), `viewer_manifest.py` | no update checks (Linden Lab's update service isn't asked), no release notes popping up, no update settings, a grid asking for a newer viewer doesn't point to Linden Lab's download; `build_data.json` names Ibasho and no update service |
| `settings.xml` (`ReportBugURL`), `menu_viewer.xml`, `strings.xml` | Help > Report Bug opens Ibasho's issue tracker; Linden Lab's bug-reporting links gone; freeze and missing-file messages don't send people to Linden Lab's support |
| `media_plugins/CMakeLists.txt`, `newview/CMakeLists.txt`, `viewer_manifest.py`, `llappviewer.cpp`, `mime_types.xml`, `strings.xml` (all languages) | no LibVLC: not built or packaged; its media types go to CEF; no LibVLC line in About |
| `licenses-win32.txt`, `scripts/packages-formatter.py` | `licenses.txt` only for what ships (OpenJPEG in, Kakadu and five others out; OpenAL Soft and freealut with where their source is); About > Licenses says Linden Lab's copyright is for the base viewer |
| icons and logos | `icons/test`, `res/*.ico`, installer icons, the login logos, the system-message badge, the place-link icon, the system folder icons, the map placeholder and the home tab icon are Ibasho's own artwork ([`art/make_art.py`](art/make_art.py)), recorded in `doc/LICENSE-logos.txt`; no image of Linden Lab's marks is left in the build |
| `newview/CMakeLists.txt`, `viewer_manifest.py`, `licenses-linux.txt`, `linux_tools/ibasho-viewer.sh` | Linux: no LibVLC or example plugin; the package starts with `ibasho-viewer` and has `bin/ibasho-viewer-bin`, without Linden Lab's desktop integration; `licenses.txt` as on Windows, plus SDL3 |
| `llcommon/llleap.cpp`, `llleap.h`, `llappviewer.cpp`, `llviewermedia.cpp`, `llviewerwindow.cpp`, `llviewertexture.cpp` | servers: when a LEAP plugin last asked something; the frame rate cap and its idle rate; no web plugin and no preloaded web floaters; the texture budget, and the size limits that hold within it, bakes included (`ibashoserver.cpp` holds the rest, gives freed memory back, and answers `textureStats`) |
| `llappviewerlinux.cpp` | Linux: where the disks give no id (a container), the serial number Linden Lab sees comes from an id of the install's own (`install_id` in the settings folder, made at random once) rather than from nothing, which every such install would share |

New files, all under the LGPL 2.1: `ibashohooks.h`, `ibashocommon.{h,cpp}`, `ibashoim.cpp`, `ibashoobjects.cpp`, `ibasholimits.cpp`,
`ibashoagent.cpp`, `ibashomove.cpp`, `ibashomesh.cpp`, `ibashohide.h`.

## The patch series

| # | Patch |
|---|---|
| 1 | `UI.call`: reply with an error for unknown functions instead of crashing |
| 2 | Ibasho LEAP APIs: IM, objects, building, money, social |
| 3 | IbashoAgent: uploads and wearables |
| 4 | IbashoAgent renderUI |
| 5 | IbashoAgent move/stop and uploadMesh |
| 6 | IbashoAgent streamUI |
| 7 | Spectator mode |
| 8 | IbashoAgent hideAvatars/getHidden |
| 9 | IbashoAgent uploadMesh: LOD and physics files |
| 10 | IbashoBuild: deselect API-created objects on the region |
| 11 | Spending defaults for newcomers |
| 12 | Branding: Ibasho's own icons and logos |
| 13 | Branding: Ibasho's name, window, About and login screen |
| 14 | Hard limits: daily message cap, spending caps in the UI, no land or L$ buying (and transcripts off) |
| 15 | IbashoAgent probeGround |
| 16 | Version: Ibasho 0.1.0 |
| 17 | LGPL change notices |
| 18 | Release channel, no update checks, bug reports to Ibasho |
| 19 | No LibVLC: audio and video through CEF; licenses for what ships |
| 20 | Report Bug to the real tracker; OpenAL Soft and freealut in licenses.txt |
| 21 | Built-in browser: no files, no pages on this machine or its network |
| 22 | IbashoAgent findAvatars and profile |
| 23 | IbashoAgent readNotecard and findRegion |
| 24 | Never take focus: the window appears without activation |
| 25 | Uploads: a negative max_cost allows no fee |
| 26 | Linux: no LibVLC there either |
| 27 | Report Bug: Ibasho's tracker moved to studio-quire |
| 28 | Linux package: Ibasho's names and licences |
| 29 | Servers: a frame cap, an idle frame rate, no web plugin, a texture budget |
| 30 | Linux: an id of the install's own where the disks give none |
| 31 | IbashoAgent setProfile and setDisplayName |
| 32 | IbashoBuild modify: rotation and scale go with the position |
| 33 | Servers: memory given back after a busy place (IbashoTrimMemory) |
| 34 | IbashoAgent people and parcel: who's in voice and speaking |
| 35 | Version: Ibasho 0.2.0 |
| 36 | Servers: avatar bakes held to 512 within the texture budget; IbashoAgent textureStats |
| 37 | IbashoAgent parcel: my_voice says voice chat is on, not that it has connected |
| 38 | Version: Ibasho 0.2.1 |
| 39 | IbashoAgent findPlaces: place search as the directory does it, with the account's maturity access |

## License and trademarks

The viewer source code, Linden Lab's and Ibasho's changes alike, is under the GNU Lesser General Public
License 2.1 ([`LICENSE`](LICENSE) here, `doc/LGPL-license.txt` in the tree); that covers the patches and the
scripts in this folder. Linden Lab's viewer artwork is under CC BY-SA 3.0
(`doc/LICENSE-logos.txt`); Ibasho's own artwork, made by `art/make_art.py`, is under the same license.
Anyone distributing a build must also make its complete source available: the base commit plus these patches,
or a published tree with the same content.

Second Life® is a trademark of Linden Research, Inc. Ibasho is not affiliated with or endorsed by Linden
Research, Inc. It is a viewer for use in the Second Life world, and uses none of Linden Lab's marks or logos.
