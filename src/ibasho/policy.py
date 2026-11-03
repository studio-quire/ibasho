"""What the daemon lets through to the viewer, and the counters behind it.

Every request to the viewer passes `Policy.check` before it goes out, whether it came from an `ibasho` command,
the MCP server, `ibasho raw` or another program on the HTTP API. Two layers:

- the allowlist (`ALLOWED`): the viewer APIs and ops the daemon passes on, with their arguments checked where an
  op names a setting, a notification, a file or a place. Unknown means refused. It holds what Ibasho's own
  commands, MCP tools, hearing and keeper send, the Ibasho viewer's APIs for living in the world, and the stock
  ops for the avatar, inventory, outfits and gestures. The person can add to it in config.toml (`[policy] allow`,
  `Widening`); the agent can't.
- the hard rules, which hold for everything, added or not (docs: README "Safety defaults"):
  - messages: a daily count (Second Life time, shared by everything on this install) well under Linden Lab's
    5,000, and a per-minute rate, over what reaches people: local chat (channel 0), IMs and group messages, as the
    Ibasho viewer counts them against its own `IbashoMaxMessagesPerDay`, plus friendship and teleport offers and
    gives, which the viewer doesn't count. Chat to scripts on other channels and dialog answers aren't messages;
  - money: a cap per payment, per login and per day; uploads get a max cost the viewer enforces; no buying L$,
    no buying land, no granting objects permission to take L$;
  - maturity: never raised past the configured rating;
  - logging in: nothing reads or drives the viewer while it's on its login screen (the password field is there),
    and only the person answers MFA prompts;
  - files: the viewer writes pictures only into the pictures folder, and reads files to upload only from the life
    folder and the pictures folder;
  - pages on this machine or its network aren't opened;
  - the Linden grid gate: on a Linden Lab grid without the scripted-agent flag confirmed, everything but quitting
    is refused.

`check_rpc` and `check_listen` do the same for the script editor's calls and for relaying more of the viewer's
event pumps.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Caps, MATURITY, inside

log = logging.getLogger("ibasho.policy")
SLT = ZoneInfo("America/Los_Angeles")


def slt_day(t: float | None = None) -> str:
    return dt.datetime.fromtimestamp(t if t is not None else time.time(), SLT).date().isoformat()


# ---- counters ----

class Ledger:
    """Messages and L$ spent per SLT day, and L$ spent this login; kept on disk so restarts don't reset them."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.s = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.s = {}
        self.s.setdefault("day", slt_day())
        self.s.setdefault("messages", 0)
        self.s.setdefault("spent", 0)
        self.s.setdefault("session", {"key": None, "spent": 0})

    def _roll(self):
        today = slt_day()
        if self.s["day"] != today:
            self.s.update(day=today, messages=0, spent=0)

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s), encoding="utf-8")
        # Windows refuses to replace a file someone has open for a moment (a reader, a virus scanner): try again
        # shortly. If it never works, the counts in memory still hold, and the next save writes them.
        for wait in (0.02, 0.05, 0.1, 0.2, 0.4, None):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if wait is None:
                    log.warning("couldn't save %s (in use); keeping the counts in memory", self.path)
                    return
                time.sleep(wait)

    @property
    def messages(self) -> int:
        self._roll()
        return self.s["messages"]

    @property
    def spent_today(self) -> int:
        self._roll()
        return self.s["spent"]

    @property
    def spent_session(self) -> int:
        return self.s["session"]["spent"]

    def add_messages(self, n: int = 1):
        self._roll()
        self.s["messages"] += n
        self._save()

    def sync_messages(self, day: str, count: int):
        """Another count of the same SLT day's messages (the viewer's): keep the higher."""
        self._roll()
        if day == self.s["day"] and count > self.s["messages"]:
            self.s["messages"] = count
            self._save()

    def add_spend(self, amount: int):
        self._roll()
        self.s["spent"] += amount
        self.s["session"]["spent"] += amount
        self._save()

    def new_session(self, key: str):
        """A login. The same key again (a daemon restart mid-login) keeps the session's spend."""
        if self.s["session"].get("key") != key:
            self.s["session"] = {"key": key, "spent": 0}
            self._save()


class RateLimiter:
    """Token bucket: `per_minute` on average, bursts of a few."""

    def __init__(self, per_minute: int, burst: int | None = None, clock=time.monotonic):
        self.rate = per_minute / 60.0
        self.capacity = float(burst if burst is not None else max(1, min(5, per_minute)))
        self.tokens = self.capacity
        self.clock = clock
        self.last = clock()

    def _refill(self):
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now

    def wait_time(self) -> float:
        """0 if a message may go now, else seconds until one may."""
        if self.rate <= 0:
            return float("inf")
        self._refill()
        return 0.0 if self.tokens >= 1 else (1 - self.tokens) / self.rate

    def take(self):
        self._refill()
        self.tokens -= 1


# ---- the allowlist ----

# Pump -> {op: the Policy method that checks its arguments, or None for an op that names nothing on this machine
# (no file, program, page, window or setting) and acts only in the world or reads}. Everything else is refused
# unless the person adds it ([policy] allow).
ALLOWED: dict[str, dict[str, str | None]] = {
    "_command": dict.fromkeys(("getAPIs", "getAPI", "ping", "getFeatures", "getFeature")),
    # The Ibasho viewer's APIs for living in the world. Not its window controls (spectator, renderUI, streamUI,
    # hideAvatars, getHidden): the window and what it shows are the person's.
    "IbashoAgent": {**dict.fromkeys((
        "balance", "pay", "give", "attach", "detach", "attachPoints", "offerFriendship", "offerTeleport",
        "requestTeleport", "joinGroup", "leaveGroup", "activateGroup", "setHome", "parcel", "people", "friends",
        "names", "upload", "uploadMesh", "wearables", "createWearable", "editWearable", "rebake", "move", "stop",
        "probeGround", "messages")),
        # a name search like the viewer's avatar picker, reading a profile, reading one of my notecards, and a
        # region looked up on the map (viewers that have them); my own profile and display name, whatever the
        # agent chooses to put there
        "findAvatars": "_find_avatars", "profile": "_profile", "readNotecard": "_read_notecard",
        "findRegion": "_find_region", "setProfile": "_set_profile", "setDisplayName": "_set_display_name"},
    "IbashoIM": dict.fromkeys(("send", "sessions", "history", "markRead", "leave", "conference")),
    "IbashoObjects": dict.fromkeys(("nearby", "get", "find", "request", "buy", "select", "deselect", "selection")),
    "IbashoBuild": dict.fromkeys(("create", "modify", "duplicate", "rez", "link", "unlink", "take", "delete",
                                  "returnObjects")),
    # Linden Lab's stock APIs: the avatar, the camera, inventory, outfits, gestures.
    "LLAgent": {**dict.fromkeys((
        "requestSit", "requestStand", "requestTouch", "resetAxes", "getPosition", "startAutoPilot", "getAutoPilot",
        "startFollowPilot", "setAutoPilotTarget", "stopAutoPilot", "lookAt", "getGroups", "setCameraParams",
        "setFollowCamActive", "removeCameraParams", "playAnimation", "stopAnimation", "getAnimationInfo", "getID",
        "getNearbyAvatarsList", "getNearbyObjectsList", "getAttachedObjectsList", "getAgentScreenPos",
        "getCamera")), "requestTeleport": "_teleport"},
    "LLAppearance": dict.fromkeys(("wearOutfit", "wearItems", "detachItems", "getOutfitsList", "getOutfitItems")),
    "LLInventory": dict.fromkeys(("getItemsInfo", "getFolderTypeNames", "getAssetTypeNames", "getBasicFolderID",
                                  "getDirectDescendants", "collectDescendantsIf")),
    "LLGesture": dict.fromkeys(("getActiveGestures", "isGesturePlaying", "startGesture", "stopGesture")),
    "LLChatBar": {"sendChat": None},
    "LLNotifications": {"respond": "_respond", "cancel": None},
    "LLViewerControl": {"get": "_setting", "set": "_setting", "toggle": "_setting"},
    "LLViewerWindow": {"saveSnapshot": "_snapshot"},
    "UI": {"getValue": "_ui_value", "call": "_ui_call"},
    "LLAppViewer": dict.fromkeys(("requestQuit", "forceQuit")),
    "LLStartUp": dict.fromkeys(("postStartupState", "getStateTable")),
}

