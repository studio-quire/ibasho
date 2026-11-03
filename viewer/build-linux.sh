#!/usr/bin/env bash
# Build the Ibasho viewer for Linux x86_64, from source to the release tarball.
#
#   bash viewer/build-linux.sh [--reset] [--jobs N] [--no-package]
#
# Runs on Ubuntu 22.04 with the packages in viewer/linux/Dockerfile, or in that container. Into the work folder
# ($IBASHO_VIEWER_WORK, default viewer/work-linux beside this script):
#   viewer-src/       Linden Lab's viewer at the commit in BASE, plus patches/*.patch, on branch "ibasho"
#   ab-venv/          a Python venv with autobuild and llsd
#   build-variables/  Linden Lab's build-variables, pinned
#   autobuild-cache/  prebuilt libraries (~1 GB)
#   dist/             Ibasho-<version>-linux-x86_64.tar.xz, made by package.py
# An existing viewer-src is left as it is (so a tree being worked on builds incrementally); --reset puts branch
# ibasho back to BASE plus the patches. The compiler writes no debug information (IBASHO_DEBUG_INFO=1 keeps
# it, at three times the disk).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${IBASHO_VIEWER_WORK:-$HERE/work-linux}"
BASE="$(tr -d '[:space:]' < "$HERE/BASE")"
BUILD_VARIABLES_COMMIT=f8fad0dbbf6c821e1ae6e254e1ed203e93815a94
AUTOBUILD_VERSION=3.10.2
SRC="$WORK/viewer-src"
RESET="" JOBS="$(nproc)" PACKAGE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --reset) RESET=1 ;;
    --jobs) JOBS="$2"; shift ;;
    --no-package) PACKAGE="" ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$WORK"
if [ ! -d "$SRC/.git" ]; then
  git init -q "$SRC"
  git -C "$SRC" remote add origin https://github.com/secondlife/viewer.git
fi
if git -C "$SRC" rev-parse -q --verify ibasho >/dev/null && [ -z "$RESET" ]; then
  echo "viewer-src already has branch ibasho; building it as it is (--reset puts it back to the patches)"
else
  git -C "$SRC" fetch -q --depth 1 origin "$BASE"
  git -C "$SRC" checkout -q -f -B ibasho "$BASE"
  git -C "$SRC" -c user.name="Ibasho build" -c user.email="build@ibasho.invalid" am -q "$HERE"/patches/*.patch
  echo "viewer-src: branch ibasho = $(git -C "$SRC" rev-list --count "$BASE..ibasho") patches on $BASE"
fi

if [ ! -x "$WORK/ab-venv/bin/python" ]; then
  python3 -m venv "$WORK/ab-venv"
  "$WORK/ab-venv/bin/python" -m pip install -q "autobuild==$AUTOBUILD_VERSION" llsd
fi
if [ ! -d "$WORK/build-variables/.git" ]; then
  git clone -q https://github.com/secondlife/build-variables.git "$WORK/build-variables"
fi
git -C "$WORK/build-variables" checkout -q "$BUILD_VARIABLES_COMMIT"
mkdir -p "$WORK/autobuild-cache"

# Without debug information: a launcher that puts -g0 last, after the -g Linden Lab's CMake adds.
LAUNCHER=()
if [ -z "${IBASHO_DEBUG_INFO:-}" ]; then
  printf '#!/bin/sh\nexec "$@" -g0\n' > "$WORK/no-debug-info"
  chmod +x "$WORK/no-debug-info"
  LAUNCHER=(-DCMAKE_C_COMPILER_LAUNCHER="$WORK/no-debug-info" -DCMAKE_CXX_COMPILER_LAUNCHER="$WORK/no-debug-info")
fi

export PATH="$WORK/ab-venv/bin:$PATH"
export AUTOBUILD_ADDRSIZE=64 AUTOBUILD_CONFIGURATION=ReleaseOS AUTOBUILD_CPU_COUNT="$JOBS"
export AUTOBUILD_INSTALLABLE_CACHE="$WORK/autobuild-cache"
export AUTOBUILD_VARIABLES_FILE="$WORK/build-variables/variables"
export LL_SKIP_REQUIRE_SYSROOT=1
revision="$(git -C "$SRC" rev-list --count "$BASE..HEAD")"
export revision

# ReleaseOS: the open-source configuration (OpenJPEG instead of Kakadu, no Havok, no FMOD).
(cd "$SRC" && autobuild configure -A 64 -c ReleaseOS -- \
  -DVIEWER_CHANNEL:STRING="Ibasho" \
  -DPython3_EXECUTABLE:FILEPATH="$WORK/ab-venv/bin/python" \
  -DLL_TESTS:BOOL=OFF \
  -DPACKAGE:BOOL=OFF \
  "${LAUNCHER[@]}")
echo "$revision" > "$SRC/build-linux-x86_64/ibasho-revision.txt"
echo "configured Ibasho $(tr -d '[:space:]' < "$SRC/indra/newview/VIEWER_VERSION.txt").$revision"

cmake --build "$SRC/build-linux-x86_64" --parallel "$JOBS"

if [ -n "$PACKAGE" ]; then
  "$WORK/ab-venv/bin/python" "$HERE/package.py" --src "$SRC" --out "$WORK/dist"
fi
