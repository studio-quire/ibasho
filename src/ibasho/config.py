"""The install's one config file, and the paths that follow from it.

An install is one avatar: one config, one data dir, one daemon. Everything lives under IBASHO_HOME (default
~/Ibasho): config.toml there, data in IBASHO_HOME/data unless the config says otherwise.
"""

from __future__ import annotations

import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

LINDEN_GRIDS = {
    # name in config -> what the viewer's --grid takes
    "secondlife": "util.agni.lindenlab.com",
    "agni": "util.agni.lindenlab.com",
    "aditi": "util.aditi.lindenlab.com",
}

MATURITY = {"general": 13, "moderate": 21, "adult": 42}  # the viewer's PreferredMaturity values


class ConfigError(Exception):
    pass


def home() -> Path:
    return Path(os.environ.get("IBASHO_HOME") or Path.home() / "Ibasho")


def grid_key(grid: str) -> str:
    g = (grid or "").strip().lower()
    return LINDEN_GRIDS.get(g, g)


def is_linden_grid(grid: str) -> bool:
    """Any grid run by Linden Lab, where its Scripted Agent Policy applies (the beta grid too)."""
    g = (grid or "").strip().lower()
    return g in LINDEN_GRIDS or "lindenlab.com" in g or "secondlife.com" in g or g in ("agni", "aditi")


@dataclass
class Caps:
    max_payment: int = 100            # L$ per payment or purchase
    max_session_spend: int = 500      # L$ per login
    max_daily_spend: int = 1000       # L$ per SLT day, across logins
    max_upload_cost: int = 50         # L$ per upload
    messages_per_day: int = 3000      # Linden Lab allows 5,000 per owner per SLT day; stay well under
    messages_per_minute: int = 20
    maturity: str = "moderate"        # general | moderate | adult


@dataclass
class Config:
    path: Path
    name: str = ""
    nicknames: list[str] = field(default_factory=list)
    grid: str = "secondlife"
    login_uri: str = ""
    scripted_agent_flag_confirmed: bool = False
    viewer_path: str = ""
    viewer_user_dir: str = ""
    viewer_chat_logs: bool = False
    leap_port: int = 9263
    http_port: int = 9270
    editor_port: int = 9220
    data_dir_setting: str = ""
    retention_days: int = 7
    probe_channel: int = 0
    caps: Caps = field(default_factory=Caps)
    policy_allow: list[str] = field(default_factory=list)   # the person's additions to the daemon's allowlist

    # ---- derived ----

    @property
    def home(self) -> Path:
        return self.path.parent

    @property
    def data_dir(self) -> Path:
        return self.home / self.data_dir_setting if self.data_dir_setting else self.home / "data"   # absolute wins

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def events_dir(self) -> Path:
        return self.data_dir / "events"

    @property
    def shots_dir(self) -> Path:
        return self.data_dir / "shots"

    @property
    def token_file(self) -> Path:
        return self.state_dir / "token"

    @property
    def linden_grid(self) -> bool:
        return is_linden_grid(self.grid)

    @property
    def grid_arg(self) -> str:
        return LINDEN_GRIDS.get(self.grid.lower(), self.grid)

    @property
    def maturity_value(self) -> int:
        return MATURITY[self.caps.maturity]

    def name_words(self) -> list[str]:
        """Words that mean me in chat: the parts of my name and my nicknames."""
        words = [w for w in re.split(r"[\s._]+", self.name) if w and w.lower() != "resident"]
        return [w for w in dict.fromkeys(words + list(self.nicknames)) if len(w) >= 2]

    @property
    def ibasho_viewer(self) -> bool:
        """The configured viewer is an Ibasho build (by its exe's name; an empty path means the Ibasho viewer)."""
        return not self.viewer_path or Path(self.viewer_path).name.lower() == "ibashoviewer.exe"

    def viewer_dirs(self) -> list[Path]:
        """Where the viewer keeps per-user files (settings, saved login, per-account transcripts): the configured
        dir, else the Ibasho viewer's own %APPDATA%\\Ibasho, or %APPDATA%\\SecondLife for Linden Lab's viewer."""
        if self.viewer_user_dir:
            return [Path(self.viewer_user_dir)]
        appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return [appdata / ("Ibasho" if self.ibasho_viewer else "SecondLife")]

    def account_dirs(self, *names: str) -> list[Path]:
        """The viewer's folders for this avatar's account only (its settings and transcripts), never another
        account's that the same viewer has logged in: named after agent.name, and any `names` (the username the
        grid reported), as the viewer names them."""
        folders = {f for n in (self.name, *names) if (f := account_folder(n))}
        return [d / f for d in self.viewer_dirs() for f in sorted(folders)]

    def grids_file(self) -> Path | None:
        """The viewer's list of extra grids (`user_settings/grids.xml`), where we know the viewer's folder."""
        if not (self.viewer_user_dir or self.ibasho_viewer):
            return None
        return self.viewer_dirs()[0] / "user_settings" / "grids.xml"

    def saved_login_file(self) -> Path | None:
        """The viewer's remembered login (`user_settings/bin_conf.dat`), where we know the viewer's folder: the
        Ibasho viewer's, or a configured one. Other viewers keep theirs in folders of their own."""
        if not (self.viewer_user_dir or self.ibasho_viewer):
            return None
        return self.viewer_dirs()[0] / "user_settings" / "bin_conf.dat"


