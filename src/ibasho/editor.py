"""Client for the viewer's external-editor JSON-RPC WebSocket (secondlife/viewer doc/external-editor-json-rpc.md):
script read/write/compile with diagnostics, script errors, object inventory listings, viewer commands.

The viewer only listens after `Tools.ToggleScriptEditorServer`; the daemon turns it on when first needed.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path
from typing import Callable

import aiohttp

from . import __version__


class RPCError(Exception):
    def __init__(self, err: dict):
        super().__init__(err.get("message"))
        self.err = err


class EditorClient:
    def __init__(self, port: int, on_event: Callable[[str, dict], None], expect_agent: Callable[[], str] = lambda: ""):
        self.port = port
        self.on_event = on_event
        self.expect_agent = expect_agent   # our avatar's id: the server on this port must be our viewer's
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.ready = asyncio.Event()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._session: aiohttp.ClientSession | None = None

    @property
    def connected(self) -> bool:
        return self.ws is not None and not self.ws.closed and self.ready.is_set()

    async def connect(self, timeout: float = 10):
        if self.connected:
            return
        self._session = self._session or aiohttp.ClientSession()
        self.ready.clear()
        self.ws = await self._session.ws_connect(f"ws://127.0.0.1:{self.port}/", max_msg_size=0, heartbeat=30)
        asyncio.create_task(self._reader(self.ws))
        await asyncio.wait_for(self.ready.wait(), timeout)

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
        if self._session is not None:
            await self._session.close()

    async def _reader(self, ws):
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                m = json.loads(msg.data)
                for one in m if isinstance(m, list) else [m]:
                    await self._handle(ws, one)
        finally:
            self.ready.clear()
            for f in self._pending.values():
                if not f.done():
                    f.set_exception(ConnectionError("editor socket closed"))
            self._pending.clear()
            self.on_event("editor.closed", {})

    async def _handle(self, ws, m: dict):
        if "method" in m and "id" in m:  # a call from the viewer
            method, params = m["method"], m.get("params") or {}
            if method == "session.handshake":
                me, theirs = self.expect_agent(), str(params.get("agent_id") or "")
                if me and theirs and me.lower() != theirs.lower():
                    self.on_event("editor.refused", {"reason": "another avatar's viewer holds the editor port"})
                    await ws.close()
                    return
                challenge = Path(str(params.get("challenge") or ""))
                if params.get("challenge") and not (challenge.name.startswith("sl_script_challenge_")
                                                    and challenge.suffix == ".tmp"):
                    await ws.close()   # only the viewer's own challenge file is read
                    return
                result = {
                    "client_name": "ibasho", "client_version": __version__, "protocol_version": "1.0",
                    "languages": params.get("languages", ["lsl", "luau"]),
                    "features": {"live_sync": False, "error_reporting": True, "unified_diagnostics": True,
                                 "object_publish": True, "commands": False},
                }
                if params.get("challenge"):  # a file only a local process can read
                    result["challenge_response"] = Path(params["challenge"]).read_text().strip()
                await ws.send_json({"jsonrpc": "2.0", "id": m["id"], "result": result})
            elif method == "system.ping":
                await ws.send_json({"jsonrpc": "2.0", "id": m["id"], "result": {}})
            else:
                await ws.send_json({"jsonrpc": "2.0", "id": m["id"],
                                    "error": {"code": -32601, "message": f"Method not found: {method}"}})
        elif "method" in m:  # a notification
            if m["method"] == "session.ok":
                self.ready.set()
            self.on_event(m["method"], m.get("params") or {})
        elif "id" in m:
            f = self._pending.pop(m["id"], None)
            if f and not f.done():
                if "error" in m:
                    f.set_exception(RPCError(m["error"]))
                else:
                    f.set_result(m.get("result"))

    async def call(self, method: str, params=None, timeout: float = 30):
        await self.connect()
        i = next(self._ids)
        f = asyncio.get_running_loop().create_future()
        self._pending[i] = f
        msg = {"jsonrpc": "2.0", "id": i, "method": method}
        if params is not None:
            msg["params"] = params
        await self.ws.send_json(msg)
        try:
            return await asyncio.wait_for(f, timeout)
        finally:
            self._pending.pop(i, None)