# Why some of what isn't on the list isn't, for the refusal.
NOT_PASSED = {
    "LLWindow": "keys and clicks land in whatever window the viewer has open (its settings, its upload and web "
                "windows)",
    "LLFloaterReg": "the viewer's windows include uploading files from this machine, changing any setting and "
                    "opening web pages",
    "UI call": "menu functions include uploading files, changing settings, opening web pages and turning on the "
               "microphone; the one passed is World.EnvSettings with sunrise, noon, sunset, midnight or region "
               "(`ibasho sky`)",
    "UI": "the viewer's menus and controls reach what the rest of these rules keep out",
    "LLURLDispatcher": "links can open windows and web pages",
    "LLCommandDispatcher": "links can open windows and web pages",
    "LLTeleportHandler": "teleport with `LLAgent requestTeleport` (`ibasho tp`)",
    "LLNotifications requestAdd": "a made-up notification can open a web page",
    "LLViewerControl set_notation": "settings that take LLSD include commands and lists of files",
    "LLViewerControl vars": "the dump of every setting includes the saved login",
    "GroupChat": "group messages go through `IbashoIM send` (`ibasho im GROUP`)",
    "_command listen": "relaying another pump goes through POST /listen",
}
for _op in ("spectator", "renderUI", "streamUI", "hideAvatars", "getHidden"):
    NOT_PASSED[f"IbashoAgent {_op}"] = ("the window and what it shows (spectator mode, the UI, what's held back "
                                        "for a stream, hidden avatars) are the person's")

# Settings the agent may read and change: how the world looks and sounds to it, the camera, name tags and what
# the window shows. Numbers, true/false and vectors, none of which can name a file, a program or
# a page. Graphics by family (Render...), the rest by name.
SETTINGS = frozenset((
    # sound
    "AudioLevelMaster", "AudioLevelAmbient", "AudioLevelMedia", "AudioLevelMusic", "AudioLevelSFX", "AudioLevelUI",
    "AudioLevelVoice", "AudioLevelWind", "AudioStreamingMusic", "AudioStreamingMedia", "MuteAudio", "MuteAmbient",
    "MuteMedia", "MuteMusic", "MuteSounds", "MuteUI", "MuteVoice", "MuteWhenMinimized", "EnableCollisionSounds",
    "EnableGestureSounds", "MediaRollOffMin", "MediaRollOffMax", "MediaRollOffRate", "ParcelMediaAutoPlayEnable",
    "MediaTentativeAutoPlay", "MediaShowOnOthers", "MediaShowOutsideParcel", "MediaShowWithinParcel",
    # the camera
    "CameraAngle", "CameraOffsetScale", "CameraOffsetRearView", "CameraOffsetBuild", "CameraPositionSmoothing",
    "CameraPresetType", "CameraFNumber", "CameraFocalLength", "CameraFieldOfView", "CameraFocusTransitionTime",
    "CameraMaxCoF", "CameraDoFResScale", "DisableCameraConstraints", "FirstPersonAvatarVisible",
    "AutoPilotLocksCamera",
    # detail and the frame rate it aims for
    "TargetFPS", "AutoTuneFPS", "AutoTuneLock", "AutoTuneImpostorByDistEnabled",
    "AutoTuneImpostorFarAwayDistance", "AutoTuneRenderFarClipMin", "AutoTuneRenderFarClipTarget",
    "TextureDiscardLevel", "TextureLoadFullRes", "TextureCameraBoost", "AvatarPhysics",
    # the sky
    "SkyAmbientScale", "SkyNightColorShift", "SkySunDefaultPosition", "SkyMoonDefaultPosition",
    "SkyOverrideSimSunPosition", "EnvironmentPersistAcrossLogin",
    # names, chat and what the window shows
    "AvatarNameTagMode", "NameTagShowDisplayNames", "NameTagShowFriends", "NameTagShowGroupTitles",
    "NameTagShowUsernames", "UseChatBubbles", "ChatBubbleOpacity", "ChatFontSize", "ChatFullWidth",
    "ConsoleMaxLines", "NearbyToastLifeTime", "NearbyToastFadingTime", "UIScaleFactor", "ShowNavbarNavigationPanel",
    "ShowNavbarFavoritesPanel", "ShowMiniLocationPanel", "ShowNetStats", "ShowHoverTips", "ShowLandHoverTip",
    "ShowAllObjectHoverTip", "ShowPropertyLines", "ShowParcelOwners", "ShowBanLines", "ShowSelectionBeam",
    "ShowCrosshairs", "ShowScriptErrors", "ShowScriptErrorsLocation", "ShowAxes", "ShowNewInventory",
    "ShowInInventory", "ShowOfferedInventory", "HideSelectedObjects", "EffectScriptChatParticles", "PlayTypingAnim",
    "PlayChatAnim",
    # moving
    "AutomaticFly", "AvatarHoverOffsetZ",
))
RENDER_SETTINGS = re.compile(r"^Render[A-Z]\w*$")
# ...but not the ones for debugging the renderer or its startup, which can leave the viewer unable to start
NOT_RENDER_SETTINGS = re.compile(r"^Render(Debug|GL|InitError|Nsight|MaxOpenGLVersion|PerformanceTest|AppleUse|"
                                 r"ShaderCache|AutoMuteLogging)")
TEXT_SETTINGS = {"VoiceOutputAudioDevice"}   # the one text setting: hearing points voice chat at its device
# Readable, not changeable: the grid, and how much of the machine the viewer leaves to the rest of it (the
# person's choice: a viewer that stops yielding takes the graphics card from whatever else they run).
READ_ONLY_SETTINGS = {"CurrentGrid", "IbashoSpectator", "YieldTime", "BackgroundYieldTime"}
SETTING_GROUPS = (None, "Global", "PerAccount")

# Notifications the agent answers as it likes: dialogs and permission requests from scripts, offers of things,
# friendship, teleports and groups, and the confirmations Ibasho's own ops lead to. Any other notification it
# can only dismiss (`cancel`, or one of DISMISS's buttons): some open web pages in the system's browser
# (LoadWebPage, GotoURL), and new ones come with every viewer.
ANSWERABLE = frozenset((
    "ScriptDialog", "ScriptDialogGroup", "ScriptQuestion", "ScriptQuestionExperience", "ObjectGiveItem",
    "OwnObjectGiveItem", "UserGiveItem", "OfferFriendship", "OfferFriendshipNoMessage", "OfferCallingCard",
    "TeleportOffered", "TeleportOffered_MaturityExceeded", "TeleportRequest", "TeleportViaSLAPP", "JoinGroup",
    "JoinGroupCanAfford", "JoinGroupNoCost", "GroupLeaveConfirmMember", "GroupLeaveConfirmMemberNoFee",
    "ReplaceAttachment", "ParcelPlayingMedia",
))
DISMISS = frozenset(("Cancel", "Decline", "Discard", "No", "Deny", "Later", "Ignore", "Close", "Dismiss", "Mute",
                     "Block", "Client_Side_Mute", "Client_Side_Ignore", "BlockExperience"))
