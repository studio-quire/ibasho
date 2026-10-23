"""The Python tools run on: an embeddable CPython from python.org, pinned by hash, unpacked beside config.toml.

Ibasho-owned, so the access entries the sandbox needs never touch the person's own Python. Packages go into its
site-packages through `ibasho tool install`, run by the person: pip (the one Ibasho itself runs on) fetches wheels
only, built for the runtime's version, so installing runs no package code.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PYTHON = "3.14.9"
TAG = "314"
DOWNLOADS = {
    # machine: (file, sha256), checked against python.org's SPDX records for the release
    "amd64": ("python-3.14.9-embed-amd64.zip", "3c01d7cfc666f746a6f064f60fb2156ab0ec464d3d738ab796371a46a9bb24a2"),
    "arm64": ("python-3.14.9-embed-arm64.zip", "a8212664f7622ebc0cb0f9a42d7beee36cebedab97ff47c6bba668cfd85b676d"),
}
URL = "https://www.python.org/ftp/python/{version}/{file}"
PTH = f"python{TAG}.zip\n.\nsite-packages\nimport site\n"


class SetupError(Exception):
    pass


def machine() -> str:
    m = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(m, m)


def python_dir(rdir: Path) -> Path:
    return rdir / "python"


def python_exe(rdir: Path) -> Path:
    return python_dir(rdir) / "python.exe"


def site_dir(rdir: Path) -> Path:
    return python_dir(rdir) / "site-packages"


def state(rdir: Path) -> dict:
    try:
        d = json.loads((rdir / "runtime.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(rdir: Path, d: dict):
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "runtime.json").write_text(json.dumps(d, indent=2), encoding="utf-8")


def ready(rdir: Path) -> bool:
    return state(rdir).get("python") == PYTHON and python_exe(rdir).is_file()


def fetch(dest: Path, zip_path: Path | None = None, log=print) -> Path:
    """The pinned zip, downloaded (or the given copy), checked against its hash."""
    m = machine()
    if m not in DOWNLOADS:
        raise SetupError(f"no tools runtime for this machine ({platform.machine()}); amd64 and arm64 only")
    name, want = DOWNLOADS[m]
    if zip_path is None:
        zip_path = dest / name
        url = URL.format(version=PYTHON, file=name)
        log(f"downloading {url}")
        try:
            with urllib.request.urlopen(url, timeout=60) as r, open(zip_path, "wb") as f:
                shutil.copyfileobj(r, f)
        except OSError as e:
            raise SetupError(f"download failed ({e}); `ibasho tool setup --zip FILE` takes a copy you fetched "
                                f"yourself ({name})") from None
    h = hashlib.sha256(Path(zip_path).read_bytes()).hexdigest()
    if h != want:
        raise SetupError(f"{zip_path} isn't {name} as python.org published it (sha256 {h}, expected {want})")
    return Path(zip_path)


def unpack(rdir: Path, zip_path: Path):
    """Unpack into python/ (replacing an older runtime's), with Ibasho's path file: the stdlib, the runtime's own
    folder and its site-packages, nothing from the environment."""
    new = rdir / "python.new"
    if new.exists():
        shutil.rmtree(new)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(new)
    (new / f"python{TAG}._pth").write_text(PTH, encoding="ascii")
    (new / "site-packages").mkdir()
    old = python_dir(rdir)
    if old.exists():
        shutil.rmtree(old)
    new.rename(old)


def pip_install(rdir: Path, packages: list[str], log=print) -> int:
    """Wheels for the runtime (its version and machine) into its site-packages, with the pip Ibasho runs on."""
    plat = "win_amd64" if machine() == "amd64" else "win_arm64"
    args = [sys.executable, "-m", "pip", "install", "--target", str(site_dir(rdir)), "--upgrade",
            "--only-binary=:all:", "--python-version", PYTHON.rsplit(".", 1)[0], "--implementation", "cp",
            "--abi", f"cp{TAG}", "--platform", plat, "--disable-pip-version-check", *packages]
    log("running: pip install " + " ".join(packages) + f" (wheels for Python {PYTHON}, into {site_dir(rdir)})")
    try:
        return subprocess.run(args).returncode
    except OSError as e:
        raise SetupError(f"couldn't run pip ({e})") from None


def has_pip() -> bool:
    try:
        return subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True).returncode == 0
    except OSError:
        return False


def installed(rdir: Path) -> list[str]:
    """name==version of what's in site-packages."""
    out = []
    for d in sorted(site_dir(rdir).glob("*.dist-info")):
        stem = d.name[:-len(".dist-info")]
        name, _, ver = stem.partition("-")
        out.append(f"{name}=={ver}" if ver else name)
    return out
