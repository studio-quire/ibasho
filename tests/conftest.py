from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest

from ibasho import config as config_mod
from ibasho import daemon as daemon_mod
from ibasho.api import Api

from fake_viewer import FakeViewer


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def write_config(home: Path, **over) -> config_mod.Config:
    ports = {"leap": free_port(), "http": free_port(), "editor": free_port()}
    grid = over.pop("grid", "localhost-9000")
    confirmed = over.pop("confirmed", False)
    viewer = over.pop("viewer", "")
    caps = "\n".join(f"{k} = {v!r}" if not isinstance(v, str) else f'{k} = "{v}"' for k, v in over.items())
    text = f"""
[agent]
name = "Test Avatar"
nicknames = ["testy"]
[grid]
name = "{grid}"
scripted_agent_flag_confirmed = {str(confirmed).lower()}
[viewer]
path = '{viewer}'
[ports]
leap = {ports['leap']}
http = {ports['http']}
editor = {ports['editor']}
[caps]
{caps}
"""
    path = home / "config.toml"
    path.write_text(text, encoding="utf-8")
    return config_mod.load(path)


class World:
    """The daemon in a background event loop, a fake viewer behind the real stub, and the CLI's API client."""

    def __init__(self, cfg: config_mod.Config, ibasho: bool = True):
        self.cfg = cfg
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.daemon: daemon_mod.Daemon = self.run(self._start_daemon())
        self.viewer = FakeViewer(cfg.leap_port, config_mod.leap_token(self.daemon.token), ibasho=ibasho, log=cfg.state_dir / "stub.log")
        self.api = Api(cfg)

    async def _start_daemon(self):
        started = self.loop.create_future()
        self.serve_task = asyncio.ensure_future(daemon_mod.serve(self.cfg, started))
        return await asyncio.wait_for(started, 10)

    def run(self, coro, timeout=20):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def call(self, fn, *args):
        """Run a plain function on the loop (fake viewer writes must happen there)."""
        async def go():
            return fn(*args)
        return self.run(go())

    def connect(self, logged_in: bool = True):
        """Connect the fake viewer and wait until the daemon has finished with it, so a test starts from a settled
        state even on a busy machine: the daemon's link setup ends by asking for the startup state, and its
        after-login setup by logging "in world"."""
        since = self.api.seq()
        self.run(self.viewer.start())
        self.until(lambda: self.viewer.sent("LLStartUp", "postStartupState"), 20)
        if logged_in:
            self.call(self.viewer.startup, "STATE_STARTED")
            self.until(lambda: any(e["data"].get("msg") == "in world"
                                   for e in self.api.events(since, kinds=["daemon"])), 20)

    def emit(self, pump, data):
        self.call(self.viewer.emit, pump, data)

    def until(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if pred():
                    return
            except Exception:
                pass
            time.sleep(0.05)
        raise AssertionError("condition not met in time")

    def close(self):
        try:
            self.run(self.viewer.stop(), 10)
        except Exception:
            pass
        self.daemon.stopping.set()
        try:
            self.run(asyncio.wait_for(self.serve_task, 10), 15)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


@pytest.fixture(autouse=True)
def _sessions_dir(tmp_path_factory, monkeypatch):
    """Which config each session ran with goes to a test folder, not this machine's %LOCALAPPDATA%."""
    d = tmp_path_factory.mktemp("sessions")
    monkeypatch.setattr(config_mod, "sessions_dir", lambda: d)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("IBASHO_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return tmp_path


@pytest.fixture
def world(home):
    w = World(write_config(home))
    yield w
    w.close()


@pytest.fixture
def world_connected(world):
    world.connect()
    return world