# Buttons from the notification templates carry the template's name: "OK_okbutton", "Cancel_okcancelbuttons".
TEMPLATE_SUFFIX = re.compile(r"_[a-z]+$")

# The script editor's JSON-RPC (/rpc): what it does with objects and their scripts, and reading the script
# language's syntax. Not its file watcher (script.*), meant for an editor on this machine.
EDITOR_METHODS = frozenset((
    "object.list", "object.request", "object.item.create", "object.item.delete", "object.item.modify",
    "object.content.get", "object.content.save", "object.script.set_running", "object.script.reset",
    "object.modify", "object.unpublish", "language.syntax.id", "language.syntax", "language.syntax.get",
    "command.list", "command.execute"))
EDITOR_COMMANDS = {"viewer.teleport", "viewer.camera.focus", "viewer.object.save_back_to_contents",
                   "viewer.script.reset_all", "viewer.script.recompile_all"}

# My own sky (World > Environment in the viewer's menu, which only changes what this viewer shows): the menu
# function's parameters for the fixed skies and going back to the region's.
SKY_FUNCTION = "World.EnvSettings"
SKY_PARAMETERS = ("sunrise", "noon", "sunset", "midnight", "region")
UI_CALL_ARGS = {"op", "function", "parameter", "reply", "reqid"}
BALANCE_PATH = "/main_view/menu_stack/status_bar_container/status/balance_bg/balance"   # a stock viewer's L$
SNAPSHOT_ARGS = {"op", "filename", "width", "height", "showui", "showhud", "rebuild", "type", "reply", "reqid"}
SNAPSHOT_TYPES = ("COLOR", "DEPTH")    # anything else stops the viewer
REGION_NAME = re.compile(r"^[^/\\?#:%&<>\"\x00-\x1f]{1,64}$")
AVATAR_QUERY = re.compile(r"^[^\x00-\x1f]{1,64}$")   # a name, or part of one, to search for
UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# ---- the hard rules ----

MESSAGE_OPS = {
    ("LLChatBar", "sendChat"), ("GroupChat", "sendGroupIM"), ("IbashoIM", "send"), ("IbashoIM", "conference"),
    ("IbashoAgent", "offerFriendship"), ("IbashoAgent", "offerTeleport"), ("IbashoAgent", "requestTeleport"),
    ("IbashoAgent", "give"),
}
UPLOAD_OPS = {("IbashoAgent", "upload"), ("IbashoAgent", "uploadMesh")}
# Ops that take a path on this machine: (read or write, the keys holding paths). Writes go to the pictures folder
# only; reads come from the life folder or the pictures.
PATH_OPS = {
    ("LLViewerWindow", "saveSnapshot"): ("write", ("filename",)),
    ("IbashoAgent", "upload"): ("read", ("path",)),
    ("IbashoAgent", "uploadMesh"): ("read", ("path", "lod_med", "lod_low", "lod_lowest", "physics")),
}
DENY_UI_FUNCTIONS = {"buycurrency", "land.buy", "land.buypass"}
DENY_FLOATERS = {"buy_currency", "buy_currency_html", "buy_land"}
DENY_URL = re.compile(r"buy_?currency|buy_?land|land_?buy", re.I)
CAP_SETTINGS = {"ibashomaxpayment": "max_payment", "ibashomaxsessionspend": "max_session_spend",
                "ibashomaxuploadcost": "max_upload_cost", "ibashomaxmessagesperday": "messages_per_day"}
OFF_SETTINGS = {"ibashoallowlandbuy", "ibashoallowcurrencybuy"}   # the viewer's own "no land, no L$" switches
LOG_SETTINGS = {"lognearbychat": False, "keepconversationlogtranscripts": 0, "logchat": False, "logchatim": False}
# Settings with a rule of their own (below): changeable from here only the safe way.
RULED_SETTINGS = {"preferredmaturity"} | set(CAP_SETTINGS) | OFF_SETTINGS | set(LOG_SETTINGS)
SECRET_SETTINGS = {"userlogininfo"}
SETTING_OPS = ("set", "toggle", "set_notation")
# Pumps that can open a page or a link in the viewer's browser (or the system's): only reachable once the person
# allows them, and then still never to a page on this machine or its network.
NAVIGATING_PUMPS = {"LLFloaterReg", "LLURLDispatcher", "LLCommandDispatcher", "LLTeleportHandler", "UI",
                    "LLNotifications"}
# Pages on this machine or its network: a file, the machine itself, a private address. The viewer's browser
# would show them, and a snapshot would read them back. (The Ibasho viewer refuses them itself too, in more
# spellings.) 100.64/10 is shared address space: carrier NAT, and VPN meshes that put a person's own machines there.
LOCAL_URL = re.compile(
    r"\bfile:|\bfilesystem:|\bview-source:|%66%69%6c%65%3a|file%3a|"
    r"\b(?:https?|wss?|ftp)(?::|%3a)(?://|%2f%2f)(?:[^/\s\"'@]*@)?"
    r"(?:localhost|127\.|0\.0\.0\.0|\[|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|169\.254\.|"
    r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)", re.I)
CHANNEL_PREFIX = re.compile(r"^/(-?\d+)")
PRE_LOGIN_PUMPS = {"LLStartUp", "LLAppViewer"}
PRE_LOGIN_COMMANDS = {"getAPIs", "getAPI", "ping", "getFeature", "getFeatures"}
# The login request (with the password's hash) passes through pumps like these; nothing may listen to them.
LOGIN_PUMPS = re.compile(r"login|xmlrpc", re.I)
CHAT_URL = re.compile(r"/app/chat/", re.I)

# What still holds for a request the person allowed with a wildcard ("UI call", "LLViewerControl set"), unless
# their entry names the exact thing: settings that name a program, a command, a file or folder, a URL, or open
# up the browser, plugins or debugging; menu functions that start programs, record or replay input, or open
# pages; windows that upload files, change settings or show pages, or are given a path.
DANGEROUS_SETTINGS = re.compile(
    r"editor|leap|command|websocket|(?<!pro)file|path|dir(?![a-z])|dir_|folder|location|cache|logfile|skin|plugin|"
    r"cef|browser|debug|recorder|pilot|stats|crash|snapshotbase|url|uri\b|proxy|exec|launch|lua", re.I)
SETTING_UI_FUNCTIONS = {"togglecontrol", "toggleshadercontrol", "checkcontrol"}
DENY_UI = re.compile(r"externaleditor|scripteditorserver|eventrecorder|agentpilot|showurl|webcontent|webbrowser|"
                     r"reportbug|marketplace|^url\.|showhelp|webinspector|showsource|lua|^file\.|upload|"
                     r"debugsettings|showdebug|preferences|voice|speak|microphone", re.I)
DANGEROUS_FLOATERS = re.compile(r"upload|bulk|file|debug|settings|preferences|web|browser|media|help|marketplace|"
                                r"search|guidebook|how_to|lua|script|event|snapshot|outfit_photo|report", re.I)
# Menu functions that open the window their parameter names, as LLFloaterReg does: the window rules apply to them
# too. A dangerous window opens only through an exact "LLFloaterReg showInstance NAME" entry.
FLOATER_UI_FUNCTIONS = {"floater.show", "floater.showorbringtofront", "floater.toggle", "floater.toggleorbringtofront",
                        "button.togglefloater", "showsidetraypanel"}
PATHLIKE = re.compile(r"^[A-Za-z]:|[\\/]|\.\.")   # a path, or a URL
FILE_KEYS = re.compile(r"file|path|dir|folder|url", re.I)

POSITIVE = re.compile(r"^\s*(yes|ok|okay|allow|accept|grant|buy|pay|confirm|purchase|join|teleport|go|continue|"
                      r"agree|change( preference)?|upload|publish)\b", re.I)
