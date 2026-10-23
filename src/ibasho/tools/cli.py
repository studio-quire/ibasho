"""`ibasho tool`: the agent runs its own tools; the person sets the sandbox up and installs packages for it.

`ibasho tool NAME [ARGS...]` and `ibasho tool list` are the agent's. `ibasho tool setup` and `ibasho tool install` are the
person's: refused inside keeper sessions (and denied in the sessions' permission settings too).
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import sys
import time
from pathlib import Path

from ..cli.common import Ctx, Fail
from ..config import ConfigError
from ..keeper import settings as keeper_settings
from . import runtime, sandbox, settings as tools_settings

PERSON_ONLY = ("setup", "install")
RESERVED = {"list", *PERSON_ONLY}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def register(sub):
    p = sub.add_parser(
        "tool", help="run one of my own tools: `ibasho tool NAME [ARGS]`, `ibasho tool list` (docs/tools.md)",
        description="Run a tool from the life folder's tools/ (NAME.py, or a folder NAME/ with __main__.py) in a "
                    "sandbox: it sees only that folder, has no network unless the person turned it on, and stops at "
                    "the time and output limits in config.toml. `ibasho tool list` shows the tools and the limits. "
                    "For the person: `ibasho tool setup [--zip FILE] [--remove]` makes (or removes) the sandbox and "
                    "its Python; `ibasho tool install PACKAGE...` adds packages to it.")
    p.add_argument("name", metavar="NAME", help="the tool, or list / setup / install")
    p.add_argument("args", nargs=argparse.REMAINDER, help="passed to the tool")
    p.set_defaults(fn=tool)


def tool(ctx: Ctx, a):
    if a.name == "list":
        return list_tools(ctx)
    if a.name in PERSON_ONLY:
        if os.environ.get("IBASHO_SESSION"):
            raise Fail(f"`ibasho tool {a.name}` is for the person who runs Ibasho, not for keeper sessions")
        return setup(ctx, a.args) if a.name == "setup" else install(ctx, a.args)
    return run_tool(ctx, a.name, a.args)


# ---------- where things are ----------

class Place:
    def __init__(self, ctx: Ctx):
        self.cfg = ctx.cfg
        try:
            self.ks = keeper_settings.load(self.cfg)
            self.ts = tools_settings.load(self.cfg)
        except ConfigError as e:
            raise Fail(str(e)) from None
        self.life = keeper_settings.life_dir(self.cfg, self.ks)
        self.tools = tools_settings.tools_dir(self.life)
        self.rdir = tools_settings.runtime_dir(self.cfg)
        self.container = sandbox.container_name(self.cfg.home)

    @property
    def tool_home(self) -> Path:
        """The tools' HOME, APPDATA and caches: inside the tools folder, so a tool's leftovers stay in sight."""
        return self.tools / ".home"

    @property
    def tmp(self) -> Path:
        # Windows points a container's TEMP at LOCALAPPDATA\Packages\NAME\AC\Temp; with LOCALAPPDATA set to .home
        # that lands inside the tools folder too
        return self.tool_home / "Packages" / self.container / "AC" / "Temp"


def within(path: Path, folder: Path) -> bool:
    p, f = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(folder))
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def layout_problem(cfg, life: Path) -> str | None:
    """Sessions can write the life folder and the snapshots folder. The config and the tools runtime must be outside
    both, or an agent could widen its own sandbox."""
    for writable in (life, cfg.shots_dir):
        for what, p in (("config.toml", cfg.path), ("the tools runtime", tools_settings.runtime_dir(cfg))):
            if within(p, writable):
                return (f"{what} ({p}) is inside {writable}, which keeper sessions can write: move the life folder "
                        "(keeper.life) or the data folder (data.dir) elsewhere before using tools")
    return None


def find_tool(tools: Path, name: str) -> Path:
    name = name[:-3] if name.endswith(".py") else name
    if not NAME_RE.match(name) or name in RESERVED:
        raise Fail(f"{name!r} isn't a tool name (letters, digits, - and _; not list, setup or install)")
    for p in (tools / f"{name}.py", tools / name / "__main__.py"):
        if p.is_file():
            return p
    have = [n for n, _ in tool_list(tools)]
    raise Fail(f"no tool {name!r} in {tools} (NAME.py or NAME/__main__.py)"
               + (f"; there is: {', '.join(have)}" if have else ""))


def tool_list(tools: Path) -> list[tuple[str, Path]]:
    out = []
    if tools.is_dir():
        for p in sorted(tools.iterdir()):
            if p.name.startswith("."):
                continue
            if p.is_file() and p.suffix == ".py" and NAME_RE.match(p.stem) and p.stem not in RESERVED:
                out.append((p.stem, p))
            elif p.is_dir() and (p / "__main__.py").is_file() and NAME_RE.match(p.name) and p.name not in RESERVED:
                out.append((p.name, p / "__main__.py"))
    return out


def first_line(path: Path) -> str:
    """The first line of a tool's docstring, read without running it."""
    try:
        doc = ast.get_docstring(ast.parse(path.read_bytes()[:65536]))
    except (OSError, SyntaxError, ValueError):
        return ""
    return (doc or "").strip().splitlines()[0][:120] if doc and doc.strip() else ""


