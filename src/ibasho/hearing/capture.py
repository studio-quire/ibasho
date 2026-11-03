"""Capturing sound as 30 ms frames: proccap.exe on Windows, the sound server's `parec` on Linux.

Windows: proccap captures one process tree (the viewer's) or one output device. Linux: the viewer plays on a
sink of Ibasho's own (WORLD_SINK; the container makes it, docs/linux.md) and `parec` records that sink's monitor,
so nothing else the machine plays is heard; voice chat goes to another sink, recorded the same way.

A capture's pipe is drained by its own thread into a queue, so audio keeps coming in while Whisper works (a
full pipe would stall the capture and drop sound).
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio import FRAME_BYTES, RATE

WINDOWS = sys.platform == "win32"
NO_WINDOW = 0x08000000 if WINDOWS else 0
REFUSED = 3   # proccap's exit code for a default device (the Linux capture ends with it too)
WORLD_SINK = "ibasho_world"   # Linux: the sink the viewer plays on (docker/pulse.pa makes it)
VOICE_SINK = "ibasho_voice"   # Linux: the sink voice chat plays on, recorded only if hearing.voice_device names it
VOICE_ENGINE = "WEBRTC VoiceEngine"   # the application name the viewer's voice streams carry


class CaptureError(Exception):
    pass


@dataclass
class Device:
    id: str
    name: str
    roles: list[str]       # default-output roles it holds: Windows' console, multimedia, communications; "default"

    @property
    def is_default(self) -> bool:
        return bool(self.roles)

    @property
    def viewer_id(self) -> str:
        """What the viewer's VoiceOutputAudioDevice calls it: Windows' device id; on Linux, voice (WebRTC) names a
        sink by its description."""
        return self.id if WINDOWS else self.name


def list_devices(tool: Path) -> list[Device]:
    """Output devices: Windows' through proccap, the sound server's sinks on Linux."""
    if not WINDOWS:
        return _sinks(tool)
    try:
        r = subprocess.run([str(tool), "--list"], capture_output=True, timeout=15, creationflags=NO_WINDOW)
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


# ---- Linux: the sound server (PulseAudio, or PipeWire's pulse server) ----

def _pactl(parec: Path, *args: str) -> str:
    pactl = parec.with_name("pactl") if parec.with_name("pactl").exists() else shutil.which("pactl")
    if not pactl:
        raise CaptureError("pactl not found (the sound server's tools: pulseaudio-utils)")
    try:
        r = subprocess.run([str(pactl), *args], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CaptureError(f"pactl: {e}") from None
    if r.returncode:
        err = r.stderr.decode("utf-8", "replace").strip()
        raise CaptureError(f"no sound server ({err})" if "onnect" in err else f"pactl {' '.join(args)}: {err}")
    return r.stdout.decode("utf-8", "replace")


def _sinks(parec: Path) -> list[Device]:
    default = _pactl(parec, "get-default-sink").strip()
    try:
        sinks = [(s["name"], s.get("description") or s["name"]) for s in json.loads(_pactl(parec, "-f", "json",
                                                                                        "list", "sinks"))]
    except (CaptureError, ValueError, KeyError, TypeError):   # an older pactl without JSON
        sinks = [(ln.split("\t")[1], ln.split("\t")[1]) for ln in _pactl(parec, "list", "short", "sinks").splitlines()
                 if "\t" in ln]
    return [Device(n, d, ["default"] if n == default else []) for n, d in sinks]


def sink_exists(parec: Path, name: str) -> bool:
    return any(d.id == name for d in _sinks(parec))


def voice_device_for_viewer(parec: Path | None) -> str | None:
    """The viewer's name for VOICE_SINK, if the sound server has it: `ibasho up` points voice chat there, so voice
    never plays on the world sink the viewer ear records."""
    try:
        return next((d.viewer_id for d in _sinks(parec) if d.id == VOICE_SINK), None) if parec else None
    except CaptureError:
        return None


def keep_voice_off_world(parec: Path) -> None:
    """Voice streams found on WORLD_SINK (the viewer's voice output changed after `ibasho up` pointed it away) are
    moved to VOICE_SINK, or muted without one: voice is heard only when hearing.voice_device opts in."""
    sinks = {s["name"]: s["index"] for s in json.loads(_pactl(parec, "-f", "json", "list", "sinks"))}
    world = sinks.get(WORLD_SINK)
    for si in json.loads(_pactl(parec, "-f", "json", "list", "sink-inputs")):
        if si.get("sink") == world and (si.get("properties") or {}).get("application.name") == VOICE_ENGINE:
            if VOICE_SINK in sinks:
                _pactl(parec, "move-sink-input", str(si["index"]), VOICE_SINK)
            else:
                _pactl(parec, "set-sink-input-mute", str(si["index"]), "1")


class Capture:
    """A running capture: `frames()` yields int16 arrays of one frame each until the capture ends. `guard`, if
    given, is asked every few seconds whether to go on: it returns None, or an exit code to end the capture with."""

    def __init__(self, argv: list[str], name: str, guard=None, every: float = 5.0):
        self.name = name
        self.q: queue.Queue = queue.Queue(maxsize=int(120 * RATE / (FRAME_BYTES // 2)))   # 2 minutes at most
        self.err = b""
        self.code: int | None = None
        try:
            self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except OSError as e:
            raise CaptureError(f"can't start {Path(argv[0]).name}: {e}") from None
        threading.Thread(target=self._drain, daemon=True, name=f"{name}-pipe").start()
        threading.Thread(target=self._errors, daemon=True, name=f"{name}-err").start()
        if guard:
            threading.Thread(target=self._guard, args=(guard, every), daemon=True, name=f"{name}-guard").start()

    def _drain(self):
        out = self.proc.stdout
        while True:
            raw = out.read(FRAME_BYTES)
            if len(raw) < FRAME_BYTES:
                self.q.put(None)
                return
            try:
                self.q.put_nowait(raw)
            except queue.Full:     # the reader is far behind: drop the oldest rather than block the capture
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass
                self.q.put_nowait(raw)

    def _errors(self):
        self.err = self.proc.stderr.read()

    def _guard(self, guard, every):
        while self.proc.poll() is None:
            time.sleep(every)
            try:
                code = guard()
            except Exception:
                code = None
            if code is not None:
                self.code = code
                self.stop()
                return

    def frames(self):
        while (raw := self.q.get()) is not None:
            yield np.frombuffer(raw, dtype="<i2")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()

    def wait(self) -> int:
        code = self.proc.wait()
        return self.code if self.code is not None else code

    @property
    def message(self) -> str:
        lines = [ln for ln in self.err.decode("utf-8", "replace").splitlines() if ln.strip()]
        return lines[-1] if lines else ""


def _parec(parec: Path, sink: str) -> list[str]:
    return [str(parec), "-d", f"{sink}.monitor", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1",
            "--latency-msec=60", "--client-name=ibasho-hearing"]


def process_capture(tool: Path, pid: int) -> Capture:
    """The viewer's sound: its process tree on Windows; on Linux its sink, until the viewer process ends."""
    if WINDOWS:
        return Capture([str(tool), str(pid), str(RATE), "1"], "viewer")
    if not sink_exists(tool, WORLD_SINK):
        raise CaptureError(f"the sound server has no {WORLD_SINK} sink for the viewer to play on (docs/hearing.md)")
    from .. import procs

    def guard():
        if not procs.alive(pid):
            return 0
        keep_voice_off_world(tool)
        return None
    keep_voice_off_world(tool)
    return Capture(_parec(tool, WORLD_SINK), "viewer", guard=guard, every=2)


def device_capture(tool: Path, device_id: str) -> Capture:
    """One whole output device; it ends with REFUSED if the device becomes a default output."""
    if WINDOWS:
        return Capture([str(tool), "--device", device_id, str(RATE), "1"], "voice")

    def still_spare():
        return REFUSED if _pactl(tool, "get-default-sink").strip() == device_id else None
    return Capture(_parec(tool, device_id), "voice", guard=still_spare, every=10)