PERSON_ONLY_NOTES = re.compile(r"MFA|TOS|TermsOfService")  # case-sensitive: whole-word-ish in CamelCase names
CURRENCY_NOTES = re.compile(r"currency|lindendollar|paymentmethod", re.I)
MATURITY_NOTES = re.compile(r"entryaccess|maturity|ratingchange|accessblocked", re.I)
SPEND_NOTES = {"LandBuyPass", "BuyOriginal", "BuyOriginalNoOwner", "BuyCopy", "BuyCopyNoOwner", "BuyContents",
               "BuyContentsNoOwner", "PayConfirmation", "JoinGroupCanAfford", "CreateGroupCost",
               "UploadCostConfirmation", "PublishClassified"}
DEBIT_TEXT = re.compile(r"linden dollars|\bL\$|debit", re.I)
LINDEN_AMOUNT = re.compile(r"L\$\s*([\d,]+)")


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    status: int = 200          # HTTP status when refused: 403 policy, 429 limits, 409 state
    retry_after: float = 0.0
    messages: int = 0          # to count once sent
    spend: int = 0             # to count once sent (if not reported by the reply)
    data: dict | None = None   # the request as it should go out (caps injected)
    rule: str = ""             # which rule refused it


def refuse(reason, status=403, rule="", **kw) -> Verdict:
    return Verdict(False, reason, status, rule=rule, **kw)


@dataclass
class ViewerState:
    logged_in: bool = False
    gate: str = ""                  # non-empty: everything but quitting is refused, with this reason
    notifications: dict | None = None


class Widening:
    """The person's additions to the allowlist: `[policy] allow` in config.toml, entries "PUMP", "PUMP OP" or
    "PUMP OP NAME" (OP may be *). NAME is what the op names: a setting's key, a menu function, a window, a
    notification, a pump to relay, a UI path; for "rpc METHOD NAME", a command of the script editor's
    command.execute. A NAME must match exactly."""

    def __init__(self, entries=()):
        self.exact: set[tuple[str, str, str]] = set()
        self.ops: set[tuple[str, str]] = set()
        for e in entries:
            parts = str(e).split(None, 2)
            if len(parts) == 1:
                self.ops.add((parts[0], "*"))
            elif len(parts) == 2:
                self.ops.add((parts[0], parts[1]))
            elif parts:
                self.exact.add((parts[0], parts[1], parts[2]))

    def allows(self, pump: str, op: str, name: str | None = None) -> str | None:
        """"exact" if an entry names this very thing, "any" if one allows the op whatever it names, else None."""
        if name is not None and (pump, op, name) in self.exact:
            return "exact"
        if (pump, op) in self.ops or (pump, "*") in self.ops:
            return "any"
        return None


def how_to_allow(*entry) -> str:
    e = " ".join(str(x) for x in entry if x is not None and x != "")
    return (f"The person who runs Ibasho can allow it in config.toml: [policy] allow = [\"{e}\"] "
            "(docs/safety.md says what that opens)")


