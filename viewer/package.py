"""Make the Windows release zip of a built Ibasho viewer.

    python viewer/package.py [--src TREE] [--out DIR]

The build folder (`build-vc170-64/newview/Release`) runs as it is, but it also holds what the build leaves
behind: `secondlife-bin.exe` and its `.pdb`/`.map`/`.lib`, installer scripts, batch files. So the release is made
from Linden Lab's own manifest (`indra/newview/viewer_manifest.py`, the list of files their installer ships),
copied into a fresh folder, plus the licence texts and a README, then zipped: `Ibasho-<version>-windows-x64.zip`.

Needs the Python of the viewer build (`work/ab-venv`, for the `llsd` module); started with another one, it
switches to that one by itself. Run `configure.sh` and `build.ps1` first, so the version and binaries are current.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = "https://github.com/Shoalstone/ibasho"
VIEWER_SOURCE = "https://github.com/Shoalstone/ibasho-viewer"
TRACKER = REPO + "/issues"
ZIP_EPOCH = 315532800 + 86400   # 1980-01-02 UTC: the earliest time a zip entry holds, with a day to spare
NOT_SHIPPED = ("secondlife-bin.*", "*.pdb", "*.map", "*.lib", "*.exp", "*.nsi", "*.bat", "*_Setup.exe")


def work_dir() -> Path:
    return Path(os.environ.get("IBASHO_VIEWER_WORK") or HERE / "work")


def ensure_build_python():
    try:
        import llsd  # noqa: F401
        return
    except ImportError:
        pass
    py = work_dir() / "ab-venv" / "Scripts" / "python.exe"
    if not py.is_file():
        sys.exit("package.py needs the `llsd` module: run it with the viewer build's Python (work/ab-venv), "
                 "or pip install llsd")
    sys.exit(subprocess.call([str(py), *sys.argv]))


README = """\
Ibasho {version}: a modified version of Linden Lab's viewer, for the Second Life® world
Made for AI agents. Windows, 64-bit.

This software is not provided or supported by Linden Lab, the makers of Second Life. Ibasho comes without
support either: reports are welcome at {tracker},
but nobody promises an answer or a fix.

It is Linden Lab's open-source viewer with Ibasho's patches: LEAP APIs so a program can do what a resident does,
and limits on spending and messages. The Ibasho tooling (the `ibasho` command) starts it for you; the viewer's own
README in the Ibasho repository has the details.

What to tell people, as Linden Lab's Third Party Viewer Policy asks:
- a program can control this viewer completely over LEAP;
- spectator mode is on at every launch: the local mouse and keyboard don't reach the viewer;
- it can pay L$, buy objects and pay upload fees without a confirmation dialog, within its caps
  (IbashoMaxPayment, IbashoMaxSessionSpend, IbashoMaxUploadCost);
- hideAvatars leaves chosen avatars out of what the window shows (locally, as "Do not render" does);
- streamUI keeps other people's words off the window.

Files: settings, logs and chat logs go to %APPDATA%\\Ibasho, the cache to %LOCALAPPDATA%\\Ibasho.
Uninstalling: delete this folder, %APPDATA%\\Ibasho and %LOCALAPPDATA%\\Ibasho. Nothing else is installed.

Source: the viewer is under the GNU Lesser General Public License 2.1 (licenses\\LGPL-2.1.txt). Its complete
source, Linden Lab's viewer with Ibasho's changes, is at
  {viewer_source} (tag v{version})
