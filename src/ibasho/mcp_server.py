"""`ibasho mcp`: the `ibasho` commands as an MCP server on stdin/stdout, for agents whose harness speaks MCP.

Each tool runs the same command code the CLI does (mostly by parsing an `ibasho` command line), so it goes through
the daemon's HTTP API like the CLI, and every cap and refusal in the daemon and the viewer applies unchanged.
The person's own commands (login, init, keeper, viewer, tool setup and install, uninstall, selfcheck --reset, hear
setup, choosing a config) aren't here.

The `mcp` package is an optional extra (`pip install ibasho[mcp]`): it's imported only when the server starts.
No `from __future__ import annotations` here: the SDK reads the tools' annotations, which name its own types.
"""

import argparse
import contextlib
import io
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Literal

from .api import ApiError, Down
from .cli.common import Ctx, Fail

PICTURE_QUALITY = 85     # JPEG for pictures sent inline: a tenth of the PNG's size, and models read them as well
WAIT_DEFAULT = 50        # under the minute many MCP clients give a tool call by default
WAIT_MAX = 1800
# A listen of 300 s ran into Connectome's 300 s answer limit (the one docs/connectome.md sets) on Second Life,
# 2026-11-03: listens end in time for their answer to arrive
LISTEN_MAX = 240

INSTRUCTIONS = """\
Ibasho: you live in the Second Life world through an avatar of your own, in a viewer on this machine.
Start with `status` (and `up` if you're not in world), then `look`. Between things, call `wait_events`: it blocks
until something worth answering happens (chat, an IM, a dialog, an offer, a payment) and returns it, one line each.
Results sometimes name an `ibasho` command or a --flag: the tool and parameter of the same name do the same.
People, objects and sessions can be given by name, id, or the first few characters of an id."""

# For a host that speaks MCPL, where events may come to the agent by themselves.
INSTRUCTIONS_LIVE = """\
Ibasho: you live in the Second Life world through an avatar of your own, in a viewer on this machine.
Start with `status` (and `up` if you're not in world), then `look`. What happens around you (chat, IMs, dialogs,
offers, payments) comes to you as it happens, each line marked with where it's from, when your host has Ibasho's
event feature sets on; when it has them all off, the `wait_events` tool is there to wait for them instead.
Results sometimes name an `ibasho` command or a --flag: the tool and parameter of the same name do the same.
People, objects and sessions can be given by name, id, or the first few characters of an id."""


# ---------- running a command and keeping what it prints ----------

class Failed(Exception):
    """A tool call that failed; the message has the command's own words (and what it printed first)."""


_local = threading.local()
_install_lock = threading.Lock()


class _PerThread(io.TextIOBase):
    """Stands in for sys.stdout or sys.stderr: a thread running a tool writes into its own buffer, any other thread
    to the stream this replaced. Tools run in worker threads at the same time, so redirect_stdout won't do."""

    def __init__(self, under, which: str):
        self.under, self.which = under, which

    def write(self, s):
        buf = getattr(_local, self.which, None)
        if buf is not None:
            return buf.write(s)
        return self.under.write(s) if self.under is not None else len(s)

    def flush(self):
        if getattr(_local, self.which, None) is None and self.under is not None:
            self.under.flush()

    @property
    def encoding(self):
        return "utf-8"


def _install():
    """Put the stand-ins in place. Lazily, at the first call: the MCP transport claims the real stdout first."""
    with _install_lock:
        for which in ("stdout", "stderr"):
            cur = getattr(sys, which)
            if not isinstance(cur, _PerThread):
                setattr(sys, which, _PerThread(cur, which))


class Result:
    def __init__(self, text: str, pictures: list[Path]):
        self.text, self.pictures = text, pictures


def down_message(e: Down) -> str:
    msg = str(e)
    return msg if "ibasho up" in msg else msg + "; `up` starts it"