def account_folder(login_name: str) -> str:
    """The folder name a Linden Lab-based viewer gives an account under its per-user folder: the login name split
    at its first space, dot or underscore into first and last name ("Resident" if there's no second part),
    lowercased and joined with "_" ("Jo Example" and "jo.example" -> jo_example, "jo" -> jo_resident)."""
    name = (login_name or "").strip()
    if not name or re.search(r"[\x00-\x1f]", name):
        return ""
    first, sep, last = name.partition(next((c for c in name if c in " ._"), "\0"))
    last = last.strip() if sep else "Resident"
    if not first or not last:
        return ""
    if " " in last:   # three words or more: the viewer logs in with the whole name as an account name
        folder = name.lower().replace(" ", "_")
    else:
        folder = f"{first}_{last}".lower()
    # a name a grid reported could hold anything; only a plain folder name is one of the viewer's account folders
    return "" if re.search(r"[\\/:]|^\.+$|[\x00-\x1f]", folder) or ".." in folder else folder


def flag_checks(cfg: "Config") -> dict:
    """The in-world scripted-agent checks, by grid: {grid: {t, flagged, display_name}}."""
    import json
    try:
        d = json.loads((cfg.state_dir / "flag_check.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def flag_check(cfg: "Config", grid: str | None = None) -> dict:
    return flag_checks(cfg).get(grid_key(grid if grid is not None else cfg.grid)) or {}


def save_flag_check(cfg: "Config", grid: str, result: dict | None):
    import json
    d = flag_checks(cfg)
    if result is None:
        d.pop(grid_key(grid), None)
    else:
        d[grid_key(grid)] = result
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "flag_check.json").write_text(json.dumps(d))


def _read_grids(path: Path) -> dict:
    import llsd
    try:
        d = llsd.parse_xml(path.read_bytes())
        return d if isinstance(d, dict) else {}
    except OSError:
        return {}
    except Exception as e:   # llsd raises its own parse errors
        raise ConfigError(f"{path} isn't readable LLSD XML ({e}); fix or delete it") from None


def viewer_grid_problem(cfg: "Config") -> str | None:
    """None if the viewer will log in to the configured grid, else why not. Given a grid name it doesn't know, the
    viewer quietly falls back to the main Second Life grid, so any other grid must be in its grids.xml; with
    grid.login_uri set, Ibasho adds it there (keeping whatever else the file lists)."""
    if cfg.linden_grid:
        return None
    path = cfg.grids_file()
    if path is None:
        return None    # another viewer keeps its grids elsewhere; the daemon still checks the grid it lands on
    grids = _read_grids(path)
    key = cfg.grid.lower()

    def known(entry) -> bool:
        return isinstance(entry, dict) and key in {str(entry.get(k) or "").lower()
                                                     for k in ("keyname", "label", "grid_login_id")}
    entry = grids.get(key) if key in grids else next((e for e in grids.values() if known(e)), None)
    if not cfg.login_uri:
        if entry is not None:
            return None
        return (f"the viewer doesn't know the grid {cfg.grid!r} (it isn't in {path}) and would go to the main "
                "Second Life grid instead: set grid.login_uri in the config to the grid's login URL")
    uris = entry.get("login_uri") if isinstance(entry, dict) else None
    if isinstance(uris, list) and cfg.login_uri in uris or uris == cfg.login_uri:
        return None
    base = re.match(r"https?://[^/]+/", cfg.login_uri + "/").group(0)
    grids[key] = {"keyname": key, "label": cfg.grid, "grid_login_id": cfg.grid, "login_uri": [cfg.login_uri],
                  "helper_uri": base, "login_page": base, "web_profile_url": base,
                  "login_identifier_types": ["agent", "account"]}
    import llsd
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(llsd.format_pretty_xml(grids) if hasattr(llsd, "format_pretty_xml") else llsd.format_xml(grids))
    return None


def leap_token(token: str) -> str:
    """The stub's proof, derived from the install token, so the viewer's environment (inherited by everything it
    starts) never holds the token that drives the HTTP API."""
    import hashlib
    import hmac
    return hmac.new(token.encode(), b"ibasho leap stub", hashlib.sha256).hexdigest()


def _get(d: dict, key: str, kind, default):
    v = d.get(key, default)
    if kind is int and isinstance(v, bool) or not isinstance(v, kind):
        raise ConfigError(f"{key}: expected {kind.__name__}, got {v!r}")
    return v


def config_path(path: Path | str | None = None) -> Path:
    """The config file `load` reads: `path`, else IBASHO_CONFIG, else config.toml in IBASHO_HOME. Absolute, since the
    paths that follow from it go to the viewer, whose working folder isn't ours."""
    return Path(os.path.abspath(path or os.environ.get("IBASHO_CONFIG") or home() / "config.toml"))


def sessions_dir() -> Path:
    """Which config each agent session runs with (`session_config_problem`): per Windows user, outside every
    install, beside the Ibasho viewer's cache."""
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Ibasho" / "sessions"


def bind_session(sid: str, path: Path | str):
    """Record that the agent session `sid` (IBASHO_SESSION) runs with this config. The keeper does it as it starts
    a session; otherwise the session's first `ibasho` does."""
    import hashlib
    d = sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 30 * 86400
    for old in d.iterdir():
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    (d / hashlib.sha256(sid.encode()).hexdigest()[:32]).write_text(str(Path(path)), encoding="utf-8")


def session_config_problem(sid: str, path: Path | str) -> str | None:
    """None if the agent session `sid` runs with this config (binding it on first use), else why not: a session
    keeps the config it started with, so IBASHO_CONFIG or IBASHO_HOME changed inside it don't bring in another
    one with other caps. (An agent that can also change IBASHO_SESSION, or unset it, gets round this: where the
    harness can keep it from setting environment variables, it should.)"""
    import hashlib
    f = sessions_dir() / hashlib.sha256(sid.encode()).hexdigest()[:32]
    try:
        bound = f.read_text(encoding="utf-8").strip()
    except OSError:
        bound = ""
    if not bound:
        try:
            bind_session(sid, path)
        except OSError:
            pass
        return None
    if os.path.normcase(os.path.abspath(bound)) != os.path.normcase(os.path.abspath(path)):
        return (f"this session runs with the config {bound}; another (here {path}) is the person's to choose, "
                "not the agent's (IBASHO_CONFIG and IBASHO_HOME stay as the session started)")
    try:
        os.utime(f)   # in use: the 30-day clearing-out leaves it alone
    except OSError:
        pass
    return None


def inside(path: Path | str, *folders: Path | str) -> bool:
    """`path` is one of `folders` or in one, after following links and `..` (it needn't exist yet)."""
    try:
        p = os.path.normcase(os.path.realpath(path))
    except (ValueError, OSError):   # a NUL in it, say: no such path anywhere
        return False
    for f in folders:
        f = os.path.normcase(os.path.realpath(f)).rstrip("\\/")
        if p == f or p.startswith(f + os.sep):
            return True
    return False


def load(path: Path | None = None) -> Config:
    path = config_path(path)
    if not path.exists():
        raise ConfigError(f"no config at {path}; `ibasho init` writes one")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    c = Config(path=path)
    agent, grid, viewer = raw.get("agent", {}), raw.get("grid", {}), raw.get("viewer", {})
    ports, data, caps, probe = raw.get("ports", {}), raw.get("data", {}), raw.get("caps", {}), raw.get("probe", {})
    c.name = _get(agent, "name", str, "").strip()
    c.nicknames = [str(n) for n in _get(agent, "nicknames", list, [])]
    c.grid = _get(grid, "name", str, c.grid).strip()
    c.login_uri = _get(grid, "login_uri", str, "").strip()
    c.scripted_agent_flag_confirmed = _get(grid, "scripted_agent_flag_confirmed", bool, False)
    c.viewer_path = _get(viewer, "path", str, "")
    c.viewer_user_dir = _get(viewer, "user_dir", str, "")
    c.viewer_chat_logs = _get(viewer, "chat_logs", bool, False)
    c.leap_port = _get(ports, "leap", int, c.leap_port)
    c.http_port = _get(ports, "http", int, c.http_port)
    c.editor_port = _get(ports, "editor", int, c.editor_port)
    c.data_dir_setting = _get(data, "dir", str, "")
    c.retention_days = _get(data, "retention_days", int, c.retention_days)
    c.probe_channel = _get(probe, "attachment_channel", int, 0)
    pol = raw.get("policy", {})
    if not isinstance(pol, dict) or set(pol) - {"allow"}:
        raise ConfigError("[policy] takes one key, allow (a list of requests to pass on, docs/safety.md)")
    c.policy_allow = [" ".join(str(e).split()) for e in _get(pol, "allow", list, [])]
    defaults = Caps()
    for k in vars(defaults):
        setattr(c.caps, k, _get(caps, k, type(getattr(defaults, k)), getattr(defaults, k)))
    validate(c)
    return c


def validate(c: Config):
    if not c.name:
        raise ConfigError("agent.name is empty: set it to the avatar's name")
    if c.caps.maturity not in MATURITY:
        raise ConfigError(f"caps.maturity must be one of {', '.join(MATURITY)}")
    if c.caps.messages_per_day > 4500:
        raise ConfigError("caps.messages_per_day over 4500: Linden Lab's limit is 5,000 a day for all of an "
                          "owner's agents together, so keep a margin")
    if c.login_uri and c.linden_grid:
        raise ConfigError("grid.login_uri is for other grids; Linden Lab's grids are built into the viewer")
    if ":" in c.grid and not c.linden_grid:
        # the viewer reads a grid's name back out of place links (SLURLs) as a host name, port dropped, so a
        # name like host:port makes every teleport by region name fail with "on a different grid"
        raise ConfigError(f"grid.name {c.grid!r}: no ':' in a grid's name (the viewer's teleports would fail); "
                          "e.g. " + c.grid.replace(":", "-"))
    if c.login_uri and not re.match(r"https?://[^/\s]+", c.login_uri):
        raise ConfigError("grid.login_uri must be an http(s) URL, e.g. http://127.0.0.1:9000/")
    if len({c.leap_port, c.http_port, c.editor_port}) < 3:
        raise ConfigError("ports.leap, ports.http and ports.editor must differ")
    if c.retention_days < 1:
        raise ConfigError("data.retention_days must be at least 1")
    for k in ("max_payment", "max_session_spend", "max_daily_spend", "max_upload_cost", "messages_per_minute"):
        if getattr(c.caps, k) < 0:
            raise ConfigError(f"caps.{k} can't be negative")
    for e in c.policy_allow:
        if not ALLOW_ENTRY.match(e):
            raise ConfigError(f"policy.allow: {e!r} isn't \"PUMP\", \"PUMP OP\" or \"PUMP OP NAME\" (OP may be *, "
                              "then without a NAME), e.g. \"LLViewerControl set RenderGlow\"")


# "PUMP", "PUMP OP" or "PUMP OP NAME": a pump's name, an op or *, and a name (a setting, a window...) after an op
ALLOW_ENTRY = re.compile(r"^[A-Za-z_][\w.]*(?: (?:\*|[A-Za-z_][\w.]*(?: \S+)?))?$")


TEMPLATE = """\
# Ibasho: one avatar per install. Edit, then `ibasho check`.

[agent]
# The avatar's name as you log in with it ("Firstname Lastname", or a username).
name = "{name}"
# Other words people may call it by; a mention wakes the watch.
nicknames = []

[grid]
# secondlife (the main grid), aditi (the beta grid), or a name for another grid (e.g. "127.0.0.1-9000"; no ':').
name = "{grid}"
# Other grids only: the grid's login URL (e.g. "http://127.0.0.1:9000/"). Ibasho adds the grid to the viewer's
# grids.xml with it; a viewer given a grid it doesn't know would log in to the main Second Life grid instead.
login_uri = "{login_uri}"
# Linden Lab requires every account an AI drives to be flagged as a scripted agent
# (https://secondlife.com/my/account/sisa.php). Flag it, set this to true, then run `ibasho selfcheck` in-world.
scripted_agent_flag_confirmed = false

[viewer]
# The viewer's exe: the Ibasho viewer's IbashoViewer.exe (Linden Lab's own viewer works too, with fewer abilities).
path = '{viewer}'
# The viewer's per-user folder (settings, saved login, chat logs); empty = %APPDATA%\\Ibasho for the Ibasho viewer,
# %APPDATA%\\SecondLife for Linden Lab's. Set it for any other viewer.
user_dir = ''
# Keep the viewer's own chat and IM transcripts on disk (they're kept forever unless you delete them).
chat_logs = false

[ports]
leap = 9263
http = 9270
editor = 9220

[data]
dir = ''            # empty = a "data" folder beside this file (a relative path is from this file's folder)
retention_days = 7  # days the event log, pictures, keeper logs and notes, and session transcripts are kept

[caps]
max_payment = 100         # L$ per payment or purchase
max_session_spend = 500   # L$ per login
max_daily_spend = 1000    # L$ per day (Second Life time)
max_upload_cost = 50      # L$ per upload
messages_per_day = 3000   # local chat lines, IMs, offers and gives (chat to scripts doesn't count); Linden Lab's
                          # limit is 5,000 for all your agents together. The Ibasho viewer is set to the same cap.
messages_per_minute = 20
maturity = "moderate"     # general | moderate | adult

[policy]
# The daemon passes on to the viewer only the requests Ibasho uses (docs/api.md, "The safety rules"); anything
# else is refused, `ibasho raw` included. To let more through, list it here: "PUMP OP" for one op whatever it
# names, "PUMP OP NAME" for one setting, window, menu function or notification, e.g.
#   allow = ["LLViewerControl set RenderGlowStrength", "LLNotifications respond GotoURL"]
# Each entry is a way around the defaults that a stranger could talk the agent into using: windows
# (LLFloaterReg), menu functions (UI call) and keys and clicks (LLWindow) reach files, settings and web pages on
# this machine. Read docs/safety.md first. The agent can't change this file.
allow = []

[probe]
# Only for viewers without IbashoAgent probeGround: the channel a worn probe attachment listens on
# (lsl/probe_attachment.lsl). 0 = none.
attachment_channel = 0

[hearing]
# Hearing: the viewer's sound (and only the viewer's) transcribed on this machine by Whisper into `heard`
# events. Off by default: it needs `pip install ".[hearing]"` (in the Ibasho folder), proccap.exe
# (audio\\build.bat), a model download (~500 MB for small.en; `ibasho hear setup`) and a steady share of the CPU.
# What it hears is other people talking: it's kept only in the event log, for retention_days below
# (`ibasho hear forget` clears it sooner), and the model reads it. Read docs/hearing.md before turning it on.
enabled = false
model = "small.en"        # tiny.en / base.en are lighter and miss more; medium.en is better and slower
threads = 0               # Whisper's CPU threads; 0 = half the cores, at most 4
retention_days = 2        # heard speech is deleted after this many days (shorter than the rest of the event log)
follow_music = false      # also transcribe the parcel's music stream (DJs talk over it): keeps Whisper busy
                          # all the time while a stream plays; wakes only on the name
mute_viewer_music = true  # the viewer's own music to 0 while hearing, so songs don't keep Whisper busy
claps = true              # claps and knocks as rhythm events
heard_as = []             # how Whisper tends to mishear the avatar's name, e.g. ["Avery", "Ivory"]
# Voice chat: opt-in, off while empty. Second Life voice isn't part of the viewer's own sound, so hearing it
# means playing it on an output device nothing else uses and recording that whole device: set this to a
# spare device's name or id from `ibasho hear devices` (a virtual audio cable, an unused output). Devices that
# are a Windows default output are refused, since recording one would capture everything this machine plays
# (your own calls included). Recording people's voices has legal and community-standards sides: see
# docs/hearing.md first.
voice_device = ""

[keeper]
# The keeper (`ibasho keeper`) runs the agent's Claude Code sessions for you: docs/keeper.md.
person = ""               # what the agent's wake prompt and notes call you; empty = "the person who runs Ibasho for you"
situation = ""            # if this isn't an open-ended life (a trial, a test, a run with an end), say so plainly:
                          # what it's for, how long it lasts, what happens to the agent and its memory after.
                          # Shown at the start of every wake. The agent deserves to know what's really happening.
life = ''                 # the agent's memory folder; empty = a "life" folder beside this file (a relative
                          # path is from this file's folder too)
# Caps, because tokens cost money (0 = no cap). Raise them as you like.
hours_per_day = 4         # awake time per day, all sessions together
sessions_per_day = 8      # sessions started per day; handoffs and restarts count
usd_per_day = 0           # API-key logins: stop for the day past this estimated spend
max_plan_use = 0.8        # subscriptions: stop at this share of the plan's 5-hour or weekly limit, which your own
                          # use of Claude shares (1 = no cap)
allowed_hours = ""        # when the agent may be up, e.g. "17:00-23:30"; empty = any time
model = ""                # empty = Claude Code's default
claude_config = "separate"  # the agent's own Claude Code folder (`ibasho keeper login` once); "shared" = yours,
                            # for developing Ibasho only: it loads your own Claude Code setup (docs/keeper.md)
auth = "login"            # sessions use that folder's login only; "env" also passes on ANTHROPIC_API_KEY and other
                          # provider settings from your environment (then they bill that key)

[tools]
# Tools: Python programs the agent writes for itself in its life folder's tools/ and runs with `ibasho tool NAME`,
# in a Windows sandbox that sees only that folder. Off by default: read docs/tools.md (what's contained and what
# isn't), run `ibasho tool setup`, then set enabled = true. Only you turn this on; the agent can't.
enabled = false
network = false           # let tools reach the internet (and, depending on how Windows sees your network, the
                          # local one); they never reach Ibasho or other programs on this machine
timeout_s = 100           # one run's limit, in seconds
max_output_kb = 64        # a run that prints more is stopped (all of it goes into the model's context)
memory_mb = 1024
cpu_percent = 50          # of the whole machine
"""


def set_viewer_path(path: Path, exe: Path | str, check_only: bool = False):
    """Point [viewer] path in a config file at `exe`, leaving the rest of the file (comments too) as it is. The
    result is checked as a config first (ConfigError if it wouldn't load); `check_only` stops there."""
    text = path.read_text(encoding="utf-8")
    value = f"'{exe}'" if "'" not in str(exe) else '"' + str(exe).replace("\\", "\\\\").replace('"', '\\"') + '"'
    section = re.search(r"(?ms)^[ \t]*\[[ \t]*viewer[ \t]*\][ \t]*(?:#[^\n]*)?$(.*?)(?=^[ \t]*\[|\Z)", text)
    if section is None:
        text = text.rstrip("\n") + f"\n\n[viewer]\npath = {value}\n"
    else:
        body = section.group(1)
        line = re.compile(r"""(?m)^[ \t]*["']?path["']?[ \t]*=.*$""")
        body = line.sub(lambda m: f"path = {value}", body, count=1) if line.search(body) else \
            f"\npath = {value}" + body
        text = text[:section.start(1)] + body + text[section.end(1):]
    new = path.with_name(path.name + ".new")
    new.write_text(text, encoding="utf-8")
    try:
        if Path(load(new).viewer_path) != Path(exe):
            raise ConfigError(f"couldn't set viewer.path in {path}; set it to {exe} by hand")
    except ConfigError:
        new.unlink(missing_ok=True)
        raise
    if check_only:
        new.unlink(missing_ok=True)
        return
    os.replace(new, path)


def write_template(path: Path, name: str = "", grid: str = "secondlife", viewer: str = "") -> Path:
    if path.exists():
        raise ConfigError(f"{path} exists; edit it instead")
    login_uri = ""
    if m := re.match(r"https?://([^/\s:]+)(?::(\d+))?", grid.strip()):   # a login URL: name the grid after it
        login_uri, grid = grid.strip(), m.group(1).lower() + (f"-{m.group(2)}" if m.group(2) else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.format(name=name.replace('"', ""), grid=grid.replace('"', ""), viewer=viewer.replace("'", ""),
                                    login_uri=login_uri.replace('"', "")), encoding="utf-8")
    return path
