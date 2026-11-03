"""Starting and stopping: init, check, login (the person's), up, down, status, daemon, selfcheck, forget."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

from .. import config as config_mod, launch, procs, stub
from ..config import LINDEN_GRIDS, flag_check, leap_token, save_flag_check
from ..api import ApiError, Down
from .common import Ctx, Fail, is_uuid, name_of, names, out_json, position, short


def register(sub):
    p = sub.add_parser("init", help="write a config file to fill in")
    p.add_argument("--name", default="", help="the avatar's name")
    p.add_argument("--grid", default="secondlife", help="secondlife, aditi, or another grid's login URL")
    p.add_argument("--viewer", default=os.environ.get("IBASHO_VIEWER", ""),
                   help="path to the viewer's exe (default: IBASHO_VIEWER, which the container image sets)")
    p.set_defaults(fn=init)
    sub.add_parser("check", help="check the config and install").set_defaults(fn=check)
    p = sub.add_parser("login", help="for the person: open the viewer to log in once by hand (remembered after)")
    p.add_argument("--start", metavar="REGION",
                   help="log in at this region (Second Life's main grid) instead of where the avatar was last, or "
                        "its home: for a home that sends it somewhere it can't go")
    p.set_defaults(fn=login)
    p = sub.add_parser("up", help="start the daemon and the viewer, log in from the saved login, wait till in world")
    p.add_argument("--timeout", type=float, default=180)
    p.set_defaults(fn=up)
    p = sub.add_parser("down", help="log out and stop the viewer and daemon")
    p.add_argument("--keep-daemon", action="store_true")
    p.set_defaults(fn=down)
    p = sub.add_parser("status", help="daemon, viewer, login (my name and avatar id), today's counts")
    p.add_argument("--json", action="store_true", help="the daemon's status as JSON (my id: self.id)")
    p.set_defaults(fn=status)
    p = sub.add_parser("daemon", help="run the daemon in the foreground (`up` starts it in the background)")
    p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)   # `up`: log to daemon.log only
    p.set_defaults(fn=daemon)
    p = sub.add_parser("selfcheck", help="check in-world that the account is flagged as a scripted agent")
    p.add_argument("--host", help="one of your objects to run the check in (default: a temporary prim here)")
    p.add_argument("--reset", action="store_true",
                   help="for the person: forget a failed check (after flagging the account)")
    p.set_defaults(fn=selfcheck)
    p = sub.add_parser("forget", help="delete a person's words from the event log (and the viewer's transcript)")
    p.add_argument("who", help="their avatar id, or a name they've used")
    p.set_defaults(fn=forget)
    p = sub.add_parser("uninstall", help="for the person: what removing Ibasho takes (and removing the viewer's files)")
    p.add_argument("--viewer-data", action="store_true",
                   help="delete the Ibasho viewer's settings, logs, saved login and cache now")
    p.set_defaults(fn=uninstall)


# ---------- setup ----------

def init(ctx: Ctx, a):
    path = Path(a.config) if a.config else config_mod.home() / "config.toml"
    try:
        config_mod.write_template(path, a.name, a.grid, a.viewer)
    except config_mod.ConfigError as e:
        raise Fail(str(e)) from None
    print(f"wrote {path}; fill in agent.name and viewer.path, then `ibasho check`")


def check(ctx: Ctx, a):
    cfg = ctx.cfg
    print(f"config {cfg.path}: {cfg.name} on {cfg.grid}{' (Linden Lab grid)' if cfg.linden_grid else ''}")
    problems = []
    if not cfg.viewer_path or not Path(cfg.viewer_path).is_file():
        problems.append(f"viewer.path {cfg.viewer_path!r} isn't a file")
    saved = cfg.saved_login_file()
    print(f"viewer: {'Ibasho' if cfg.ibasho_viewer else 'not the Ibasho viewer'}, its folder {cfg.viewer_dirs()[0]}"
          + ("" if saved is None else ", saved login " + ("found" if saved.is_file() else
                                                           "missing (the person runs `ibasho login` once)")))
    if cfg.linden_grid and not cfg.scripted_agent_flag_confirmed:
        problems.append("grid.scripted_agent_flag_confirmed is false: flag the account "
                        "(secondlife.com/my/account/sisa.php) before logging in")
    if problem := grid_problem(cfg):
        problems.append(problem)
    flag = flag_check(cfg)
    if cfg.linden_grid and flag.get("flagged") is False:
        problems.append("the last in-world check found the account unflagged")
    notes = []
    if cfg.linden_grid and "flagged" not in flag:
        notes.append("the scripted-agent flag hasn't been checked in world yet: "
                     "`ibasho selfcheck` once in world, somewhere you can build")
    print(f"data {cfg.data_dir} (events kept {cfg.retention_days} days)")
    c = cfg.caps
    print(f"caps: L${c.max_payment}/payment, L${c.max_session_spend}/login, L${c.max_daily_spend}/day, "
          f"{c.messages_per_day} messages/day, {c.messages_per_minute}/min, maturity {c.maturity}")
    if cfg.policy_allow:
        print("policy: also passes on " + ", ".join(cfg.policy_allow) + " (docs/safety.md)")
    st = ctx.api.is_up() if cfg.token_file.exists() else None
    print("daemon " + ("up" if st else "down"))
    for n in notes:
        print("NOTE: " + n)
    for p in problems:
        print("PROBLEM: " + p)
    if problems:
        raise SystemExit(1)


def uninstall(ctx: Ctx, a):
    """Lists everything an install leaves on the machine; deletes only the viewer's own folders, on request. The
    install's home (the agent's memory among it) is never deleted from here."""
    try:
        cfg = ctx.cfg
    except Fail:
        cfg = None
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    viewer_dirs = [appdata / "Ibasho", local / "Ibasho"]
    home = cfg.home if cfg else config_mod.home()
    print("Removing Ibasho from this machine:")
    print("  1. `ibasho keeper stop` and `ibasho down` (nothing may be running). If you set tools up, "
          "`ibasho tool setup --remove`: it removes their app container, which Windows keeps in the registry and "
          "under %LOCALAPPDATA%\\Packages.")
    if cfg and cfg.viewer_path:
        folder = Path(cfg.viewer_path).parent
        kept = folder.with_name(folder.name + ".previous")
        print(f"  2. The viewer: delete its folder, {folder}"
              + (f", and {kept}, the copy an update kept" if kept.is_dir() else ""))
    else:
        print("  2. The viewer: delete the folder IbashoViewer.exe is in.")
    print("  3. The viewer's settings, logs, saved login and cache: " + " and ".join(str(d) for d in viewer_dirs)
          + " (`ibasho uninstall --viewer-data` deletes them).")
    if cfg and cfg.data_dir_setting:
        print(f"  4. The data folder (event log, pictures, keeper logs): {cfg.data_dir}")
    print(f"  {5 if cfg and cfg.data_dir_setting else 4}. This install's home, {home}: config.toml, data, the "
          "agent's life folder (its memory and tools: keep a copy if it should live on), claude/, its Claude Code "
          "folder, and tools-runtime/, the tools' Python.")
    print("  Then `pip uninstall ibasho`. Apart from the tools' app container, Ibasho writes nothing to the registry, "
          "and it adds no shortcuts or services.")
    print("  With keeper.claude_config = \"shared\", the agent's Claude Code transcripts are in your own Claude Code "
          "folder, under projects/ named after the life folder's path.")
    if not a.viewer_data:
        return
    if cfg and (viewer_pid(cfg) or (cfg.token_file.exists() and ctx.api.is_up())):
        raise Fail("the viewer or daemon is running: `ibasho down` first")
    import shutil
    for d in viewer_dirs:
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            print(("could not fully delete " if d.exists() else "deleted ") + str(d))


# ---------- the viewer ----------

def viewer_exe(cfg) -> Path:
    exe = Path(cfg.viewer_path)
    if not exe.is_file():
        raise Fail(f"viewer.path {cfg.viewer_path!r} isn't a file (config {cfg.path})")
    return exe


def viewer_pid(cfg) -> int | None:
    return procs.from_file(cfg.state_dir / "viewer.pid")


def grid_problem(cfg) -> str | None:
    try:
        return config_mod.viewer_grid_problem(cfg)
    except (config_mod.ConfigError, OSError) as e:
        return str(e)


def linden_gate(cfg):
    if not cfg.linden_grid:
        if problem := grid_problem(cfg):
            raise Fail(problem)
        return
    if not cfg.scripted_agent_flag_confirmed:
        raise Fail("not logging in to a Linden Lab grid: the account must be flagged as a scripted agent first "
                   "(secondlife.com/my/account/sisa.php), then grid.scripted_agent_flag_confirmed = true in "
                   f"{cfg.path}. That's for the person who owns the account to do.")
    if flag_check(cfg).get("flagged") is False:
        raise Fail("the in-world check found this account isn't flagged as a scripted agent. The person flags it "
                   "(secondlife.com/my/account/sisa.php); then `ibasho selfcheck --reset` and `ibasho up`.")


def login(ctx: Ctx, a):
    cfg = ctx.cfg
    linden_gate(cfg)
    if viewer_pid(cfg):
        raise Fail("the viewer is already running; `ibasho down` first")
    exe = viewer_exe(cfg)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    # No --leap: nothing but the person touches this viewer while the password is on screen. It's theirs to
    # use, so it comes to the front as a normal window would (the agent's never does: launch_viewer).
    args = ["--grid", cfg.grid_arg, "--multiple", "--set", "IbashoSpectator", "0", "--set", "IbashoNoActivate", "0",
            "--set", "RememberUser", "1", "--set", "RememberPassword", "1", *server_args(cfg, web=True),
            *start_slurl(cfg, a.start)]   # --url goes last
    try:
        cmd, cwd, extra = launch.viewer_process(exe, args)
    except launch.LaunchError as e:
        raise Fail(str(e)) from None
    pid = procs.spawn(cmd, cfg.state_dir / "viewer.log", cwd=cwd, env=procs.env_with(**extra) if extra else None)
    procs.write_pid(cfg.state_dir / "viewer.pid", pid)
    if view := login_view(pid):
        print(view)
    print("The viewer is open for the person to log in once, by hand:\n"
          "  1. Log in with the avatar's name and password, with \"Remember username\" and \"Remember password\"\n"
          "     ticked (they are by default).\n"
          "  2. Accept the Terms of Service if asked (that's yours to read and agree to, not the model's).\n"
          "  3. Once in world, quit the viewer (File > Quit or close the window).\n"
          "After that, `ibasho up` logs in from the saved login; the password never passes through Ibasho.\n"
          "If the viewer gets stuck once in world (a teleport that keeps failing, say) and won't quit, `ibasho down`\n"
          "closes it: the login is saved by then. If that was the account's home, `ibasho login --start REGION`\n"
          "logs in somewhere else.")


def stop_daemon(ctx: Ctx):
    cfg = ctx.cfg
    try:
        ctx.api.http("POST", "/shutdown", {})
    except ApiError:
        pass
    pid = procs.from_file(cfg.state_dir / "daemon.pid")
    end = time.time() + 10
    while pid and procs.alive(pid) and time.time() < end:
        time.sleep(0.3)
    if pid and procs.alive(pid):
        procs.kill_tree(pid)


def ensure_daemon(ctx: Ctx) -> dict:
    api, cfg = ctx.api, ctx.cfg
    st = None
    if cfg.token_file.exists():
        st = api.is_up()
    if st and st.get("config_mtime") != cfg.path.stat().st_mtime:
        # the config changed since the daemon read it: restart it (the viewer's stub reconnects by itself)
        print("config changed; restarting the daemon")
        stop_daemon(ctx)
        st = None
    if st:
        return st
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    pid = procs.spawn(procs.ibasho_args("--config", str(cfg.path), "daemon", "--background"),
                      cfg.state_dir / "daemon.out", env=procs.env_with(PYTHONPATH=_src_path()))
    end = time.time() + 20
    while time.time() < end:
        time.sleep(0.3)
        if cfg.token_file.exists() and (st := api.is_up()):
            return st
        if not procs.alive(pid):
            break
    raise Fail(f"the daemon didn't start; see {cfg.state_dir / 'daemon.log'} and daemon.out")


def _src_path() -> str:
    here = str(Path(__file__).resolve().parents[2])   # works from an install and from a source checkout
    return os.pathsep.join(p for p in (here, os.environ.get("PYTHONPATH", "")) if p)


def server_args(cfg, web: bool | None = None) -> list[str]:
    """The viewer's command-line settings for running without a graphics card ([server]); the rest are set over
    LEAP once it's in world, where its graphics presets can't undo them (the daemon's `apply_server_settings`).
    `web` overrides server.web (the person's login has the browser: Linden Lab's terms are a web page)."""
    s = cfg.server
    if not s.enabled:
        return []
    w, h = s.window_size()
    args = ["--set", "FullScreen", "0", "--set", "WindowWidth", str(w), "--set", "WindowHeight", str(h),
            "--set", "EnableVoiceChat", "1" if s.voice else "0"]
    if s.voice and sys.platform != "win32":
        # voice chat on a sink of its own, never the one hearing's viewer ear records (docs/hearing.md)
        from ..hearing.capture import voice_device_for_viewer
        parec = shutil.which("parec")
        if dev := voice_device_for_viewer(Path(parec) if parec else None):
            args += ["--set", "VoiceOutputAudioDevice", dev]
    if cfg.ibasho_viewer:
        web = s.web if web is None else web
        args += ["--set", "IbashoNoWebPlugin", "0" if web else "1", "--set", "IbashoMaxFPS", f"{s.max_fps:g}"]
    return args


def login_view(viewer_pid: int) -> str | None:
    """For a viewer that isn't on a screen in front of the person (a server's virtual display): opens their view of
    it for the login, and returns what they do to see it. IBASHO_LOGIN_VIEW names the program that provides the
    view (the container's is noVNC through an SSH tunnel), run as `PROGRAM start VIEWER_PID`: it prints the
    person's instructions and closes the view when that viewer exits. Unset: the viewer is on a screen of its own."""
    program = os.environ.get("IBASHO_LOGIN_VIEW")
    if not program:
        return None
    import subprocess
    try:
        r = subprocess.run([program, "start", str(viewer_pid)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Fail(f"the viewer started, but the person's view of it didn't ({program}: {e})") from None
    if r.returncode:
        raise Fail(f"the viewer started, but the person's view of it didn't: {(r.stderr or r.stdout).strip()}")
    return r.stdout.rstrip()


def launch_viewer(ctx: Ctx):
    cfg = ctx.cfg
    exe = viewer_exe(cfg)
    try:
        leap_cwd, leap = launch.leap_command(Path(procs.pythonw()), Path(stub.__file__))
        # IbashoNoActivate: the window never takes focus or comes in front of what the person is doing (the
        # viewer's default too; said here so it holds whatever the viewer's settings say)
        cmd, cwd, extra = launch.viewer_process(exe, ["--leap", leap, "--grid", cfg.grid_arg, "--autologin",
                                                      "--multiple", "--set", "IbashoNoActivate", "1",
                                                      *server_args(cfg), *start_slurl(cfg)])   # --url goes last
    except launch.LaunchError as e:
        raise Fail(str(e)) from None
    if sys.platform == "win32":
        cwd = leap_cwd   # the --leap command names Python relative to it
    env = procs.env_with(IBASHO_LEAP_PORT=cfg.leap_port, IBASHO_LEAP_TOKEN=leap_token(ctx.api.token),
                         IBASHO_STUB_LOG=cfg.state_dir / "stub.log", **extra)
    (cfg.state_dir / "viewer.log").write_bytes(b"")
    pid = procs.spawn(cmd, cfg.state_dir / "viewer.log", cwd=cwd, env=env)
    procs.write_pid(cfg.state_dir / "viewer.pid", pid)
    return pid


def login_failure(cfg) -> str | None:
    try:
        text = (cfg.state_dir / "viewer.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.findall(r"Login failed.*?getResponse\(\):\s*(.*)", text)
    return m[-1].strip()[:300] if m else None


def needs_person(ctx: Ctx, since: int) -> str | None:
    """Something during login only the person can do: new terms, a critical message, an MFA code."""
    try:
        text = (ctx.cfg.state_dir / "viewer.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if re.search(r"#LLLogin#.*:\s+ToS\s*$", text, re.M):
        return "Linden Lab's Terms of Service need accepting"
    if "handleLoginFailure Crit" in text:
        return "there's a critical message to read"
    try:
        if any(e["data"].get("name") == "PromptMFAToken" for e in ctx.api.events(since, kinds=["notify"])):
            return "the account asks for an MFA code"
    except ApiError:
        pass
    return None


def remember_place(ctx: Ctx):
    """Where the avatar is, for the next login to start there. Second Life's own "last location" can give the
    account's home instead (seen on the real grid, 2026-11-03), which may be somewhere it can't stay."""
    try:
        if not ctx.api.ibasho:
            return
        region = str(ctx.api.op("IbashoAgent", "parcel", timeout=5).get("region", {}).get("name") or "")
        pos = position(ctx)["region"]
    except (ApiError, KeyError, TypeError):
        return
    if region:
        place = {"grid": ctx.cfg.grid_arg, "region": region, "pos": [round(float(v)) for v in pos[:3]],
                 "t": time.time()}
        (ctx.cfg.state_dir / "last_place.json").write_text(json.dumps(place), encoding="utf-8")


def start_slurl(cfg, region: str | None = None) -> list[str]:
    """The viewer's start location for a login: `region` if given, else where the avatar was at its last `down`, if
    that's recent; else the viewer's own choice, which for the person's login is the account's home (a new
    account's can be an orientation island that keeps trying to send it on to a place that refuses it, leaving the
    person's viewer stuck teleporting: seen 2026-11-03). Only on Second Life's main grid: the viewer reads secondlife://REGION/X/Y/Z as a
    place there and switches its login to that grid, whatever --grid said (on a test grid it sent the login to
    Linden Lab's)."""
    if cfg.grid_arg != LINDEN_GRIDS["secondlife"]:
        if region:
            raise Fail("--start works only on Second Life's main grid")
        return []
    if region and region.strip():
        return ["--url", f"secondlife://{urllib.parse.quote(region.strip())}/128/128/30"]
    try:
        place = json.loads((cfg.state_dir / "last_place.json").read_text(encoding="utf-8"))
        if place.get("grid") != cfg.grid_arg or time.time() - float(place["t"]) > 30 * 86400:
            return []
        x, y, z = (min(255, max(0, int(v))) for v in place["pos"])
        region = urllib.parse.quote(str(place["region"]))
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return ["--url", f"secondlife://{region}/{x}/{y}/{z}"]


def quit_viewer(ctx: Ctx, wait: float = 20):
    cfg = ctx.cfg
    pid = viewer_pid(cfg)
    remember_place(ctx)
    try:
        ctx.api.post("LLAppViewer", "requestQuit")
    except ApiError:
        pass
    end = time.time() + wait
    while pid and procs.alive(pid) and time.time() < end:
        time.sleep(0.5)
    if pid and procs.alive(pid):
        procs.kill_tree(pid)
    (cfg.state_dir / "viewer.pid").unlink(missing_ok=True)


def up(ctx: Ctx, a):
    cfg = ctx.cfg
    linden_gate(cfg)
    viewer_exe(cfg)
    saved = cfg.saved_login_file()
    if saved is not None and not saved.is_file() and not viewer_pid(cfg):
        raise Fail(f"the viewer has no saved login yet ({saved} doesn't exist). The person logs in once by hand "
                   "with `ibasho login`; after that `ibasho up` works without them.")
    st = ensure_daemon(ctx)
    if st.get("gate"):
        raise Fail(st["gate"])
    if st.get("connected") and st.get("logged_in") and (why := stranded(ctx, st)):
        print(f"{why}; restarting the viewer")
        quit_viewer(ctx)
        end = time.time() + 15
        while (st := ctx.api.status(fresh=True)).get("connected") and time.time() < end:
            time.sleep(0.5)
    if not st.get("connected"):
        if viewer_pid(cfg):
            print("viewer running; waiting for its LEAP link")
        else:
            launch_viewer(ctx)
    api = ctx.api
    since = int(st.get("seq") or 0)
    end = time.time() + 90
    while not (st := api.status(fresh=True)).get("connected"):
        if not viewer_pid(cfg):
            raise Fail(f"the viewer exited before connecting; see {cfg.state_dir / 'viewer.log'}")
        if time.time() > end:
            raise Fail(f"the viewer didn't connect over LEAP; see {cfg.state_dir / 'stub.log'} and viewer.log")
        time.sleep(1)
    end, at_screen = time.time() + a.timeout, None
    while not st.get("logged_in"):
        if st.get("gate"):
            raise Fail(st["gate"])
        if st.get("login_failed"):
            reason = login_failure(cfg) or "see viewer.log"
            quit_viewer(ctx)
            raise Fail(f"login failed: {reason}. If it's the password or a new Terms of Service, the person logs in "
                       "by hand once more (`ibasho login`).")
        if why := needs_person(ctx, since):
            quit_viewer(ctx)
            raise Fail(f"logging in needs the person: {why}. They run `ibasho login` and see it through by hand "
                       "(that's theirs to agree to, not the model's); then `ibasho up` again.")
        if not st.get("connected") and not viewer_pid(cfg):
            raise Fail(f"the viewer exited during login; see {cfg.state_dir / 'viewer.log'}")
        if st.get("login_state") == "STATE_LOGIN_WAIT":
            at_screen = at_screen or time.time()
            if time.time() - at_screen > 10:
                quit_viewer(ctx)
                raise Fail("the viewer stopped at its login screen: there's no saved login for this grid (or it "
                           "needs something only the person can give: an MFA code, new terms). The person logs in "
                           "once by hand with `ibasho login`; after that `ibasho up` works without them.")
        if time.time() > end:
            raise Fail(f"not in world after {a.timeout:.0f} s (viewer at {st.get('login_state')})")
        time.sleep(1)
        st = api.status(fresh=True)
    me = st.get("self") or {}
    where = ""
    try:
        if st.get("viewer") == "ibasho":
            where = " in " + str(api.op("IbashoAgent", "parcel").get("region", {}).get("name"))
        pos = position(ctx)["region"]
        where += f" at {pos[0]:.0f},{pos[1]:.0f},{pos[2]:.0f}"
    except ApiError:
        pass
    print(f"in world as {name_of(me, cfg.name)}{where} ({st.get('viewer')} viewer)")
    from . import viewer as viewer_cmd
    if line := viewer_cmd.finish_update(cfg):
        print(line)
    if cfg.linden_grid and "flagged" not in flag_check(cfg):
        print("the scripted-agent flag hasn't been checked in-world yet: `ibasho selfcheck` somewhere you can build")
    from . import ears
    if line := ears.start_if_enabled(ctx):
        print(line)


def stranded(ctx: Ctx, st: dict) -> str | None:
    """Logged in as far as the viewer's startup goes, but not really in world: the grid dropped the login (the
    viewer then waits for someone to press OK before it quits), or it's in no region (after failed teleports)."""
    if st.get("logged_out"):
        return f"the grid logged me out ({' '.join(str(st['logged_out']).split())[:200]})"
    if st.get("viewer") != "ibasho":
        return None
    for _ in range(6):   # a region can be a moment coming, just after a login or a teleport
        try:
            if ctx.api.op("IbashoAgent", "parcel", timeout=10).get("region", {}).get("name"):
                return None
        except ApiError:
            pass
        time.sleep(2)
    return "the viewer is logged in but not in any region (cut off after failed teleports?)"


def down(ctx: Ctx, a):
    cfg = ctx.cfg
    st = ctx.api.is_up() if cfg.token_file.exists() else None
    from . import ears
    if ears.stop(ctx):
        print("hearing off")
    if st or viewer_pid(cfg):
        quit_viewer(ctx)
        print("viewer closed")
    if a.keep_daemon or not st:
        return
    stop_daemon(ctx)
    print("daemon stopped")


def status(ctx: Ctx, a):
    cfg = ctx.cfg
    st = ctx.api.is_up() if cfg.token_file.exists() else None
    if a.json:
        out_json(st or {"daemon": False})
        return
    if not st:
        print("daemon down" + (" (viewer still running)" if viewer_pid(cfg) else ""))
        return
    c = st["caps"]
    if not st["connected"]:
        line = "daemon up, no viewer"
    elif st["logged_in"]:
        me = st.get("self") or {}
        user = f" ({me['username']})" if me.get("username") and me["username"] != name_of(me) else ""
        line = (f"in world as {name_of(me, cfg.name)}{user} {me.get('id') or '(id not known yet)'} "
                f"({st['viewer']} viewer, grid {st.get('grid') or '?'})")
        if st.get("logged_out"):
            line = (f"LOGGED OUT by the grid: {' '.join(str(st['logged_out']).split())[:200]} (`ibasho up` logs "
                    f"back in). Was {line.removeprefix('in world as ')}")
    else:
        line = f"viewer connected, not logged in ({st.get('login_state') or '?'})"
    print(line)
    print(f"today: {st['messages_today']}/{c['messages_per_day']} messages, L${st['spent_today']}/"
          f"{c['max_daily_spend']} spent (this login L${st['spent_session']}/{c['max_session_spend']})")
    if st.get("gate"):
        print("BLOCKED: " + st["gate"])


def daemon(ctx: Ctx, a):
    from .. import daemon as d
    d.run(ctx.cfg, console=not a.background)


# ---------- the flag check ----------

def selfcheck(ctx: Ctx, a):
    cfg = ctx.cfg
    if a.reset:
        save_flag_check(cfg, cfg.grid_arg, None)
        print("cleared the last check; `ibasho up`, then `ibasho selfcheck` again")
        return
    from . import make
    api = ctx.api
    temp = None
    host = a.host
    if not host:
        if not api.ibasho:
            raise Fail("without the Ibasho viewer, give --host (one of your objects within reach)")
        here = position(ctx)["region"]
        try:
            r = api.op("IbashoBuild", "create", {"shape": "box", "scale": [0.2, 0.2, 0.2], "temp": True,
                                                 "phantom": True, "name": "ibasho self-check",
                                                 "pos": [here[0], here[1], here[2] + 1.5]}, timeout=20)
        except ApiError as e:
            raise Fail(f"the check needs a moment's building, and it isn't allowed here ({e}): try it in a sandbox, "
                       "or give --host (one of your objects within reach)") from None
        temp = host = r["id"]
        make.unselect_on_sim(ctx, host)
    else:
        host = make.rid(ctx, host)
    try:
        since = api.seq()
        make.save_script(ctx, host, "ibasho self-check", make.lsl("self_check.lsl"), check_contents=False, quiet=True)
        got = make.wait_owner_say(ctx, since, "IBASHO|selfcheck|", 30)
    finally:
        if temp:
            try:
                api.op("IbashoBuild", "delete", {"ids": [temp]})
            except ApiError:
                pass
    if not got:
        raise Fail("no answer from the check script within 30 s (can you build or run scripts here?)")
    _, _, flagged, info, shown = (got[0].split("|", 4) + ["", ""])[:5]
    ok = flagged == "1"
    save_flag_check(cfg, api.status().get("grid") or cfg.grid_arg,
                    {"t": time.time(), "flagged": ok, "display_name": shown})
    print(("flagged as a scripted agent" if ok else "NOT flagged as a scripted agent") + f"; display name {shown!r}")
    if not ok and cfg.linden_grid:
        quit_viewer(ctx)
        raise Fail("logged out: this account isn't flagged. The person flags it at "
                   "secondlife.com/my/account/sisa.php, then `ibasho selfcheck --reset` and `ibasho up`.")


# ---------- forgetting someone ----------

def forget(ctx: Ctx, a):
    who = a.who.strip()
    ids, ns = set(), set()
    api = ctx.api
    if is_uuid(who):
        ids.add(who.lower())
    else:
        ns.add(who)
        for e in api.events(0, kinds=["im", "chat", "im_transcript"]):
            d = e["data"]
            if str(d.get("from") or "").strip().lower() == who.lower() and d.get("from_id"):
                ids.add(str(d["from_id"]).lower())
    me = (api.status().get("self") or {}).get("id")
    ids.discard(str(me).lower())
    try:  # usernames are unique; display names aren't, so they'd catch other people
        ns |= {n["username"] for n in names(ctx, ids).values() if n.get("username")}
    except ApiError:
        pass
    r = api.http("POST", "/forget", {"ids": sorted(ids), "names": sorted(ns)})
    left = 0
    if api.ibasho and ids:   # the viewer keeps open conversations in memory (`im-log` reads them): close theirs
        try:
            for s in api.op("IbashoIM", "sessions").get("sessions", []):
                if s.get("type") == "p2p" and str(s.get("other_id") or "").lower() in ids:
                    api.op("IbashoIM", "leave", {"session_id": s["session_id"]})
                    left += 1
        except ApiError:
            pass
    label = ", ".join(sorted(ns)) or who
    print(f"forgot {label}{' (' + ', '.join(short(i) for i in sorted(ids)) + ')' if ids else ''}: "
          f"{r['events_removed']} events, {r['transcripts_removed']} viewer transcripts"
          + (f", {left} open conversation{'s' if left != 1 else ''} closed" if left else ""))
    if not ids and not is_uuid(who):
        print("(no avatar id found for that name; only lines under exactly that name went)")
