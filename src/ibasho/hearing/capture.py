"""proccap.exe from Python: listing output devices, and capture streams as 30 ms frames.

A capture's pipe is drained by its own thread into a queue, so audio keeps coming in while Whisper works (a
full pipe would stall proccap and drop sound).
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio import FRAME_BYTES, RATE

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
REFUSED = 3   # proccap's exit code for a default device


class CaptureError(Exception):
    pass


@dataclass
class Device:
    id: str
    name: str
    roles: list[str]       # the Windows default-output roles it holds: console, multimedia, communications

    @property
    def is_default(self) -> bool:
        return bool(self.roles)


def list_devices(proccap: Path) -> list[Device]:
    try:
        r = subprocess.run([str(proccap), "--list"], capture_output=True, timeout=15, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CaptureError(f"proccap --list: {e}") from None
    if r.returncode:
        raise CaptureError(f"proccap --list: {r.stderr.decode('utf-8', 'replace').strip()}")
    out = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            out.append(Device(parts[0], parts[2], [] if parts[1] == "-" else parts[1].split(",")))
    return out


def find_device(devices: list[Device], want: str) -> Device:
    """By exact id, else by a unique piece of the name."""
    w = want.strip().lower()
    for d in devices:
        if d.id.lower() == w:
            return d
    hits = [d for d in devices if w and w in d.name.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise CaptureError(f"no output device matches {want!r} (`ibasho hear devices` lists them)")
    raise CaptureError(f"{want!r} matches several devices: " + "; ".join(d.name for d in hits))


class Capture:
    """A running proccap: `frames()` yields int16 arrays of one frame each until the capture ends."""

    def __init__(self, proccap: Path, args: list[str], name: str):
        self.name = name
        self.q: queue.Queue = queue.Queue(maxsize=int(120 * RATE / (FRAME_BYTES // 2)))   # 2 minutes at most
        self.err = b""
        try:
            self.proc = subprocess.Popen([str(proccap), *args, str(RATE), "1"], stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        except OSError as e:
            raise CaptureError(f"can't start proccap: {e}") from None
        threading.Thread(target=self._drain, daemon=True, name=f"{name}-pipe").start()
        threading.Thread(target=self._errors, daemon=True, name=f"{name}-err").start()

    def _drain(self):
        out = self.proc.stdout
        while True:
            raw = out.read(FRAME_BYTES)
            if len(raw) < FRAME_BYTES:
                self.q.put(None)
                return
            try:
                self.q.put_nowait(raw)
            except queue.Full:     # the reader is far behind: drop the oldest rather than block proccap
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass
                self.q.put_nowait(raw)

    def _errors(self):
        self.err = self.proc.stderr.read()

    def frames(self):
        while (raw := self.q.get()) is not None:
            yield np.frombuffer(raw, dtype="<i2")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()

    def wait(self) -> int:
        code = self.proc.wait()
        return code

    @property
    def message(self) -> str:
        lines = [ln for ln in self.err.decode("utf-8", "replace").splitlines() if ln.strip()]
        return lines[-1] if lines else ""


def process_capture(proccap: Path, pid: int) -> Capture:
    return Capture(proccap, [str(pid)], "viewer")


def device_capture(proccap: Path, device_id: str) -> Capture:
    return Capture(proccap, ["--device", device_id], "voice")
