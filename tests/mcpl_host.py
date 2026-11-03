"""A small MCPL host for tests: `ibasho mcp` as a subprocess, spoken to over its stdin and stdout the way Connectome's
agent-framework does (MCP 2024-11-05, MCPL 0.5 in `experimental`, the grant as a featureSets/update Request)."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time

EVERYTHING = ["tools", "pushEvents", "contextHooks.beforeInference.inject.beforeUser"]


class Host:
    def __init__(self, config_path, mcpl: bool = True):
        self.mcpl = mcpl
        self.proc = subprocess.Popen([sys.executable, "-m", "ibasho", "--config", str(config_path), "mcp"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.ids = 0
        self.answers: dict = {}
        self.pushes: list[dict] = []
        self.notes: list[str] = []      # the methods of the server's notifications, in order
        self.refuse_pushes = False
        self.cond = threading.Condition()
        self.stderr: list[str] = []
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:
            msg = json.loads(raw)
            if msg.get("method") == "push/event":
                with self.cond:
                    self.pushes.append(msg["params"])
                    self.cond.notify_all()
                self.send({"jsonrpc": "2.0", "id": msg["id"],
                           "result": {"accepted": False, "reason": "test"} if self.refuse_pushes else
                           {"accepted": True, "inferenceId": f"inf-{len(self.pushes)}"}})
            elif "id" in msg and "method" not in msg:
                with self.cond:
                    self.answers[msg["id"]] = msg
                    self.cond.notify_all()
            elif "id" not in msg:
                with self.cond:
                    self.notes.append(msg["method"])
                    self.cond.notify_all()

    def _read_err(self):
        for raw in self.proc.stderr:
            self.stderr.append(raw.decode("utf-8", "replace"))

    def send(self, msg: dict):
        with self.lock:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 20) -> dict:
        self.ids += 1
        rid = self.ids
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})})
        with self.cond:
            if not self.cond.wait_for(lambda: rid in self.answers, timeout):
                raise AssertionError(f"no answer to {method} in {timeout} s; stderr: {''.join(self.stderr)[-2000:]}")
            return self.answers.pop(rid)

    def start(self, grant: list[str] | None = EVERYTHING, **policy) -> dict:
        """initialize, initialized, and (for an MCPL host) the grant. Returns the initialize result."""
        caps = {"experimental": {"mcpl": {"version": "0.5", "pushEvents": True,
                                          "contextHooks": {"beforeInference": True}, "featureSets": True}}} \
            if self.mcpl else {}
        r = self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": caps,
                                        "clientInfo": {"name": "test-host", "version": "0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if self.mcpl and grant is not None:
            self.receipt = self.request("featureSets/update", {"effectiveCapabilities": grant, **policy})
        return r["result"]

    def wait_pushing(self, timeout: float = 15):
        """Until the server has begun reading events to push."""
        if not wait_until(lambda: any("pushing events after" in line for line in self.stderr), timeout):
            raise AssertionError(f"not pushing; stderr: {''.join(self.stderr)[-2000:]}")

    def wait_push(self, pred=lambda p: True, timeout: float = 15) -> dict:
        with self.cond:
            if not self.cond.wait_for(lambda: any(pred(p) for p in self.pushes), timeout):
                raise AssertionError(f"no such push in {timeout} s; pushes: {self.pushes}")
            return next(p for p in self.pushes if pred(p))

    def wait_note(self, method: str, count: int = 1, timeout: float = 10):
        """Until the server has sent `count` notifications of `method`."""
        with self.cond:
            if not self.cond.wait_for(lambda: self.notes.count(method) >= count, timeout):
                raise AssertionError(f"{count} x {method} not sent in {timeout} s; sent: {self.notes}")

    def tool_names(self) -> set[str]:
        return {t["name"] for t in self.request("tools/list", {})["result"]["tools"]}

    def text(self, p: dict) -> str:
        return p["payload"]["content"][0]["text"]

    def call_tool(self, name: str, args: dict | None = None, timeout: float = 30) -> str:
        r = self.request("tools/call", {"name": name, "arguments": args or {}}, timeout)
        return "\n".join(c.get("text", "") for c in r["result"]["content"])

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(10)
        except Exception:
            self.proc.kill()
            self.proc.wait(5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def wait_until(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False