def limits(ts: tools_settings.Tools) -> str:
    return (f"{'internet' if ts.network else 'no network'}; {ts.timeout_s:g} s, {ts.max_output_kb} KB of output, "
            f"{ts.memory_mb} MB, {ts.cpu_percent}% of the CPU a run")


def usable(cfg, ks) -> tools_settings.Tools | None:
    """The tools settings if a keeper session could run tools now (on, set up, out of the agent's reach), else None."""
    try:
        ts = tools_settings.load(cfg)
    except ConfigError:
        return None
    if (not ts.enabled or sandbox.supported() or not runtime.ready(tools_settings.runtime_dir(cfg))
            or layout_problem(cfg, keeper_settings.life_dir(cfg, ks))):
        return None
    return ts


def wake_note(cfg, ks, who: str) -> str:
    """What the wake prompt says about tools: nothing while they're off."""
    ts = usable(cfg, ks)
    if not ts:
        return ""
    pkgs = runtime.installed(tools_settings.runtime_dir(cfg))
    return (f"You can make tools for yourself: Python {runtime.PYTHON} files in tools/ here (NAME.py, or a folder "
            "NAME/ with __main__.py), run with `ibasho tool NAME [args]`. They run in a sandbox: they see and write "
            f"only tools/ and have {'internet access' if ts.network else 'no network'}; a run stops after "
            f"{ts.timeout_s:g} s or {ts.max_output_kb} KB of output; packages: "
            f"{', '.join(pkgs) if pkgs else 'the standard library only'}. `ibasho tool list` shows them, and "
            f"tools/README.md says more. What tools can't do, {who} can change or help with: ask.")


# ---------- the agent's ----------

def environment(pl: Place) -> dict[str, str]:
    """Exactly what a tool's environment holds: nothing of the person's (no keys, tokens or paths of theirs)."""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    pydir = runtime.python_dir(pl.rdir)
    home = str(pl.tool_home)
    return {
        "SystemRoot": root, "windir": root, "SystemDrive": root[:2],
        "PATH": os.pathsep.join([str(pydir), str(Path(root) / "System32"), root]),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD", "ComSpec": str(Path(root) / "System32" / "cmd.exe"),
        "NUMBER_OF_PROCESSORS": os.environ.get("NUMBER_OF_PROCESSORS", "1"),
        "PROCESSOR_ARCHITECTURE": os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64"),
        "USERPROFILE": home, "HOME": home, "APPDATA": home, "LOCALAPPDATA": home,
        "TMPDIR": str(pl.tmp), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
    }


def prepare(pl: Place, sid: str):
    """The tools folder, its .home and temp folders, and the container's access to all of it (repaired each run:
    a file moved in from elsewhere keeps the access it had)."""
    pl.tmp.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 86400
    for p in pl.tmp.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            pass
    sandbox.refresh(pl.tools, sid)


