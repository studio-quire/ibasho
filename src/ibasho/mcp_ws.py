"""`ibasho mcp --websocket HOST:PORT`: the MCP server over WebSocket.

For MCP hosts that dial a URL rather than start a program, such as Connectome's `url` entries: chiefly a host on
the same server as Ibasho's container, which can't start `ibasho mcp` inside the container itself (and shouldn't be
given Docker to do it with: that's the whole machine).

Each connection gets an `ibasho mcp` of its own, started as a stdio host would start it, with one JSON-RPC message
per WebSocket text message. One connection at a time, since one install's events go to one reader: a new
connection (the host reconnecting) ends the one before. The token (IBASHO_HOME/mcp-token, made on first use) comes
as a Bearer header or a `token` query parameter, as Connectome sends either; requests that carry an Origin header
come from a web page and are refused.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import sys
from pathlib import Path

from . import config as config_mod

FRAME_MAX = 64 << 20   # pictures come base64 in tool results


def token_file() -> Path:
    return config_mod.home() / "mcp-token"


def ensure_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    return token


def parse_listen(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"--websocket takes HOST:PORT, e.g. 127.0.0.1:9280 (got {value!r})")
    return host.strip("[]") or "127.0.0.1", int(port)


class Bridge:
    def __init__(self, token: str, child_args: list[str]):
        self.token = token
        self.child_args = child_args
        self.current: asyncio.Future | None = None

    def authorized(self, request) -> bool:
        given = request.query.get("token", "")
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        return bool(given) and hmac.compare_digest(given.encode(), self.token.encode())

    async def handle(self, request):
        from aiohttp import WSMsgType, web
        if request.headers.get("Origin"):
            return web.Response(status=403, text="no web pages")
        if not self.authorized(request):
            return web.Response(status=401, text="token required")
        ws = web.WebSocketResponse(max_msg_size=FRAME_MAX, heartbeat=30)
        await ws.prepare(request)
        if self.current is not None and not self.current.done():
            self.current.set_result(None)   # the one before ends: one reader of the events at a time
        ended = asyncio.get_running_loop().create_future()
        self.current = ended
        proc = await asyncio.create_subprocess_exec(*self.child_args, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, limit=FRAME_MAX)
        print(f"ibasho mcp-ws: connection from {request.remote}; ibasho mcp pid {proc.pid}", file=sys.stderr)

        async def to_host():
            while line := await proc.stdout.readline():
                if line.strip():
                    await ws.send_str(line.decode("utf-8", "replace").rstrip("\r\n"))

        async def to_server():
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:   # one message a line, whatever the host's JSON looked like
                    data = json.dumps(json.loads(msg.data), ensure_ascii=False, separators=(",", ":"))
                except ValueError:
                    continue
                proc.stdin.write(data.encode("utf-8") + b"\n")
                await proc.stdin.drain()

        tasks = [asyncio.ensure_future(to_host()), asyncio.ensure_future(to_server()), ended]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (ConnectionError, OSError):
            pass
        finally:
            for t in tasks[:2]:
                t.cancel()
            try:
                proc.stdin.close()
            except (ConnectionError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            await ws.close()
            print(f"ibasho mcp-ws: connection from {request.remote} ended", file=sys.stderr)
        return ws


def serve(listen: str, config: str | None):
    from aiohttp import web
    host, port = parse_listen(listen)
    path = token_file()
    token = ensure_token(path)
    child = [sys.executable, "-P", "-m", "ibasho", *(["--config", config] if config else []), "mcp"]
    app = web.Application()
    app.router.add_get("/", Bridge(token, child).handle)
    print(f"ibasho mcp-ws: MCP over WebSocket at ws://{host}:{port}/ (token in {path})", file=sys.stderr)
    web.run_app(app, host=host, port=port, print=None, handle_signals=True)
