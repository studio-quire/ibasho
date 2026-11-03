"""The agent's viewer never takes the person's focus: what the launcher asks of Windows and of the viewer. (The
viewer's own part, IbashoNoActivate, is in the viewer's patch series.)"""

from __future__ import annotations

import argparse
import sys

import psutil
import pytest

from ibasho import procs
from ibasho.cli import lifecycle
from ibasho.cli.common import Ctx

from conftest import write_config

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows")

# Prints (STARTF_USESHOWWINDOW set, wShowWindow) from the process's own STARTUPINFO.
REPORT = """
import ctypes
from ctypes import wintypes as w
class SI(ctypes.Structure):
    _fields_ = ([("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR)]
                + [(n, w.DWORD) for n in ("x", "y", "xs", "ys", "xc", "yc", "fill", "flags")]
                + [("show", w.WORD), ("cb2", w.WORD), ("reserved2", ctypes.c_void_p),
                   ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)])
si = SI()
si.cb = ctypes.sizeof(SI)
ctypes.windll.kernel32.GetStartupInfoW(ctypes.byref(si))
print(si.flags & 1, si.show)
"""


@windows_only
def test_spawned_programs_are_asked_not_to_activate(tmp_path):
    out = tmp_path / "si.out"
    # the real interpreter: a venv's python.exe is a launcher that starts it with a STARTUPINFO of its own
    python = getattr(sys, "_base_executable", None) or sys.executable
    pid = procs.spawn([python, "-c", REPORT], out)
    try:
        psutil.Process(pid).wait(20)
    except psutil.NoSuchProcess:
        pass
    assert out.read_text().split() == ["1", str(procs.SW_SHOWNOACTIVATE)]


@windows_only
def test_the_agents_viewer_is_told_never_to_activate(home, monkeypatch):
    exe = home / "viewer" / "IbashoViewer.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    cfg = write_config(home, viewer=str(exe))
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.token_file.write_text("token")
    started = []
    monkeypatch.setattr(procs, "spawn", lambda cmdline, *a, **kw: started.append(cmdline) or 4242)
    lifecycle.launch_viewer(Ctx(argparse.Namespace(config=str(cfg.path))))
    assert started and started[0].endswith("--set IbashoNoActivate 1")
