"""Write the Ibasho series (branch ibasho of a viewer tree) into viewer/patches, without Linden Lab's images.

    python viewer/export_patches.py [--src TREE]

`git format-patch` writes a binary change as two hunks: the new file, then the old one (so the patch can be
reversed). For the branding commit the old files are Linden Lab's Eye-in-Hand icons and logos, which Ibasho
mustn't ship, so this drops every second hunk. `git am` applies a binary patch without it. Run it instead of
format-patch whenever the series changes; it refuses a tree with uncommitted changes.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUNK = re.compile(rb"^(literal|delta) \d+\r?$")


def strip_reverse_hunks(data: bytes) -> tuple[bytes, int]:
    lines = data.split(b"\n")
    out, i, dropped = [], 0, 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        if line.rstrip(b"\r") != b"GIT binary patch":
            continue
        # forward hunk: method line, data, blank line
        while i < len(lines):
            out.append(lines[i])
            i += 1
            if not lines[i - 1].rstrip(b"\r"):
                break
        # reverse hunk, if any: skip it and its blank line
        if i < len(lines) and HUNK.match(lines[i]):
            dropped += 1
            i += 1
            while i < len(lines) and lines[i].rstrip(b"\r"):
                i += 1
            i += 1
    return b"\n".join(out), dropped


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    work = Path(os.environ.get("IBASHO_VIEWER_WORK") or HERE / "work")
    ap.add_argument("--src", default=str(work / "viewer-src"), help="the viewer tree (default: work/viewer-src)")
    a = ap.parse_args()
    src = Path(a.src)
    base = (HERE / "BASE").read_text().strip()
    if subprocess.run(["git", "-C", str(src), "status", "--porcelain", "--untracked-files=no"],
                      capture_output=True, text=True, check=True).stdout.strip():
        sys.exit(f"{src} has uncommitted changes: commit them to branch ibasho first")

    patches = HERE / "patches"
    for p in patches.glob("*.patch"):
        p.unlink()
    subprocess.run(["git", "-C", str(src), "format-patch", "-q", "-o", str(patches), "--zero-commit",
                    "--no-signature", f"{base}..ibasho"], check=True)
    total = 0
    for p in sorted(patches.glob("*.patch")):
        data, n = strip_reverse_hunks(p.read_bytes())
        if n:
            p.write_bytes(data)
            total += n
    print(f"{len(list(patches.glob('*.patch')))} patches in {patches}; dropped {total} reverse binary hunks")


if __name__ == "__main__":
    main()