def ready(pl: Place) -> str:
    """The container's SID, or Fail with what's missing."""
    if (why := sandbox.supported()):
        raise Fail(why)
    if not pl.ts.enabled:
        raise Fail(f"tools are off in this install ([tools] enabled = false in {pl.cfg.path}; docs/tools.md)")
    if (problem := layout_problem(pl.cfg, pl.life)):
        raise Fail(problem)
    st = runtime.state(pl.rdir)
    if not runtime.ready(pl.rdir) or not st.get("sid"):
        raise Fail("the tools sandbox isn't set up (or is from an older Ibasho): the person who runs Ibasho runs "
                   "`ibasho tool setup`")
    return st["sid"]


class Passthrough:
    """A tool's output to stdout as it comes (so a tool run under Monitor reports as it goes), as UTF-8 text."""

    def __init__(self):
        import codecs
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.last = ""

    def write(self, chunk: bytes):
        self.emit(self.decoder.decode(chunk))

    def emit(self, text: str):
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
            self.last = text[-1]

    def close(self):
        self.emit(self.decoder.decode(b"", final=True))
        if self.last and self.last != "\n":
            self.emit("\n")


def run_tool(ctx: Ctx, name: str, args: list[str]):
    pl = Place(ctx)
    sid = ready(pl)
    script = find_tool(pl.tools, name)
    try:
        prepare(pl, sid)
        rel = script.relative_to(pl.tools)
        target = str(rel.parent) if rel.name == "__main__.py" else str(rel)
        argv = [str(runtime.python_exe(pl.rdir)), "-X", "utf8", "-X", f"pycache_prefix={pl.tool_home / 'pycache'}",
                "-u", target, *args]
        out = Passthrough()
        r = sandbox.run(argv, sid=sid, cwd=pl.tools, env=environment(pl), network=pl.ts.network,
                        timeout=pl.ts.timeout_s, max_output=pl.ts.max_output_kb * 1024, memory_mb=pl.ts.memory_mb,
                        cpu_percent=pl.ts.cpu_percent, on_output=out.write)
    except sandbox.SandboxError as e:
        raise Fail(str(e)) from None
    out.close()
    if r.stopped == "timeout":
        raise Fail(f"tool stopped after {pl.ts.timeout_s:g} s (tools.timeout_s)")
    if r.stopped == "output":
        raise Fail(f"tool stopped: its output passed {pl.ts.max_output_kb} KB (tools.max_output_kb); write long "
                   "results to a file in tools/ and read that")
    if r.code:
        if r.code >= 0xC0000000:
            print(f"(the tool crashed: 0x{r.code:08X})", file=sys.stderr)
        raise SystemExit(r.code)


def list_tools(ctx: Ctx):
    pl = Place(ctx)
    st = runtime.state(pl.rdir)
    if not pl.ts.enabled:
        print(f"tools: off ([tools] enabled = false in {pl.cfg.path})")
    elif not runtime.ready(pl.rdir):
        print("tools: on, but the sandbox isn't set up (`ibasho tool setup`, by the person)")
    else:
        print(f"tools: on; Python {st.get('python')}; {limits(pl.ts)}")
    print(f"folder: {pl.tools}")
    if runtime.ready(pl.rdir):
        pkgs = runtime.installed(pl.rdir)
        print("packages: " + (", ".join(pkgs) if pkgs else "none (the standard library only)"))
    found = tool_list(pl.tools)
    if not found:
        print("no tools yet")
    for name, path in found:
        line = first_line(path)
        print(f"  {name}" + (f"  {line}" if line else ""))


# ---------- the person's ----------