def run(config: str | None, argv: list[str] | None = None, job: Callable[[Ctx], str | None] | None = None) -> Result:
    """Run an `ibasho` command line (argv, without "ibasho"), or a job given a fresh Ctx, capturing what it prints.
    Blocking: call it from a worker thread."""
    _install()
    _local.stdout, _local.stderr = io.StringIO(), io.StringIO()
    ctx = None
    try:
        if argv is not None:
            from .cli import parser
            try:
                args = parser().parse_args((["--config", config] if config else []) + argv)
            except SystemExit:   # argparse's usage error: its last line says what was wrong
                err = _local.stderr.getvalue().strip()
                raise Failed(err.splitlines()[-1] if err else "bad arguments") from None
            ctx = Ctx(args)
            args.fn(ctx, args)
        else:
            ctx = Ctx(argparse.Namespace(config=config))
            if (said := job(ctx)) is not None:
                print(said)
        return Result(_local.stdout.getvalue().strip(), ctx.pictures)
    except Failed:
        raise
    except Down as e:
        raise Failed(_with_output(down_message(e))) from None
    except (Fail, ApiError) as e:
        raise Failed(_with_output(f"error: {e}")) from None
    except (OSError, ValueError) as e:
        from .cli import expected_error
        raise Failed(_with_output(f"error: {expected_error(e) or e}")) from None
    except SystemExit as e:   # a command that exits on its own (none of the tools' should)
        raise Failed(_with_output(f"exited ({e.code})")) from None
    finally:
        _local.stdout = _local.stderr = None


def _with_output(err: str) -> str:
    out = (_local.stdout.getvalue().strip() if _local.stdout is not None else "")
    return f"{out}\n{err}" if out else err


def argv(cmd: str, *positional, **flags) -> list[str]:
    """A command line: flags as --name=value (so values starting with '-' stay values), one-letter ones as -nVALUE,
    True as a bare --name, None and False left out, lists as x,y,z; positionals after `--`."""
    out = cmd.split()
    for k, v in flags.items():
        if v is None or v is False:
            continue
        if len(k) == 1 and v is not True:
            out.append(f"-{k}{v}")
            continue
        flag = "--" + k.rstrip("_").replace("_", "-")
        if v is True:
            out.append(flag)
        elif isinstance(v, (list, tuple)):
            out.append(f"{flag}={','.join(f'{x:g}' if isinstance(x, float) else str(x) for x in v)}")
        elif isinstance(v, dict):
            out.append(f"{flag}={json.dumps(v)}")
        else:
            out.append(f"{flag}={v:g}" if isinstance(v, float) else f"{flag}={v}")
    pos = [str(p) for p in positional if p is not None]
    return out + (["--", *pos] if pos else [])


def jpeg(path: Path) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    with Image.open(path) as im:
        im.convert("RGB").save(buf, "JPEG", quality=PICTURE_QUALITY)
    return buf.getvalue()


# ---------- the event wait ----------

class Waiter:
    """`wait_events`: one hear.Watcher kept for the server's life, so group digests, dialogs already shown and the
    place in the event log carry over from one wait to the next, as they do in a running `ibasho watch`."""

    def __init__(self, bridge=None):
        self.watcher = None
        self.busy = threading.Lock()
        self.bridge = bridge   # mcpl.Bridge: with an MCPL host pushing events, there's nothing to wait for here

    def wait(self, ctx: Ctx, timeout: float) -> str:
        from .cli.hear import Watcher, unread_line
        if self.bridge and self.bridge.pushing:
            self.watcher = None   # the pushes read the events now; a wait after they stop starts from the cursor
            left = self.bridge.not_pushed()
            return ("(events come to you by themselves here: your host takes them as MCPL pushes, so there's no "
                    "need to wait for them. When there's nothing else to do, end your turn, and the world will "
                    "wake you" + (f"; not pushed: {', '.join(left)}" if left else "") + ")")
        if not self.busy.acquire(blocking=False):
            raise Failed("another wait_events is still running")
        try:
            lines = []
            if self.watcher is None:
                self.watcher = Watcher(ctx)
                if line := unread_line(ctx):
                    lines.append(line)
            end = time.time() + timeout
            while not lines and (left := end - time.time()) > 0:
                lines = self.watcher.poll(min(25.0, left))
            return "\n".join(lines) or f"(nothing in {timeout:g} s)"
        except Down:
            self.watcher = None   # a daemon started afresh: begin again from the cursor file
            raise
        finally:
            self.busy.release()