class Policy:
    def __init__(self, caps: Caps, ledger: Ledger, limiter: RateLimiter | None = None, chat_logs: bool = False,
                 shots: Path | None = None, readable: list[Path] | None = None, allow=()):
        self.caps = caps
        self.ledger = ledger
        self.limiter = limiter or RateLimiter(caps.messages_per_minute)
        self.chat_logs = chat_logs
        self.shots = shots                 # the one folder the viewer may write files into (pictures)
        self.readable = readable or []     # the folders it may read files from for uploads (life/, the pictures)
        self.widen = Widening(allow)
        self.reserved = 0   # L$ in requests still waiting for the viewer's answer

    # -- files --

    def _paths(self, pump: str, op: str, data: dict) -> Verdict | None:
        """Ops that make the viewer read or write a file on this machine: only in the folders that are the
        agent's. Absolute paths only, since a relative one would be the viewer's to resolve."""
        rule = PATH_OPS.get((pump, op))
        if rule is None:
            return None
        mode, keys = rule
        folders = [self.shots] if mode == "write" else self.readable
        where = " or ".join(str(f) for f in folders if f) or "nowhere (no folders configured)"
        if mode == "write" and not data.get(keys[0]):
            return refuse(f"{pump} {op}: give {keys[0]}, a file in {where}", rule="files")
        ok = lambda p: isinstance(p, str) and os.path.isabs(p) and inside(p, *[f for f in folders if f])
        verb = "writes" if mode == "write" else "reads"
        for key in keys:
            for p in _leaves(data[key]) if data.get(key) is not None else []:
                if not ok(p):
                    return refuse(f"{pump} {op} {verb} files only in {where} (absolute paths); not {p!r}",
                                  rule="files")
                if op == "uploadMesh":
                    # a model names more files: glTF buffers, and images, which the upload window loads for its
                    # preview even without `textures`. (The daemon checks them again in the copies it hands the
                    # viewer: stage_upload.)
                    try:
                        refs = model_refs(Path(p).read_bytes(), Path(p).suffix)
                    except (OSError, ValueError):
                        refs = []   # nothing to read here, nor for the viewer
                    if refs is None:
                        return refuse(f"{pump} {op}: couldn't read {p} to check the files it refers to",
                                      rule="files")
                    if bad := next((r for r in refs if not plain_ref(r)
                                    or not ok(os.path.join(os.path.dirname(p), r))), None):
                        return refuse(f"{pump} {op}: {os.path.basename(p)} refers to {bad}; models upload only "
                                      f"with files in {where}, named relative to the model (in its folder or "
                                      "below)", rule="files")
        return None

    # -- the hard rules, for anything that would go out --

    def _hard(self, pump: str, op: str, data: dict) -> Verdict | None:
        if pump == "_command":
            if op == "listen" and LOGIN_PUMPS.search(str(data.get("source") or "")):
                return refuse("the login pumps carry the password's hash; not listening to them", rule="login")
            if op == "stoplistening" and str(data.get("listener") or "").startswith("ibasho_"):
                return refuse("the daemon's own listeners stay", rule="daemon")
        if pump == "LLNotifications" and op in ("ignore", "forward"):
            return refuse(f"LLNotifications {op}: the daemon relies on seeing every notification", rule="daemon")
        if v := self._paths(pump, op, data):
            return v
        if pump == "LLViewerControl" and (v := self._setting_rules(op, data)):
            return v
        if pump == "UI" and op == "call":
            fn = str(data.get("function") or "")
            if fn.lower() in DENY_UI_FUNCTIONS or (fn.lower() in FLOATER_UI_FUNCTIONS
                                                   and str(data.get("parameter") or "").lower() in DENY_FLOATERS):
                return refuse(f"{fn} {data.get('parameter') or ''}: no buying L$ or land from here", rule="money")
        if pump in NAVIGATING_PUMPS and LOCAL_URL.search(json.dumps(data)):
            return refuse("pages on this machine or its network (file:, localhost, private addresses) aren't "
                          "opened from here", rule="local pages")
        if pump == "LLFloaterReg" and str(data.get("name") or "").lower() in DENY_FLOATERS:
            return refuse(f"{data.get('name')}: no buying L$ or land from here", rule="money")
        if pump in ("LLURLDispatcher", "LLCommandDispatcher", "LLTeleportHandler"):
            if DENY_URL.search(json.dumps(data)):
                return refuse("no buying L$ or land from here", rule="money")
            if CHAT_URL.search(json.dumps(data)):
                return refuse("chat goes through LLChatBar sendChat (`ibasho say`), where it's counted",
                              rule="messages")
        return None

    def _setting_rules(self, op: str, data: dict) -> Verdict | None:
        """The settings no request may change past what the person set: the saved login, maturity, the viewer's
        own caps and switches, its chat logs."""
        key = str(data.get("key") or "").lower()
        if key in SECRET_SETTINGS or op == "vars":
            return refuse(f"{data.get('key') or 'vars'}: not readable from here", rule="settings")
        if op not in SETTING_OPS:
            return None
        if key == "preferredmaturity":
            cap = MATURITY[self.caps.maturity]
            below = {name: n for name, n in MATURITY.items() if n <= cap}
            v = data.get("value")
            if op == "toggle" or isinstance(v, bool) or v not in below.values():
                return refuse(f"PreferredMaturity takes one of {', '.join(f'{n} ({name})' for name, n in below.items())}"
                              f": maturity is capped at {self.caps.maturity} by the person's config", rule="maturity")
        if key in LOG_SETTINGS and not self.chat_logs and data.get("value") != LOG_SETTINGS[key]:
            return refuse(f"{data.get('key')}: the viewer's own chat logs are off by the person's config "
                          "(viewer.chat_logs)", rule="settings")
        if key in OFF_SETTINGS and (op == "toggle" or data.get("value") not in (False, 0, None)):
            return refuse(f"{data.get('key')}: no buying land or L$ from here (the person can turn it on in "
                          "the viewer)", rule="money")
        if key in CAP_SETTINGS:
            limit = getattr(self.caps, CAP_SETTINGS[key])
            try:
                if not 0 < int(data.get("value")) <= limit:
                    return refuse(f"{data.get('key')} comes from the config (caps.{CAP_SETTINGS[key]}); it can "
                                  f"only be lowered from here (1 to {limit})", rule="settings")
            except (TypeError, ValueError):
                return refuse(f"{data.get('key')} takes a number", rule="settings")
        return None

    # -- the allowlist's argument checks --

    def _setting(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        key = str(data.get("key") or "")
        if data.get("group") not in SETTING_GROUPS:
            return refuse(f"LLViewerControl {op}: group {data.get('group')!r}; settings here are Global or "
                          "PerAccount", rule="arguments")
        listed = settable(key) or key in TEXT_SETTINGS
        if op == "get" and (listed or key in READ_ONLY_SETTINGS or key.lower() in RULED_SETTINGS):
            return None
        if op == "set" and key.lower() in RULED_SETTINGS:
            return None    # _setting_rules checked the value
        if op == "set" and listed:
            if "value" in data and not plain_value(data["value"], text=key in TEXT_SETTINGS):
                return refuse(f"{key} takes " + ("a device name" if key in TEXT_SETTINGS else
                                                 "a number, true or false, or a vector of numbers"),
                              rule="arguments")
            return None
        if op == "toggle" and settable(key):
            return None
        w = self.widen.allows(pump, op, key)
        if w == "exact" or (w == "any" and not DANGEROUS_SETTINGS.search(key)):
            return None
        hint = ""
        if near := next((s for s in sorted(SETTINGS | TEXT_SETTINGS | READ_ONLY_SETTINGS)
                         if s.lower() == key.lower() and s != key), None):
            hint = f" (setting names are case-sensitive: {near})"
        elif key.lower().startswith("render") and not key.startswith("Render"):
            hint = " (setting names are case-sensitive, as the viewer spells them: RenderFarClip...)"
        what = ("the settings changed from here are graphics (Render...), sound volumes, the camera, the sky, "
                "name tags and what the window shows; docs/api.md lists them")
        if w == "any":
            what = "settings that name programs, files, folders or web addresses, or open up the browser or plugins"
        return refuse(f"LLViewerControl {op} {key or '(no key)'}: not passed on{hint}; {what}. "
                      + how_to_allow(pump, op, key), rule="allowlist")

    def _snapshot(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        if extra := sorted(set(data) - SNAPSHOT_ARGS):
            return refuse(f"saveSnapshot takes filename, width, height, showui, showhud, rebuild, type; not "
                          f"{', '.join(extra)}", rule="arguments")
        if "type" in data and data["type"] not in SNAPSHOT_TYPES:
            return refuse(f"saveSnapshot type is COLOR or DEPTH, not {data['type']!r}", rule="arguments")
        for k in ("width", "height"):
            v = data.get(k)
            if k in data and not (isinstance(v, int) and not isinstance(v, bool) and 16 <= v <= 8192):
                return refuse(f"saveSnapshot {k}: whole pixels, 16 to 8192", rule="arguments")
        for k in ("showui", "showhud", "rebuild"):
            if k in data and data[k] not in (True, False, 0, 1):
                return refuse(f"saveSnapshot {k}: true or false", rule="arguments")
        return None

    def _teleport(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        name = data.get("regionname")
        if not isinstance(name, str) or not REGION_NAME.match(name):
            return refuse(f"requestTeleport regionname {name!r}: a region's name (no / \\ : ? # % &)",
                          rule="arguments")
        for k in ("x", "y", "z"):
            v = data.get(k)
            if v is not None and not (isinstance(v, (int, float)) and not isinstance(v, bool)):
                return refuse(f"requestTeleport {k}: a number", rule="arguments")
        return None

    def _find_avatars(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        name = data.get("name")
        if not isinstance(name, str) or not AVATAR_QUERY.match(name) or not name.strip():
            return refuse(f"findAvatars name {name!r}: a name or part of one, on one line, up to 64 characters",
                          rule="arguments")
        return _timeout_check(op, data)

    def _profile(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        if not isinstance(data.get("id"), str) or not UUID.match(data["id"]):
            return refuse(f"profile id {data.get('id')!r}: an avatar's id (a UUID)", rule="arguments")
        return _timeout_check(op, data)

    def _set_profile(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        # Types only: what the profile says is the agent's own call.
        if "about" in data and not isinstance(data["about"], str):
            return refuse("setProfile about: text", rule="arguments")
        if "image_id" in data and not (isinstance(data["image_id"], str) and UUID.match(data["image_id"])):
            return refuse(f"setProfile image_id {data['image_id']!r}: a texture's asset id (a UUID)",
                          rule="arguments")
        return _timeout_check(op, data)

    def _set_display_name(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        if not isinstance(data.get("name"), str):
            return refuse("setDisplayName name: text (\"\" for the username)", rule="arguments")
        return _timeout_check(op, data)

    def _read_notecard(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        if not isinstance(data.get("item_id"), str) or not UUID.match(data["item_id"]):
            return refuse(f"readNotecard item_id {data.get('item_id')!r}: an inventory item's id (a UUID)",
                          rule="arguments")
        return _timeout_check(op, data)

    def _find_region(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        name = data.get("name")
        if not isinstance(name, str) or not REGION_NAME.match(name) or not name.strip():
            return refuse(f"findRegion name {name!r}: a region's name (no / \\ : ? # % &)", rule="arguments")
        return _timeout_check(op, data)

    def _ui_call(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        """Menu functions: only choosing my own sky passes, unless the person allows more."""
        fn = str(data.get("function") or "")
        if fn == SKY_FUNCTION and data.get("parameter") in SKY_PARAMETERS and set(data) <= UI_CALL_ARGS:
            return None
        w = self.widen.allows(pump, op, fn)
        if w == "exact" and fn.lower() not in FLOATER_UI_FUNCTIONS:
            return None
        if w:   # a window function named exactly still opens only the windows a wildcard would
            return self._backstop(pump, op, data)
        if fn == SKY_FUNCTION:
            return refuse(f"UI call {fn}: parameter {data.get('parameter')!r}; the skies passed are "
                          f"{', '.join(SKY_PARAMETERS)} (`ibasho sky`)", rule="arguments")
        return refuse(f"UI call {fn or '(no function)'}: not passed on; {NOT_PASSED['UI call']}. "
                      + how_to_allow(pump, op, fn), rule="allowlist")

    def _ui_value(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        path = str(data.get("path") or "")
        if path == BALANCE_PATH or self.widen.allows(pump, op, path):
            return None
        return refuse(f"UI getValue {path or '(no path)'}: only the L$ balance ({BALANCE_PATH}) is read this way. "
                      + how_to_allow(pump, op, path), rule="allowlist")

    def _respond(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        """Which notifications are answered, and how: the rest of the rules are in `_notification`."""
        n = notes.get(str(data.get("uuid")))
        if not n:   # not seen since the daemon started: can't tell what answering it would do
            return refuse("that notification isn't one the daemon has seen (it may be from before a restart, or "
                          "gone); `respond ID --cancel` dismisses it", rule="notifications")
        name = str(n.get("name") or "")
        if PERSON_ONLY_NOTES.search(name):
            return refuse(f"{name}: only the person answers this one (logins, codes, terms)", rule="login")
        if name in ANSWERABLE or name in SPEND_NOTES:
            return None
        buttons = [str(b.get("name")) for b in n.get("form") or []
                   if isinstance(b, dict) and b.get("type") == "button"]
        lone_ok = [button_base(b) for b in buttons] == ["OK"]   # an alert's only button acknowledges it
        dismissal = lambda k: button_base(k) in DISMISS or (lone_ok and button_base(k) == "OK")
        resp = data.get("response") if isinstance(data.get("response"), dict) else {}
        acts = [k for k, v in resp.items() if v and not dismissal(str(k))]
        if not acts or self.widen.allows(pump, op, name):
            return None
        dismissals = [b for b in buttons if dismissal(b)]
        how = f"press {' or '.join(dismissals)}, or " if dismissals else ""
        return refuse(f"{name}: a notification Ibasho only dismisses ({how}`respond ID --cancel`); answering it "
                      "could open a web page or act outside the world. " + how_to_allow(pump, op, name),
                      rule="notifications")

    def _allowlist(self, pump: str, op: str, data: dict, notes: dict) -> Verdict | None:
        ops = ALLOWED.get(pump)
        if ops is not None and op in ops:
            check = ops[op]
            return getattr(self, check)(pump, op, data, notes) if check else None
        name = _named(pump, op, data)
        w = self.widen.allows(pump, op, name)
        if w == "exact":
            return None
        if w == "any":
            return self._backstop(pump, op, data)
        why = NOT_PASSED.get(f"{pump} {op}") or NOT_PASSED.get(pump)
        if ops is None:
            what = f"{pump}: not one of the viewer APIs Ibasho passes requests to"
        else:
            what = f"{pump} {op}: not one of the {pump} requests Ibasho passes on ({', '.join(sorted(ops))})"
        return refuse(f"{what}{'; ' + why if why else ''}. " + how_to_allow(pump, op if op else None, name),
                      rule="allowlist")

    def _backstop(self, pump: str, op: str, data: dict) -> Verdict | None:
        """For a request the person allowed with a wildcard: the dangerous kinds still need naming exactly."""
        bad = None
        if pump == "LLViewerControl" and DANGEROUS_SETTINGS.search(str(data.get("key") or "")):
            bad = "a setting that names a program, a file, a folder or a web address, or opens up the browser"
        elif pump == "UI" and op == "call":
            fn = str(data.get("function") or "")
            if DENY_UI.search(fn) or (fn.lower() in SETTING_UI_FUNCTIONS
                                      and DANGEROUS_SETTINGS.search(str(data.get("parameter") or ""))):
                bad = "a menu function that starts a program, uploads, records, changes such settings or opens a page"
            elif fn.lower() in FLOATER_UI_FUNCTIONS:
                name = str(data.get("parameter") or "")
                if DANGEROUS_FLOATERS.search(name):
                    bad = ("a window that uploads files, changes settings or shows pages (it opens only with an exact "
                           f"\"LLFloaterReg showInstance {name}\" entry)")
        elif pump == "LLFloaterReg":
            key = data.get("key")
            if DANGEROUS_FLOATERS.search(str(data.get("name") or "")) or any(
                    isinstance(x, str) and PATHLIKE.search(x) for x in _leaves(key)) or any(
                    FILE_KEYS.search(str(k)) for k in _keys(key)):
                bad = "a window that uploads files, changes settings or shows pages, or one given a path"
        if bad:
            return refuse(f"{pump} {op} {_named(pump, op, data) or ''}: {bad}; allowed only when the config names "
                          "it exactly. " + how_to_allow(pump, op, _named(pump, op, data)), rule="allowlist")
        return None

    # -- the parts --

    def _messages(self, pump: str, op: str, data: dict) -> int:
        if (pump, op) not in MESSAGE_OPS:
            return 0
        if pump == "LLChatBar":
            if not str(data.get("message") or "").strip():
                return 0
            return 1 if chat_channel(data) in (0, None) else 0   # "//" (the last channel used): count it, to be safe
        if (pump, op) == ("IbashoIM", "conference") and not data.get("message"):
            return 0
        return 1

    def _spend_room(self) -> int:
        c, l = self.caps, self.ledger
        return max(0, min(c.max_session_spend - l.spent_session, c.max_daily_spend - l.spent_today) - self.reserved)

    def _check_spend(self, amount: int, what: str) -> Verdict | None:
        if amount < 0:
            return refuse(f"{what}: negative amount", rule="money")
        if amount > self.caps.max_payment:
            return refuse(f"{what}: L${amount} is over the per-payment cap of L${self.caps.max_payment}",
                          rule="money")
        pending = f", L${self.reserved} pending" if self.reserved else ""
        if amount > self.caps.max_session_spend - self.ledger.spent_session - self.reserved:
            return refuse(f"{what}: L${amount} would pass this login's cap of L${self.caps.max_session_spend} "
                          f"(L${self.ledger.spent_session} spent{pending})", rule="money")
        if amount > self.caps.max_daily_spend - self.ledger.spent_today - self.reserved:
            return refuse(f"{what}: L${amount} would pass today's cap of L${self.caps.max_daily_spend} "
                          f"(L${self.ledger.spent_today} spent{pending}, SLT day)", rule="money")
        return None

    def _notification(self, data: dict, notes: dict) -> Verdict | None:
        """Saying yes to a notification (`_respond` has let it through): what it would spend or change."""
        n = notes.get(str(data.get("uuid"))) or {}
        name = str(n.get("name") or "")
        buttons = {b.get("name"): str(b.get("text") or b.get("name") or "")
                   for b in n.get("form") or [] if isinstance(b, dict) and b.get("type") == "button"}
        resp = data.get("response") if isinstance(data.get("response"), dict) else {}
        chosen = [buttons[k] for k, v in resp.items() if v and k in buttons]
        if not any(POSITIVE.search(t) for t in chosen):
            return None
        msg = str(n.get("message") or "")
        if CURRENCY_NOTES.search(name):
            return refuse(f"{name}: no buying L$ (real money) from here", rule="money")
        if MATURITY_NOTES.search(name):
            return refuse(f"{name}: maturity stays at {self.caps.maturity} (the person's setting)", rule="maturity")
        if name.startswith("ScriptQuestion") and DEBIT_TEXT.search(msg):
            return refuse(f"{name}: an object asking to take L$ from you; that permission isn't granted from here",
                          rule="money")
        if name in SPEND_NOTES:
            m = LINDEN_AMOUNT.search(msg)
            if not m:
                return refuse(f"{name}: couldn't read the price, so not confirming it", rule="money")
            amount = int(m.group(1).replace(",", ""))
            return self._check_spend(amount, name) or Verdict(True, spend=amount)
        return None

    # -- the gate --

    def check(self, pump: str, data: dict, state: ViewerState) -> Verdict:
        op = str(data.get("op") or "")
        notes = state.notifications or {}
        if state.gate and not (pump == "LLAppViewer" and op in ("requestQuit", "forceQuit")):
            return refuse(state.gate, 409, rule="gate")
        if not state.logged_in and pump not in PRE_LOGIN_PUMPS and not (
                pump == "_command" and op in PRE_LOGIN_COMMANDS) and not (pump == "LLViewerControl" and op == "get"):
            return refuse("the viewer isn't logged in yet (its login screen is the person's)", 409, rule="login")
        if v := self._hard(pump, op, data):
            return v
        if v := self._allowlist(pump, op, data, notes):
            return v

        out = dict(data)
        if pump in ("LLURLDispatcher", "LLCommandDispatcher"):
            # as a link clicked in a web page: handlers that would act without asking ask (or refuse)
            out["trusted"] = False
        if (pump, op) == ("LLChatBar", "sendChat"):
            ch = data.get("channel")
            if ch is not None and not (isinstance(ch, int) and not isinstance(ch, bool) and -2**31 <= ch < 2**31):
                # the viewer would read "-5" or -5.5 as a number too, and the counting below wouldn't
                return refuse(f"sendChat channel: a whole number (32-bit), not {ch!r}", rule="arguments")
            if ch is not None and ch < 0:
                # the chat bar op resets a negative channel to 0, which would say it in public; the "/-N text"
                # form reaches the negative channel instead
                out["message"] = f"/{ch} {data.get('message') or ''}"
                out["channel"] = 0
        spend = 0
        if (pump, op) == ("IbashoAgent", "pay"):
            v = self._check_spend(_int(data.get("amount")), "pay")
            if v:
                return v
            spend = _int(data.get("amount"))
        elif (pump, op) == ("IbashoObjects", "buy"):
            v = self._check_spend(_int(data.get("price")), "buy")
            if v:
                return v
            spend = _int(data.get("price"))
        elif (pump, op) in UPLOAD_OPS:
            # The viewer reads `quote` its own way (a map or a list counts as false: an upload, which this check
            # would have taken for a quote), and viewers before 0.1.0.25 read a negative max_cost as no limit of
            # the request's own: so only true/false and whole L$ from 0 pass.
            quote, asked = data.get("quote"), data.get("max_cost")
            if quote is not None and not isinstance(quote, bool):
                return refuse(f"{op} quote: true or false, not {quote!r}", rule="arguments")
            if asked is not None and not (isinstance(asked, int) and not isinstance(asked, bool) and asked >= 0):
                return refuse(f"{op} max_cost: whole L$, 0 or more, not {asked!r}", rule="arguments")
            if not quote:
                room = min(self.caps.max_upload_cost, self.caps.max_payment, self._spend_room())
                out["max_cost"] = min(asked, room) if asked is not None else room
        elif pump == "LLNotifications" and op == "respond":
            v = self._notification(data, notes)
            if v and not v.ok:
                return v
            spend = v.spend if v else 0

        msgs = self._messages(pump, op, data)
        if msgs:
            if self.ledger.messages + msgs > self.caps.messages_per_day:
                return refuse(f"daily message cap reached ({self.ledger.messages}/{self.caps.messages_per_day}, "
                              "SLT day); it resets at midnight Second Life time", 429, rule="messages")
            wait = self.limiter.wait_time()
            if wait > 0:
                return refuse(f"too many messages a minute (cap {self.caps.messages_per_minute})", 429,
                              rule="messages", retry_after=round(wait, 2))
        return Verdict(True, messages=msgs, spend=spend, data=out)

    def _state(self, state: ViewerState) -> Verdict | None:
        if state.gate:
            return refuse(state.gate, 409, rule="gate")
        if not state.logged_in:
            return refuse("the viewer isn't logged in yet (its login screen is the person's)", 409, rule="login")
        return None

    def check_listen(self, pump: str, state: ViewerState) -> Verdict:
        """`/listen`: relaying another of the viewer's event pumps. None by default; the person names them
        ("_command listen PUMP")."""
        if v := self._state(state):
            return v
        if LOGIN_PUMPS.search(pump):
            return refuse("the login pumps carry the password's hash; not listening to them", rule="login")
        if self.widen.allows("_command", "listen", pump):
            return Verdict(True)
        return refuse(f"relaying {pump}: the daemon relays the pumps it listens to itself, and others only when "
                      "the person names them (a pump the viewer creates later under a fixed name stops it if "
                      "listened to early). " + how_to_allow("_command", "listen", pump), rule="allowlist")

    def check_rpc(self, method: str, params, state: ViewerState) -> Verdict:
        """`/rpc`: a call to the viewer's script editor. It reads and saves scripts in objects and can run a few
        viewer commands on objects; nothing in it messages anyone, spends, or touches files here."""
        if v := self._state(state):
            return v
        cmd = params.get("command") if method == "command.execute" and isinstance(params, dict) else None
        if method == "command.execute":
            if cmd in EDITOR_COMMANDS or self.widen.allows("rpc", method, str(cmd)):
                return Verdict(True)
            return refuse(f"command.execute {cmd!r}: not one of the editor commands passed on "
                          f"({', '.join(sorted(EDITOR_COMMANDS))}). " + how_to_allow("rpc", method, cmd),
                          rule="allowlist")
        if method in EDITOR_METHODS or self.widen.allows("rpc", method):
            return Verdict(True)
        return refuse(f"{method}: not one of the script editor's methods passed on "
                      f"({', '.join(sorted(EDITOR_METHODS))}). " + how_to_allow("rpc", method), rule="allowlist")

    def sent(self, v: Verdict):
        """Count the messages of a request that went out. (Spending is counted once it's known to have
        happened: `charged`.)"""
        for _ in range(v.messages):
            self.limiter.take()
        if v.messages:
            self.ledger.add_messages(v.messages)

    def reserve(self, amount: int):
        self.reserved += max(0, amount)

    def release(self, amount: int):
        self.reserved = max(0, self.reserved - max(0, amount))

    def charged(self, amount: int):
        """L$ that went out (a payment or purchase that went through, an upload's fee)."""
        if amount > 0:
            self.ledger.add_spend(amount)


def settable(key: str) -> bool:
    """A setting the agent may change: listed, or graphics."""
    return key in SETTINGS or bool(RENDER_SETTINGS.match(key) and not NOT_RENDER_SETTINGS.match(key))


def _timeout_check(op: str, data: dict) -> Verdict | None:
    for k in ("timeout", "limit"):
        v = data.get(k)
        if k in data and not (isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 120):
            return refuse(f"{op} {k}: a number from 0 to 120", rule="arguments")
    return None


def button_base(name: str) -> str:
    """A notification button's name without its template's suffix ("Cancel_okcancelbuttons" -> "Cancel")."""
    return TEMPLATE_SUFFIX.sub("", name)


def plain_value(v, text: bool = False) -> bool:
    """A setting's value that can't name anything: a number, true/false, or a short vector of numbers (colours,
    positions). `text`: a short line of text too."""
    if text:
        return isinstance(v, str) and len(v) <= 200 and "\n" not in v
    num = lambda x: isinstance(x, (int, float))   # bool is an int
    return num(v) or isinstance(v, list) and 1 <= len(v) <= 4 and all(num(x) and not isinstance(x, bool) for x in v)


def _named(pump: str, op: str, data: dict) -> str | None:
    """What an op names, for the person's exact entries: a setting, a menu function, a window, a notification's
    name, a pump to relay, a UI path."""
    for key in {"LLViewerControl": ("key",), "UI": ("function", "path"), "LLFloaterReg": ("name",),
                "_command": ("source", "api")}.get(pump, ()):
        if isinstance(data.get(key), str):
            return data[key]
    return None


def chat_channel(data: dict) -> int | None:
    """The channel the viewer says an `LLChatBar sendChat` on: `channel` if given, else a leading "/N" in the
    message, as the chat bar reads it; None for "//" (whichever channel was used last)."""
    try:
        ch = int(data.get("channel") or 0)
    except (TypeError, ValueError):
        ch = 0
    if ch and ch < 2147483647:   # the viewer treats the debug channel and above as 0
        return ch
    msg = str(data.get("message") or "")
    if msg.startswith("//"):
        return None
    m = CHANNEL_PREFIX.match(msg)
    return int(m.group(1)) if m else 0


def model_refs(raw: bytes, ext: str) -> list[str] | None:
    """The other files a model's bytes name, as written (URL-decoded once, file: URLs as paths, another machine's
    share as \\\\host\\share; embedded data: left out): a glTF's buffers and images, or a COLLADA file's images. Images
    count whether or not the upload includes textures, since the upload window loads them for its preview anyway.
    None if the bytes can't be read as that kind of model."""
    refs: list[str] = []
    ext = ext.lower()
    if ext in (".gltf", ".glb"):
        if ext == ".glb":   # a 12-byte header, then chunks (length, type, data), the JSON first
            if raw[:4] != b"glTF" or len(raw) < 20:
                return None
            raw = raw[20:20 + int.from_bytes(raw[12:16], "little")]
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        for kind in ("buffers", "images"):
            refs += [x["uri"] for x in doc.get(kind) or [] if isinstance(x, dict) and isinstance(x.get("uri"), str)]
    else:
        refs = collada_images(raw)
        if refs is None:
            return None
    out = []
    for r in refs:
        if r.startswith("data:"):
            continue
        r = urllib.parse.unquote(r)
        if r[:5].lower() == "file:":
            rest = r[5:]
            if rest.startswith("//"):
                host, _, rest = rest[2:].partition("/")
                if host and host.lower() != "localhost":
                    out.append(f"\\\\{host}\\{rest}")   # another machine's share
                    continue
                rest = "/" + rest
            r = rest[1:] if re.match(r"^/[A-Za-z]:", rest) else rest
        out.append(r)
    return out


def collada_images(raw: bytes) -> list[str] | None:
    """A COLLADA file's image paths: the <init_from> of each <image> (its text, or its <ref> in COLLADA 1.5), read
    by an XML parser, so CDATA, character references and odd spacing come out as the viewer's parser sees them.
    None if it isn't XML, or declares a DOCTYPE (entities could make a path out of anything)."""
    if re.search(rb"<!DOCTYPE|<!ENTITY", raw, re.I):
        return None
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    local = lambda e: e.tag.rsplit("}", 1)[-1] if isinstance(e.tag, str) else ""
    out = []
    for image in root.iter():
        if local(image) != "image":
            continue
        for init in image:
            if local(init) != "init_from":
                continue
            refs = [c for c in init if local(c) == "ref"]
            for text in ([c.text for c in refs] if refs else [init.text]):
                if text and text.strip():
                    out.append(text.strip())
    return out


def plain_ref(r: str) -> bool:
    """A model's reference the upload can follow: relative to the model's folder and not climbing out of it. (The
    viewer reads it relative to the model's folder, or to its own: either way inside.) Absolute paths aren't
    followed: the daemon hands the viewer copies of the files it checked (`stage_upload`), and a copy can't
    redirect an absolute path."""
    return bool(r) and not os.path.isabs(r) and not re.match(r"^[/\\]", r) and not REF_REFUSED.search(r) \
        and ".." not in re.split(r"[/\\]", r)


# In a reference: a drive or a stream (":"), what the viewer would decode again ("%", "&"), control characters
REF_REFUSED = re.compile(r"[:%&\x00-\x1f]")


class StageError(Exception):
    pass


def opened_path(f) -> str:
    """Where an open file really is: the system's own answer for the open file (Windows' for the handle, Linux's
    /proc link for the descriptor), after every link and junction."""
    if os.name != "nt":
        try:
            return os.readlink(f"/proc/self/fd/{f.fileno()}")
        except OSError:
            if sys.platform.startswith("linux"):
                raise
            return os.path.realpath(f.name)   # no /proc (macOS): the name, resolved after opening
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    buf = ctypes.create_unicode_buffer(32768)
    n = k32.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(f.fileno()), buf, len(buf), 0)
    if not n or n >= len(buf):
        raise OSError(ctypes.get_last_error(), "couldn't tell where an open file is")
    p = buf.value
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    return p[4:] if p.startswith("\\\\?\\") else p


def stage_upload(op: str, data: dict, folders: list, dest: Path) -> dict:
    """The upload request with its files swapped for copies in a fresh folder under `dest`, which only the daemon
    writes. Each file is opened first and then checked where Windows says the open file is, and its copy is made
    from what was read, so nothing changed after the check (a folder swapped for a junction, a model rewritten to
    name other files) reaches the viewer. A model's own references are copied beside it, as the viewer will look
    for them. Raises StageError with the reason if a file can't be read or isn't in `folders`."""
    roots = [os.path.realpath(f) for f in folders if f]
    keys = PATH_OPS[("IbashoAgent", op)][1]
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="upload-", dir=dest))
    written: dict[str, bytes] = {}

    def read(path: str) -> tuple[bytes, str, int]:
        try:
            with open(path, "rb") as f:
                real = opened_path(f)
                where = next((i for i, r in enumerate(roots) if inside(real, r)), None)
                if where is None:
                    raise StageError(f"{path} is really {real}, outside the folders uploads read from")
                return f.read(), real, where
        except (OSError, ValueError) as e:
            raise StageError(f"couldn't read {path} ({getattr(e, 'strerror', None) or e})") from None

    def write(target: Path, raw: bytes):
        if not inside(target, stage):
            raise StageError(f"{target.name}: can't place it beside its model")
        key = os.path.normcase(str(target))
        if key in written:
            if written[key] != raw:   # read twice and changed in between: not what was checked
                raise StageError(f"{target.name} changed while it was being read")
            return
        written[key] = raw
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)

    out = dict(data)
    try:
        for key in keys:
            p = data.get(key)
            if p is None:
                continue
            if not isinstance(p, str):
                raise StageError(f"{key}: one path")
            raw, real, where = read(p)
            target = stage / str(where) / os.path.relpath(real, roots[where])
            write(target, raw)
            if op == "uploadMesh":
                refs = model_refs(raw, Path(p).suffix)
                if refs is None:
                    raise StageError(f"couldn't read {p} as a model")
                for r in refs:
                    if not plain_ref(r):
                        raise StageError(f"{os.path.basename(p)} refers to {r}; a model's files are named relative "
                                         "to it, in its folder or below")
                    src = os.path.join(os.path.dirname(p), r)
                    if not os.path.lexists(src):
                        continue   # missing here, so missing beside the copy too: the viewer finds nothing
                    write(Path(os.path.normpath(target.parent / r)), read(src)[0])
            out[key] = str(target)
        if op == "uploadMesh":
            out["textures"] = bool(data.get("textures"))   # said outright: the window keeps its last choice
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return out


def prune_staged(dest: Path, older_than_s: float = 3600):
    """Uploads' copies, once the viewer is long done with them."""
    cutoff = time.time() - older_than_s
    try:
        old = [d for d in Path(dest).iterdir() if d.is_dir() and d.stat().st_mtime < cutoff]
    except OSError:
        return
    for d in old:
        shutil.rmtree(d, ignore_errors=True)


def _leaves(v) -> list:
    """What's in a value: itself, or what's in a list or a map's values (nested)."""
    if isinstance(v, list):
        return [s for x in v for s in _leaves(x)]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _leaves(x)]
    return [v]


def _keys(v) -> list:
    """A map's keys, nested (a window key like {filename: ...} names a file by its key)."""
    if isinstance(v, list):
        return [k for x in v for k in _keys(x)]
    if isinstance(v, dict):
        return list(v) + [k for x in v.values() for k in _keys(x)]
    return []


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1
