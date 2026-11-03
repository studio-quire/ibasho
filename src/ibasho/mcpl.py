"""MCPL ("MCP Live", github.com/anima-research/mcpl, spec 0.5) on `ibasho mcp`: what a host that speaks it gets
beyond the tools. The world's events come to the agent as `push/event`s, the same things `ibasho watch` wakes
for, tagged with MCPL's `chat:*` vocabulary so the host's attention rules can sort them; and before each inference
the host can be given a status line (where the avatar is, who's near). Feature sets turn each on or off.

It's a layer between the stdio transport and the MCP SDK's server: the MCPL methods are answered here and never
reach the SDK, the push requests go out beside the SDK's own messages, the `initialize` answer gains
`experimental.mcpl` when the host offered it, and while events are pushed, `tools/list` leaves out `wait_events`.
A plain MCP client sees the server as it always was.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import re
import threading
import time

from .api import ApiError, Down
from .cli.common import Ctx, position
from .cli.hear import Wake
from .config import is_linden_grid

log = logging.getLogger("ibasho.mcpl")

VERSION = "0.5"
CHAT, MESSAGES, NOTICES, STATUS = "ibasho.chat", "ibasho.messages", "ibasho.notices", "ibasho.status"
PUSH_SETS = (CHAT, MESSAGES, NOTICES)
NAMESPACE = "ibasho"            # the status line's injection namespace
PUSH_TIMEOUT = 30               # seconds to wait for the host's answer to a push
STATUS_FRESH = 10               # a status line this young is used again as it is
STATUS_WAIT = 3.5               # how long the hook waits for a new one (hosts give it 5 s)
STATUS_STALE = 120              # past this, an old line isn't given when a new one is late
HIDDEN_WHILE_PUSHING = {"wait_events"}   # nothing for it to do then; still answers if called
NEAR_M = 64
NEAR_NAMES = 6
POLL_WAIT = 10

# SPEC §6.2: the closed vocabulary of capability paths.
CAPABILITY_PATHS = (
    "pushEvents", "tools", "modelInfo", "inferenceRequest", "inferenceRequest.streaming", "inferenceLifecycle",
    "contextHooks.beforeInference.observe", "contextHooks.beforeInference.inject.system",
    "contextHooks.beforeInference.inject.beforeUser", "contextHooks.beforeInference.inject.afterUser",
    "channels.register", "channels.lifecycle", "channels.publish", "channels.incoming", "channels.streaming",
    "channels.acknowledge", "channels.typing",
)

# ---------- what the events are: tags ----------

TAGS = {
    "ibasho:local": "Local chat: said aloud near the avatar.",
    "ibasho:heard": "Speech the avatar heard in the world (voice or media), turned into text by Ibasho's hearing.",
    "ibasho:avatar": "From an avatar: a person, or another agent; Second Life doesn't say which.",
    "ibasho:object": "From a scripted object (also chat:from-bot).",
    "ibasho:talking": "The avatar has spoken there lately: in local chat within 90 s, or in that group within "
                      "5 minutes. Usually the reply to something it said, even without its name.",
    "ibasho:digest": "Several lines gathered into one: a group's chat every couple of minutes, or an object that "
                     "keeps talking.",
    "ibasho:im": "An instant message, from one person, a group or a conference.",
    "ibasho:dialog": "A script's menu or text box, or a script asking for a permission; answered with the "
                     "dialog or respond tools.",
    "ibasho:offer": "Someone offers something: a teleport, friendship, an item, a group; answered with respond.",
    "ibasho:question": "A notification asking the avatar to choose (respond answers it).",
    "ibasho:payment": "L$ paid to the avatar.",
    "ibasho:refused": "Ibasho's caps or the viewer's limits refused something the avatar tried.",
    "ibasho:script-error": "An error from a script in one of the avatar's own objects.",
    "ibasho:teleport": "A teleport that failed.",
    "ibasho:connection": "The viewer lost its connection, is closing, or couldn't log in.",
    "ibasho:notice": "Any other notice from the grid or the place (a region restarting, a group notice...).",
    "ibasho:unread": "How many IMs were unread when events began to come.",
    "ibasho:keeper": "A note from the person or the keeper running this install, not from the world.",
}
CORE_TAGS = ["chat:addressed", "chat:mention", "chat:dm", "chat:private", "chat:ambient", "chat:group",
             "chat:from-bot", "chat:system"]
SUGGESTED = [
    {"tagsAny": ["chat:addressed"], "behavior": "immediate",
     "rationale": "Someone said the avatar's name, IMed it, a script asked it something, or an offer or payment "
                  "came. Declining leaves these until something else wakes you."},
    {"tagsAny": ["ibasho:talking"], "behavior": "immediate",
     "rationale": "The avatar spoke there lately, so this is most likely the answer. In Second Life people rarely "
                  "say the name of whoever they're talking to; declining makes conversations wait."},
    {"tagsAny": ["chat:system"], "behavior": "immediate",
     "rationale": "Notices about the avatar's own situation: a region restarting, a refused action, a lost "
                  "connection. They're rare."},
]
ONTOLOGY = {"coreTags": CORE_TAGS, "tags": {k: {"desc": v} for k, v in TAGS.items()},
            "suggestedTreatment": SUGGESTED, "open": True}

MANIFEST = {
    "version": VERSION,
    "pushEvents": True,
    "contextHooks": {"beforeInference": {"inject": {"beforeUser": True}}},
    "featureSets": {
        CHAT: {"description": "Local chat near the avatar, and with hearing on, speech it hears: what `ibasho "
                              "watch` wakes for (in a crowd, only lines that name it, greet the room, or come "
                              "while it's talking; an object that keeps talking, as a digest).",
               "uses": ["pushEvents"], "tagOntology": ONTOLOGY},
        MESSAGES: {"description": "IMs: from people and objects at once; a group's as a digest every couple of "
                                  "minutes, at once when the avatar is named or talking there.",
                   "uses": ["pushEvents"], "tagOntology": ONTOLOGY},
        NOTICES: {"description": "Script dialogs, offers, payments, refusals by the caps, errors from the "
                                 "avatar's own scripts, a lost connection, and other notices.",
                  "uses": ["pushEvents"], "tagOntology": ONTOLOGY},
        STATUS: {"description": "Before each inference, one line: where the avatar is and who's near. Never "
                                "reads the conversation.",
                 "uses": ["contextHooks.beforeInference.inject.beforeUser"]},
    },
}

OFFER_NOTIFY = re.compile(r"^(ObjectGiveItem|OwnObjectGiveItem|UserGiveItem|OfferFriendship|OfferCallingCard"
                          r"|TeleportOffered|TeleportRequest|JoinGroup)")
OWN_VOICE = {"refused", "daemon", "startup", "keeper"}   # Ibasho's own lines, not the world's


def classify(w: Wake) -> tuple[str, list[str]]:
    """The feature set a wake goes out under, and its tags (SPEC §16). Producers should emit the core closure
    themselves (§16.3), so chat:mention and chat:dm come with chat:addressed."""
    tags: list[str] = []

    def addressed_or_ambient():
        tags.extend(["chat:mention", "chat:addressed"] if w.named else ["chat:ambient"])

    if w.digest == "object":
        return CHAT, ["chat:ambient", "chat:from-bot", "ibasho:object", "ibasho:local", "ibasho:digest"]
    if w.digest == "group":
        return MESSAGES, ["chat:group", "chat:ambient", "ibasho:im", "ibasho:digest"]
    if w.event is None:   # the unread line
        return MESSAGES, ["chat:system", "ibasho:unread"]
    k, d = w.event["kind"], w.event.get("data") or {}
    if k == "chat":
        tags.append("ibasho:local")
        if d.get("source") == 2:
            tags += ["chat:from-bot", "ibasho:object"]
        else:
            tags.append("ibasho:avatar")
        if d.get("chat_type") == 8:   # an object of mine speaking to me alone (llOwnerSay)
            tags += ["chat:dm", "chat:addressed", "chat:private"]
        else:
            addressed_or_ambient()
        if w.talking:
            tags.append("ibasho:talking")
        return CHAT, tags
    if k.startswith("heard"):
        addressed_or_ambient()
        return CHAT, tags + ["ibasho:heard"]
    if k == "im":
        tags += ["ibasho:im", "ibasho:avatar"]
        st = d.get("session_type")
        if st == "p2p":
            tags += ["chat:dm", "chat:addressed", "chat:private"] + (["chat:mention"] if w.named else [])
        else:
            tags.append("chat:group")
            if st != "group":
                tags.append("chat:private")   # a conference: a private IM among several
            addressed_or_ambient()
            if w.talking:
                tags.append("ibasho:talking")
        return MESSAGES, tags
    if k == "object_im":
        return MESSAGES, ["chat:dm", "chat:addressed", "chat:private", "chat:from-bot", "ibasho:im", "ibasho:object"]
    if k == "im_transcript":   # a stock viewer's IMs, read from its transcripts: one person or a group, unknown
        return MESSAGES, ["chat:private", "ibasho:im"]
    if k == "money":
        return NOTICES, ["chat:system", "chat:addressed", "ibasho:payment"]
    if k == "notify":
        from .policy import ANSWERABLE
        name = str(d.get("name") or "")
        if name.startswith("Script"):
            return NOTICES, ["chat:addressed", "chat:from-bot", "ibasho:dialog"]
        if OFFER_NOTIFY.match(name):
            return NOTICES, ["chat:addressed", "ibasho:offer"] + (["chat:from-bot"] if "Object" in name else [])
        if name in ("PaymentReceived", "PaymentSent"):
            return NOTICES, ["chat:system", "chat:addressed", "ibasho:payment"]
        if name in ANSWERABLE:
            return NOTICES, ["chat:addressed", "ibasho:question"]
        return NOTICES, ["chat:system", "ibasho:notice"]
    if k == "refused":
        return NOTICES, ["chat:system", "ibasho:refused"]
    if k == "editor":
        return NOTICES, ["chat:system", "ibasho:script-error"]
    if k == "teleport":
        return NOTICES, ["chat:system", "ibasho:teleport"]
    if k in ("daemon", "startup"):
        return NOTICES, ["chat:system", "ibasho:connection"]
    if k == "keeper":
        return NOTICES, ["ibasho:keeper"]
    return NOTICES, ["chat:system", "ibasho:" + re.sub(r"[^a-z0-9-]+", "-", k.lower()).strip("-")]


def world_name(grid: str) -> str:
    return "Second Life" if is_linden_grid(grid) else f"grid {grid}"


def push_params(w: Wake, grid: str, unread_addressed: bool = False) -> tuple[str, dict]:
    """The feature set and `push/event` params for a wake. The text opens with where it's from, in brackets: hosts
    show the content as it is and keep `origin` from the model, and these are other people's words."""
    fs, tags = classify(w)
    if w.event is None and w.digest is None and unread_addressed:
        tags.append("chat:addressed")   # IMs to the avatar itself are waiting
    kind = w.event["kind"] if w.event else (w.digest + "_digest" if w.digest else "unread")
    where = "Ibasho" if kind in OWN_VOICE else world_name(grid)
    origin: dict = {"grid": grid, "event": kind, "seq": w.seq}   # not "kind": a key the host keeps for itself
    d = (w.event or {}).get("data") or {}
    author = (str(d.get("from_id") or d.get("object_id") or ""), str(d.get("from") or "")) if w.event else w.who
    if kind in ("chat", "im", "object_im") and author and author[0]:
        origin["authorId"], origin["authorName"] = author
    elif w.digest == "object" and author:
        origin["authorId"], origin["authorName"] = author
    elif w.digest == "group" and author:
        origin["session"], origin["sessionName"] = author
    if kind == "im" and d.get("session_id"):
        origin["session"] = str(d["session_id"])
    t = w.t or time.time()
    event_id = f"ibasho-{w.digest}-digest-{w.seq}" if w.digest else \
        f"ibasho-{w.seq}" if w.event else f"ibasho-unread-{int(t * 1000)}"
    return fs, {
        "featureSet": fs,
        "eventId": event_id,
        "timestamp": dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "origin": origin,
        "tags": list(dict.fromkeys(tags)),
        "payload": {"content": [{"type": "text", "text": f"[{where}] {w.line}"}]},
    }