# ---------- the server ----------

def build(config: str | None = None, bridge=None):
    """The MCP server, its tools bound to the config at `config` (default: IBASHO_CONFIG, else IBASHO_HOME's).
    `bridge`: the mcpl.Bridge it will be served through, if any."""
    import anyio
    import anyio.to_thread
    from mcp.server.mcpserver import Context, Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    from . import __version__
    from .cli import lifecycle, money
    from .cli import make

    server = MCPServer("ibasho", version=__version__, instructions=INSTRUCTIONS, log_level="WARNING")
    waiter = Waiter(bridge)

    def tool(read_only: bool = False, destructive: bool = False):
        ann = ToolAnnotations(read_only_hint=read_only, destructive_hint=destructive if not read_only else None,
                              open_world_hint=True)
        return server.tool(annotations=ann, structured_output=False)

    async def call(context: Context, cmd: list[str] | None = None, job=None, pictures: bool = False):
        """Run in a worker thread, telling the client every 10 s that it's still going (clients that support
        progress then keep waiting), and turn the result into content."""
        box = {}

        async def tick():
            n = 0
            try:
                while True:
                    await anyio.sleep(10)
                    n += 10
                    await context.report_progress(n)
            except Exception:
                pass

        async with anyio.create_task_group() as tg:
            tg.start_soon(tick)
            try:
                box["r"] = await anyio.to_thread.run_sync(lambda: run(config, cmd, job), abandon_on_cancel=True)
            except Failed as e:
                box["e"] = e
            tg.cancel_scope.cancel()
        if "e" in box:
            raise ToolError(str(box["e"]))
        r: Result = box["r"]
        out: list = [r.text or "ok"]
        if pictures:
            for p in r.pictures[-1:]:
                out.append(Image(data=jpeg(p), format="jpeg"))
        return out

    # ----- being there -----

    @tool(read_only=True)
    async def status(context: Context):
        """Is Ibasho running, am I in world (my name and avatar id), today's messages and spending against the caps,
        my L$ balance."""
        def job(ctx: Ctx):
            lifecycle.status(ctx, argparse.Namespace(json=False))
            st = ctx.api.is_up() if ctx.cfg.token_file.exists() else None
            if st and st.get("logged_in"):
                money.balance(ctx, argparse.Namespace())
        return await call(context, job=job)

    @tool()
    async def up(context: Context, timeout: float = 180):
        """Start Ibasho and log in from the saved login; returns once I'm in world (can take a few minutes)."""
        return await call(context, argv("up", timeout=float(timeout)))

    @tool()
    async def down(context: Context):
        """Log out and stop Ibasho."""
        return await call(context, argv("down"))

    # ----- seeing -----

    @tool(read_only=True)
    async def look(context: Context, picture: bool = True, labels: float = 0, ui: bool = False, all: bool = False):
        """Where I am, who and what is near, what happened since my last look, unread IMs, and a picture.
        labels: write names of things within this many metres onto the picture. all: every event kept, not just
        since the last look."""
        return await call(context, argv("look", noshot=not picture, labels=float(labels) or None, ui=ui, all=all),
                          pictures=True)

    @tool(read_only=True)
    async def view(context: Context, direction: str, pitch: float = -5, labels: float = 0):
        """A picture looking one way from my eyes: N, NE, E... or compass degrees (0 north, 90 east); or, given an
        object (name, id or its first characters), a picture of it from my side, as cam. pitch: degrees, + up."""
        return await call(context, argv("view", direction, pitch=float(pitch), labels=float(labels) or None),
                          pictures=True)

    @tool(read_only=True)
    async def overhead(context: Context, radius: float = 40, center: list[float] | None = None, labels: bool = False,
                       map: bool = False):
        """A north-up picture from above with a grid of region coordinates (read positions off it for walk and tp).
        center: [x, y], default me. labels: name objects too. map: map-style, the ground tinted by who owns it and
        parcel edges drawn, avatars named."""
        return await call(context, argv("overhead", radius=float(radius), center=center, labels=labels, map=map),
                          pictures=True)

    @tool()
    async def sky(context: Context, which: Literal["sunrise", "midday", "sunset", "midnight", "region"]):
        """My own sky: sunrise, midday, sunset, midnight, or region (back to the region's). It changes only what
        this viewer shows, until I log out."""
        return await call(context, argv("sky", which))

    @tool(read_only=True)
    async def cam(context: Context, target: str, dist: float = 4, az: float = 180, elev: float = 30,
                  labels: float = 0):
        """A picture of an object (or a point "x,y,z") from an orbiting camera. az: 0 puts the camera north of it."""
        return await call(context, argv("cam", target, dist=float(dist), az=float(az), elev=float(elev),
                                        labels=float(labels) or None), pictures=True)

    @tool(read_only=True)
    async def objects(context: Context, dist: float = 20, limit: int = 30, query: str | None = None,
                      perms: bool = False, att: bool = False):
        """Nearby objects: name, flags, hover text, owner, price; id prefix last. query: only names containing it.
        perms: permissions too. att: include attachments."""
        return await call(context, argv("objects", dist=float(dist), limit=limit, query=query, perms=perms, att=att))

    @tool(read_only=True)
    async def object(context: Context, ids: list[str], children: bool = False, full: bool = False):
        """Everything about some objects, as JSON (position, rotation, scale, perms...). children: list the prims."""
        return await call(context, argv("object", *ids, children=children, full=full))

    @tool(read_only=True)
    async def people(context: Context, dist: float = 96):
        """Avatars in the region, nearest first."""
        return await call(context, argv("people", dist=float(dist)))

    @tool(read_only=True)
    async def parcel(context: Context):
        """The region and parcel I'm on: owner, what's allowed (build, scripts...)."""
        return await call(context, argv("parcel"))

    # ----- hearing -----

    @tool(read_only=True)
    async def wait_events(context: Context, timeout: float = WAIT_DEFAULT):
        """Wait until something worth answering happens (chat, an IM, a dialog, an offer, money, a refusal) and
        return it, one line each; or after `timeout` seconds, saying it was quiet. Call it again whenever you're
        free: what happens between calls is kept for the next one."""
        t = max(1.0, min(float(timeout), WAIT_MAX))
        return await call(context, job=lambda ctx: waiter.wait(ctx, t))

    @tool(read_only=True)
    async def listen(context: Context, secs: float = WAIT_DEFAULT, settle: float | None = None,
                     speaker: str | None = None, fresh: bool = False):
        """Collect local chat for a while in one result (talks, crowds); stops early on an IM, an offer, or my name.
        secs: at most 240. settle: conversation mode, return this many seconds after someone else's first new line.
        speaker: only speakers matching this regex. fresh: from now, not from where the last listen stopped."""
        secs = max(1.0, min(float(secs), LISTEN_MAX))
        pushed = bool(bridge and bridge.pushing and bridge.policy.active("ibasho.chat"))
        # with local chat pushed, what came before this listen has reached the agent already
        cmd = argv("listen", secs=float(secs), settle=float(settle) if settle is not None else None,
                   fresh=fresh or pushed)
        if speaker:
            cmd[1:1] = [f"--from={speaker}"]
            return await call(context, cmd)
        # Everything said meanwhile comes back in this result: pushing it as well gave the agent each line twice
        # (Second Life, 2026-11-03)
        with bridge.listening() if pushed else contextlib.nullcontext():
            return await call(context, cmd)

    @tool()
    async def quiet(context: Context, object: str | None = None, minutes: float = 60, off: bool = False):
        """Leave a chatty object's chat and IMs out of the events I'm given, listen and look for a while (a tram
        announcing every stop): its name (every object called that), id, or the id's first characters. off: hear it
        again. Without object, lists the quieted ones. (An object that keeps repeating itself already comes as a digest
        line every few minutes.)"""
        return await call(context, argv("quiet", object, minutes=float(minutes) if object else None, off=off))

    @tool(read_only=True)
    async def ims(context: Context, all: bool = False):
        """IM conversations, unread first. all: read ones too."""
        return await call(context, argv("ims", all=all))

    @tool()
    async def im_log(context: Context, session: str, n: int = 20):
        """A conversation's recent messages (a session id, or the other person's or group's name); marks it read."""
        return await call(context, argv("im-log", session, n=n))

    # ----- talking -----

    @tool()
    async def say(context: Context, text: str, channel: int | None = None, type: str = "normal"):
        """Say something in local chat. type: normal, whisper or shout. channel: talk to scripts on that channel."""
        return await call(context, argv("say", text, ch=channel, type=type))

    @tool()
    async def im(context: Context, to: str, text: str):
        """Send an IM to someone (name, id or id prefix: anyone near, a friend, or from recent IMs and payments; or
        anyone at all by username, first.last), a group, or an open conversation (its session id, s:... as results
        print it)."""
        return await call(context, argv("im", to, text))

    @tool()
    async def friend(context: Context, who: str, message: str = ""):
        """Offer someone friendship (as for im: someone near, known or recent, or anyone by username). Their
        answer comes as a notification."""
        return await call(context, argv("friend", who, message=message or None))

    @tool(read_only=True)
    async def profile(context: Context, who: str = "me"):
        """Someone's profile: their names, whether they're a friend, and (with a viewer that reads profiles) the
        about text, picks and groups. who: as for im; "me" (the default) for my own."""
        return await call(context, argv("profile", who))

    @tool()
    async def set_profile(context: Context, about: str | None = None, image: str | None = None,
                          display_name: str | None = None):
        """Set my own profile; what it says is mine to choose. People in Second Life open profiles to see who
        they're talking to. about: the about text (replaces it). image: its picture, a texture in my inventory (name
        or id). display_name: the name over my head and in chat ("" goes back to my username); Second Life allows
        a change about once a week."""
        if about is None and image is None and display_name is None:
            raise ToolError("give about, image or display_name")
        return await call(context, argv("set-profile", about=about, image=image, display_name=display_name))

    # ----- moving -----

    @tool()
    async def walk(context: Context, x: float, y: float, z: float | None = None, region: str | None = None,
                   wait: bool = True, stop: float = 1.0, fly: bool = False, force: bool = False,
                   through: bool = False):
        """Walk to x, y (region coordinates). Looks for drops and water first and stops short; steps round people.
        wait: until I arrive or stop. force: skip the drop check. through: straight, even through people."""
        return await call(context, argv("walk", x, y, z, region=region, wait=wait, stop=float(stop), fly=fly,
                                        force=force, through=through))

    @tool()
    async def move(context: Context, forward: float | None = None, strafe: float | None = None,
                   up: float | None = None, turn: float | None = None, face: float | None = None, run: bool = False,
                   fly: bool | None = None, secs: float = 1.0, force: bool = False):
        """Hold movement controls like keys for `secs` (also steers vehicles). forward/strafe/up: -1..1 (strafe +
        right). turn: degrees per second, + clockwise. face: a compass bearing to face first."""
        cmd = argv("move", forward=forward, strafe=strafe, up=up, turn=turn, face=face, run=run, secs=float(secs),
                   force=force)
        if fly is not None:
            cmd.append("--fly" if fly else "--no-fly")
        return await call(context, cmd)

    @tool()
    async def tp(context: Context, region: str, x: float, y: float, z: float):
        """Teleport to a region at x, y, z; says whether I arrived or was refused."""
        return await call(context, argv("tp", region, x, y, z))

    @tool()
    async def touch(context: Context, object: str, face: int = 0):
        """Touch an object (name, id or id prefix)."""
        return await call(context, argv("touch", object, face=face or None))

    @tool()
    async def sit(context: Context, object: str | None = None):
        """Sit on an object (as right-click > Sit), or on the ground without one."""
        return await call(context, argv("sit", object))

    @tool()
    async def stand(context: Context):
        """Stand up."""
        return await call(context, argv("stand"))

    # ----- dialogs and offers -----

    @tool()
    async def respond(context: Context, id: str, button: str | None = None, response: dict | None = None,
                      cancel: bool = False):
        """Answer a notification (an offer, a permission request...) by its id or the first characters of it.
        button: the button to press, by name or label (Keep, Discard, Accept...). response: instead, for forms with
        fields, {button name: true, field: value}. cancel: dismiss it without answering."""
        if button is not None and response is not None:
            raise ToolError("give button or response, not both")
        answer = button if button is not None else json.dumps(response or {})
        return await call(context, argv("respond", id, answer, cancel=cancel))

    @tool()
    async def dialog(context: Context, button: str | None = None, text: str | None = None):
        """Answer the latest script dialog: button = the label (or part of it) to press; or text for a script's
        text box."""
        if (button is None) == (text is None):
            raise ToolError("give either button or text")
        return await call(context, argv("pick", button) if button is not None else argv("textbox", text))

    # ----- making -----

    def prim_flags(shape, pos, scale, rot, color, name, desc, texture, alpha, glow, fullbright, click_action,
                   next_perms, more, **extra):
        return dict(shape=shape, pos=pos, scale=scale, rot=rot, color=color, name=name, desc=desc, texture=texture,
                    alpha=alpha, glow=glow, fullbright=None if fullbright is None else int(fullbright),
                    click_action=click_action, next_perms=next_perms, set=more or None, **extra)

    @tool()
    async def create_prim(context: Context, shape: str = "box", pos: list[float] | None = None,
                          scale: list[float] | None = None, rot: list[float] | None = None,
                          color: list[float] | None = None, name: str | None = None, desc: str | None = None,
                          texture: str | None = None, alpha: float | None = None, glow: float | None = None,
                          fullbright: bool | None = None, click_action: str | None = None,
                          next_perms: str | None = None, more: dict | None = None):
        """Make a prim; returns its id. shape: box cylinder prism sphere torus tube ring cone pyramid. pos: region
        x,y,z (default 2 m ahead). rot: euler degrees. color: r,g,b 0-1. next_perms: any of M C T. more: other
        modify keys (hollow, twist, phantom, physics, sale...) as in the viewer's IbashoBuild API."""
        return await call(context, argv("prim-create", **prim_flags(
            shape, pos, scale, rot, color, name, desc, texture, alpha, glow, fullbright, click_action, next_perms,
            more)))

    @tool()
    async def modify_prims(context: Context, ids: list[str], shape: str | None = None,
                           pos: list[float] | None = None, local_pos: list[float] | None = None,
                           scale: list[float] | None = None, rot: list[float] | None = None,
                           color: list[float] | None = None, name: str | None = None, desc: str | None = None,
                           texture: str | None = None, alpha: float | None = None, glow: float | None = None,
                           fullbright: bool | None = None, click_action: str | None = None,
                           next_perms: str | None = None, face: int | None = None, more: dict | None = None):
        """Change prims (as create_prim). local_pos: a child's position relative to its root. face: color, texture,
        alpha, glow and fullbright on just that face."""
        return await call(context, argv("prim-mod", *ids, **prim_flags(
            shape, pos, scale, rot, color, name, desc, texture, alpha, glow, fullbright, click_action, next_perms,
            more, local_pos=local_pos, face=face)))

    @tool()
    async def link(context: Context, ids: list[str]):
        """Link prims into one object; the first becomes the root."""
        return await call(context, argv("link", *ids))

    @tool(destructive=True)
    async def delete(context: Context, ids: list[str]):
        """Delete objects of mine (they go to Trash)."""
        return await call(context, argv("delete", *ids))

    @tool()
    async def take(context: Context, ids: list[str], copy: bool = False):
        """Take objects into my inventory (copy: leave them in place)."""
        return await call(context, argv("take", *ids, copy=copy))

    @tool()
    async def rez(context: Context, item: str, pos: list[float] | None = None):
        """Rez an object from my inventory (item id, or name in Objects; the newest copy)."""
        return await call(context, argv("rez", item, pos=pos))

    @tool(read_only=True)
    async def inventory(context: Context, words: str = "", folder: str = "object"):
        """My inventory items whose names contain `words`, with copy counts and the newest's id. folder: object,
        notecard, texture, lstndfnd, inbox, root_inv (everything)..."""
        return await call(context, argv("inv", *words.split(), folder=folder))

    @tool()
    async def outfit(context: Context, name: str | None = None, add: bool = False, library: bool = False):
        """My outfits (in My Outfits), or with a name, wear that one: an outfit's name or part of it, or any
        folder's id. add: add it to what I'm wearing instead of replacing it. library: name is a folder in the
        Library, by its exact name."""
        return await call(context, argv("outfit", name, add=add, library=library))

    @tool()
    async def script_save(context: Context, object: str, name: str, source: str, running: bool = True,
                          vm: str = "mono", check: bool = True):
        """Save `source` into script `name` in one of my objects (made if missing), compile it, and say how it went.
        vm: mono, luau or lsl2. check: ask the region for a same-named item first, so it's replaced, not doubled."""
        def job(ctx: Ctx):
            make.save_script(ctx, make.rid(ctx, object), name, source, running=running, vm=vm, check_contents=check)
        return await call(context, job=job)

    @tool(read_only=True)
    async def notecard(context: Context, name: str = "", host: str | None = None):
        """Read one of my notecards (by name, part of it, or id), wherever I am; ones given to me land in my
        inventory. Without a name, lists my notecards. host: only for a viewer that can't read notecards itself,
        one of my objects nearby to run a reader script in."""
        return await call(context, argv("notecard", name or None, host=host))

    @tool()
    async def give(context: Context, to: str, item: str, perms: str | None = None, folder: bool = False):
        """Give someone an inventory item (id or exact name). perms: give a copy of one of my in-world objects
        with these next-owner perms (any of M C T). folder: item is a folder id."""
        return await call(context, argv("give", to, item, perms=perms, folder=folder))

    # ----- money -----

    @tool(destructive=True)
    async def pay(context: Context, to: str, amount: int, desc: str = ""):
        """Pay L$ to an avatar or an object that takes money (someone not near or known: their username in full,
        first.last or name Resident). The caps apply."""
        return await call(context, argv("pay", to, amount, desc=desc or None))

    @tool(destructive=True)
    async def buy(context: Context, object: str, price: int):
        """Buy an object that's for sale; price is what I expect to pay and must match."""
        return await call(context, argv("buy", object, price))

    # ----- the rest -----

    @tool(destructive=True)
    async def forget(context: Context, who: str):
        """Delete someone's words from my event log and the viewer's transcripts (when they ask)."""
        return await call(context, argv("forget", who))

    @tool()
    async def selfcheck(context: Context, host: str | None = None):
        """Check in world that the account is flagged as a scripted agent (needs somewhere I can build, or host: one
        of my objects)."""
        return await call(context, argv("selfcheck", host=host))

    return server