and the same changes are a patch series in the Ibasho repository,
  {repo} (viewer/BASE names Linden Lab's commit, viewer/patches holds the changes).
Third-party licences: licenses.txt. OpenAL Soft and freealut (OpenAL32.dll, alut.dll) are unmodified LGPL
libraries; their source is at https://github.com/secondlife/3p-openal-soft, tag v1.24.2-r1
(licenses\\openal-soft.txt, licenses\\freealut.txt).

Second Life(R) is a trademark of Linden Research, Inc. Ibasho is not affiliated with or endorsed by Linden
Research, Inc.
"""


WORD_LISTS = ("*.dic", "*.aff")  # spell-check dictionaries: a user name that's a word is in them on any machine


def _contains_word(data: bytes, needle: bytes, unit: int) -> bool:
    """needle in data, not run together with letters or digits (unit: 1 for UTF-8, 2 for UTF-16-LE)."""
    def wordish(i):
        if i < 0 or i + unit > len(data):
            return False
        c = data[i:i + unit]
        return (c[:1].isalnum() and c[:1].isascii()) and (unit == 1 or c[1:] == b"\0")
    i = data.find(needle)
    while i >= 0:
        if not wordish(i - unit) and not wordish(i + len(needle)):
            return True
        i = data.find(needle, i + 1)
    return False


def machine_names_in(stage: Path) -> list:
    """Every staged file that names this machine: the user profile's path (either slash), or the user name or
    computer name as a word of its own (so "jo" in "Jones" doesn't count), as UTF-8 or UTF-16 and in any
    case. Returns [(relative path, what it contains)]."""
    profile = os.environ.get("USERPROFILE", "")
    needles = []  # (what, bytes, unit, whole word)
    if profile:
        for form in {profile.replace("/", "\\"), profile.replace("\\", "/")}:
            needles += [(f"the path {profile}", form.lower().encode(enc), unit, False)
                        for enc, unit in (("utf-8", 1), ("utf-16-le", 2))]
    for var, what in (("USERNAME", "the user name"), ("COMPUTERNAME", "the computer name")):
        name = os.environ.get(var, "")
        if len(name) >= 3:
            needles += [(f"{what} {name}", name.lower().encode(enc), unit, True)
                        for enc, unit in (("utf-8", 1), ("utf-16-le", 2))]
    hits = []
    for p in sorted(stage.rglob("*")):
        if not p.is_file():
            continue
        data = p.read_bytes().lower()
        word_list = any(p.match(g) for g in WORD_LISTS)
        found = sorted({what for what, needle, unit, whole in needles
                        if (_contains_word(data, needle, unit) if whole else needle in data)
                        and not (whole and word_list)})
        if found:
            hits.append((p.relative_to(stage).as_posix(), ", ".join(found)))
    return hits


def run_manifest(src: Path, dest: Path, log: Path) -> list:
    """Linden Lab's manifest with its copy action, as when it packages the installer (not the build's partial
    copy), into dest. Returns its file list."""
    newview = src / "indra" / "newview"
    build = src / "build-vc170-64" / "newview"
    sys.path.insert(0, str(newview))
    import viewer_manifest as vm
    vm.ViewerManifest.is_packaging_viewer = lambda self: True
    extra = [dict(name=n, description="", default=d) for n, d in
             (("bugsplat", ""), ("discord", "OFF"), ("openal", "OFF"), ("tracy", "OFF"), ("velopack", "OFF"))]
    argv = ["viewer_manifest.py", "--actions=copy", "--arch=x86_64", f"--artwork={newview}", "--bugsplat=",
            "--discord=", "--openal=ON", "--tracy=OFF", "--velopack=OFF", f"--build={build}",
            "--buildtype=Release", "--channel=Ibasho", "--configuration=Release", f"--dest={dest}", "--grid=agni",
            f"--source={newview}", f"--versionfile={build / 'viewer_version.txt'}"]
    files: list = []
    real_do = vm.LLManifest.do

    def do(self, *actions):
        out = real_do(self, *actions)
        files.extend(out)
        return out
    vm.LLManifest.do = do
    old_argv, old_cwd = sys.argv, os.getcwd()
    try:
        sys.argv = argv
        os.chdir(build)
        with open(log, "w", encoding="utf-8") as f, contextlib.redirect_stdout(f):
            vm.main(extra=extra)
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)
    return files


def main():
    ap = argparse.ArgumentParser(description="Make the Windows release zip of a built Ibasho viewer.")
    ap.add_argument("--src", help="the patched viewer tree (default: work/viewer-src)")
    ap.add_argument("--out", help="where the zip goes (default: work/dist)")
    ap.add_argument("--allow-profile-paths", action="store_true",
                    help="package even if shipped files name the user profile, the user or the computer (a zip "
                         "for this machine only)")
    a = ap.parse_args()
    ensure_build_python()
    src = Path(a.src).resolve() if a.src else work_dir() / "viewer-src"
    out = Path(a.out).resolve() if a.out else work_dir() / "dist"
    build = src / "build-vc170-64"
    version = (build / "newview" / "viewer_version.txt").read_text().strip()
    if not (build / "newview" / "Release" / "secondlife-bin.exe").is_file():
        sys.exit(f"no built viewer in {build / 'newview' / 'Release'}: run build.ps1 first")
    name = f"Ibasho-{version}-windows-x64"
    stage = out / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    log = out / f"{name}-manifest.log"
    files = run_manifest(src, stage, log)

    shipped = [p for p in stage.rglob("*") if p.is_file()]
    bad = [p for p in shipped if any(p.match(g) for g in NOT_SHIPPED)]
    if bad or not (stage / "IbashoViewer.exe").is_file():
        sys.exit(f"the staged viewer looks wrong (missing IbashoViewer.exe, or build leftovers: {bad[:5]}); "
                 f"see {log}")

    lic = stage / "licenses"
    lic.mkdir()
    shutil.copyfile(src / "doc" / "LGPL-license.txt", lic / "LGPL-2.1.txt")
    for n in ("openal-soft.txt", "freealut.txt"):
        shutil.copyfile(build / "packages" / "LICENSES" / n, lic / n)
    (stage / "README.txt").write_text(README.format(version=version, tracker=TRACKER, repo=REPO, viewer_source=VIEWER_SOURCE).replace("\n", "\r\n"),
                                      encoding="utf-8", newline="")

    if not a.allow_profile_paths:
        hits = machine_names_in(stage)
        if hits:
            for rel, what in hits[:20]:
                print(f"  {rel}: {what}", file=sys.stderr)
            sys.exit(f"{len(hits)} shipped files name this machine or its user (above). The compiler writes source "
                     "paths into the binaries, so build release binaries in a folder outside the user profile "
                     "(IBASHO_VIEWER_WORK, e.g. C:\\ibasho-work), or pass --allow-profile-paths for a zip that stays "
                     "on this machine")

    zpath = out / f"{name}.zip"
    zpath.unlink(missing_ok=True)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(p for p in stage.rglob("*") if p.is_file()):
            # times in UTC: a zip stores local time without a zone, which would tell anyone this machine's
            info = zipfile.ZipInfo(f"{name}/{p.relative_to(stage).as_posix()}",
                                   date_time=time.gmtime(max(p.stat().st_mtime, ZIP_EPOCH))[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with p.open("rb") as src, z.open(info, "w") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
    n = sum(1 for p in stage.rglob("*") if p.is_file())
    print(f"{zpath}: {n} files ({len(files)} from the manifest), {zpath.stat().st_size / 1e6:.0f} MB")
    print(f"staged in {stage} (the manifest's log: {log})")


if __name__ == "__main__":
    main()
