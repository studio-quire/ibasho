"""The real keeper: Claude Code sessions, the daemon, the log, one keeper per install.

Each session is `claude -p` in stream-json mode (see session.py), run in the agent's life folder with:
- its own Claude Code config folder by default (keeper.claude_config = "separate"), so none of the person's own
  Claude Code settings, permissions, MCP servers, plugins, hooks or memory reach a session that reads strangers'
  words all day; the person logs that folder in once with `ibasho keeper login`. CLAUDE_CONFIG_DIR moves
  `.claude.json` (user MCP servers, per-folder state) into that folder too, not just settings and the login
  (checked with Claude Code 2.1: `claude auth status` reports it as configDirectory and writes `.claude.json` there);
- that folder's login and nothing else: API keys and other provider settings in the environment are dropped
  (keeper.auth = "env" keeps them), since `claude -p` would otherwise bill a stray ANTHROPIC_API_KEY;
- only user-level settings from that folder plus the session settings file (never project settings from the life
  folder, which the agent can edit: it could otherwise widen its own permissions, and project skills, which could
  pre-approve tools, load only with project settings);
- exactly the MCP servers of an Ibasho-owned config, which has none (`--strict-mcp-config`, so an `.mcp.json` the
  agent writes is ignored); auto-compaction off (the keeper hands off instead); permission prompts answered "no"
  by themselves, since nobody is there to answer them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import time
from importlib import resources
from pathlib import Path

import psutil

from .. import config as config_mod
from ..api import Api, ApiError
from . import prompts, settings as settings_mod
from .core import Keeper
from .session import AUTH_ENV, ClaudeSession, auth_status, clean_env, find_claude
from .settings import KeeperSettings
from .store import Store


class KeeperError(Exception):
    pass


# ---------- the life folder ----------

def template_dir() -> Path | None:
    """The life-template folder: beside the source tree, or where `pip install` put it."""
    here = Path(__file__).resolve()
    for p in (here.parents[3] / "life-template", Path(sys.prefix) / "share" / "ibasho" / "life-template"):
        if (p / "README.md").is_file():
            return p
    return None


def ensure_life(life: Path) -> bool:
    """Create the life folder from the template if it doesn't exist or is empty. True if it was created. The
    template's tools/ is left out: `ibasho tool setup` adds it when the person turns tools on."""
    if life.exists() and any(life.iterdir()):
        return False
    src = template_dir()
    if not src:
        raise KeeperError("can't find the life-template folder (reinstall Ibasho from its source folder)")
    life.mkdir(parents=True, exist_ok=True)
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if rel.parts[0] == "tools":
            continue
        if p.is_dir():
            (life / rel).mkdir(parents=True, exist_ok=True)
        elif p.name != ".keep":
            (life / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, life / rel)
    return True


# ---------- Claude Code ----------

