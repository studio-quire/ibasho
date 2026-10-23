#!/usr/bin/env bash
# Set up an Ibasho viewer source tree and its build tools (Git Bash on Windows).
#
#   bash setup.sh [--force]
#
# Into the work folder ($IBASHO_VIEWER_WORK, default viewer/work beside this script):
#   viewer-src/       Linden Lab's viewer at the commit in BASE, plus patches/*.patch, on branch "ibasho"
#   ab-venv/          a Python venv with autobuild
#   build-variables/  Linden Lab's build-variables, pinned
#   autobuild-cache/  filled by configure.sh (prebuilt libraries, ~350 MB)
# Re-running it leaves an existing tree alone unless --force (which resets branch ibasho to the patches).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(cygpath -u "${IBASHO_VIEWER_WORK:-$HERE/work}")"
BASE="$(tr -d '[:space:]' < "$HERE/BASE")"
BUILD_VARIABLES_COMMIT=f8fad0dbbf6c821e1ae6e254e1ed203e93815a94
AUTOBUILD_VERSION=3.10.2
SRC="$WORK/viewer-src"
FORCE="${1:-}"

mkdir -p "$WORK"

# Source: only the base commit is fetched (shallow), then the patches go on top.
if [ ! -d "$SRC/.git" ]; then
  git init -q "$SRC"
  git -C "$SRC" remote add origin https://github.com/secondlife/viewer.git
fi
if git -C "$SRC" rev-parse -q --verify ibasho >/dev/null && [ "$FORCE" != "--force" ]; then
  echo "viewer-src already has branch ibasho; leaving it (run with --force to reset it to the patches)"
else
  git -C "$SRC" fetch -q --depth 1 origin "$BASE"
  git -C "$SRC" checkout -q -B ibasho "$BASE"
  # Committer identity for the applied patches only; authors come from the patches.
  git -C "$SRC" -c user.name="Ibasho build" -c user.email="build@ibasho.invalid" am -q "$HERE"/patches/*.patch
  echo "viewer-src: branch ibasho = $(git -C "$SRC" rev-list --count "$BASE..ibasho") patches on $BASE"
fi

# autobuild, in its own venv (Python 3.10+; the py launcher's 3.11 if there is one)
if [ ! -x "$WORK/ab-venv/Scripts/python.exe" ]; then
  if py -3.11 -c "" 2>/dev/null; then PY=(py -3.11); else PY=(python); fi
  "${PY[@]}" -m venv "$WORK/ab-venv"
  "$WORK/ab-venv/Scripts/python.exe" -m pip install -q "autobuild==$AUTOBUILD_VERSION"
fi

if [ ! -d "$WORK/build-variables/.git" ]; then
  git clone -q https://github.com/secondlife/build-variables.git "$WORK/build-variables"
fi
git -C "$WORK/build-variables" checkout -q "$BUILD_VARIABLES_COMMIT"

mkdir -p "$WORK/autobuild-cache"
echo "ready: bash \"$HERE/configure.sh\", then powershell -File \"$HERE/build.ps1\""