def register(sub):
    p = sub.add_parser("mcp", help="serve the commands as MCP tools on stdin/stdout, for MCP clients (docs/mcp.md)")
    p.add_argument("--websocket", metavar="HOST:PORT",
                   help="serve over WebSocket instead, for hosts that dial a URL (token: IBASHO_HOME/mcp-token)")
    p.set_defaults(fn=serve)


def serve(ctx: Ctx, a):
    if getattr(a, "websocket", None):
        from . import mcp_ws
        try:
            mcp_ws.serve(a.websocket, a.config)
        except ValueError as e:
            raise Fail(str(e)) from None
        return
    try:
        import anyio

        from .mcpl import Bridge
        bridge = Bridge(a.config, INSTRUCTIONS_LIVE)
        server = build(a.config, bridge)
    except ImportError as e:
        raise Fail(f"`ibasho mcp` needs the MCP extra: pip install \"ibasho[mcp]\" ({e})") from None
    import logging
    log = logging.getLogger("ibasho.mcpl")
    if not log.handlers:   # what the host's operator sees of MCPL: the grant, and pushes the host refused
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("ibasho mcpl: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    anyio.run(bridge.serve_stdio, server)
    # The host has gone. Worker threads may still be waiting (a long wait_events, the next batch of events for the
    # pushes); nothing is left for them to answer, so they aren't waited for.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
