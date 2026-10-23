"""The `ibasho` command. `ibasho -h` lists everything; each command's -h says more."""

from __future__ import annotations

import argparse
import os
import sys
import traceback

from .. import __version__
from .. import config as config_mod
from ..api import ApiError, Down
from . import act, ears, hear, lifecycle, low, make, money, see, viewer
from .common import Ctx, Fail, in_session, stdout_utf8
from .. import mcp_server, recall
from ..keeper import cli as keeper
from ..tools import cli as tools

GROUPS = [
    ("lifecycle", lifecycle), ("viewer", viewer), ("seeing", see), ("hearing", hear), ("ears", ears), ("acting", act),
    ("making", make), ("money", money), ("keeper", keeper), ("memory", recall), ("tools", tools),
    ("low level", low), ("mcp", mcp_server),
]


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ibasho", description="Live in a virtual world through a LEAP-enabled viewer.")
    ap.add_argument("--config", help="config file (default: $IBASHO_CONFIG, else IBASHO_HOME/config.toml; IBASHO_HOME "
                                     "default ~/Ibasho)")
    ap.add_argument("--version", action="version", version=f"ibasho {__version__}")
    sub = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for _, mod in GROUPS:
        mod.register(sub)
    return ap


PERSON_COMMANDS = {"init", "login", "uninstall", "keeper", "viewer"}


def persons_command(args) -> str | None:
    """The person's commands, which `ibasho` refuses inside the agent's sessions (IBASHO_SESSION set), whatever the
    harness allows: setting the install up, the keeper, and anything that widens what the agent can do."""
    cmd = args.command
    if cmd in PERSON_COMMANDS:
        return f"ibasho {cmd}"
    if cmd == "selfcheck" and args.reset:
        return "ibasho selfcheck --reset"
    if cmd == "tool" and args.name in tools.PERSON_ONLY:
        return f"ibasho tool {args.name}"
    if cmd == "hear" and args.hear_command == "setup":
        return "ibasho hear setup"
    if cmd == "hear" and args.hear_command == "run" and (args.pid or args.file):
        return "ibasho hear run --pid/--file"
    if cmd == "hear" and args.hear_command in ("on", "run") and args.model and args.model not in ears.SESSION_MODELS:
        # a path or another repository's model is code and data the person hasn't chosen, and the large models
        # take gigabytes and the CPU: the person sets those in hearing.model
        return f"ibasho hear {args.hear_command} --model {args.model}"
    if args.config and config_mod.config_path(args.config) != config_mod.config_path(None):
        return "--config"   # another install's config, with other caps; the session's own is fine
    return None


def main(argv=None) -> int:
    stdout_utf8()
    args = parser().parse_args(argv)
    if in_session() and (what := persons_command(args)):
        print(f"error: `{what}` is for the person who runs Ibasho, and refused in the agent's sessions "
              "(IBASHO_SESSION is set)", file=sys.stderr)
        return 1
    if in_session() and (why := config_mod.session_config_problem(os.environ["IBASHO_SESSION"],
                                                                   config_mod.config_path(args.config))):
        print(f"error: {why}", file=sys.stderr)
        return 1
    ctx = Ctx(args)
    try:
        args.fn(ctx, args)
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Down as e:
        print(f"error: {e}; `ibasho up` starts it", file=sys.stderr)
        return 1
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:   # one line for a model to read; the traceback on request
        print(f"error: {expected_error(e) or f'{type(e).__name__}: {e} (unexpected: a bug in ibasho?)'}",
              file=sys.stderr)
        if os.environ.get("IBASHO_DEBUG"):
            traceback.print_exc()
        elif not expected_error(e):
            print("(IBASHO_DEBUG=1 shows the traceback)", file=sys.stderr)
        return 1
    return 0


def expected_error(e: Exception) -> str | None:
    """Failures that come from what was asked rather than from ibasho: missing files, unreadable values."""
    if isinstance(e, FileNotFoundError):
        return f"no such file: {e.filename}"
    if isinstance(e, IsADirectoryError):
        return f"that's a folder, not a file: {e.filename}"
    if isinstance(e, PermissionError):
        return f"not allowed to read or write {e.filename}"
    if isinstance(e, OSError) and e.filename:
        return f"{e.strerror or e}: {e.filename}"
    if isinstance(e, UnicodeDecodeError):
        return f"not a text file (UTF-8): {e.reason}"
    if isinstance(e, ValueError):
        return f"bad value: {e}"
    return None
