"""The daemon: the viewer's LEAP peer on one side, a small HTTP API for `ibasho` commands on the other.

- LEAP (127.0.0.1, ports.leap): the stub the viewer runs (`--leap`) connects here, proves it's ours with the
  install token, and relays the viewer's messages. The daemon listens to the viewer's event pumps and writes what
  it hears to the event log.
- HTTP (127.0.0.1, ports.http): JSON in and out, every request with `Authorization: Bearer <install token>`.
  Anything that looks like a browser (an Origin header, a Sec-Fetch-Site other than "none", a Host other than
  127.0.0.1/localhost) is refused, so a web page can't drive the avatar, DNS rebinding included. Every request
  bound for the viewer goes through `policy.Policy` first: /cmd through `check`, /rpc (the script editor)
  through `check_rpc`, /listen through `check_listen`. The daemon's own requests to the viewer (its listeners,
  the caps it sets, starting the script editor) go straight out.

Endpoints: GET /status, POST /cmd {pump, data, timeout?, wait?}, POST /listen {pump}, GET /events?since&wait&kinds,
POST /event {kind, data} (for side channels like hearing), POST /rpc {method, params?, timeout?} (script editor),
POST /forget {ids, names} or {kinds, after?}, POST /shutdown.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hmac
import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from . import __version__, procs
from .config import Config, ConfigError, MATURITY, flag_check, is_linden_grid, leap_token
from .editor import EditorClient, RPCError
from .eventlog import EventLog, involves, notification_time
from .hearing import settings as hearing_settings
from .keeper import settings as keeper_settings
from .leap import AUTH_MAGIC, LeapError, LeapPeer, parse, read_frame, to_jsonable
from .policy import UPLOAD_OPS, Ledger, Policy, StageError, ViewerState, prune_staged, stage_upload

log = logging.getLogger("ibasho.daemon")

NOTIFY_PUMP = "IbashoNotify"         # the viewer forwards visible notifications here
EVENTS_PUMP = "IbashoEvents"         # the Ibasho viewer's own events (IMs, money, created objects...)
# Pumps that exist from the viewer's start, so listening early is safe. Listening early to a pump the viewer
# later creates under a fixed name (the IM floater's, for one) makes it fail on the duplicate name.
EARLY_PUMPS = {"StartupState": "startup", "LLChat": "chat", "LLTeleport": "teleport", "LLAutopilot": "autopilot"}
IBASHO_APIS = ("IbashoAgent", "IbashoIM", "IbashoObjects", "IbashoBuild")
VIEWER_KINDS = {"chat", "teleport", "autopilot", "notify", "startup", "daemon", "editor", "im_transcript",
                "im", "object_im", "money", "object_created", "object_properties", "move_done", "refused"}
SIDE_KIND = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
LOGIN_SCREEN = {"STATE_LOGIN_SHOW", "STATE_LOGIN_WAIT"}
NOT_TRANSCRIPTS = {"chat.txt", "teleport_history.txt"}
MESSAGE_REFUSALS = {"chat line", "IM"}   # `refused` events from the viewer's own daily message cap
# The viewer drops an `LLChatBar sendChat` that comes within a second of the previous one, without a word.
CHAT_GAP_S = 1.2
# The viewer pairs new objects with create requests in the order they arrive, which the region doesn't keep: requests
# made together got each other's shape and colour on Second Life (2026-11-03). One at a time, each waits for its object.
CREATE_OPS = {("IbashoBuild", "create"), ("IbashoBuild", "rez"), ("IbashoBuild", "duplicate")}
# Settings held where they're safe whatever happened before: pages the built-in browser opens from files may
# read other files (the Ibasho viewer refuses file: pages anyway; this is for any viewer, and stays saved).
FORCED_SETTINGS = (("BrowserFileAccessFromFileUrls", False),)
# At login the viewer shows again the notifications it saved from earlier sessions (payments, offers not yet
# answered), with their old ids and times. One whose time is older than this when it arrives is such a replay.
REPLAY_AGE_S = 60
NOTIFY_IDS_KEPT = 2000                 # notification ids remembered across sessions (to know a replay was shown)
NOTIFY_IDS_DAYS = 30
LOGGED_OUT_NOTES = {"YouHaveBeenLoggedOut"}   # the grid dropped the session; the viewer waits for an OK to quit


class NotifyIds:
    """Ids of the notifications the daemon has passed on, kept on disk, so a replay at the next login is known as
    one that was already shown."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.ids: dict[str, float] = {str(k): float(v) for k, v in
                                          json.loads(path.read_text(encoding="utf-8")).items()}
        except (OSError, ValueError, AttributeError, TypeError):
            self.ids = {}

    def __contains__(self, nid: str) -> bool:
        return nid in self.ids

    def add(self, nid: str):
        if nid in self.ids:
            return
        now = time.time()
        self.ids[nid] = now
        old = now - NOTIFY_IDS_DAYS * 86400
        keep = sorted(((t, i) for i, t in self.ids.items() if t >= old), reverse=True)[:NOTIFY_IDS_KEPT]
        self.ids = {i: t for t, i in keep}
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.ids), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("couldn't save %s (%s); notification ids kept in memory", self.path, e)