# ---------- the status line ----------

def quoted(name: str) -> str:
    """A name from the world, in quotes, so it can't read as anything but a name."""
    return json.dumps(name, ensure_ascii=False)


def status_line(ctx: Ctx) -> str:
    """Where the avatar is and who's near, in a line. Blocking (viewer requests)."""
    world = world_name(ctx.cfg.grid)
    api = ctx.api
    try:
        st = api.status(fresh=True)
    except Down:
        return f"{world}: not in world; Ibasho isn't running (the `up` tool starts it and logs in)."
    world = world_name(st.get("grid") or ctx.cfg.grid)
    if not st.get("logged_in"):
        return f"{world}: not in world (the `up` tool logs in)."
    try:
        pos = position(ctx).get("region") or [0, 0, 0]
    except ApiError:
        pos = None
    region, people = "", []
    if api.ibasho:
        try:
            region = str(api.op("IbashoAgent", "parcel", timeout=3).get("region", {}).get("name") or "")
        except ApiError:
            pass
        try:
            people = api.op("IbashoAgent", "people", {"dist": NEAR_M}, timeout=3).get("people", [])
        except ApiError:
            people = None
    else:
        try:
            people = [{"display_name": p.get("name")} for p in
                      api.cmd("LLAgent", "getNearbyAvatarsList", {"dist": NEAR_M}, timeout=3).get("result", [])]
        except ApiError:
            people = None
    at = (quoted(region) + " " if region else "") + \
        ("(" + ", ".join(f"{v:.0f}" for v in pos[:3]) + ")" if pos else "")
    line = f"{world}: in world at {at.strip() or 'an unknown place'}."
    from .cli.hear import unread_line
    unread = unread_line(ctx)
    tail = f" {unread[0].upper()}{unread[1:].replace('`ibasho ims`', 'the `ims` tool')}." if unread else ""
    if people is None:
        return line + tail
    if not people:
        return line + f" Nobody within {NEAR_M} m." + tail
    named = []
    for p in people[:NEAR_NAMES]:
        dist = f" {p['dist']:.0f} m" if isinstance(p.get("dist"), (int, float)) else ""
        named.append(quoted(str(p.get("display_name") or p.get("username") or "?")) + dist)
    more = f", and {len(people) - NEAR_NAMES} more" if len(people) > NEAR_NAMES else ""
    return line + f" Near ({NEAR_M} m): " + ", ".join(named) + more + "." + tail


