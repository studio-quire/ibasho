"""One Claude Code session, run headless: `claude -p` with stream-json in and out.

The keeper writes user messages to the session's stdin and reads its event stream from stdout. That stream says
everything the keeper needs: when a turn starts and ends, the context size after every model call, the context
window, open tool calls, background tasks, the plan's rate-limit state and the estimated cost. Closing stdin ends
the session once Claude Code has finished what it's doing; the keeper kills the process tree if that takes too long.

Monitor works headless: an `ibasho watch` started under it delivers each line mid-turn, and a line that arrives while
the session sits between turns starts a new turn by itself. So world events never need the keeper; the keeper only
speaks at turn ends (nudges, notes, wind-downs), or through the daemon's event log, which the watch prints.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path, PureWindowsPath

import psutil

LONG_TOOLS = {"Task", "Agent"}   # subagents can run long while the main stream is quiet

# Markers a parent Claude Code session sets in its children's environment. A session started from inside another
# (a keeper launched from someone's Claude Code terminal) would otherwise think it's a nested child.
NESTING_ENV = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
               "CLAUDE_PID", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET",
               "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_PLUGIN_DIRS", "CLAUDE_EFFORT",
               "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION"}

# Environment variables that choose how Claude Code authenticates or whom it bills: an API key, a token, another
# provider (Bedrock, Vertex, Foundry, a gateway), another endpoint. `claude -p` uses ANTHROPIC_API_KEY whenever
# it's set, ahead of the folder's login, so one left in the person's environment would quietly bill every session
# to it. Unless keeper.auth = "env", sessions get none of them and run on the login of their config folder.
AUTH_ENV = re.compile(r"^(ANTHROPIC_\w*|AWS_BEARER_TOKEN_BEDROCK|CLAUDE_CODE_(OAUTH\w*|USE_\w+|SKIP_\w*AUTH|"
                      r"SESSION_ACCESS_TOKEN|CLIENT_KEY|CUSTOM_OAUTH_URL|API_KEY\w*|SDK_HAS_OAUTH_REFRESH))$", re.I)


class Session:
    """What the keeper reads and does with a session. ClaudeSession is the real one; tests use a fake."""

    id: str
    pid: int | None = None
    started: float
    turn_open: bool = False          # a turn is running (the model or a tool is working)
    last_activity: float             # the stream last said anything
    turn_ended_at: float | None = None
    turns: int = 0
    tool_calls: int = 0
    context: int = 0                 # tokens in context after the last model call
    context_window: int | None = None
    model: str | None = None         # as Claude Code names it at the start of each turn
    cost_usd: float = 0.0            # Claude Code's estimate for this session so far
    rate: dict | None = None         # the latest rate_limit_info
    error: str | None = None         # the last turn's error, if it ended in one

    def alive(self) -> bool: ...
    def exit_code(self) -> int | None: ...
    def send(self, text: str) -> bool: ...
    def close(self, wait: float = 15): ...
    def pending_tool(self) -> tuple[str, float] | None: ...
    def long_running(self) -> bool: ...
    def processes(self) -> list[psutil.Process]: ...


class ClaudeSession(Session):
    def __init__(self, args: list[str], cwd: Path, env: dict, log_path: Path, stderr_path: Path,
                 session_id: str, now=time.time):
        self.id = session_id
        self.now = now
        self.started = self.last_activity = now()
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, float]] = {}
        self._log = open(log_path, "a", encoding="utf-8")
        self._stderr = open(stderr_path, "ab")
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(args, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._stderr, creationflags=flags, encoding="utf-8", errors="replace",
                                     bufsize=1)
        self.pid = self.proc.pid
        try:
            self.create_time = psutil.Process(self.pid).create_time()
        except psutil.Error:
            self.create_time = None
        self._reader = threading.Thread(target=self._read, daemon=True, name=f"session-{session_id[:8]}")
        self._reader.start()

    # ---- the stream ----

    def _note(self, line: str):
        self._log.write(time.strftime("%H:%M:%S ") + line.replace("\n", " / ")[:2000] + "\n")
        self._log.flush()

    def _read(self):
        for line in self.proc.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            try:
                with self._lock:
                    self._on(e)
            except Exception as ex:  # never let one odd message stop the reader
                self._note(f"(keeper couldn't read a stream message: {ex!r})")
        self._note("(stream closed)")

    def _on(self, e: dict):
        t = self.now()
        kind, sub = e.get("type"), e.get("subtype")
        if kind == "rate_limit_event":
            self.rate = e.get("rate_limit_info") or self.rate
            return
        self.last_activity = t
        if kind == "system" and sub == "init":
            self.turn_open = True
            self.model = e.get("model") or self.model
        elif kind == "assistant":
            self.turn_open = True
            m = e.get("message") or {}
            u = m.get("usage") or {}
            if u and not e.get("parent_tool_use_id"):
                self.context = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                                + u.get("cache_creation_input_tokens", 0) + u.get("output_tokens", 0))
            for c in m.get("content") or []:
                if c.get("type") == "text" and c.get("text", "").strip():
                    self._note("said: " + c["text"].strip())
                elif c.get("type") == "tool_use":
                    self.tool_calls += 1
                    self._pending[c.get("id")] = (c.get("name"), t)
                    self._note(f"tool: {c.get('name')} {json.dumps(c.get('input'), ensure_ascii=False)[:300]}")
        elif kind == "user":
            content = (e.get("message") or {}).get("content")
            for c in content if isinstance(content, list) else []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    self._pending.pop(c.get("tool_use_id"), None)
                    if c.get("is_error"):
                        self._note("tool error: " + json.dumps(c.get("content"), ensure_ascii=False)[:300])
        elif kind == "result":
            self.turn_open = False
            self.turn_ended_at = t
            self.turns += 1
            self._pending.clear()
            for mu in (e.get("modelUsage") or {}).values():
                if mu.get("contextWindow"):
                    self.context_window = int(mu["contextWindow"])
            if e.get("total_cost_usd") is not None:
                self.cost_usd = float(e["total_cost_usd"])
            self.error = None
            if e.get("is_error") or (sub and sub != "success"):
                self.error = str(e.get("result") or sub or "error")[:500]
                self._note(f"turn ended with an error: {self.error}")
            else:
                self._note(f"turn ended (context {self.context // 1000}k)")

    # ---- control ----

    def alive(self) -> bool:
        return self.proc.poll() is None

    def exit_code(self) -> int | None:
        return self.proc.poll()

    def send(self, text: str) -> bool:
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            return False
        with self._lock:
            self.turn_open = True
            self.last_activity = self.now()
        self._note("keeper sent: " + text[:300])
        return True

    def pending_tool(self) -> tuple[str, float] | None:
        with self._lock:
            calls = [(n, t) for n, t in self._pending.values() if n not in LONG_TOOLS]
        return min(calls, key=lambda c: c[1]) if calls else None

    def long_running(self) -> bool:
        with self._lock:
            return any(n in LONG_TOOLS for n, _ in self._pending.values())

    def processes(self) -> list[psutil.Process]:
        try:
            p = psutil.Process(self.pid)
            return [p] + p.children(recursive=True)
        except psutil.Error:
            return []

    def close(self, wait: float = 15):
        """End it: close stdin (Claude Code finishes up and exits), then kill whatever is left of its tree."""
        tree = self.processes()
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(wait)
        except subprocess.TimeoutExpired:
            pass
        for p in tree:
            try:
                p.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(tree, timeout=5)
        self._reader.join(timeout=5)
        self._log.close()
        self._stderr.close()


def watch_armed(procs: list[psutil.Process]) -> bool:
    """An `ibasho watch` is running in the session's process tree (the command a Monitor runs), so world events and
    the keeper's daemon-side messages can reach it."""
    for p in procs[1:]:
        try:
            args = p.cmdline()
        except psutil.Error:
            continue
        if "watch" not in args:
            continue
        if any(PureWindowsPath(a).stem.lower() == "ibasho" for a in args[:args.index("watch")]):  # / or \
            return True
    return False


def clean_env(base: dict, keep_auth: bool = False) -> dict:
    """The environment for Claude Code: no nesting markers, and (unless keep_auth) none of AUTH_ENV."""
    return {k: v for k, v in base.items()
            if k.upper() not in NESTING_ENV and (keep_auth or not AUTH_ENV.match(k))}


def find_claude(configured: str = "") -> str | None:
    import shutil
    if configured:
        return configured if Path(configured).exists() or shutil.which(configured) else None
    found = shutil.which("claude")
    if found:
        return found
    for p in (Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude"),):
        if p.exists():
            return str(p)
    return None


def auth_status(claude: str, env: dict) -> dict:
    """`claude auth status` (no tokens spent): {loggedIn, authMethod, subscriptionType...}, or {error}."""
    try:
        r = subprocess.run([claude, "auth", "status"], env=env, capture_output=True, text=True, timeout=60,
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        return json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return {"error": str(e)}