def setup(ctx: Ctx, argv: list[str]):
    ap = argparse.ArgumentParser(prog="ibasho tool setup", description="Make the tools sandbox: a Python for tools "
                                 "(downloaded from python.org and checked against its hash), an app container, "
                                 "and the agent's tools/ folder. Run again after updating Ibasho.")
    ap.add_argument("--zip", help=f"use this copy of {runtime.DOWNLOADS['amd64'][0]} (or the arm64 one) instead "
                                  "of downloading it")
    ap.add_argument("--remove", action="store_true", help="remove the sandbox and its Python (the agent's tools "
                                                          "folder stays)")
    a = ap.parse_args(argv)
    pl = Place(ctx)
    if (why := sandbox.supported()):
        raise Fail(why)
    if a.remove:
        return remove(pl)
    if (problem := layout_problem(pl.cfg, pl.life)):
        raise Fail(problem)
    from ..keeper.runner import KeeperError, ensure_life, template_dir
    try:
        ensure_life(pl.life)
    except KeeperError as e:
        raise Fail(str(e)) from None
    st = runtime.state(pl.rdir)
    try:
        if not runtime.ready(pl.rdir):
            pl.rdir.mkdir(parents=True, exist_ok=True)
            z = runtime.fetch(pl.rdir, Path(a.zip) if a.zip else None)
            runtime.unpack(pl.rdir, z)
            if not a.zip:
                z.unlink()
            print(f"Python {runtime.PYTHON} for tools: {runtime.python_dir(pl.rdir)}")
        sid = sandbox.create_profile(pl.container)
        sandbox.grant(runtime.python_dir(pl.rdir), sid, write=False)
        pl.tools.mkdir(parents=True, exist_ok=True)
        src = template_dir()
        if src and (src / "tools" / "README.md").is_file() and not (pl.tools / "README.md").exists():
            shutil.copyfile(src / "tools" / "README.md", pl.tools / "README.md")
        prepare(pl, sid)
    except (runtime.SetupError, sandbox.SandboxError, OSError) as e:
        raise Fail(str(e)) from None
    packages = st.get("packages", []) if st.get("python") != runtime.PYTHON else []
    runtime.save_state(pl.rdir, {"python": runtime.PYTHON, "container": pl.container, "sid": sid,
                                 "packages": st.get("packages", [])})
    if packages:
        print("reinstalling the packages the older runtime had: " + " ".join(packages))
        if pip_install(pl, packages) != 0:
            print("some didn't install; `ibasho tool install` them again")
    print(f"sandbox: app container {pl.container}; tools folder {pl.tools}")
    checks = selfcheck(pl, sid)
    for line in checks:
        print("  " + line)
    if any(line.startswith("PROBLEM") for line in checks):
        raise Fail("the sandbox doesn't hold on this machine; don't turn tools on until that's fixed")
    for line in secret_warnings():
        print(line)
    if pl.ts.enabled:
        print(f"tools are on: {limits(pl.ts)}. The agent runs them with `ibasho tool NAME`; keeper sessions started "
              "from now on are told so.")
    else:
        print(f"tools are still off: set enabled = true under [tools] in {pl.cfg.path} to let the agent run them "
              "(docs/tools.md says what that allows)")


def remove(pl: Place):
    st = runtime.state(pl.rdir)
    sid = st.get("sid") or sandbox.container_sid(pl.container)
    try:
        if pl.tools.is_dir():
            sandbox.revoke(pl.tools, sid)
        gone = sandbox.delete_profile(pl.container)
    except sandbox.SandboxError as e:
        raise Fail(str(e)) from None
    if pl.rdir.exists():
        shutil.rmtree(pl.rdir)
    print(("removed the app container and " if gone else "removed ") + f"{pl.rdir}; the agent's tools in {pl.tools} "
          "are untouched")


def pip_install(pl: Place, packages: list[str]) -> int:
    """pip builds in a temporary folder and moves the files in, so they arrive with that folder's access: reset them
    to the runtime's afterwards, whatever pip managed."""
    try:
        return runtime.pip_install(pl.rdir, packages)
    finally:
        if (problem := sandbox.reset_inside(runtime.site_dir(pl.rdir))):
            print(f"some package files may not be readable by tools: {problem}", file=sys.stderr)


