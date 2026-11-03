"""The Linux side: finding the viewer's binary, its command line and environment, [server], and `ibasho mcp`'s
WebSocket bridge. These run anywhere; the container itself is tested by hand (docs/linux.md)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from ibasho import config as config_mod, launch, mcp_ws
from ibasho.cli import lifecycle


def linux_tree(tmp_path: Path) -> Path:
    root = tmp_path / "Ibasho-0.1.0.29-linux-x86_64"
    (root / "bin").mkdir(parents=True)
    (root / "lib").mkdir()
    (root / "bin" / "ibasho-viewer-bin").write_bytes(b"")
    (root / "ibasho-viewer").write_text("#!/bin/bash\n")
    (root / "build_data.json").write_text(json.dumps({"Channel": "Ibasho", "Executable": "bin/ibasho-viewer-bin"}))
    return root


def test_linux_viewer_from_script_binary_or_folder(tmp_path):
    root = linux_tree(tmp_path)
    for given in (root / "ibasho-viewer", root / "bin" / "ibasho-viewer-bin", root):
        assert launch.linux_viewer(given) == (root / "bin" / "ibasho-viewer-bin", root)
    with pytest.raises(launch.LaunchError):
        launch.linux_viewer(tmp_path / "nothing" / "here")


def test_linux_command_and_environment(tmp_path, monkeypatch):
    root = linux_tree(tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/usr/local/lib")
    monkeypatch.delenv("LP_NUM_THREADS", raising=False)
    monkeypatch.delenv("MALLOC_MMAP_THRESHOLD_", raising=False)
    cmd, cwd, env = launch.viewer_process(root / "ibasho-viewer", ["--grid", "x y"])
    assert cmd == [str(root / "bin" / "ibasho-viewer-bin"), "--grid", "x y"]   # a list: no shell, no quoting
    assert cwd == root
    assert env["LD_LIBRARY_PATH"].startswith(str(root / "lib"))
    assert env["LD_LIBRARY_PATH"].endswith("/usr/local/lib")
    assert int(env["LP_NUM_THREADS"]) >= 1
    assert env["MALLOC_MMAP_THRESHOLD_"] == "131072"   # freed textures go back to the system


def test_leap_command_on_linux_names_python_absolutely():
    _, value = launch.leap_command(Path("/opt/ibasho/bin/python3"), Path("/opt/x y/stub.py"), posix=True)
    assert value == "/opt/ibasho/bin/python3 /opt/x\\ y/stub.py"


def write_config(tmp_path, server=""):
    """A config from the template, with `server` lines added to its [server] section."""
    path = tmp_path / "config.toml"
    config_mod.write_template(path, "Lin Port", "http://127.0.0.1:9640/", "/opt/ibasho-viewer/ibasho-viewer")
    path.write_text(path.read_text(encoding="utf-8").replace("[server]\n", "[server]\n" + server, 1),
                    encoding="utf-8")
    return path


def test_server_section(tmp_path):
    cfg = config_mod.load(write_config(tmp_path))
    assert cfg.ibasho_viewer
    assert cfg.server.window_size() == (800, 600)
    path = write_config(tmp_path / "b", "enabled = true\nmax_fps = 10\nweb = true\nwindow = \"640x480\"\n")
    cfg = config_mod.load(path)
    assert cfg.server.enabled and cfg.server.max_fps == 10.0 and cfg.server.web
    args = lifecycle.server_args(cfg)
    assert args[args.index("WindowWidth") + 1] == "640"
    assert args[args.index("IbashoNoWebPlugin") + 1] == "0"
    assert args[args.index("IbashoMaxFPS") + 1] == "10"
    assert args[args.index("EnableVoiceChat") + 1] == "0"
    assert lifecycle.server_args(cfg, web=False)[args.index("IbashoNoWebPlugin") + 1] == "1"
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(write_config(tmp_path / "c", "window = \"big\"\n"))
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(write_config(tmp_path / "d", "fps = 3\n"))


def test_server_off_adds_nothing(tmp_path):
    cfg = config_mod.load(write_config(tmp_path, "enabled = false\n"))
    assert lifecycle.server_args(cfg) == []


def test_linux_viewer_dirs(tmp_path, monkeypatch):
    cfg = config_mod.load(write_config(tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("IBASHO_USER_DIR", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    assert cfg.viewer_dirs() == [tmp_path / "home" / ".ibasho"]
    monkeypatch.setenv("IBASHO_USER_DIR", str(tmp_path / "elsewhere"))
    assert cfg.saved_login_file() == tmp_path / "elsewhere" / "user_settings" / "bin_conf.dat"


async def test_websocket_bridge(tmp_path):
    """A stdio "server" that answers each line with its own JSON wrapped, through the bridge, with and without
    the token."""
    from aiohttp import ClientSession, WSServerHandshakeError, web
    echo = tmp_path / "echo.py"
    echo.write_text("import sys, json\nfor line in sys.stdin:\n    print(json.dumps({'got': json.loads(line)}), "
                    "flush=True)\n")
    bridge = mcp_ws.Bridge("sekrit", [sys.executable, str(echo)])
    app = web.Application()
    app.router.add_get("/", bridge.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as s:
            with pytest.raises(WSServerHandshakeError):
                await s.ws_connect(f"ws://127.0.0.1:{port}/?token=wrong")
            with pytest.raises(WSServerHandshakeError):   # a web page
                await s.ws_connect(f"ws://127.0.0.1:{port}/?token=sekrit", headers={"Origin": "http://evil"})
            async with s.ws_connect(f"ws://127.0.0.1:{port}/", headers={"Authorization": "Bearer sekrit"}) as ws:
                await ws.send_str('{"jsonrpc": "2.0",\n "id": 1}')   # a newline inside: still one line
                got = json.loads((await ws.receive(timeout=10)).data)
                assert got == {"got": {"jsonrpc": "2.0", "id": 1}}
    finally:
        await runner.cleanup()