def load_token(cfg: Config) -> str:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        tok = cfg.token_file.read_text().strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    cfg.token_file.write_text(tok)
    return tok


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        for d in (cfg.state_dir, cfg.events_dir, cfg.shots_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.token = load_token(cfg)
        self.events = EventLog(cfg.events_dir, cfg.retention_days)
        self.ledger = Ledger(cfg.state_dir / "ledger.json")
        self.policy = Policy(cfg.caps, self.ledger, chat_logs=cfg.viewer_chat_logs, shots=cfg.shots_dir,
                             readable=[keeper_settings.life_dir_of(cfg), cfg.shots_dir], allow=cfg.policy_allow)
        self.config_mtime = cfg.path.stat().st_mtime if cfg.path.exists() else 0
        self.editor_lock = asyncio.Lock()
        self.peer: LeapPeer | None = None
        self.vstate = ViewerState(notifications=collections.OrderedDict())
        self.notify_ids = NotifyIds(cfg.state_dir / "notify_ids.json")
        self.logged_out = ""   # what the grid said when it dropped this login (the viewer stays open)
        self.ibasho = False
        self.ops: dict[str, list[str]] = {}
        self.login_state = ""
        self.login_failed = False
        self.self_id = ""
        self.self_names: dict = {}
        self.grid = ""
        self.connected_at: float | None = None
        self.listening: set[str] = set()
        self.obj_ims: collections.deque = collections.deque(maxlen=20)
        self.chat_lock = asyncio.Lock()
        self.create_lock = asyncio.Lock()
        self.last_chat = 0.0
        self.editor = EditorClient(cfg.editor_port,
                                   lambda m, p: self.events.append("editor", {"method": m, "params": to_jsonable(p)}),
                                   expect_agent=lambda: self.self_id)
        self.stopping = asyncio.Event()

    # ---------- the viewer ----------

    async def handle_leap(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            hello = await asyncio.wait_for(reader.readline(), 10)
            parts = hello.split()
            if len(parts) < 2 or parts[0] != AUTH_MAGIC or \
                    not hmac.compare_digest(parts[1], leap_token(self.token).encode()):
                log.warning("LEAP connection without our token; closed")
                writer.close()
                return
            pid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
            handshake = parse(await asyncio.wait_for(read_frame(reader), 10))
            peer = LeapPeer(reader, writer, handshake, self.on_viewer_event, pid)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, LeapError, ValueError,
                KeyError, TypeError) as e:
            log.warning("bad LEAP connection: %r", e)
            writer.close()
            return
        if self.peer is not None:
            self.peer.close()
        self.peer = peer
        runner = asyncio.create_task(peer.run())
        try:
            await self.on_viewer(peer)
        except Exception:
            log.exception("setting up the viewer link failed")
        await runner
        if self.peer is peer:
            self.peer = None
            self.vstate.logged_in = False
            self.vstate.gate = ""        # judged afresh for the next viewer
            self.logged_out = ""
            self.listening.clear()
            self.events.append("daemon", {"msg": "viewer disconnected"})

    async def on_viewer(self, peer: LeapPeer):
        self.connected_at = time.time()
        self.login_state, self.login_failed = "", False   # the last viewer's, not this one's
        self.events.append("daemon", {"msg": "viewer connected", "pid": peer.viewer_pid})
        # Fixed listener names: after a daemon restart the viewer still holds the old registrations (the stub
        # kept its connection to the viewer), so a re-listen is refused rather than doubling every event.
        for pump in list(EARLY_PUMPS) + [NOTIFY_PUMP]:
            await peer.listen(pump, f"ibasho_{pump}")
            self.listening.add(pump)
        await peer.send("LLNotifications", {"op": "forward", "channel": "Visible", "pump": NOTIFY_PUMP,
                                            "forward": True})
        for key, value in FORCED_SETTINGS:
            await peer.send("LLViewerControl", {"op": "set", "group": "Global", "key": key, "value": value})
        apis = await peer.command("getAPIs")
        self.ibasho = "IbashoAgent" in apis
        self.ops = {}
        if self.ibasho:
            for api in IBASHO_APIS:
                info = await peer.command("getAPI", api=api)
                self.ops[api] = sorted(str(o.get("name")) for o in info.get("ops") or [] if isinstance(o, dict))
            await peer.listen(EVENTS_PUMP, f"ibasho_{EVENTS_PUMP}")
            self.listening.add(EVENTS_PUMP)
            for key, value in (("IbashoMaxPayment", self.cfg.caps.max_payment),
                               ("IbashoMaxSessionSpend", max(1, self.cfg.caps.max_session_spend)),
                               ("IbashoMaxUploadCost", self.cfg.caps.max_upload_cost),
                               ("IbashoMaxMessagesPerDay", max(1, self.cfg.caps.messages_per_day))):
                await peer.send("LLViewerControl", {"op": "set", "group": "Global", "key": key, "value": value})
        await self.read_grid(peer)
        self.check_gate()
        await peer.send("LLStartUp", {"op": "postStartupState"})

    async def read_grid(self, peer: LeapPeer):
        try:
            self.grid = str((await peer.request("LLViewerControl", {"op": "get", "group": "Global",
                                                                     "key": "CurrentGrid"})).get("value") or "")
        except (asyncio.TimeoutError, ConnectionError):
            self.grid = ""
        self.grid = self.grid or self.cfg.grid_arg   # can't tell: assume the configured one

    def check_gate(self):
        """Linden Lab grids need the scripted-agent flag. Without it confirmed, or after the in-world check found
        it missing, refuse everything and close the viewer."""
        if not is_linden_grid(self.grid):
            self.vstate.gate = ""
            return
        flag = flag_check(self.cfg, self.grid)
        if not self.cfg.scripted_agent_flag_confirmed:
            reason = ("this is a Linden Lab grid and the scripted-agent flag isn't confirmed in the config "
                      "(grid.scripted_agent_flag_confirmed); closing the viewer")
        elif flag.get("flagged") is False:
            reason = ("the in-world check found this account isn't flagged as a scripted agent; flag it at "
                      "secondlife.com/my/account/sisa.php, then `ibasho selfcheck --reset`")
        else:
            self.vstate.gate = ""
            return
        if not self.vstate.gate:
            self.vstate.gate = reason
            self.events.append("daemon", {"msg": reason})
            if self.peer is not None:
                asyncio.create_task(self.peer.send("LLAppViewer", {"op": "forceQuit"}))

    def on_viewer_event(self, pump: str, data):
        data = to_jsonable(data)
        if pump == "StartupState":
            self.on_startup_state(data)
            return
        if pump == NOTIFY_PUMP:
            if isinstance(data, dict) and data.get("id"):
                nid = str(data["id"])
                notes = self.vstate.notifications
                notes[nid] = {k: data.get(k) for k in ("name", "type", "message", "form")}
                while len(notes) > 500:
                    notes.popitem(last=False)
                born = notification_time(data.get("time"))
                if born is not None and time.time() - born > REPLAY_AGE_S:
                    data["replayed"] = True            # from an earlier session, shown again at login
                    data["shown_before"] = nid in self.notify_ids
                self.notify_ids.add(nid)
                if data.get("name") in LOGGED_OUT_NOTES and self.vstate.logged_in:
                    self.logged_out = str(data.get("message") or data.get("name"))
            self.events.append("notify", data)
            return
        if pump == EVENTS_PUMP and isinstance(data, dict):
            kind = str(data.get("event") or "viewer")
            if kind == "object_im":
                self.obj_ims.append((time.time(), str(data.get("object_id")), data.get("message")))
            elif kind == "refused" and data.get("what") in MESSAGE_REFUSALS:
                asyncio.create_task(self.sync_messages())   # the viewer counted more than we did
            self.events.append(kind if kind in VIEWER_KINDS else f"viewer_{kind}"[:32], data)
            return
        if pump == "LLChat" and isinstance(data, dict) and self.is_object_im_echo(data):
            return  # the viewer shows object IMs in nearby chat too; the object_im event already has it
        self.events.append(EARLY_PUMPS.get(pump, "viewer"), data if pump in EARLY_PUMPS else {"pump": pump, "data": data})

    def is_object_im_echo(self, msg: dict) -> bool:
        now = time.time()
        return any(now - t < 5 and oid == str(msg.get("from_id")) and text == msg.get("message")
                   for t, oid, text in self.obj_ims)

    def on_startup_state(self, data):
        state = str((data or {}).get("str") or "")
        prev, self.login_state = self.login_state, state
        if state == "STATE_STARTED":
            if not self.vstate.logged_in:
                self.vstate.logged_in = True
                self.login_failed = False
                self.logged_out = ""
                asyncio.create_task(self.on_logged_in())
        else:
            self.vstate.logged_in = False
            if state in LOGIN_SCREEN and prev and prev not in LOGIN_SCREEN and prev != "STATE_FIRST" \
                    and prev != "STATE_BROWSER_INIT":
                self.login_failed = True
            if state == "STATE_LOGIN_WAIT" and prev != state:  # the login panel is up, waiting for a person
                self.events.append("startup", {"state": state, "after_failure": self.login_failed})

    async def on_logged_in(self):
        peer = self.peer
        if peer is None:
            return
        try:
            await self.read_grid(peer)
            self.check_gate()
            if self.vstate.gate:
                return
            self.self_id = str((await peer.request("LLAgent", {"op": "getID"})).get("id") or "")
            if self.ibasho and self.self_id:
                names = await peer.request("IbashoAgent", {"op": "names", "ids": [self.self_id]}, 10)
                self.self_names = (names.get("names") or {}).get(self.self_id) or {}
            self.ledger.new_session(f"{peer.viewer_pid}:{self.self_id}")
            await self.sync_messages()
            await self.cap_maturity(peer)
            await self.apply_server_settings(peer)
            if self.ibasho and not self.cfg.viewer_chat_logs:
                for key, value in (("LogNearbyChat", False), ("KeepConversationLogTranscripts", 0)):
                    await peer.send("LLViewerControl", {"op": "set", "group": "PerAccount", "key": key, "value": value})
            self.events.append("daemon", {"msg": "in world", "id": self.self_id, **self.self_names})
        except Exception:
            log.exception("after-login setup failed")

    async def apply_server_settings(self, peer: LeapPeer):
        """[server]: settings for drawing without a graphics card, set once in world, after the viewer's graphics
        presets (which override several of them given on the command line) have had their say."""
        s = self.cfg.server
        if not s.enabled:
            return
        settings = [("RenderFarClip", float(s.draw_distance)), ("RenderMaxTextureResolution", s.max_texture_size),
                    ("RenderAvatarMaxNonImpostors", s.avatar_impostors),
                    ("RenderAvatarMaxComplexity", s.avatar_max_complexity)]
        if self.ibasho:
            settings += [("IbashoMaxFPS", float(s.max_fps)), ("IbashoIdleFPS", float(s.idle_fps)),
                         ("IbashoIdleAfter", float(s.idle_after)), ("IbashoTextureBudget", s.texture_budget_mb),
                         ("IbashoTrimMemory", float(s.trim_memory))]   # viewers before 0.2 just don't have it
        for key, value in settings:
            await peer.send("LLViewerControl", {"op": "set", "group": "Global", "key": key, "value": value})

    async def sync_messages(self):
        """Take the Ibasho viewer's own count of today's messages if it's higher than ours: it also saw what went
        out while the daemon wasn't running, and the person's own typing. (Ours can be higher: it counts offers
        and gives too.)"""
        peer = self.peer
        if peer is None or "messages" not in self.ops.get("IbashoAgent", []):
            return
        try:
            r = await peer.request("IbashoAgent", {"op": "messages"}, 5)
        except (asyncio.TimeoutError, ConnectionError):
            return
        if isinstance(r, dict) and isinstance(r.get("count"), int):
            self.ledger.sync_messages(str(r.get("date") or ""), r["count"])

    async def cap_maturity(self, peer: LeapPeer):
        cap = MATURITY[self.cfg.caps.maturity]
        cur = (await peer.request("LLViewerControl", {"op": "get", "group": "Global", "key": "PreferredMaturity"}))
        try:
            if int(cur.get("value")) <= cap:
                return
        except (TypeError, ValueError):
            pass
        await peer.send("LLViewerControl", {"op": "set", "group": "Global", "key": "PreferredMaturity", "value": cap})

    # ---------- side jobs ----------

    async def ensure_editor(self):
        """Start the viewer's script-editor server if needed (it's a toggle, so check first, and only one caller at
        a time), then connect."""
        async with self.editor_lock:
            await self._ensure_editor()

    async def _ensure_editor(self):
        if self.editor.connected:
            return
        if not await port_open(self.cfg.editor_port):
            for key, value in (("ExternalEditorTightIntegration", False),
                               ("ExternalWebsocketSyncPort", self.cfg.editor_port)):
                await self.peer.send("LLViewerControl", {"op": "set", "group": "Global", "key": key, "value": value})
            await self.peer.send("UI", {"op": "call", "function": "Tools.ToggleScriptEditorServer"})
            for _ in range(20):
                await asyncio.sleep(0.25)
                if await port_open(self.cfg.editor_port):
                    break
        await self.editor.connect()

    async def tail_transcripts(self):
        """A stock viewer has no IM events over LEAP, but writes per-conversation transcripts
        (<account>/<other party>.txt). Tail this avatar's into `im_transcript` events (never another account's on
        the same viewer). The Ibasho viewer reports IMs itself."""
        offsets: dict[Path, int] = {}
        first = True
        while not self.stopping.is_set():
            if self.peer is not None and not self.ibasho:
                for root in self.account_dirs():
                    try:
                        files = list(root.glob("*.txt"))
                    except OSError:
                        continue
                    for f in files:
                        if f.name in NOT_TRANSCRIPTS:
                            continue
                        try:
                            size = f.stat().st_size
                        except OSError:
                            continue
                        old = offsets.setdefault(f, size if first else 0)
                        if size > old:
                            with f.open("rb") as fh:
                                fh.seek(old)
                                chunk = fh.read(size - old)
                            offsets[f] = size
                            for line in chunk.decode("utf-8", "replace").splitlines():
                                if line.strip():
                                    self.events.append("im_transcript", {"conversation": f.stem, "line": line})
                        elif size < old:
                            offsets[f] = size
                first = False
            await asyncio.sleep(1)

    def account_dirs(self) -> list[Path]:
        """The viewer's folders for this avatar's account: what pruning and forgetting may touch. Other accounts
        logged in on the same viewer (the person's own, with Linden Lab's viewer) are left alone."""
        return self.cfg.account_dirs(*[n for n in (self.self_names.get("username"),) if n])

    def transcript_files(self) -> list[Path]:
        out = []
        for root in self.account_dirs():
            try:
                out += [f for f in root.glob("*.txt") if f.name != "teleport_history.txt"]
            except OSError:
                pass
        return out

    def prune_transcripts(self):
        """With the viewer's own chat logs off by config, whatever it still wrote (before the daemon could turn
        them off: the person's own login, a new account's first seconds) ages out with the event log. Transcript
        lines start with [YYYY/MM/DD HH:MM]."""
        cutoff = self.events.cutoff().strftime("%Y/%m/%d")
        for f in self.transcript_files():
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            except OSError:
                continue
            keep = [ln for ln in lines if not (re.match(r"\[(\d{4}/\d{2}/\d{2})", ln) and ln[1:11] < cutoff)]
            try:
                if not keep:
                    f.unlink()
                elif len(keep) != len(lines):
                    f.write_text("".join(keep), encoding="utf-8")
            except OSError:
                pass

    def heard_retention_s(self) -> float:
        try:
            days = hearing_settings.load(self.cfg).retention_days
        except ConfigError as e:
            days = hearing_settings.Hearing().retention_days
            log.warning("hearing settings unreadable (%s); keeping heard speech %g days", e, days)
        return days * 86400

    def prune(self):
        self.events.prune()
        heard_cutoff = time.time() - self.heard_retention_s()
        kinds = set(hearing_settings.HEARD_KINDS)
        self.events.forget(lambda e: e["kind"] in kinds and e["t"] < heard_cutoff)
        if not self.cfg.viewer_chat_logs:
            self.prune_transcripts()
        prune_staged(self.cfg.state_dir / "uploads")
        cutoff = time.time() - self.cfg.retention_days * 86400
        for p in self.cfg.shots_dir.glob("*"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    async def prune_loop(self):
        while not self.stopping.is_set():
            self.prune()
            try:
                await asyncio.wait_for(self.stopping.wait(), 3600)
            except asyncio.TimeoutError:
                pass

    def forget_transcripts(self, names: set[str]) -> int:
        """Delete the viewer's own transcript of a conversation with this person, if it kept one."""
        stems = set()
        for n in names:
            n = n.strip().lower()
            if not n:
                continue
            for v in (n, n.replace(" ", "_"), n.replace(".", "_"), n.replace(".", " ")):
                stems |= {v, v.replace(" ", "_"), v + "_resident", v + " resident"}
        speakers = {n.strip().lower() for n in names if n.strip()}
        gone = 0
        for f in self.transcript_files():
            try:
                if f.name == "chat.txt":   # nearby chat: drop just their lines ("[date time] Name: text")
                    lines = f.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                    keep = [ln for ln in lines
                            if not (m := re.match(r"\[[^\]]*\]\s*([^:]+):", ln)) or m.group(1).strip().lower() not in speakers]
                    if len(keep) != len(lines):
                        f.write_text("".join(keep), encoding="utf-8")
                        gone += 1
                elif f.stem.lower() in stems:
                    f.unlink()
                    gone += 1
            except OSError:
                pass
        return gone

    def status(self) -> dict:
        c = self.cfg
        return {
            "version": __version__, "connected": self.peer is not None, "connected_at": self.connected_at,
            "viewer": ("ibasho" if self.ibasho else "stock") if self.peer else None,
            "viewer_pid": self.peer.viewer_pid if self.peer else None,
            "login_state": self.login_state, "logged_in": self.vstate.logged_in, "login_failed": self.login_failed,
            "self": {"id": self.self_id, **self.self_names}, "grid": self.grid, "gate": self.vstate.gate,
            "logged_out": self.logged_out,
            "seq": self.events.seq, "listening": sorted(self.listening), "ops": self.ops,
            "messages_today": self.ledger.messages, "spent_session": self.ledger.spent_session,
            "spent_today": self.ledger.spent_today,
            "caps": vars(c.caps), "name": c.name, "name_words": c.name_words(), "probe_channel": c.probe_channel,
            "config_mtime": self.config_mtime,
        }


async def port_open(port: int) -> bool:
    try:
        _, w = await asyncio.open_connection("127.0.0.1", port)
        w.close()
        return True
    except OSError:
        return False


# ---------- HTTP ----------

def json_error(status: int, msg: str, **extra) -> web.Response:
    return web.json_response({"error": msg, **extra}, status=status)


def make_app(d: Daemon) -> web.Application:
    expected = ("Bearer " + d.token).encode()

    @web.middleware
    async def guard(req: web.Request, handler):
        # Browsers send Sec-Fetch-Site with every request ("none" only for one the person typed or bookmarked, which
        # can't carry the token). Other programs' HTTP clients may send Sec-Fetch-Mode (Node's fetch does) but not
        # Sec-Fetch-Site, which only a browser can vouch for.
        site = req.headers.get("Sec-Fetch-Site")
        if "Origin" in req.headers or (site is not None and site.strip().lower() != "none"):
            log.warning("refused %s %s from a browser (Origin=%r, Sec-Fetch-Site=%r)", req.method, req.path,
                        req.headers.get("Origin"), site)
            return json_error(403, "local scripts only")
        host = req.headers.get("Host", "")
        hostname = host[1:].split("]")[0] if host.startswith("[") else host.rsplit(":", 1)[0]
        if hostname.lower() not in ("127.0.0.1", "localhost"):
            log.warning("refused %s %s for Host %r", req.method, req.path, host)
            return json_error(403, "local scripts only")
        if not hmac.compare_digest(req.headers.get("Authorization", "").encode(), expected):
            return json_error(401, "missing or wrong install token")
        if req.method == "POST" and req.content_type != "application/json":
            return json_error(415, "JSON only")
        return await handler(req)

    def bad_request(msg: str):
        return web.HTTPBadRequest(text=json.dumps({"error": msg}), content_type="application/json")

    async def body(req) -> dict:
        try:
            b = await req.json()
        except (ValueError, UnicodeDecodeError):
            raise bad_request("bad JSON")
        if not isinstance(b, dict):
            raise bad_request("expected a JSON object")
        return b

    def seconds(b: dict, default: float, most: float) -> float:
        v = b.get("timeout", default)
        try:
            if isinstance(v, bool):
                raise TypeError
            t = float(v)
        except (TypeError, ValueError):
            raise bad_request("timeout: a number of seconds") from None
        if t != t:   # NaN
            raise bad_request("timeout: a number of seconds")
        return max(0.0, min(t, most))

    routes = web.RouteTableDef()

    @routes.get("/status")
    async def status(req):
        return web.json_response(d.status())

    @routes.post("/cmd")
    async def cmd(req):
        b = await body(req)
        pump, data = b.get("pump"), b.get("data") or {}
        if not isinstance(pump, str) or not isinstance(data, dict):
            return json_error(400, "need {pump: str, data: object}")
        timeout = seconds(b, 15, 600)
        v = d.policy.check(pump, data, d.vstate)
        if not v.ok:
            return json_error(v.status, v.reason, refused=True, rule=v.rule, retry_after=v.retry_after)
        if (pump, data.get("op")) in UPLOAD_OPS:
            # the viewer reads copies made from what was checked, not the files themselves a moment later
            uploads = d.cfg.state_dir / "uploads"
            prune_staged(uploads)
            try:
                v.data = stage_upload(data["op"], v.data, d.policy.readable, uploads)
            except (StageError, OSError) as e:
                return json_error(403, f"{pump} {data['op']}: {e}", refused=True, rule="files", retry_after=0)
        if (pump, data.get("op")) in CREATE_OPS and b.get("wait", True):
            async with d.create_lock:
                return await run_cmd(pump, data, v, b, timeout)
        return await run_cmd(pump, data, v, b, timeout)

    async def run_cmd(pump, data, v, b, timeout):
        peer = d.peer
        if peer is None:
            return json_error(503, "viewer not connected")
        real = peer.command_pump if pump == "_command" else pump
        chat = (pump, data.get("op")) == ("LLChatBar", "sendChat")
        async with d.chat_lock if chat else contextlib.nullcontext():
            if chat:   # one line at a time, spaced so the viewer doesn't drop the second
                await asyncio.sleep(max(0.0, d.last_chat + CHAT_GAP_S - time.monotonic()))
                d.last_chat = time.monotonic()
            try:
                pending = peer.begin(real, v.data)
            except ConnectionError:
                return json_error(503, "viewer not connected")
        d.policy.sent(v)
        if not b.get("wait", True):
            d.policy.charged(v.spend)  # no answer to wait for: count it as spent
            asyncio.create_task(_quietly(peer.finish(pending, 60)))
            return web.json_response({"posted": True})
        d.policy.reserve(v.spend)      # so a second payment meanwhile sees this one
        try:
            reply = await peer.finish(pending, timeout)
        except asyncio.TimeoutError:
            d.policy.charged(v.spend)  # it may well have gone through
            return json_error(504, "timeout")
        except ConnectionError:
            return json_error(503, "viewer disconnected")
        finally:
            d.policy.release(v.spend)
        if not (isinstance(reply, dict) and reply.get("error")):
            d.policy.charged(v.spend)
        if (pump, data.get("op")) in (("IbashoAgent", "upload"), ("IbashoAgent", "uploadMesh")) and isinstance(reply, dict):
            if reply.get("charged") and isinstance(reply.get("cost"), int):
                d.policy.charged(reply["cost"])
        if (pump, data.get("op")) == ("IbashoAgent", "setDisplayName") and isinstance(reply, dict) \
                and not reply.get("error") and reply.get("display_name"):
            d.self_names["display_name"] = str(reply["display_name"])   # what my name is in chat from now on
            log.info("display name set: %s", reply["display_name"])
        if (pump, data.get("op")) == ("IbashoAgent", "setProfile") and isinstance(reply, dict) \
                and not reply.get("error"):
            log.info("profile set: %s", ", ".join(k for k in ("about", "image_id") if k in data))
        return web.json_response(to_jsonable(reply))

    @routes.post("/listen")
    async def listen(req):
        b = await body(req)
        pump = b.get("pump")
        if not isinstance(pump, str) or not pump:
            return json_error(400, "need {pump}")
        if d.peer is None:
            return json_error(503, "viewer not connected")
        v = d.policy.check_listen(pump, d.vstate)
        if not v.ok:
            return json_error(v.status, v.reason, refused=True, rule=v.rule, retry_after=0)
        await d.peer.listen(pump, f"ibasho_{pump}")
        d.listening.add(pump)
        return web.json_response({"listening": sorted(d.listening)})

    @routes.post("/event")
    async def post_event(req):
        b = await body(req)
        kind = b.get("kind")
        if not isinstance(kind, str) or not SIDE_KIND.match(kind) or kind in VIEWER_KINDS:
            return json_error(400, "kind: lowercase letters/digits/_, not one of the viewer's kinds")
        ev = d.events.append(kind, to_jsonable(b.get("data") or {}))
        return web.json_response({"seq": ev["seq"]})

    @routes.get("/events")
    async def events(req):
        try:
            since = int(req.query.get("since", 0))
            wait = min(float(req.query.get("wait", 0)), 60)
            limit = int(req.query.get("limit", 0)) or None
        except ValueError:
            return json_error(400, "since/wait/limit must be numbers")
        kinds = [k for k in req.query.get("kinds", "").split(",") if k] or None
        if wait and not d.events.since(since, kinds, 1):
            end = time.monotonic() + wait
            while time.monotonic() < end and not d.events.since(since, kinds, 1):
                await d.events.wait(d.events.seq, end - time.monotonic())
        return web.json_response(d.events.since(since, kinds, limit))

    @routes.post("/rpc")
    async def rpc(req):
        b = await body(req)
        method, params = b.get("method"), b.get("params")
        if not isinstance(method, str) or not (params is None or isinstance(params, (dict, list))):
            return json_error(400, "need {method: str, params?: object or list}")
        timeout = seconds(b, 30, 300)
        if d.peer is None:
            return json_error(503, "viewer not connected")
        v = d.policy.check_rpc(method, params, d.vstate)
        if not v.ok:
            return json_error(v.status, v.reason, refused=True, rule=v.rule, retry_after=0)
        try:
            await d.ensure_editor()
            res = await d.editor.call(method, params, timeout)
        except RPCError as e:
            return web.json_response({"rpc_error": e.err}, status=400)
        except asyncio.TimeoutError:
            return json_error(504, f"the viewer's script editor didn't answer {method} within {timeout:g} s (the "
                                   "viewer may be slow right now)")
        except (ConnectionError, OSError, aiohttp.ClientError) as e:
            return json_error(503, repr(e))
        return web.json_response({"result": res})

    @routes.post("/forget")
    async def forget(req):
        b = await body(req)
        if b.get("kinds") is not None:  # everything of some kinds (what hearing heard has no speaker), since `after`
            kinds = b["kinds"]
            if not isinstance(kinds, list) or not kinds or not all(isinstance(k, str) for k in kinds):
                return json_error(400, "kinds: a list of event kinds")
            try:
                after = float(b.get("after") or 0)
            except (TypeError, ValueError):
                return json_error(400, "after: a time in seconds")
            removed = d.events.forget(lambda e: e["kind"] in kinds and e["t"] >= after)
            return web.json_response({"events_removed": removed, "transcripts_removed": 0})
        ids = [str(i) for i in b.get("ids") or [] if str(i).lower() != d.self_id.lower()]
        names = {str(n) for n in b.get("names") or [] if str(n).strip()}
        if not ids and not names:
            return json_error(400, "need ids or names")
        removed = d.events.forget(involves(ids, names))
        files = d.forget_transcripts(names)
        return web.json_response({"events_removed": removed, "transcripts_removed": files})

    @routes.post("/shutdown")
    async def shutdown(req):
        d.stopping.set()
        return web.json_response({"stopping": True})

    app = web.Application(client_max_size=50_000_000, middlewares=[guard])
    app.add_routes(routes)
    return app


async def _quietly(coro):
    try:
        await coro
    except Exception:
        pass


async def serve(cfg: Config, started: asyncio.Future | None = None):
    d = Daemon(cfg)
    try:
        leap_sock = procs.bind_exclusive(cfg.leap_port)
        http_sock = procs.bind_exclusive(cfg.http_port)
    except OSError as e:
        raise SystemExit(f"can't bind ports {cfg.leap_port}/{cfg.http_port} ({e}); is a daemon already running?")
    leap_server = await asyncio.start_server(d.handle_leap, sock=leap_sock, limit=64 * 1024 * 1024)
    runner = web.AppRunner(make_app(d), access_log=None)
    await runner.setup()
    await web.SockSite(runner, http_sock).start()
    procs.write_pid(cfg.state_dir / "daemon.pid", os.getpid())
    tasks = [asyncio.create_task(d.tail_transcripts()), asyncio.create_task(d.prune_loop())]
    log.info("ibasho daemon %s: LEAP on %d, HTTP on %d", __version__, cfg.leap_port, cfg.http_port)
    d.events.append("daemon", {"msg": "daemon started", "version": __version__})
    if started is not None:
        started.set_result(d)
    await d.stopping.wait()
    for t in tasks:
        t.cancel()
    leap_server.close()
    if d.peer is not None:
        d.peer.close()
    await d.editor.close()
    await runner.cleanup()
    (cfg.state_dir / "daemon.pid").unlink(missing_ok=True)


def run(cfg: Config, console: bool = True):
    """`console`: also log to stderr. Off when `ibasho up` starts the daemon, whose stderr is daemon.out: that file
    then only catches what bypasses logging (a crash), instead of a second copy of the log that never rotates."""
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        cfg.state_dir / "daemon.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")]
    if console and sys.stderr is not None:  # None under pythonw
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    asyncio.run(serve(cfg))
