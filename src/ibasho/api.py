"""The daemon's HTTP API, from the command side."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import Config


class ApiError(Exception):
    def __init__(self, msg: str, status: int = 0, data: dict | None = None):
        super().__init__(msg)
        self.status = status
        self.data = data or {}


class Down(ApiError):
    """The daemon isn't running."""


def slow_viewer(pump: str, op: str, timeout: float) -> str:
    return (f"the viewer didn't answer {pump} {op} within {timeout:g} s. It's probably slow right now: a heavy "
            "scene, another program busy on the graphics card, or a frame-rate limit in its settings (YieldTime, "
            "BackgroundYieldTime). Trying again in a moment usually works")


class Api:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = f"http://127.0.0.1:{cfg.http_port}"
        self._token: str | None = None
        self._status: dict | None = None

    @property
    def token(self) -> str:
        if self._token is None:
            try:
                self._token = self.cfg.token_file.read_text().strip()
            except OSError:
                raise Down("the daemon has never run here (no install token yet); `ibasho up` starts it") from None
        return self._token

    def http(self, method: str, path: str, body=None, timeout: float = 60):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                info = json.loads(raw)
            except ValueError:
                info = {"error": raw.decode(errors="replace")[:500]}
            raise ApiError(str(info.get("error") or info), e.code, info) from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise Down(f"daemon not reachable on port {self.cfg.http_port} ({getattr(e, 'reason', e)})") from None

    def status(self, fresh: bool = False) -> dict:
        if self._status is None or fresh:
            self._status = self.http("GET", "/status", timeout=5)
        return self._status

    def is_up(self) -> dict | None:
        try:
            return self.status(fresh=True)
        except Down:
            return None

    @property
    def ibasho(self) -> bool:
        return self.status().get("viewer") == "ibasho"

    def has_op(self, api: str, op: str) -> bool:
        return op in (self.status().get("ops") or {}).get(api, [])

    def cmd(self, pump: str, op: str, args: dict | None = None, timeout: float = 30, wait: bool = True,
            pace: float = 30):
        """One viewer request. If the message rate limit says wait, wait (up to `pace` seconds in all) and retry:
        a conversation then just goes at a human pace."""
        data = dict(args or {})
        data["op"] = op
        body = {"pump": pump, "data": data, "timeout": timeout, "wait": wait}
        deadline = time.monotonic() + pace
        while True:
            try:
                return self.http("POST", "/cmd", body, timeout=timeout + 10)
            except ApiError as e:
                ra = float(e.data.get("retry_after") or 0)
                if e.status == 429 and ra and time.monotonic() + ra < deadline:
                    time.sleep(ra + 0.05)
                    continue
                if e.status == 504:
                    raise ApiError(slow_viewer(pump, op, timeout), 504, e.data) from None
                raise

    def op(self, api: str, op: str, args: dict | None = None, timeout: float = 15):
        """A request whose reply may carry {error}: raise it."""
        r = self.cmd(api, op, args, timeout=timeout)
        if isinstance(r, dict) and r.get("error"):
            raise ApiError(f"{api}.{op}: {r['error']}")
        return r

    def post(self, pump: str, op: str, args: dict | None = None):
        return self.cmd(pump, op, args, wait=False)

    def events(self, since: int = 0, wait: float = 0, kinds: list[str] | None = None, limit: int = 0) -> list[dict]:
        q = {"since": since, "wait": wait}
        if kinds:
            q["kinds"] = ",".join(kinds)
        if limit:
            q["limit"] = limit
        return self.http("GET", "/events?" + urllib.parse.urlencode(q), timeout=wait + 15)

    def recent(self, kinds: list[str] | None = None, back: int = 5000) -> list[dict]:
        """The last `back` events (what the daemon holds in memory), for lookups that needn't read old days."""
        return self.events(max(0, self.seq() - back), kinds=kinds)

    def seq(self) -> int:
        return int(self.status(fresh=True)["seq"])

    def rpc(self, method: str, params=None, timeout: float = 60):
        r = self.http("POST", "/rpc", {"method": method, "params": params, "timeout": timeout}, timeout=timeout + 10)
        return r.get("result")

    def post_event(self, kind: str, data: dict) -> int:
        return self.http("POST", "/event", {"kind": kind, "data": data})["seq"]