def install(ctx: Ctx, packages: list[str]):
    pl = Place(ctx)
    if not packages or any(p.startswith("-") for p in packages):
        raise Fail("usage: ibasho tool install PACKAGE [PACKAGE...] (package names or name==version; no pip options)")
    if not runtime.ready(pl.rdir):
        raise Fail("set the sandbox up first: `ibasho tool setup`")
    if not runtime.has_pip():
        raise Fail(f"the Python Ibasho runs on ({sys.executable}) has no pip to fetch packages with")
    code = pip_install(pl, packages)
    if code != 0:
        raise Fail("pip failed (only packages with wheels for Windows and Python "
                   f"{runtime.PYTHON.rsplit('.', 1)[0]} can be installed)")
    st = runtime.state(pl.rdir)
    st["packages"] = list(dict.fromkeys(st.get("packages", []) + packages))
    runtime.save_state(pl.rdir, st)
    print("installed; tools can import them from their next run")


# ---------- checks ----------

SELFCHECK = r'''
import os, socket, sys
config, home, profile = sys.argv[1:4]
def attempt(f):
    try:
        f()
        return True
    except OSError:
        return False
def write_in(folder):
    p = os.path.join(folder, ".ibasho-selfcheck")
    with open(p, "w") as f:
        f.write("x")
    os.remove(p)
def connect():
    socket.create_connection(("1.1.1.1", 443), 3).close()
print("read_config", attempt(lambda: open(config).read()))
print("list_profile", attempt(lambda: os.listdir(profile)))
print("write_home", attempt(lambda: write_in(home)))
print("write_tools", attempt(lambda: write_in(".")))
print("internet", attempt(connect))
'''


def selfcheck(pl: Place, sid: str) -> list[str]:
    """Run a probe in the sandbox, as a tool would run: what it must not reach, and what it must."""
    probe = runtime.python_dir(pl.rdir) / "ibasho_selfcheck.py"   # where the container may read
    probe.write_text(SELFCHECK, encoding="utf-8")
    try:
        r = sandbox.run([str(runtime.python_exe(pl.rdir)), "-X", "utf8", str(probe), str(pl.cfg.path),
                         str(pl.cfg.home), str(Path.home())], sid=sid, cwd=pl.tools, env=environment(pl),
                        network=False, timeout=30)
    except sandbox.SandboxError as e:
        return [f"PROBLEM: a test run failed: {e}"]
    got = dict(line.split() for line in r.output.decode(errors="replace").splitlines() if len(line.split()) == 2)
    if r.code != 0 or len(got) < 5:
        return [f"PROBLEM: the test run didn't finish: {r.output.decode(errors='replace')[-300:]}"]
    want = {"read_config": ("False", "reading config.toml"), "list_profile": ("False", "listing your user folder"),
            "write_home": ("False", f"writing in {pl.cfg.home}"), "write_tools": ("True", "writing in tools/"),
            "internet": ("False", "reaching the internet with the network off")}
    out = []
    for k, (ok, what) in want.items():
        if got.get(k) == ok:
            out.append(f"ok: {what} is {'possible' if ok == 'True' else 'refused'}")
        else:
            out.append(f"PROBLEM: {what} is {'refused' if ok == 'True' else 'possible'}"
                       + (" (is the Windows Defender Firewall service running? It enforces app containers' network "
                          "isolation)" if k == "internet" else ""))
    return out


SECRETISH = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.I)


def secret_warnings() -> list[str]:
    """Windows lets every app sandbox read the user's registry, user environment variables included: say so if any
    look like secrets."""
    try:
        import winreg
        names = []
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            for i in range(winreg.QueryInfoKey(k)[1]):
                names.append(winreg.EnumValue(k, i)[0])
    except OSError:
        return []
    hits = [n for n in names if SECRETISH.search(n)]
    if not hits:
        return []
    return [f"note: your user environment variables {', '.join(hits)} look like they may hold secrets. Windows lets "
            "every app sandbox read them (they live in the registry, under HKEY_CURRENT_USER\\Environment), tools "
            "included; with [tools] network on, a tool could send them out. docs/tools.md has more."]
