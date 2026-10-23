"""LEAP: the viewer's plugin protocol, as seen from the far end of the stub.

The viewer starts a child process (`--leap CMD`) and talks LLSD with it over stdin/stdout, each message framed
as `<byte length>:<payload>`. The viewer writes binary LLSD (with its `<? LLSD/Binary ?>` header); the child must
write notation LLSD. Every message is a map `{pump, data}`.

The first message from the viewer names two pumps: `pump` is our reply pump (replies and forwarded events come
back on it or on the listened pump's own name), `data.command` is the LEAP command pump (listen, getAPIs...).
A request carries `reply` (our reply pump) and `reqid`; the viewer's answer echoes `reqid`.

Our stub (ibasho.stub) only moves whole frames between the viewer and the daemon, so this module is all the
LLSD handling there is.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import itertools
import logging
import random
import uuid
from typing import Any, Callable

import llsd

log = logging.getLogger(__name__)

FRAME_MAX = 64 * 1024 * 1024
AUTH_MAGIC = b"IBASHO-LEAP/1"


class LeapError(Exception):
    pass


def frame(payload: bytes) -> bytes:
    return str(len(payload)).encode() + b":" + payload


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readuntil(b":")
    try:
        n = int(head[:-1])
    except ValueError:
        raise LeapError(f"bad frame length {head[:20]!r}") from None
    if not 0 <= n <= FRAME_MAX:
        raise LeapError(f"frame length {n} out of range")
    return await reader.readexactly(n)


def parse(payload: bytes) -> Any:
    """What the viewer sends: binary LLSD after a `<? LLSD/Binary ?>` line (the viewer's LLLeap always does that).
    Notation and XML are accepted too, headed or not, so a fake viewer in a test can be lazy."""
    if payload.startswith(b"<?"):
        nl = payload.find(b"\n")
        if nl < 0:
            raise LeapError(f"LLSD header without a newline: {payload[:40]!r}")
        header = payload[:nl].replace(b" ", b"").lower()
        body = payload[nl + 1:]
        if b"binary" in header:
            return llsd.parse_binary(body)
        if b"notation" in header:
            return llsd.parse_notation(body)
        if b"xml" in header:
            return llsd.parse_xml(payload)
        raise LeapError(f"unknown LLSD header {payload[:nl][:40]!r}")
    return llsd.parse_notation(payload)


def encode(pump: str, data: Any) -> bytes:
    return frame(llsd.format_notation({"pump": pump, "data": data}))


def to_jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): to_jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [to_jsonable(x) for x in v]
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return {"_bytes_b64": base64.b64encode(v).decode()}
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, float) and v != v:
        return None
    return v


EventHandler = Callable[[str, Any], None]


class LeapPeer:
    """One connected viewer (through the stub). `on_event(pump, data)` gets everything that isn't a reply."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, handshake: dict,
                 on_event: EventHandler, viewer_pid: int | None = None):
        if not (isinstance(handshake, dict) and isinstance(handshake.get("data"), dict)):
            raise LeapError("bad handshake")
        self.reader, self.writer = reader, writer
        self.reply_pump: str = handshake["pump"]
        self.command_pump: str = handshake["data"]["command"]
        self.features = handshake["data"].get("features") or {}
        self.viewer_pid = viewer_pid
        self.on_event = on_event
        self.closed = asyncio.Event()
        self._pending: dict[int, asyncio.Future] = {}
        # a fresh range each connection, so a slow answer meant for an earlier daemon never matches a new request
        self._ids = itertools.count(random.randrange(1, 1 << 30))

    async def run(self):
        try:
            while True:
                msg = parse(await read_frame(self.reader))
                if isinstance(msg, dict) and "pump" in msg:
                    self._dispatch(msg["pump"], msg.get("data"))
        except (asyncio.IncompleteReadError, ConnectionError, LeapError) as e:
            log.info("viewer link closed: %r", e)
        except Exception:
            log.exception("viewer link failed")
        finally:
            self.close()

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        for f in self._pending.values():
            if not f.done():
                f.set_exception(ConnectionError("viewer disconnected"))
        self._pending.clear()
        try:
            self.writer.close()
        except Exception:
            pass

    def _dispatch(self, pump: str, data: Any):
        if pump == self.reply_pump and isinstance(data, dict) and "reqid" in data:
            f = self._pending.pop(data["reqid"], None)
            if f is not None and not f.done():
                f.set_result(data)
            return  # replies nobody waits for (sends, timeouts) are dropped
        try:
            self.on_event(pump, data)
        except Exception:
            log.exception("event handler failed for %s", pump)

    def post(self, pump: str, data: dict):
        if self.closed.is_set():
            raise ConnectionError("viewer disconnected")
        self.writer.write(encode(pump, data))

    def begin(self, pump: str, data: dict) -> tuple[int, asyncio.Future]:
        """Post a request now (synchronously, so nothing else interleaves) and return what `finish` awaits."""
        reqid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[reqid] = fut
        try:
            self.post(pump, dict(data, reply=self.reply_pump, reqid=reqid))
        except Exception:
            self._pending.pop(reqid, None)
            raise
        return reqid, fut

    async def finish(self, pending: tuple[int, asyncio.Future], timeout: float = 15.0) -> dict:
        reqid, fut = pending
        try:
            await self.writer.drain()
            reply = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(reqid, None)
        if isinstance(reply, dict):
            reply = {k: v for k, v in reply.items() if k != "reqid"}
        return reply

    async def request(self, pump: str, data: dict, timeout: float = 15.0) -> dict:
        """Send with a reply pump and wait for the answer. Ops that never answer just time out."""
        return await self.finish(self.begin(pump, data), timeout)

    async def send(self, pump: str, data: dict):
        """Fire and forget, but still with a reply pump: an op that fails then answers {error} instead of
        logging an error the viewer may treat as fatal. The answer, if any, is dropped."""
        reqid = next(self._ids)
        self.post(pump, dict(data, reply=self.reply_pump, reqid=-reqid))
        await self.writer.drain()

    async def command(self, op: str, timeout: float = 15.0, **args) -> dict:
        return await self.request(self.command_pump, dict(args, op=op), timeout)

    async def listen(self, source: str, listener: str) -> bool:
        r = await self.command("listen", source=source, listener=listener)
        return bool(r.get("status"))
