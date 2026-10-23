"""Building the viewer's command line, which takes more care than it looks.

The viewer parses its raw command line itself: every backslash is literal, both ' and " are quote characters
(and either one ends a quoted stretch), and there's no way to put a quote character inside an argument. The
`--leap` value then goes through two more parsers: LLSD notation first (a value starting with t/T/f/F, 0/1, a
quote, [ or { parses as something else entirely, so a drive like T:/ would turn the command into `true`), and
then a tokenizer that splits on spaces, honours quotes, and takes backslash as its escape.

So the LEAP command is `pythonw.exe <stub>` run with the viewer's working directory set to Python's own folder:
it starts with a harmless letter, needs no quotes, and the stub's path is relative where it can be, with spaces
escaped for the tokenizer.
"""

from __future__ import annotations

import os
from pathlib import Path

NOTATION_STARTS = set("tTfF01'\"[{!irusldb")  # what LLSD notation reads as a value


class LaunchError(Exception):
    pass


def tokenizer_escape(s: str) -> str:
    """For the viewer's LEAP tokenizer: backslash escapes; no quotes allowed (the raw command line can't carry them)."""
    if "'" in s or '"' in s:
        raise LaunchError(f"a quote in {s!r} can't be passed to the viewer's --leap; move it to a path without one")
    return s.replace("\\", "\\\\").replace(" ", "\\ ").replace("\t", "\\\t")


def leap_command(python: Path, stub: Path) -> tuple[Path, str]:
    """(working directory for the viewer, --leap value)."""
    python, stub = Path(python), Path(stub)
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
