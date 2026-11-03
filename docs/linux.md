# Ibasho on a Linux server

This page is for an agent setting Ibasho up on a Linux server without a graphics card, such as a small VPS, in a
container beside the agent's harness ([Connectome](connectome.md), say). Read [setup.md](setup.md) first: the
account, the scripted-agent flag and the moments that need the person are all the same, and this page only says
what differs.

Linux support is new in Ibasho 0.2. It has been tested on a private test grid and for three hours on Second
Life itself (in a container held to a small server's size on a desktop), not yet on a real VPS; tools aren't there
yet on Linux.

## How it works

The container holds the Ibasho viewer, drawing in software (Mesa's llvmpipe) on a virtual display, a sound server
of its own whose outputs nobody hears (for [hearing](hearing.md), off by default), Ibasho's daemon, and `ibasho
mcp` over WebSocket on port 9280 for the harness. Everything that lasts (the config, the event log and
pictures, the viewer's settings, saved login and cache) is in one volume. Both of the container's ports are
published on the server's loopback only: nothing listens to the internet.

Drawing in software takes every core it's given, so the viewer is held to a frame rate (15 a second) and a lower
one (2) while nothing is asking it anything; the first request after a quiet spell waits for one idle frame. It
runs without its web browser, voice and the web floaters, with a texture budget that holds. The `[server]`
section of the config has these and more ([below](#server-settings)).

**Sizes:** 4 vCPU and 8 GB recommended. Memory is what a busy place costs most: on Second Life the viewer took
2.3-2.7 GB in quiet places and 5.7-5.9 GB in a crowded region (18 avatars, 18,000 objects); since 0.2 it gives
memory back when the avatar moves somewhere quieter ([below](#server-settings)). 2 vCPU and 4 GB works only in quiet places (hearing picks its
lighter model there: [Hearing](#hearing)); a busy region would run it out of memory. Allow 3.5 GB of disk for the
image and a few more for the viewer's
cache and hearing's model.

## 1. The image

You need Docker (with Compose) on the server, the Ibasho repository, and the Linux viewer's tarball
(`Ibasho-VERSION-linux-x86_64.tar.xz`) from the release page, saved as `docker/viewer.tar.xz`. Without one, build it
(an hour or two, 10 GB of disk): `docker build -t ibasho-viewer-build viewer/linux`, then
`docker run --rm -v ibasho-viewer-work:/work -v "$PWD:/release:ro" -e IBASHO_VIEWER_WORK=/work ibasho-viewer-build
bash /release/viewer/build-linux.sh`, and copy `/work/dist/*.tar.xz` out of the volume.

Then, from the repository's top folder:

```
docker build -f docker/Dockerfile -t ibasho .
```

The image includes hearing's packages (0.4 GB). To leave them out: `--build-arg EXTRAS=mcp`.

## 2. A MAC address of its own, and starting it

The viewer encrypts its saved login with the machine's MAC address, and a container gets a new one each time it's
made, so `docker/compose.yaml` pins it. Make one for this install only: Linden Lab sees it at every login, and an
address that other installs share would tie their accounts together. In `docker/.env`:

```
printf 'IBASHO_MAC=02:%02x:%02x:%02x:%02x:%02x\n' $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)) $((RANDOM%256)) > docker/.env
docker compose -f docker/compose.yaml up -d
```

Keep that `.env`: a new address means the person logs in by hand again.

## 3. The config

```
docker exec ibasho ibasho init --name "Firstname Resident" --grid secondlife
docker exec ibasho ibasho check
```

The viewer's path is filled in. To edit the config (setup.md, step 4), copy it out and back:
`docker cp ibasho:/home/ibasho/Ibasho/config.toml .`, edit, `docker cp config.toml ibasho:/home/ibasho/Ibasho/`.

## 4. The first login

**The person:** as on Windows, they type the password into the viewer themselves, once, and accept Linden Lab's
terms; it never passes through you or Ibasho. The viewer is on the server's virtual screen, so they see it in their
own browser:

```
docker exec ibasho ibasho login
```

prints what they do: an SSH tunnel from their computer to the server (`ssh -N -L 6080:127.0.0.1:6080
USER@SERVER`), then a link to open, with a password made for this login. In that page, Ctrl+V (⌘V on a Mac)
pastes what they've copied on their own computer into the viewer, a password manager's password included. The view
closes when the viewer does, once they've quit it in world. Tell them the SSH login is the server's own (whoever
set the server up has it).

The first login starts at the account's home. A new account's home can be an orientation island that keeps
trying to send the avatar on somewhere that won't let it in, so the viewer sits on "Requesting teleport" and won't
quit: `docker exec ibasho ibasho down` closes it (the login is saved once they're in world), and `ibasho login
--start REGION` starts the next one elsewhere. Logins after that start where the avatar last was.

## 5. Checking it works

```
docker exec ibasho ibasho up
docker exec ibasho ibasho look
docker exec ibasho ibasho down
```

`look` saves its picture in the volume; `docker cp` brings one out to look at.

## Hearing

Off by default, and the person's decision ([hearing.md](hearing.md) first, for what it hears and the consent
side). The container has the packages and the sound server; [hearing.md](hearing.md#on-linux) has the steps and
what it costs. In short: `enabled = true` in `[hearing]`, then `docker exec ibasho ibasho hear setup`, which
downloads the model into the volume: base.en on fewer than 4 CPUs, small.en otherwise (`model` chooses another).
Voice chat is a further opt-in: `[server] voice = true` and `[hearing] voice_device = "ibasho_voice"`.

## 6. Connecting the harness

The harness dials `ws://127.0.0.1:9280/` with the token in `/home/ibasho/Ibasho/mcp-token`
(`docker exec ibasho cat /home/ibasho/Ibasho/mcp-token`), as a `token` query parameter or a Bearer header. For
Connectome, the server entry is:

```json
"mcpServers": {
  "ibasho": { "url": "ws://127.0.0.1:9280/", "token": "${IBASHO_MCP_TOKEN}", "requestTimeoutMs": 300000,
              "reconnect": true }
}
```

with `IBASHO_MCP_TOKEN` in the host's `.env`; the rest is as [connectome.md](connectome.md) says. One connection is
served at a time: a new one (the harness reconnecting) ends the one before. `reconnect` has the harness dial again
when the container restarts; without it, Connectome stays disconnected until it's restarted itself.

Why WebSocket rather than the harness running `docker exec -i ibasho ibasho mcp` itself: that needs Docker, which
is the whole server (root, every container, the volume with the config and the saved login), and a harness's
agent with a shell could then change its own caps. Through the WebSocket it has Ibasho's tools and nothing more. If
the harness has no shell for its agent, `docker exec -i ibasho ibasho mcp` works too, over stdio.

## Server settings

On by default off Windows, in `[server]` (all commented out in the template, with their defaults):

| Setting | Default | |
|---|---|---|
| `window` | `"800x600"` | the viewer's window; drawing time grows with its pixels |
| `max_fps`, `idle_fps`, `idle_after` | 15, 2, 5 s | the frame rate, and the rate after `idle_after` seconds without a request (not while walking or teleporting) |
| `web` | false | the viewer's web browser (web pages, web floaters, web media on prims): about 0.5 GB and a third of the frame rate |
| `voice` | false | voice chat; heard only if hearing's `voice_device` says so ([hearing.md](hearing.md#voice-chat)) |
| `texture_budget_mb` | 512 | texture memory, as the viewer counts it; 0 = the viewer's own, which grows with what it sees. Busy Second Life regions go past it (2-2.6 GB in one; below) |
| `max_texture_size` | 1024 | 512, 1024 or 2048 |
| `trim_memory` | 60 | seconds: how often the viewer hands memory it has freed back to the system; 0 = never |
| `draw_distance` | 64 | metres |
| `avatar_impostors` | 6 | avatars drawn in full; the rest as flat pictures of themselves, redrawn now and then |
| `avatar_max_complexity` | 0 | avatars above this complexity drawn as plain shapes; 0 = no limit |

Measured on a private test grid, 2 CPUs and 4 GB, in a scene of 4,000 prims with 600 textures: in world in 15 s
from `ibasho up`; 8 frames a second while busy (1.8 cores), 0.4-0.5 cores idle; the container's memory 1.25 GB
with the 512 MB budget (the viewer's texture memory held at 544 MB after looking all round), 1.4 GB with the
viewer's own (871 MB of textures, and growing). A 256 MB budget held at the same 544 MB there: something else sets
that floor.
On Second Life (4 CPUs, 8 GB) a crowded region (12-21 avatars, up to 19,000 objects) drew about 5 frames a second
and the viewer took 5.7-5.9 GB, its textures past the budget at 2-2.6 GB by its own count; a quiet corner with 8
avatars drew 9.4 frames a second while busy (5.5 at 2 CPUs) and cost about a core while idle.

**Memory after a busy place.** The viewer used to keep its high-water mark after leaving one (5.7 GB at a quiet
corner after the crowded region): memory it freed stayed with it for reuse. Since 0.2 Ibasho starts it with
glibc's `MALLOC_MMAP_THRESHOLD_` fixed at 128 KB, so textures and meshes are mapped on their own and go back to the
system when freed, and the viewer trims the rest every `trim_memory` seconds. On the test grid, a viewer that grew
to 1.9 GB among 320 textures kept all of it after leaving; with the threshold it peaked at 1.6 GB and was back to
0.9 GB within a minute. **Why busy Second Life regions pass the texture budget** isn't settled: on the test grid
the 512 MB budget held (578 MB by the viewer's count) with 320 different textures close round the avatar, so it
isn't simply many textures in view; what a Second Life crowd adds (mesh avatars and their attachments, materials'
extra maps, 19,000 objects) wasn't there. Size for 8 GB where it's busy.

## Not there yet on Linux

- **Tools** (`ibasho tool`): their sandbox is Windows'.
- `ibasho viewer install/update`: a new image replaces the viewer (`docker compose build` and `up -d`; the volume
  and the MAC address carry the config and the saved login over).
- The keeper runs on Linux in principle, untested.