def claude_env(cfg, ks: KeeperSettings) -> dict:
    """The environment for every `claude` the keeper runs (sessions, `auth status`, `auth login`)."""
    env = clean_env(os.environ, keep_auth=ks.auth == "env")
    cdir = settings_mod.claude_config_dir(cfg, ks)
    if cdir:
        cdir.mkdir(parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = str(cdir)
    return env


def session_settings(cfg, ks: KeeperSettings, kdir: Path) -> Path:
    """The settings file sessions run with: keeper.settings if set, else the shipped template.

    With the agent's own config folder, Claude Code's transcripts there age out with the install's retention
    (`cleanupPeriodDays`, in the folder's own settings.json and in the session settings). With "shared" they're
    left alone: the cleanup sweeps the whole config folder, so it would cut the person's own history too."""
    cdir = settings_mod.claude_config_dir(cfg, ks)
    if cdir:
        set_retention(cdir / "settings.json", cfg.retention_days)
    if ks.settings:
        p = Path(ks.settings).expanduser()
        if not p.is_file():
            raise KeeperError(f"keeper.settings {ks.settings!r} isn't a file")
        return p
    data = json.loads(resources.files("ibasho.keeper").joinpath("session-settings.json").read_text(encoding="utf-8"))
    if cdir:
        data["cleanupPeriodDays"] = cfg.retention_days
    if not tools_on(cfg, ks):
        # `ibasho tool` refuses by itself while tools are off; this makes the refusal Claude Code's too
        data["permissions"]["deny"] += ["Bash(ibasho tool *)", "Bash(ibasho tool)"]
    out = kdir / "session-settings.json"
    kdir.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return out


def set_retention(path: Path, days: int):
    """cleanupPeriodDays in the agent's config folder's own settings, leaving anything else there as it is. A file
    that isn't a JSON object is the person's business: left alone (the session settings still carry it)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or data.get("cleanupPeriodDays") == days:
        return
    data["cleanupPeriodDays"] = days
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def tools_on(cfg, ks: KeeperSettings) -> bool:
    from ..tools.cli import usable
    return usable(cfg, ks) is not None


def mcp_config(kdir: Path) -> Path:
    """Ibasho's own MCP config for sessions: no servers. With --strict-mcp-config it's the only one read."""
    out = kdir / "mcp-servers.json"
    kdir.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    return out


def ibasho_bin_dir() -> Path:
    """Where the `ibasho` command lives (beside this Python), so sessions find the same install."""
    return Path(sys.executable).parent


def hearing_note(cfg) -> str:
    """What the wake prompt says about hearing: nothing while it's off."""
    from ..hearing import settings as hearing_settings
    try:
        if not hearing_settings.load(cfg).enabled:
            return ""
    except Exception:
        return ""
    return ("Hearing is on: speech near you comes through the watch as `heard: ...` lines, turned into words by "
            "Whisper on this machine. Words are all it gives: music, songs' tunes and other sounds don't come through "
            "(lyrics sometimes do, as words), it doesn't know who spoke, and it often mishears names.")


def make_launcher(cfg, ks: KeeperSettings, kdir: Path, life: Path, claude: str):
    sessions = kdir / "sessions"

    def launch(sid: str) -> ClaudeSession:
        sessions.mkdir(parents=True, exist_ok=True)
        settings = session_settings(cfg, ks, kdir)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        args = [claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--session-id", sid, "--permission-mode", ks.permission_mode, "--permission-prompts", "none",
                "--settings", str(settings), "--setting-sources", "user",
                "--strict-mcp-config", "--mcp-config", str(mcp_config(kdir)),
                "--add-dir", str(cfg.shots_dir), "-n", f"{cfg.name} {stamp}"]
        if ks.model:
            args += ["--model", ks.model]
        if ks.effort:
            args += ["--effort", ks.effort]
        env = claude_env(cfg, ks)
        env.update(DISABLE_AUTO_COMPACT="1", IBASHO_SESSION=sid, IBASHO_CONFIG=str(cfg.path))
        config_mod.bind_session(sid, cfg.path)
        env["PATH"] = str(ibasho_bin_dir()) + os.pathsep + env.get("PATH", "")
        cfg.shots_dir.mkdir(parents=True, exist_ok=True)
        return ClaudeSession(args, life, env, sessions / f"{stamp}_{sid[:8]}.log",
                             sessions / f"{stamp}_{sid[:8]}.stderr", sid)

    return launch


# ---------- the daemon ----------

class DaemonWorld:
    def __init__(self, cfg):
        self.cfg = cfg

    def post(self, text: str) -> bool:
        try:
            Api(self.cfg).post_event("keeper", {"text": text, "wake": True})
            return True
        except ApiError:
            return False

    def logged_in(self) -> bool:
        try:
            return bool((Api(self.cfg).is_up() or {}).get("logged_in"))
        except ApiError:
            return False

    def down(self):
        from ..cli import lifecycle
        from ..cli.common import Ctx
        ctx = Ctx(argparse.Namespace(config=str(self.cfg.path)))
        lifecycle.down(ctx, argparse.Namespace(keep_daemon=False))


# ---------- one keeper per install ----------

class Lock:
    """An OS file lock: released when the process dies, however it dies."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(path, "a+")

    def acquire(self) -> bool:
        try:
            if sys.platform == "win32":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


def kill_orphan(store: Store, log) -> None:
    """A session left by a keeper that died can't be steered any more (its stdin went with the keeper), and it may
    still be driving the avatar. End it before starting another, so two minds never share one body."""
    s = (store.status() or {}).get("session") or {}
    pid, created = s.get("pid"), s.get("create_time")
    if not pid or not created:
        return
    try:
        p = psutil.Process(pid)
        if abs(p.create_time() - created) > 2:
            return
        tree = [p] + p.children(recursive=True)
    except psutil.Error:
        return
    log(f"ending session {str(s.get('id'))[:8]} left running by a keeper that stopped (pid {pid})")
    for q in tree:
        try:
            q.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(tree, timeout=5)


LOG_TIME = "%Y-%m-%d %H:%M:%S"


def prune_logs(kdir: Path, days: int, store: Store | None = None):
    """The keeper's records age out with the install's retention: session logs, keeper.log's lines (which include
    the agent's notes) and notes that have been delivered or read."""
    cutoff = time.time() - days * 86400
    for p in (kdir / "sessions").glob("*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass
    prune_keeper_log(kdir / "keeper.log", cutoff)
    try:
        (store or Store(kdir)).prune_notes(cutoff)
    except (OSError, TimeoutError):
        pass   # next hour


def prune_keeper_log(path: Path, cutoff: float):
    """Keep keeper.log from its first line written after `cutoff`. Only the keeper writes it, from the thread that
    prunes, so rewriting it can't lose a line."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return
    start = len(lines)
    for i, line in enumerate(lines):
        try:
            t = dt.datetime.strptime(line[:19], LOG_TIME).timestamp()
        except ValueError:
            continue   # a line from an older format, or not a line of its own: goes with what's before it
        if t >= cutoff:
            start = i
            break
    if start == 0:
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text("".join(lines[start:]), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)


def make_logger(kdir: Path, echo: bool):
    kdir.mkdir(parents=True, exist_ok=True)

    def log(msg: str):
        line = f"{dt.datetime.now():{LOG_TIME}} {msg}"
        with open(kdir / "keeper.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if echo and sys.stdout:
            print(line, flush=True)

    return log


def preflight(cfg, ks: KeeperSettings) -> str:
    claude = find_claude(ks.claude)
    if not claude:
        raise KeeperError("can't find Claude Code (`claude`); install it, or set keeper.claude to its path")
    st = auth_status(claude, claude_env(cfg, ks))
    if not st.get("loggedIn"):
        where = ("the agent's own Claude Code folder isn't logged in: run `ibasho keeper login`"
                 if ks.claude_config == "separate" else "Claude Code isn't logged in: run `claude` and log in")
        if ks.auth == "login" and any(AUTH_ENV.match(k) for k in os.environ):
            where += ' (an API key in your environment isn\'t passed to sessions unless keeper.auth = "env")'
        raise KeeperError(where + (f" ({st['error']})" if st.get("error") else ""))
    return claude


def run(cfg, echo: bool = True, extra_prompt: str = "") -> int:
    ks = settings_mod.load(cfg)
    kdir = settings_mod.keeper_dir(cfg)
    store = Store(kdir)
    log = make_logger(kdir, echo)
    lock = Lock(kdir / "keeper.lock")
    if not lock.acquire():
        raise KeeperError("a keeper is already running for this install (`ibasho keeper status`)")
    if sys.stdout is None:  # pythonw: nowhere to print
        sys.stdout = sys.stderr = open(kdir / "keeper.out", "a", buffering=1, encoding="utf-8")
    kill_orphan(store, log)
    store.clear_stop()
    claude = preflight(cfg, ks)
    life = settings_mod.life_dir(cfg, ks)
    if ensure_life(life):
        log(f"made the life folder {life} from the template")
    prune_logs(kdir, cfg.retention_days, store)
    from ..tools import settings as tools_settings
    from ..tools.cli import wake_note
    tools_settings.load(cfg)   # a mistake in [tools] shows now, rather than tools quietly counting as off
    keeper = Keeper(ks=ks, store=store, launch=make_launcher(cfg, ks, kdir, life, claude), world=DaemonWorld(cfg),
                    agent_name=cfg.name, grid=cfg.grid, life=str(life),
                    handoff_exists=lambda: (life / ks.handoff_note).is_file(), log=log, extra_prompt=extra_prompt,
                    tools_note=lambda: "\n\n".join(x for x in (wake_note(cfg, ks, prompts.person_label(ks.person)),
                                                               hearing_note(cfg)) if x))
    log(f"keeper up (pid {os.getpid()}); life folder {life}")
    last_prune = time.time()
    try:
        while not keeper.exiting:
            try:
                keeper.tick()
            except Exception as e:
                log(f"tick failed: {e!r}")
            if time.time() - last_prune > 3600:
                prune_logs(kdir, cfg.retention_days, store)
                last_prune = time.time()
            time.sleep(Keeper.TICK_S)
    except KeyboardInterrupt:
        log("interrupted")
    finally:
        keeper.shutdown()
        log("keeper stopped")
    return 0