# ---------- the grant ----------

def path_matches(pattern: str, path: str) -> bool:
    """SPEC §5.4: `*` is exactly one segment, and the segment counts must be equal."""
    p, q = pattern.split("."), path.split(".")
    return len(p) == len(q) and all(a == "*" or a == b for a, b in zip(p, q))


def set_matches(pattern: str, name: str) -> bool:
    return pattern == "*" or path_matches(pattern, name)


def string_list(params: dict, key: str) -> list[str] | None:
    v = params.get(key)
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{key} must be a list of strings")
    return v


class Policy:
    """The host's grant (SPEC §5.3, §5.4, §6.7) and which feature sets it leaves active. Nothing is active until a
    `featureSets/update` Request has arrived."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ready = False
        self.paths: frozenset[str] = frozenset()
        self.enabled: list[str] | None = None
        self.disabled: list[str] = []

    @staticmethod
    def parse(params) -> tuple[frozenset[str], list[str] | None, list[str]]:
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        effective = string_list(params, "effectiveCapabilities") or []   # absent: a grant of nothing (§5.3)
        denied = string_list(params, "deniedCapabilities") or []
        if both := sorted(set(effective) & set(denied)):
            raise ValueError("in both effectiveCapabilities and deniedCapabilities: " + ", ".join(both))
        paths = frozenset(p for p in CAPABILITY_PATHS if any(path_matches(pat, p) for pat in effective))
        return paths, string_list(params, "enabled"), string_list(params, "disabled") or []

    def update(self, params) -> dict:
        """A Request: the whole grant, replacing the last. Returns the receipt; ValueError if malformed (then
        nothing is granted, failing closed)."""
        try:
            paths, enabled, disabled = self.parse(params)
        except ValueError:
            with self.lock:
                self.ready, self.paths, self.enabled, self.disabled = False, frozenset(), None, []
            raise
        with self.lock:
            self.ready, self.paths, self.enabled, self.disabled = True, paths, enabled, disabled
        return self.receipt()

    def narrow(self, params):
        """A Notification may only take away (§6.7): its `disabled`, and capabilities it leaves out."""
        try:
            paths, _, disabled = self.parse(params)
        except ValueError as e:
            log.warning("featureSets/update notification ignored: %s", e)
            return
        with self.lock:
            if isinstance(params, dict) and "effectiveCapabilities" in params:
                self.paths = self.paths & paths
            self.disabled = self.disabled + disabled

    def why_not(self, name: str) -> tuple[str, list[str]] | None:
        """None if the feature set is active, else why not and the capabilities it lacks."""
        if not self.ready:
            return "no policy yet", []
        uses = MANIFEST["featureSets"][name]["uses"]
        if missing := [u for u in uses if u not in self.paths]:
            return "capability_denied", missing
        if any(set_matches(p, name) for p in self.disabled):
            return "disabled", []
        if self.enabled is not None and not any(set_matches(p, name) for p in self.enabled):
            return "not_selected", []
        return None

    def active(self, name: str) -> bool:
        with self.lock:
            return self.why_not(name) is None

    def receipt(self) -> dict:
        with self.lock:
            off = {n: self.why_not(n) for n in MANIFEST["featureSets"]}
        off = {n: w for n, w in off.items() if w}
        r: dict = {"accepted": True}
        if off:
            r["mode"] = "degraded"
            r["unavailableFeatures"] = [{"featureSet": n, "missingCapabilities": m, "effect": "disabled"}
                                        for n, (_, m) in off.items()]
            r["notes"] = [f"{n}: {why}" for n, (why, _) in off.items()]
        return r


# ---------- the layer on the transport ----------

class Bridge:
    """Sits between the stdio transport and the SDK's server. Holds the grant, answers the MCPL methods, sends the
    pushes and patches the `initialize` answer."""

    HANDLED = {"featureSets/update", "context/beforeInference"}
    # MCPL methods this server doesn't take: requests are refused (SPEC §6.6), notifications dropped.
    OTHERS = {"inference/lifecycle", "inference/chunk", "state/rollback", "mcpl/manifest", "tools/lifecycle",
              "tools/observe"}

    def __init__(self, config: str | None, instructions: str | None = None):
        self.config = config
        self.instructions = instructions   # given to an MCPL host in place of the plain client's
        self.policy = Policy()
        self.mcpl = False                  # the host offered MCPL in `initialize`
        self.init_id = None
        self.out = None                    # the transport's write stream
        self.pending: dict[str, list] = {}
        self.ids = 0
        self.changed = None                # an anyio.Event, set and replaced when the grant changes
        self.status_cache: tuple[float, str] = (0.0, "")
        self.status_busy = False
        self.pushed = 0
        self.listens = 0                   # `listen` calls running (local chat isn't pushed meanwhile)
        self.list_ids: set = set()         # tools/list requests the SDK hasn't answered yet
        self.listed: bool | None = None    # whether the last tools/list answered was made while pushing

    @property
    def pushing(self) -> bool:
        """Some of the world's events go out as pushes, so `wait_events` has nothing to wait for."""
        return self.mcpl and any(self.policy.active(s) for s in PUSH_SETS)

    def not_pushed(self) -> list[str]:
        return [s for s in PUSH_SETS if not self.policy.active(s)]

    # ----- running -----

    async def serve_stdio(self, server):
        from mcp.server.stdio import stdio_server
        async with stdio_server() as (read, write):
            await self.run(server, read, write)

    async def run(self, server, read, write):
        """Serve `server` (an MCPServer) over a transport's streams, with this layer between."""
        import anyio
        from mcp.shared._context_streams import create_context_streams
        from mcp.shared.message import SessionMessage

        lowlevel = server._lowlevel_server
        to_server, server_in = create_context_streams[SessionMessage | Exception](0)
        server_out, from_server = create_context_streams[SessionMessage](0)
        self.out = write
        self.changed = anyio.Event()

        async def inbound():
            async with to_server:
                async for item in read:
                    if isinstance(item, Exception) or not await self.take(item):
                        await to_server.send(item)

        async def outbound():
            async with from_server:
                async for sm in from_server:
                    await write.send(self.patch(sm))

        try:
            async with anyio.create_task_group() as tg:
                self.tg = tg
                tg.start_soon(outbound)
                tg.start_soon(self.push_loop)

                async def serve():
                    await lowlevel.run(server_in, server_out, lowlevel.create_initialization_options())
                    tg.cancel_scope.cancel()
                tg.start_soon(serve)
                await inbound()
        finally:
            await write.aclose()   # the transport's writer ends with it (the SDK's server would close it itself)

    # ----- messages from the host -----

    async def take(self, sm) -> bool:
        """True if this layer dealt with the message; False to pass it to the SDK."""
        import anyio
        import mcp_types as t
        msg = sm.message
        if isinstance(msg, t.JSONRPCRequest):
            if msg.method == "initialize":
                caps = (msg.params or {}).get("capabilities") or {}
                self.mcpl = isinstance((caps.get("experimental") or {}).get("mcpl"), dict)
                self.init_id = msg.id
                return False
            if msg.method == "tools/list":
                if self.mcpl:
                    self.list_ids.add(msg.id)
                return False
            if msg.method == "featureSets/update":
                try:
                    receipt = self.policy.update(msg.params)
                except ValueError as e:
                    await self.error(msg.id, -32602, f"Invalid featureSets/update: {e}")
                else:
                    await self.answer(msg.id, receipt)
                on = [n for n in MANIFEST["featureSets"] if self.policy.active(n)]
                log.info("MCPL feature sets on: %s", ", ".join(on) or "none")
                await self.grant_changed()
                return True
            if msg.method == "context/beforeInference":
                async def hook(req=msg):
                    await self.answer(req.id, await self.before_inference())
                self.tg.start_soon(hook)   # not awaited here: the host's other messages keep coming meanwhile
                return True
            if msg.method in self.OTHERS or msg.method.startswith("channels/"):
                await self.error(msg.id, -32601, f"Method not found: {msg.method}")
                return True
            return False
        if isinstance(msg, t.JSONRPCNotification):
            if msg.method == "featureSets/update":
                self.policy.narrow(msg.params)
                await self.grant_changed()
                return True
            return msg.method in self.OTHERS or msg.method.startswith("channels/")
        if isinstance(msg, (t.JSONRPCResponse, t.JSONRPCError)) and msg.id in self.pending:
            slot = self.pending[msg.id]
            slot[1] = msg
            slot[0].set()
            return True
        return False

    async def grant_changed(self):
        import anyio
        import mcp_types as t
        self.changed.set()
        self.changed = anyio.Event()
        if self.listed is not None and self.listed != self.pushing:
            await self.send(t.JSONRPCNotification(jsonrpc="2.0", method="notifications/tools/list_changed"))

    def patch(self, sm):
        """The SDK's answers on their way out: `initialize`'s with this server's MCPL manifest when the host offered
        MCPL, and `tools/list`'s without `wait_events` while events are pushed."""
        import mcp_types as t
        from mcp.shared.message import SessionMessage
        msg = sm.message
        listing = getattr(msg, "id", None) in self.list_ids
        if listing:
            self.list_ids.discard(msg.id)
        if not isinstance(msg, t.JSONRPCResponse):
            return sm
        if self.mcpl and msg.id == self.init_id:
            self.init_id = None
            result = dict(msg.result)
            caps = dict(result.get("capabilities") or {})
            caps["experimental"] = {**(caps.get("experimental") or {}), "mcpl": MANIFEST}
            caps["tools"] = {**(caps.get("tools") or {}), "listChanged": True}
            result["capabilities"] = caps
            if self.instructions:
                result["instructions"] = self.instructions
        elif listing:
            self.listed = self.pushing
            if not self.listed:
                return sm
            result = {**msg.result, "tools": [x for x in msg.result.get("tools") or []
                                              if x.get("name") not in HIDDEN_WHILE_PUSHING]}
        else:
            return sm
        return SessionMessage(t.JSONRPCResponse(jsonrpc="2.0", id=msg.id, result=result), metadata=sm.metadata)

    # ----- messages to the host -----

    async def send(self, msg):
        from mcp.shared.message import SessionMessage
        await self.out.send(SessionMessage(msg))

    async def answer(self, rid, result: dict):
        import mcp_types as t
        await self.send(t.JSONRPCResponse(jsonrpc="2.0", id=rid, result=result))

    async def error(self, rid, code: int, message: str):
        import mcp_types as t
        await self.send(t.JSONRPCError(jsonrpc="2.0", id=rid, error=t.ErrorData(code=code, message=message)))

    async def request(self, method: str, params: dict, timeout: float):
        """Send a request and wait for its answer: the response or error message, or None after `timeout`."""
        import anyio
        import mcp_types as t
        self.ids += 1
        rid = f"ibasho-{self.ids}"   # strings, so they never meet the SDK's own (numeric) request ids
        slot = [anyio.Event(), None]
        self.pending[rid] = slot
        try:
            await self.send(t.JSONRPCRequest(jsonrpc="2.0", id=rid, method=method, params=params))
            with anyio.move_on_after(timeout):
                await slot[0].wait()
            return slot[1]
        finally:
            self.pending.pop(rid, None)

    # ----- the status line -----

    async def before_inference(self) -> dict:
        if not self.policy.active(STATUS):
            return {"contextInjections": []}
        text = await self.status()
        inj = [{"namespace": NAMESPACE, "position": "beforeUser", "content": [{"type": "text", "text": text}]}]
        return {"featureSet": STATUS, "contextInjections": inj if text else []}

    async def status(self) -> str:
        """A fresh status line, or the last one if a new one takes too long (the hook has 5 s)."""
        import anyio
        made, text = self.status_cache
        if time.time() - made < STATUS_FRESH or self.status_busy:
            return text if time.time() - made < STATUS_STALE else ""
        self.status_busy = True

        def make():
            try:
                line = status_line(Ctx(_Args(self.config)))
            except Exception as e:   # the line is a courtesy; a failure is logged, never the turn's problem
                log.warning("status line: %s", e)
                line = ""
            self.status_cache = (time.time(), line)
            self.status_busy = False
            return line

        with anyio.move_on_after(STATUS_WAIT):
            return await anyio.to_thread.run_sync(make, abandon_on_cancel=True)
        made, text = self.status_cache
        return text if time.time() - made < STATUS_STALE else ""

    # ----- pushing -----

    async def push_loop(self):
        """While some of the push feature sets are active: what `ibasho watch` would print, as pushes."""
        import anyio
        from .cli.hear import Watcher, unread_line

        watcher = None
        while True:
            if not self.pushing:
                await self.changed.wait()
                continue
            try:
                if watcher is None:
                    ctx = Ctx(_Args(self.config))
                    watcher = await anyio.to_thread.run_sync(Watcher, ctx, abandon_on_cancel=True)
                    log.info("pushing events after #%d", watcher.since)
                    unread = await anyio.to_thread.run_sync(unread_line, ctx, abandon_on_cancel=True)
                    if unread:
                        await self.push(Wake(unread, t=time.time()), ctx, " IM" in unread.split(";")[0])
                wakes = await anyio.to_thread.run_sync(watcher.poll_wakes, POLL_WAIT, abandon_on_cancel=True)
            except Down:
                watcher = None    # not running yet, or started afresh: begin again from the cursor file
                await anyio.sleep(5)
                continue
            except ApiError as e:
                log.warning("events: %s", e)
                await anyio.sleep(5)
                continue
            for w in wakes:
                await self.push(w, watcher.ctx)

    @contextlib.contextmanager
    def listening(self):
        """While the agent's own `listen` collects local chat, local chat isn't pushed too."""
        self.listens += 1
        try:
            yield
        finally:
            self.listens -= 1

    async def push(self, w: Wake, ctx: Ctx, unread_addressed: bool = False):
        import mcp_types as t
        try:
            grid = ctx.api.status().get("grid") or ctx.cfg.grid
        except ApiError:
            grid = ctx.cfg.grid
        fs, params = push_params(w, grid, unread_addressed)
        if not self.policy.active(fs) or (fs == CHAT and self.listens):
            return
        r = await self.request("push/event", params, PUSH_TIMEOUT)
        self.pushed += 1
        if r is None:
            log.warning("push %s: the host didn't answer within %d s", params["eventId"], PUSH_TIMEOUT)
        elif isinstance(r, t.JSONRPCError):
            log.warning("push %s refused: %s %s", params["eventId"], r.error.code, r.error.message)
        elif isinstance(r.result, dict) and r.result.get("accepted") is False:
            log.info("push %s not accepted: %s", params["eventId"], r.result.get("reason"))


class _Args:
    def __init__(self, config):
        self.config = config
