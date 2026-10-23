"""A stand-in for the viewer on the far side of the real stub: it launches ibasho/stub.py with pipes, writes the
viewer's binary-LLSD handshake into it, answers requests the daemon sends, and can post events on any pump."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import llsd

from ibasho import stub
from ibasho.leap import frame, read_frame, parse
from ibasho.policy import slt_day

BINARY_HEADER = b"<? LLSD/Binary ?>\n"   # what the viewer writes (llsd's own header differs in spacing)


def viewer_frame(pump: str, data) -> bytes:
    body = llsd.format_binary({"pump": pump, "data": data})
    body = body[body.index(b"\n") + 1:]
    return frame(BINARY_HEADER + body)


class FakeViewer:
    def __init__(self, port: int, token: str, ibasho: bool = True, log: Path | None = None):
        self.port, self.token, self.ibasho = port, token, ibasho
        self.log = log
        self.reply_pump, self.command_pump = "fake.reply", "fake.command"
        self.self_id = str(uuid.UUID(int=0x1234))
        self.handlers: dict[tuple[str, str], object] = {}
        self.received: list[tuple[str, dict]] = []
        self.received_at: list[float] = []   # time.monotonic() of each, in step with `received`
        self.listening: set[str] = set()
        self.settings: dict[str, object] = {"CurrentGrid": "localhost-9000", "PreferredMaturity": 13,
                                            "CameraAngle": 1.047, "RenderFarClip": 128}
        self.messages_sent = 0      # what the viewer's own daily count says (`IbashoAgent messages`)
        self.extra_ops: dict[str, list[str]] = {}   # ops a newer viewer has (set before connecting)
        self.proc: asyncio.subprocess.Process | None = None
        self._reader_task = None
        self.default_handlers()

    def default_handlers(self):
        h = self.handlers
        h[("LLAgent", "getID")] = lambda d: {"id": self.self_id}
        h[("LLAgent", "getPosition")] = lambda d: {"region": [128.0, 128.0, 22.0], "global": [256128.0, 256128.0, 22.0]}
        h[("LLViewerControl", "get")] = lambda d: {"value": self.settings.get(d.get("key"))}

        def setting(d):
            self.settings[d.get("key")] = d.get("value")
            return {}
        h[("LLViewerControl", "set")] = setting
        h[("IbashoAgent", "names")] = lambda d: {"names": {
            i: {"display_name": "Test Avatar AI", "username": "test.avatar"} if i == self.self_id else
            {"display_name": "Someone", "username": f"user{i[:4]}"} for i in d.get("ids", [])}}
        h[("IbashoAgent", "pay")] = lambda d: {"ok": True, "target": "avatar"}
        h[("IbashoAgent", "people")] = lambda d: {"people": []}
        h[("IbashoAgent", "messages")] = lambda d: {"date": slt_day(), "count": self.messages_sent,
                                                   "max": self.settings.get("IbashoMaxMessagesPerDay", 3000)}
        h[("IbashoIM", "sessions")] = lambda d: {"sessions": []}
        h[("IbashoIM", "send")] = lambda d: {"session_id": str(uuid.UUID(int=0x99))}
        h[("IbashoBuild", "create")] = lambda d: {"id": str(uuid.UUID(int=0x77)), "local_id": 77}

    async def start(self):
        env = dict(os.environ, IBASHO_LEAP_PORT=str(self.port), IBASHO_LEAP_TOKEN=self.token,
                   IBASHO_STUB_LOG=str(self.log or ""))
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, stub.__file__, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env)
        self.write(self.reply_pump, {"command": self.command_pump, "features": {}})
        self._reader_task = asyncio.create_task(self._read())

    def write(self, pump: str, data):
        self.proc.stdin.write(viewer_frame(pump, data))

    def emit(self, pump: str, data: dict):
        self.write(pump, data)

    def startup(self, state: str = "STATE_STARTED"):
        self.emit("StartupState", {"str": state, "enum": 0})

    async def _read(self):
        try:
            while True:
                msg = parse(await read_frame(self.proc.stdout))
                pump, data = msg["pump"], msg.get("data") or {}
                self.received.append((pump, data))
                self.received_at.append(time.monotonic())
                self.answer(pump, data)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    def answer(self, pump: str, data: dict):
        op = data.get("op")
        if pump == self.command_pump:
            if op == "listen":
                self.listening.add(data["source"])
                reply = {"status": True}
            elif op == "getAPIs":
                reply = {k: {"desc": k} for k in (["IbashoAgent", "IbashoIM", "IbashoObjects", "IbashoBuild"]
                                                  if self.ibasho else []) + ["LLAgent", "UI", "LLWindow"]}
            elif op == "getAPI":
                ops = {"IbashoAgent": ["pay", "names", "people", "probeGround", "messages"],
                       "IbashoIM": ["send", "sessions"], "IbashoBuild": ["create"]}
                listed = ops.get(data.get("api"), []) + self.extra_ops.get(data.get("api"), [])
                reply = {"name": data.get("api"), "ops": [{"name": o} for o in listed]}
            else:
                reply = {}
        elif pump == "LLStartUp" and op == "postStartupState":
            return
        else:
            fn = self.handlers.get((pump, op))
            if fn is None:
                return
            reply = fn(data)
            if reply is None:
                return
        if data.get("reply"):
            self.write(data["reply"], dict(reply, reqid=data.get("reqid")))

    def sent(self, pump: str, op: str | None = None) -> list[dict]:
        return [d for p, d in self.received if p == pump and (op is None or d.get("op") == op)]

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._reader_task:
            self._reader_task.cancel()
