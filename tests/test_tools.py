"""`ibasho tool`: the settings and the keeper's side everywhere; on Windows, the sandbox itself, run for real.

The sandbox tests need the tools runtime: the pinned python.org zip, from IBASHO_TEST_PYTHON_ZIP if set, else
downloaded once into pytest's cache (they're skipped when neither works).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from ibasho import config as config_mod
from ibasho.cli import main
from ibasho.keeper import prompts, runner
from ibasho.keeper.settings import load as load_keeper
from ibasho.tools import cli as tools_cli, runtime, sandbox, settings as tools_settings

CONFIG = """
[agent]
name = "Test Avatar"
[grid]
name = "localhost-9000"
[tools]
{tools}
"""


def write(home: Path, **tools) -> Path:
    lines = "\n".join(f"{k} = {json.dumps(v)}" for k, v in tools.items())
    p = home / "config.toml"
    p.write_text(CONFIG.format(tools=lines), encoding="utf-8")
    return p


def run(cfgpath, capsys, *args) -> tuple[int, str, str]:
    try:
        code = main(["--config", str(cfgpath), *args])
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def plain(home, monkeypatch):
    monkeypatch.delenv("IBASHO_SESSION", raising=False)
    return home


# ---------- settings, names, layout ----------

def test_settings_default_off_and_checked(plain):
    cfg = config_mod.load(write(plain))
    t = tools_settings.load(cfg)
    assert not t.enabled and not t.network and t.timeout_s == 100
    for bad in ({"enabeld": True}, {"network": "yes"}, {"timeout_s": 0}, {"cpu_percent": 101}):
        with pytest.raises(config_mod.ConfigError):
            tools_settings.load(config_mod.load(write(plain, **bad)))


def test_the_config_template_has_tools_off(tmp_path):
    p = config_mod.write_template(tmp_path / "config.toml", name="Some One")
    assert tools_settings.load(config_mod.load(p)).enabled is False


def test_tool_names_and_listing(tmp_path):
    tools = tmp_path / "tools"
    (tools / "grow").mkdir(parents=True)
    (tools / "grow" / "__main__.py").write_text("")
    (tools / "measure.py").write_text('"""Measures things.\n\nMore."""\n')
    (tools / "setup.py").write_text("")             # reserved: never runnable as a tool
    (tools / ".home").mkdir()
    assert [n for n, _ in tools_cli.tool_list(tools)] == ["grow", "measure"]
    assert tools_cli.find_tool(tools, "measure.py") == tools / "measure.py"
    assert tools_cli.find_tool(tools, "grow") == tools / "grow" / "__main__.py"
    assert tools_cli.first_line(tools / "measure.py") == "Measures things."
    for bad in ("../x", "a/b", "setup", "install", "list", ".home", ""):
        with pytest.raises(Exception):
            tools_cli.find_tool(tools, bad)


def test_layout_keeps_config_and_runtime_out_of_the_agents_reach(plain):
    cfg = config_mod.load(write(plain))
    assert tools_cli.layout_problem(cfg, plain / "life") is None
    assert "config.toml" in tools_cli.layout_problem(cfg, plain)          # life folder = the install's home
    assert tools_cli.layout_problem(cfg, plain.parent) is not None


def test_tools_off_is_refused(plain, capsys):
    code, _, err = run(write(plain), capsys, "tool", "anything")
    assert code == 1 and ("tools are off" in err or "Windows" in err)


def test_setup_and_install_are_the_persons(plain, capsys, monkeypatch):
    p = write(plain, enabled=True)
    monkeypatch.setenv("IBASHO_SESSION", "s1")
    for args in (("setup",), ("setup", "--remove"), ("install", "numpy")):
        code, _, err = run(p, capsys, "tool", *args)
        assert code == 1 and "refused in the agent's sessions" in err


# ---------- the keeper's side ----------

def test_session_settings_deny_tools_unless_usable(plain, tmp_path, monkeypatch):
    cfg = config_mod.load(write(plain, enabled=True))
    ks = load_keeper(cfg)
    deny = json.loads(runner.session_settings(cfg, ks, tmp_path).read_text())["permissions"]["deny"]
    assert "Bash(ibasho tool *)" in deny          # on in the config, but not set up: still denied
    assert "Bash(ibasho tool setup *)" in deny and "Bash(ibasho tool install *)" in deny
    monkeypatch.setattr(tools_cli.runtime, "ready", lambda rdir: True)
    monkeypatch.setattr(tools_cli.sandbox, "supported", lambda: None)
    deny = json.loads(runner.session_settings(cfg, ks, tmp_path).read_text())["permissions"]["deny"]
    assert "Bash(ibasho tool *)" not in deny
    assert "Bash(ibasho tool setup *)" in deny and "Bash(ibasho tool install *)" in deny


def test_wake_prompt_mentions_tools_only_when_usable(plain, monkeypatch):
    cfg = config_mod.load(write(plain, enabled=True, network=False))
    ks = load_keeper(cfg)
    assert tools_cli.wake_note(cfg, ks, "Sam") == ""
    monkeypatch.setattr(tools_cli.runtime, "ready", lambda rdir: True)
    monkeypatch.setattr(tools_cli.sandbox, "supported", lambda: None)
    note = tools_cli.wake_note(cfg, ks, "Sam")
    assert "ibasho tool NAME" in note and "no network" in note and "Sam can change" in note
    text = prompts.wake(name="A", grid="g", person="Sam", reason="r", session="s" * 8, life="L",
                        handoff_note="now.md", has_handoff=False, allowance=None, notes=[], tools=note)
    assert note in text
    assert "ibasho tool" not in prompts.wake(name="A", grid="g", person="Sam", reason="r", session="s" * 8, life="L",
                                            handoff_note="now.md", has_handoff=False, allowance=None, notes=[])
    write(plain, enabled=False)
    assert tools_cli.wake_note(config_mod.load(plain / "config.toml"), ks, "Sam") == ""


def test_new_life_folders_have_no_tools_folder(tmp_path):
    assert runner.ensure_life(tmp_path / "life")
    assert not (tmp_path / "life" / "tools").exists()


# ---------- the sandbox, for real ----------

def runtime_zip(pytestconfig) -> Path | None:
    if os.environ.get("IBASHO_TEST_PYTHON_ZIP"):
        return Path(os.environ["IBASHO_TEST_PYTHON_ZIP"])
    name, sha = runtime.DOWNLOADS.get(runtime.machine(), (None, None))
    if not name:
        return None
    cache = Path(pytestconfig.cache.mkdir("ibasho-tools-runtime")) / name
    if not cache.is_file():
        try:
            with urllib.request.urlopen(runtime.URL.format(version=runtime.PYTHON, file=name), timeout=60) as r:
                cache.write_bytes(r.read())
        except OSError:
            return None
    return cache


class Box:
    """An install with tools set up, shared by the sandbox tests (setting up takes a few seconds)."""

    def __init__(self, home: Path):
        self.home = home
        self.tools = home / "life" / "tools"
        self.settings = {"enabled": True, "timeout_s": 20}

    @property
    def cfgpath(self) -> Path:
        return self.home / "config.toml"

    def configure(self, **kv):
        write(self.home, **{**self.settings, **kv})

    def tool(self, name: str, code: str):
        (self.tools / f"{name}.py").write_text(code, encoding="utf-8")

    def run(self, capsys, name, *args, **settings):
        self.configure(**settings)
        try:
            return run(self.cfgpath, capsys, "tool", name, *args)
        finally:
            self.configure()


@pytest.fixture(scope="module")
def box(tmp_path_factory, pytestconfig):
    if sandbox.supported():
        pytest.skip(sandbox.supported())
    z = runtime_zip(pytestconfig)
    if not z:
        pytest.skip("no tools runtime zip (offline? set IBASHO_TEST_PYTHON_ZIP)")
    saved = os.environ.pop("IBASHO_SESSION", None)
    b = Box(tmp_path_factory.mktemp("toolhome"))
    b.configure()
    code = main(["--config", str(b.cfgpath), "tool", "setup", "--zip", str(z)])
    assert code == 0
    yield b
    assert main(["--config", str(b.cfgpath), "tool", "setup", "--remove"]) == 0
    assert sandbox.profile_folder(sandbox.container_name(b.home)) is None
    if saved is not None:
        os.environ["IBASHO_SESSION"] = saved


def attempts(*pairs: tuple[str, str]) -> str:
    """A tool that tries each (label, expression) and prints `label ok` or `label refused`."""
    body = "".join(f"try:\n    {expr}\n    print({label!r}, 'ok')\nexcept OSError:\n    print({label!r}, 'refused')\n"
                   for label, expr in pairs)
    return "import os, socket, sys\n" + body


def results(out: str) -> dict[str, str]:
    return dict(line.split()[:2] for line in out.splitlines() if len(line.split()) >= 2)


def test_output_args_cwd_exit_code_and_environment(box, capsys):
    os.environ["IBASHO_TEST_SECRET_ENV"] = "not for tools"
    try:
        box.tool("echo", "import os, sys\nprint('args', sys.argv[1:])\nprint('cwd', os.getcwd())\n"
                         "print('env', sorted(os.environ))\nsys.stderr.write('to stderr\\n')\nsys.exit(7)\n")
        code, out, _ = box.run(capsys, "echo", "one", "two words")
    finally:
        del os.environ["IBASHO_TEST_SECRET_ENV"]
    assert code == 7
    assert "args ['one', 'two words']" in out and "to stderr" in out
    assert f"cwd {box.tools}" in out
    env = out.split("env ", 1)[1].splitlines()[0]
    assert "IBASHO_TEST_SECRET_ENV" not in env and "PATH" in env


def test_cant_read_the_persons_files(box, capsys):
    secret = Path.home() / "AppData" / "Roaming" / f"ibasho-test-secret-{uuid.uuid4().hex[:8]}.txt"
    secret.write_text("hunter2")
    try:
        assert secret.read_text() == "hunter2"          # the person can
        box.tool("peek", attempts(("appdata", f"open({str(secret)!r}).read()"),
                                  ("profile", f"os.listdir({str(Path.home())!r})"),
                                  ("config", f"open({str(box.cfgpath)!r}).read()"),
                                  ("life", f"os.listdir({str(box.home / 'life')!r})"),
                                  ("runtime", "open(sys.executable, 'rb').read(2)")))
        code, out, _ = box.run(capsys, "peek")
    finally:
        secret.unlink()
    assert code == 0, out
    assert results(out) == {"appdata": "refused", "profile": "refused", "config": "refused", "life": "refused",
                            "runtime": "ok"}


def test_writes_only_in_its_folder(box, capsys):
    import tempfile
    profile = sandbox.profile_folder(sandbox.container_name(box.home))
    targets = {"tools": box.tools, "life": box.home / "life", "home": box.home,
               "runtime": runtime.python_dir(box.home / "tools-runtime"), "temp": Path(tempfile.gettempdir()),
               "container_profile": profile / "AC"}
    box.tool("scribble", attempts(*[(k, f"open(os.path.join({str(v)!r}, 'scribble.txt'), 'w').write('x')")
                                    for k, v in targets.items()]))
    code, out, _ = box.run(capsys, "scribble")
    assert code == 0, out
    assert results(out) == {k: "ok" if k == "tools" else "refused" for k in targets}
    assert (box.tools / "scribble.txt").read_text() == "x"
    assert not any((v / "scribble.txt").exists() for k, v in targets.items() if k != "tools")


def listener() -> socket.socket:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    s.settimeout(0.5)
    return s


def got_connection(s: socket.socket) -> bool:
    try:
        s.accept()[0].close()
        return True
    except OSError:
        return False


def host_online() -> bool:
    try:
        socket.create_connection(("1.1.1.1", 443), 5).close()
        return True
    except OSError:
        return False


def net_probe(port: int) -> str:
    return attempts(("loopback", f"socket.create_connection(('127.0.0.1', {port}), 3).close()"),
                    ("dns", "socket.getaddrinfo('python.org', 443)"),
                    ("internet", "socket.create_connection(('1.1.1.1', 443), 5).close()"))


def test_no_network_unless_the_person_opts_in(box, capsys):
    # a local listener stands in for the Ibasho daemon's HTTP API (and anything else on this machine)
    s = listener()
    try:
        box.tool("net", net_probe(s.getsockname()[1]))
        code, out, _ = box.run(capsys, "net")
        assert code == 0, out
        assert results(out) == {"loopback": "refused", "dns": "refused", "internet": "refused"}
        assert not got_connection(s)
    finally:
        s.close()


def test_network_opt_in_opens_the_internet_but_not_this_machine(box, capsys):
    if not host_online():
        pytest.skip("this machine is offline")
    s = listener()
    try:
        box.tool("net", net_probe(s.getsockname()[1]))
        code, out, _ = box.run(capsys, "net", network=True)
        assert code == 0, out
        assert results(out) == {"loopback": "refused", "dns": "ok", "internet": "ok"}
        assert not got_connection(s)
    finally:
        s.close()


def test_timeout(box, capsys):
    box.tool("sleepy", "import time\nprint('going to sleep', flush=True)\ntime.sleep(60)\n")
    t0 = time.monotonic()
    code, out, err = box.run(capsys, "sleepy", timeout_s=2)
    assert code == 1 and "stopped after 2 s" in err and "going to sleep" in out
    assert time.monotonic() - t0 < 10


def test_output_cap(box, capsys):
    box.tool("loud", "import sys\nfor i in range(100000):\n    print('la' * 40)\n")
    code, out, err = box.run(capsys, "loud", max_output_kb=1)
    assert code == 1 and "passed 1 KB" in err
    assert len(out.encode()) <= 1024 + 1


def test_memory_cap(box, capsys):
    box.tool("hungry", "try:\n    b = bytearray(400 * 1024 * 1024)\n    print('got it')\nexcept MemoryError:\n"
                       "    print('refused')\n")
    code, out, _ = box.run(capsys, "hungry", memory_mb=128)
    assert "refused" in out


def test_nothing_outlives_a_run(box, capsys):
    box.tool("spawner", "import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', "
                        "\"import time; time.sleep(3); open('late.txt', 'w').write('x')\"])\nprint('left it running')\n")
    code, out, _ = box.run(capsys, "spawner")
    assert code == 0 and "left it running" in out
    time.sleep(5)
    assert not (box.tools / "late.txt").exists()


SCREEN = r"""
import ctypes as C
from ctypes import wintypes as W
u, g, k = C.WinDLL('user32'), C.WinDLL('gdi32'), C.WinDLL('kernel32')
u.GetDC.restype = u.GetThreadDesktop.restype = C.c_void_p
g.CreateCompatibleDC.restype = g.CreateCompatibleBitmap.restype = C.c_void_p
g.CreateCompatibleDC.argtypes = [C.c_void_p]
g.CreateCompatibleBitmap.argtypes = [C.c_void_p, C.c_int, C.c_int]
g.SelectObject.argtypes = [C.c_void_p, C.c_void_p]
g.BitBlt.argtypes = [C.c_void_p] + [C.c_int] * 4 + [C.c_void_p, C.c_int, C.c_int, C.c_uint]
g.GetPixel.argtypes = [C.c_void_p, C.c_int, C.c_int]
u.GetUserObjectInformationW.argtypes = [C.c_void_p, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
name = C.create_unicode_buffer(256)
u.GetUserObjectInformationW(u.GetThreadDesktop(k.GetCurrentThreadId()), 2, name, 512, C.byref(W.DWORD()))
print('desktop', name.value)
print('foreground', u.GetForegroundWindow())
dc = u.GetDC(None)
mem = g.CreateCompatibleDC(dc)
g.SelectObject(mem, g.CreateCompatibleBitmap(dc, 256, 256))
g.BitBlt(mem, 0, 0, 256, 256, dc, 0, 0, 0x00CC0020)
print('colours', len({g.GetPixel(mem, x, y) for x in range(0, 256, 4) for y in range(0, 256, 4)}))
print('clipboard', u.OpenClipboard(None))
"""


def test_no_screen_or_other_windows(box, capsys):
    box.tool("screen", SCREEN)
    code, out, _ = box.run(capsys, "screen")
    assert code == 0, out
    r = results(out)
    assert r["desktop"].startswith("ibasho-tool-")      # a desktop of its own, with nothing on it
    assert r["foreground"] == "0" and r["colours"] == "1" and r["clipboard"] == "0"


def test_a_file_moved_in_still_runs(box, capsys, tmp_path):
    outside = tmp_path / "moved.py"
    outside.write_text("print('moved in and running')\n")
    os.replace(outside, box.tools / "moved.py")     # a move keeps the file's old access; the run repairs it
    code, out, _ = box.run(capsys, "moved")
    assert code == 0 and "moved in and running" in out


def test_runs_alongside_a_busy_tool(box, capsys):
    # one tool creating and deleting files while others start: each start repairs access across the folder
    import threading
    box.tool("churn", "import os, time\nos.makedirs('churn', exist_ok=True)\nend = time.time() + 4\n"
                      "i = 0\nwhile time.time() < end:\n    p = f'churn/{i % 50}.tmp'\n    open(p, 'w').close()\n"
                      "    os.remove(p)\n    i += 1\nprint('churned', i)\n")
    box.tool("quick", "print('quick ok')\n")
    bg = threading.Thread(target=lambda: tools_cli.run_tool(
        type("Ctx", (), {"cfg": config_mod.load(box.cfgpath)})(), "churn", []))
    bg.start()
    try:
        time.sleep(0.5)
        for _ in range(3):
            code, out, err = run(box.cfgpath, capsys, "tool", "quick")
            assert code == 0 and "quick ok" in out, err
    finally:
        bg.join(15)


def test_list(box, capsys):
    box.tool("documented", '"""Does one thing well."""\n')
    code, out, _ = run(box.cfgpath, capsys, "tool", "list")
    assert code == 0 and "Python " + runtime.PYTHON in out and "documented  Does one thing well." in out
