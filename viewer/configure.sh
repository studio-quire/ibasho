#!/usr/bin/env bash
# Configure the Ibasho viewer build (Git Bash on Windows, Visual Studio 2022 or its Build Tools installed).
#
#   bash configure.sh [SOURCE_TREE]      (default: $IBASHO_VIEWER_WORK/viewer-src, see setup.sh)
#
# Needed after setup, after CMake files change, and before every release build: the version's build
# number (0.1.0.N) is fixed here, N = number of commits on top of BASE.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(cygpath -u "${IBASHO_VIEWER_WORK:-$HERE/work}")"
SRC="${1:-$WORK/viewer-src}"
BASE="$(tr -d '[:space:]' < "$HERE/BASE")"

VSWHERE="/c/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe"
VS="$("$VSWHERE" -products '*' -latest -property installationPath | tr -d '\r')"
[ -n "$VS" ] || { echo "Visual Studio 2022 not found" >&2; exit 1; }
VS_CMAKE="$(cygpath -u "$VS")/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin"

export PATH="$VS_CMAKE:$WORK/ab-venv/Scripts:$PATH"
export AUTOBUILD_VSVER=170 AUTOBUILD_ADDRSIZE=64 AUTOBUILD_CONFIGURATION=ReleaseOS
export AUTOBUILD_INSTALLABLE_CACHE="$(cygpath -w "$WORK/autobuild-cache")"
export AUTOBUILD_VARIABLES_FILE="$(cygpath -w "$WORK/build-variables/variables")"
revision="$(git -C "$SRC" rev-list --count "$BASE..HEAD")"
export revision
PY="$(cygpath -w "$WORK/ab-venv/Scripts/python.exe")"

# ReleaseOS: the open-source configuration (OpenJPEG instead of Kakadu, no Havok, no FMOD).
(cd "$SRC" && autobuild configure -A 64 -c ReleaseOS -- \
  -DVIEWER_CHANNEL:STRING="Ibasho" \
  -DPython3_EXECUTABLE:FILEPATH="$PY" \
  -DLL_TESTS:BOOL=OFF \
  -DUSE_VELOPACK:BOOL=OFF)

echo "$revision" > "$SRC/build-vc170-64/ibasho-revision.txt"
echo "configured Ibasho $(tr -d '[:space:]' < "$SRC/indra/newview/VIEWER_VERSION.txt").$revision"
