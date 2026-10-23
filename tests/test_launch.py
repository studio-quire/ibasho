"""The viewer command line, checked against a re-implementation of the viewer's own parsing
(LLCommandLineParser::parseCommandLineString, then LLStringUtil::getTokens for the --leap value)."""

from __future__ import annotations

from pathlib import PureWindowsPath

import pytest

from ibasho import launch


def viewer_split(raw: str) -> list[str]:
    """Backslashes doubled (so always literal), then boost::escaped_list_separator: \\ escapes, either quote
    toggles quoting, space/CR/LF separate, empty tokens dropped."""
    s = raw.replace("\\", "\\\\")
    toks, cur, quoted, i = [], "", False, 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            cur += s[i + 1]
            i += 2
            continue
        if c in "\"'":
            quoted = not quoted
        elif c in " \r\n" and not quoted:
            toks.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    toks.append(cur)
    return [t for t in toks if t]


def leap_tokens(value: str) -> list[str]:
    """getTokens(value, " \\t\\r\\n", "", "\\"'", "\\\\") as the LEAP launcher uses it."""
    toks, cur, quote, i, started = [], "", None, 0, False
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            cur += value[i + 1]
            started = True
            i += 2
            continue
        if quote:
            if c == quote:
                quote = None
            else:
                cur += c
        elif c in "\"'":
            quote, started = c, True
        elif c in " \t\r\n":
            if started:
                toks.append(cur)
            cur, started = "", False
        else:
            cur += c
            started = True
        i += 1
    if started:
        toks.append(cur)
    return toks


def args_after_exe(cmdline: str) -> list[str]:
    assert cmdline.startswith('"')
    return viewer_split(cmdline[cmdline.index('"', 1) + 1:])


def test_plain_paths(tmp_path):
    py = tmp_path / "venv" / "Scripts" / "pythonw.exe"
    stub = tmp_path / "venv" / "Lib" / "site-packages" / "ibasho" / "stub.py"
    cwd, value = launch.leap_command(py, stub)
    assert cwd == py.parent and value == "pythonw.exe ../Lib/site-packages/ibasho/stub.py"
    cmd = launch.windows_cmdline(["C:/Program Files/Viewer/viewer.exe", "--leap", value, "--grid", "x", "--autologin"])
    args = args_after_exe(cmd)
    assert args == ["--leap", value, "--grid", "x", "--autologin"]
    assert leap_tokens(args[1]) == ["pythonw.exe", "../Lib/site-packages/ibasho/stub.py"]


def test_spaces_in_the_stub_path(tmp_path):
    py = tmp_path / "a" / "Scripts" / "pythonw.exe"
    stub = tmp_path / "My Projects" / "ibasho" / "stub.py"
    cwd, value = launch.leap_command(py, stub)
    args = args_after_exe(launch.windows_cmdline(["v.exe", "--leap", value]))
    assert leap_tokens(args[1]) == ["pythonw.exe", "../../My Projects/ibasho/stub.py"]


def test_quotes_are_refused():
    with pytest.raises(launch.LaunchError):
        launch.leap_command(PureWindowsPath("C:/x/pythonw.exe"), PureWindowsPath("C:/o'neil/stub.py"))
    with pytest.raises(launch.LaunchError):
        launch.windows_cmdline(["v.exe", "--grid", "it's"])


def test_value_never_reads_as_notation(tmp_path):
    _, value = launch.leap_command(tmp_path / "pythonw.exe", tmp_path / "stub.py")
    assert value[0] not in launch.NOTATION_STARTS
