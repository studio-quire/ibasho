"""Building the viewer's command line, which takes more care than it looks (on Windows).

On Windows the viewer parses its raw command line itself: every backslash is literal, both ' and " are quote characters
(and either one ends a quoted stretch), and there's no way to put a quote character inside an argument. The
`--leap` value then goes through two more parsers: LLSD notation first (a value starting with t/T/f/F, 0/1, a
quote, [ or { parses as something else entirely, so a drive like T:/ would turn the command into `true`), and
then a tokenizer that splits on spaces, honours quotes, and takes backslash as its escape.

So the LEAP command is `pythonw.exe <stub>` run with the viewer's working directory set to Python's own folder:
it starts with a harmless letter, needs no quotes, and the stub's path is relative where it can be, with spaces
escaped for the tokenizer.

On Linux the viewer gets an ordinary argument list, and the `--leap` value names the interpreter by its absolute
path (LLProcess looks a bare name up on PATH there). The binary is started directly rather than through Linden
Lab's wrapper script, which doesn't `exec` it (the pid would be the script's), so the wrapper's environment
(its own lib/ on LD_LIBRARY_PATH) is set here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

NOTATION_STARTS = set("tTfF01'\"[{!irusldb")  # what LLSD notation reads as a value


class LaunchError(Exception):
    pass


def tokenizer_escape(s: str) -> str:
    """For the viewer's LEAP tokenizer: backslash escapes; no quotes allowed (the raw command line can't carry them)."""
    if "'" in s or '"' in s:
        raise LaunchError(f"a quote in {s!r} can't be passed to the viewer's --leap; move it to a path without one")
    return s.replace("\\", "\\\\").replace(" ", "\\ ").replace("\t", "\\\t")


def leap_command(python: Path, stub: Path, posix: bool | None = None) -> tuple[Path, str]:
    """(working directory for the viewer, --leap value). On Windows the viewer must run in Python's folder; on
    Linux (`posix`) the paths are absolute and the working directory is only a suggestion."""
    python, stub = Path(python), Path(stub)
    if posix if posix is not None else os.name != "nt":
        value = f"{tokenizer_escape(python.as_posix())} {tokenizer_escape(stub.as_posix())}"
        if value[0] in NOTATION_STARTS:
            raise LaunchError(f"the interpreter's path {str(python)!r} would be misread by the viewer")
        return python.parent, value
    cwd = python.parent
    try:
        script = Path(os.path.relpath(stub, cwd)).as_posix()
    except ValueError:      # another drive
        script = stub.as_posix()
    value = f"{python.name} {tokenizer_escape(script)}"
    if value[0] in NOTATION_STARTS:
        raise LaunchError(f"the interpreter's name {python.name!r} would be misread by the viewer")
    return cwd, value


def windows_cmdline(args: list[str]) -> str:
    """A raw command line the viewer's own parser splits back into `args` (the exe first, quoted the usual way)."""
    out = [f'"{args[0]}"']
    for a in args[1:]:
        if "'" in a or '"' in a:
            raise LaunchError(f"the viewer can't be given an argument with a quote in it: {a!r}")
        out.append(f'"{a}"' if (not a or any(c in a for c in " \t\r\n")) else a)
    return " ".join(out)


LINUX_BINARIES = ("bin/ibasho-viewer-bin", "bin/do-not-directly-run-secondlife-bin")


def linux_viewer(exe: Path) -> tuple[Path, Path]:
    """(the viewer's binary, its install folder) for a configured Linux viewer path: the binary itself, the folder,
    or the start script in it (Ibasho's ibasho-viewer, Linden Lab's secondlife)."""
    exe = Path(exe)
    if exe.parent.name == "bin" and exe.is_file():
        return exe, exe.parent.parent
    root = exe if exe.is_dir() else exe.parent
    names = list(LINUX_BINARIES)
    try:
        named = json.loads((root / "build_data.json").read_text(encoding="utf-8")).get("Executable")
        if isinstance(named, str) and named.startswith("bin/") and ".." not in named:
            names.insert(0, named)
    except (OSError, ValueError, AttributeError):
        pass
    for n in names:
        if (root / n).is_file():
            return root / n, root
    raise LaunchError(f"no viewer binary in {root} (looked for {', '.join(names)})")


def cpus_available() -> int:
    """The CPUs this process may use: a container's CPU quota (cgroup v2 cpu.max) if it has one, else the CPUs it
    may run on. Mesa's llvmpipe otherwise starts a drawing thread for every core of the host."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return max(1, round(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def viewer_process(exe: Path, args: list[str]) -> tuple[list[str] | str, Path, dict]:
    """How to start the viewer at `exe` with `args`: (what procs.spawn takes, its working directory, environment
    to add). `--leap` values are made with `leap_command`, whose working directory a Windows launch must use."""
    if sys.platform == "win32":
        return windows_cmdline([str(exe), *args]), Path(exe).parent, {}
    binary, root = linux_viewer(exe)
    lib = str(root / "lib")
    old = os.environ.get("LD_LIBRARY_PATH")
    env = {"LD_LIBRARY_PATH": lib + (os.pathsep + old if old else ""), "mesa_glthread": "true"}
    if "LP_NUM_THREADS" not in os.environ:
        env["LP_NUM_THREADS"] = str(cpus_available())
    if "PULSE_LATENCY_MSEC" not in os.environ:
        # Voice chat asks the sound server for ~10 ms buffers, which on a null sink with hearing recording it cost
        # 0.5 of a core; at 100 ms the sound server, the viewer's sound and its capture take 0.05
        env["PULSE_LATENCY_MSEC"] = "100"
    if "MALLOC_MMAP_THRESHOLD_" not in os.environ:
        # glibc raises its own threshold to 32 MB once big blocks are freed, after which textures and meshes come
        # from the heap and stay with the viewer after it leaves a busy place. Fixed at 128 KB, they're mapped on
        # their own and go back to the system when freed (a test grid: 1.6 GB, then 0.9 GB on leaving; without it
        # 1.9 GB, kept).
        env["MALLOC_MMAP_THRESHOLD_"] = "131072"
    return [str(binary), *args], root, env
