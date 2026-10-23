"""Processes and ports on Windows.

Windows reuses PIDs quickly, so a pid from a file can name someone else's process by the time we read it, and
killing its tree would take theirs down. A pid file is trusted only if its process started before the file was
written.

Sockets bind with SO_EXCLUSIVEADDRUSE where available: without it Windows lets a second process bind a port
that's already listening, and connections then go to either. With it, the port doubles as a lock: a second
daemon fails to start instead of quietly sharing.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import psutil

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
SW_SHOWNOACTIVATE = 4


def write_pid(path: Path, pid: int):
    path.write_text(str(pid))


def from_file(path: Path) -> int | None:
    """The pid in a pid file if that process is still the one the file was written for, else None."""
    try:
        f = Path(path)
        pid = int(f.read_text().split()[0])
        return pid if psutil.Process(pid).create_time() <= f.stat().st_mtime + 1 else None
    except (OSError, ValueError, IndexError, psutil.Error):
        return None


def kill_tree(pid: int, timeout: float = 5):
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return
    procs = root.children(recursive=True) + [root]
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=timeout)


def bind_exclusive(port: int, host: str = "127.0.0.1") -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    s.bind((host, port))
    s.listen(16)
    s.setblocking(False)
    return s


def pythonw() -> str:
    """The windowless interpreter beside this one (falls back to this one off Windows)."""
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def ibasho_args(*args) -> list[str]:
    """`ibasho ARGS` as a background Python of its own. -P keeps the working folder off the module path, and
    `spawn` starts it in its log's folder anyway: run from the agent's life folder, `-m ibasho` would otherwise
    import an `ibasho` the agent wrote there, in place of the daemon."""
    return [pythonw(), "-P", "-m", "ibasho", *args]


OUT_MAX = 1_000_000


def quiet_startupinfo():
    """Asks a started program to show its first window without activating it (Windows applies this to the
    program's first ShowWindow call), so a background process doesn't take the person's focus. The Ibasho viewer
    makes sure of it itself (IbashoNoActivate); this also covers another viewer's first window."""
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = SW_SHOWNOACTIVATE
    return si


def spawn(args: list[str] | str, log: Path, cwd: Path | None = None, env: dict | None = None) -> int:
    """Start a background process that outlives the command that started it. Tries to leave the caller's job
    object (some harnesses kill a command's whole job when it ends), and goes without if the job forbids it.
    Its output is appended to `log`, which starts afresh once it's past OUT_MAX. Its windows, if any, start
    without taking focus. It runs in `cwd`, else in the log's folder (never the caller's, which may be the
    agent's)."""
    cwd = cwd or Path(log).parent
    flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        fresh = Path(log).stat().st_size > OUT_MAX
    except OSError:
        fresh = False
    with open(log, "wb" if fresh else "ab") as out:
        for extra in ((CREATE_BREAKAWAY_FROM_JOB, 0) if sys.platform == "win32" else (0,)):
            try:
                p = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                     creationflags=flags | extra, close_fds=True, startupinfo=quiet_startupinfo())
                return p.pid
            except PermissionError:
                if not extra:
                    raise
    raise RuntimeError("unreachable")


def alive(pid: int | None) -> bool:
    return bool(pid) and psutil.pid_exists(pid)


def env_with(**kw) -> dict:
    """The environment for one of Ibasho's own background processes (daemon, hearing, viewer, keeper). Started from
    an agent's session or not, they aren't part of it: no IBASHO_SESSION, so `ibasho` doesn't hold them to the
    agent's limits (hearing's process listens to the viewer with `hear run --pid`, which a session can't ask for)."""
    env = dict(os.environ)
    env.pop("IBASHO_SESSION", None)
    env.update({k: str(v) for k, v in kw.items()})
    return env
