"""`ibasho viewer`: installing the Ibasho viewer from a release zip, and updating it in place.

Windows' firewall remembers its answer per program path, so a viewer started from a new folder brings up the
"allow this app" prompt again, for the person to answer. An update therefore goes into the same folder as the
viewer it replaces: unpacked beside it first and checked, then swapped in, with the old copy kept until the new
one has started (`ibasho up` clears it; `ibasho viewer rollback` goes back to it). All of it is the person's:
refused in the agent's sessions.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import time
import zipfile
from pathlib import Path

import psutil

from .. import config as config_mod
from .common import Ctx, Fail

TOP = re.compile(r"^Ibasho-(\d+(?:\.\d+){3})-windows-x64$")
MAX_UNPACKED = 4 << 30   # far over a real release (about 400 MB)
FIREWALL = ("Tell the person before the viewer next starts: Windows will ask whether to allow {exe} through its "
            "firewall, since it runs from a new place. Allowing it is what the viewer needs (it talks to the grid "
            "and to Ibasho on this machine).")


def register(sub):
    p = sub.add_parser("viewer", help="for the person: install the Ibasho viewer from a release zip, or update it in "
                                      "place (the same folder, so Windows' firewall permission carries over)")
    vs = p.add_subparsers(dest="viewer_command", required=True, metavar="ACTION")
    q = vs.add_parser("install", help="unpack a release zip into a folder of its own and point the config at it")
    q.add_argument("zip", help="Ibasho-VERSION-windows-x64.zip, as downloaded from the release page")
    q.add_argument("--to", help="the folder for it (default: viewer\\ in the install's home); it mustn't exist yet, "
                                "or must be empty")
    q.set_defaults(fn=install)
    q = vs.add_parser("update", help="replace the configured viewer with a newer release, in the same folder")
    q.add_argument("zip", help="Ibasho-VERSION-windows-x64.zip")
    q.add_argument("--force", action="store_true", help="also for the same or an older version")
    q.set_defaults(fn=update)
    vs.add_parser("rollback", help="go back to the viewer an update replaced (kept until the new one has started)"
                  ).set_defaults(fn=rollback)
    vs.add_parser("status", help="the configured viewer's folder and version, and any copy an update kept"
                  ).set_defaults(fn=status)


# ---------- what a viewer folder is ----------

def build_data(folder: Path) -> dict | None:
    """An Ibasho viewer folder's build_data.json (version, channel, exe), or None if it isn't one."""
    try:
        d = json.loads((folder / "build_data.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("Channel") != "Ibasho" or not isinstance(d.get("Version"), str):
        return None
    exe = d.get("Executable")
    if not isinstance(exe, str) or not exe.lower().endswith(".exe") or "/" in exe or "\\" in exe:
        return None
    return d


def version_key(v: str) -> tuple:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def unpack(zip_path: Path, beside: Path) -> tuple[Path, dict]:
    """Unpack a release zip into a new folder beside `beside` (same drive, so the swap is a rename) and check it's
    an Ibasho viewer: one top folder Ibasho-VERSION-windows-x64 holding build_data.json for that version, its exe
    and the README. Returns (the unpacked viewer folder, its build data); removes what it unpacked on failure."""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise Fail(f"no such file: {zip_path}")
    work = beside.parent / f".{beside.name}-unpacking-{secrets.token_hex(4)}"
    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            tops = {i.filename.replace("\\", "/").split("/", 1)[0] for i in infos}
            m = TOP.match(next(iter(tops))) if len(tops) == 1 else None
            if not m:
                raise Fail(f"{zip_path.name} isn't an Ibasho viewer release (expected one folder "
                           "Ibasho-VERSION-windows-x64 inside)")
            if sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise Fail(f"{zip_path.name} unpacks to more than a viewer could; not unpacking it")
            for i in infos:
                name = i.filename.replace("\\", "/")
                parts = name.split("/")
                if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in parts or "" in parts[1:]:
                    raise Fail(f"{zip_path.name} holds a file outside its folder ({i.filename}); not unpacking it")
                if stat.S_ISLNK(i.external_attr >> 16):
                    raise Fail(f"{zip_path.name} holds a link ({i.filename}); not unpacking it")
            work.mkdir(parents=True)
            z.extractall(work)
    except zipfile.BadZipFile:
        shutil.rmtree(work, ignore_errors=True)
        raise Fail(f"{zip_path.name} isn't a readable zip") from None
    except Fail:
        shutil.rmtree(work, ignore_errors=True)
        raise
    except OSError as e:
        shutil.rmtree(work, ignore_errors=True)
        raise Fail(f"couldn't unpack {zip_path.name}: {e}") from None
    folder = work / m.group(0)
    data = build_data(folder)
    problem = None
    if data is None:
        problem = "it has no Ibasho build_data.json"
    elif data["Version"] != m.group(1):
        problem = f"its build data says version {data['Version']}, its folder {m.group(1)}"
    elif not (folder / data["Executable"]).is_file():
        problem = f"{data['Executable']} is missing"
    else:
        try:
            first = (folder / "README.txt").read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError, UnicodeDecodeError):
            first = ""
        if not first.startswith(f"Ibasho {data['Version']}:"):
            problem = "its README.txt isn't Ibasho's"
    if problem:
        shutil.rmtree(work, ignore_errors=True)
        raise Fail(f"{zip_path.name} doesn't look like an Ibasho viewer release: {problem}")
    return folder, data


def running_from(folder: Path) -> list[str]:
    """Programs running from inside `folder` (the viewer, its web and media helpers)."""
    found = []
    for p in psutil.process_iter(["exe", "name"]):
        exe = p.info.get("exe")
        if exe and config_mod.inside(exe, folder):
            found.append(f"{p.info.get('name') or Path(exe).name} (pid {p.pid})")
    return found


def rename(src: Path, dst: Path):
    """A folder rename, tried a few times: Windows refuses one while anything has a file inside open, and a virus
    scanner looking at new files does that for a moment."""
    for wait in (0.2, 0.5, 1, 2, 3, None):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if wait is None:
                raise
            time.sleep(wait)


def marker(cfg) -> Path:
    return cfg.state_dir / "viewer-update.json"


def kept(cfg) -> dict:
    try:
        d = json.loads(marker(cfg).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("previous") else {}
    except (OSError, ValueError):
        return {}


def remove_viewer_folder(folder: Path):
    """Delete a folder only if it's an Ibasho viewer's."""
    if folder.is_dir() and build_data(folder) is not None:
        shutil.rmtree(folder, ignore_errors=True)


def set_aside(folder: Path, what: str) -> Path:
    """Rename a folder out of the way (a rename either happens or doesn't; deleting could stop halfway)."""
    aside = folder.with_name(f".{folder.name}-{what}-{secrets.token_hex(4)}")
    rename(folder, aside)
    return aside


def finish_update(cfg) -> str | None:
    """The updated viewer has started: delete the copy the update kept. Returns a line saying so, if there was one.
    Only for the viewer the config names: a record about another folder is left as it is."""
    k = kept(cfg)
    if not k or not cfg.viewer_path:
        return None
    previous = Path(k["previous"])
    folder = Path(cfg.viewer_path).parent
    if os.path.normcase(str(previous)) != os.path.normcase(str(folder.with_name(folder.name + ".previous"))):
        return None
    remove_viewer_folder(previous)
    marker(cfg).unlink(missing_ok=True)
    return f"viewer {k.get('version') or ''} started; the previous one ({k.get('previous_version') or '?'}) deleted"


# ---------- the commands ----------

def install(ctx: Ctx, a):
    try:
        cfg = ctx.cfg
    except Fail:
        cfg = None
    dest = Path(a.to) if a.to else (cfg.home if cfg else config_mod.home()) / "viewer"
    dest = Path(os.path.abspath(dest))
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        hint = " (it holds an Ibasho viewer: `ibasho viewer update ZIP` replaces it in place)" \
            if build_data(dest) else ""
        raise Fail(f"{dest} exists and isn't empty{hint}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    folder, data = unpack(Path(a.zip), dest)
    if dest.exists():
        dest.rmdir()
    rename(folder, dest)
    shutil.rmtree(folder.parent, ignore_errors=True)
    exe = dest / data["Executable"]
    print(f"installed Ibasho viewer {data['Version']} in {dest}")
    if cfg is not None:
        old = cfg.viewer_path
        config_mod.set_viewer_path(cfg.path, exe)
        print(f"viewer.path in {cfg.path} is now {exe}")
        if old and os.path.normcase(os.path.abspath(old)) != os.path.normcase(str(exe)):
            print(FIREWALL.format(exe=exe))
    else:
        print(f"next: `ibasho init --viewer \"{exe}\"` (with --name and --grid)")
    print("Its first start (`ibasho login`, with the person there to log in) brings up Windows' firewall prompt for "
          f"{exe.name}: the person allows it. Updates go into this same folder, so it won't ask again.")


def update(ctx: Ctx, a):
    cfg = ctx.cfg
    exe = Path(cfg.viewer_path) if cfg.viewer_path else None
    if exe is None or not exe.is_file():
        raise Fail(f"viewer.path {cfg.viewer_path!r} isn't a viewer to update; `ibasho viewer install ZIP` installs one")
    folder = exe.parent
    current = build_data(folder)
    if current is None:
        raise Fail(f"{folder} isn't an Ibasho viewer folder (no build_data.json for the Ibasho channel); updates "
                   "replace only Ibasho viewers")
    if busy := running_from(folder):
        raise Fail(f"the viewer is running ({', '.join(busy)}): `ibasho down` first (and `ibasho keeper stop` if the "
                   "keeper runs it)")
    new_folder, data = unpack(Path(a.zip), folder)
    new_exe = folder / data["Executable"]
    renamed = new_exe.name.lower() != exe.name.lower()
    discard = None
    try:
        if version_key(data["Version"]) <= version_key(current["Version"]) and not a.force:
            raise Fail(f"the zip holds {data['Version']}, and {current['Version']} is installed: --force to install it "
                       "anyway")
        if renamed:   # the config must take the new program's path before anything moves
            try:
                config_mod.set_viewer_path(cfg.path, new_exe, check_only=True)
            except config_mod.ConfigError as e:
                raise Fail(f"{e}; nothing changed") from None
        previous = folder.with_name(folder.name + ".previous")
        k = kept(cfg)
        if k and Path(k["previous"]) == previous and previous.is_dir():
            # an update that never started is being replaced: keep the copy from before it, drop this one
            try:
                discard = set_aside(folder, "replaced")
            except OSError as e:
                raise Fail(f"couldn't move the current viewer aside ({e}): something has a file in {folder} open; "
                           "nothing changed") from None
        else:
            remove_viewer_folder(previous)
            if previous.exists():
                raise Fail(f"{previous} is in the way (not an Ibasho viewer folder, so it's left alone): move it")
            try:
                rename(folder, previous)
            except OSError as e:
                raise Fail(f"couldn't move the current viewer aside ({e}): something has a file in {folder} open "
                           "(a window showing the folder, the viewer itself); nothing changed") from None
            k = {"previous": str(previous), "previous_version": current["Version"]}
        try:
            rename(new_folder, folder)
        except OSError as e:
            if not folder.exists():   # put back what was there
                rename(discard or previous, folder)
                if not discard:
                    marker(cfg).unlink(missing_ok=True)
                discard = None
            raise Fail(f"couldn't put the new viewer in place ({e}); the viewer that was there is back") from None
    finally:
        shutil.rmtree(new_folder.parent, ignore_errors=True)
    if discard:
        shutil.rmtree(discard, ignore_errors=True)
    k["version"] = data["Version"]
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    marker(cfg).write_text(json.dumps(k), encoding="utf-8")
    print(f"viewer {current['Version']} -> {data['Version']} in {folder} (the same folder). The old copy stays in "
          f"{k['previous']} until the new one has started (`ibasho up` then deletes it; `ibasho viewer rollback` "
          "goes back)")
    if renamed:
        config_mod.set_viewer_path(cfg.path, new_exe)
        print(f"the viewer's program is now {new_exe.name}; viewer.path updated. " + FIREWALL.format(exe=new_exe))


def rollback(ctx: Ctx, a):
    cfg = ctx.cfg
    k = kept(cfg)
    previous = Path(k["previous"]) if k else None
    if previous is None or build_data(previous) is None:
        raise Fail("no previous viewer kept (an update keeps it only until the new viewer has started)")
    folder = previous.with_name(previous.name.removesuffix(".previous"))
    if busy := running_from(folder):
        raise Fail(f"the viewer is running ({', '.join(busy)}): `ibasho down` first")
    newer = set_aside(folder, "rolled-back") if folder.exists() else None
    try:
        rename(previous, folder)
    except OSError as e:
        if newer:
            rename(newer, folder)
        raise Fail(f"couldn't move {previous} back ({e}); the viewer is as it was") from None
    if newer:
        shutil.rmtree(newer, ignore_errors=True)
    marker(cfg).unlink(missing_ok=True)
    data = build_data(folder) or {}
    exe = folder / data.get("Executable", "IbashoViewer.exe")
    if os.path.normcase(str(exe)) != os.path.normcase(os.path.abspath(cfg.viewer_path or "")):
        config_mod.set_viewer_path(cfg.path, exe)
        print(f"viewer.path is {exe} again")
    print(f"back to viewer {data.get('Version') or '?'} in {folder}")


def status(ctx: Ctx, a):
    cfg = ctx.cfg
    exe = Path(cfg.viewer_path) if cfg.viewer_path else None
    if exe is None or not exe.is_file():
        print(f"no viewer at viewer.path ({cfg.viewer_path!r})")
        return
    data = build_data(exe.parent)
    print(f"{exe}: " + (f"Ibasho viewer {data['Version']}" if data else "not an Ibasho viewer (no Ibasho build data)"))
    if k := kept(cfg):
        print(f"kept from the last update, until the new viewer starts: {k['previous_version']} in {k['previous']}")
