# The Linux build environment for the Ibasho viewer: Ubuntu 22.04, as Linden Lab's own Linux builds use.
#
#   docker build -t ibasho-viewer-build viewer/linux
#   docker run --rm -v ibasho-viewer-work:/work -v "$PWD:/release:ro" -e IBASHO_VIEWER_WORK=/work \
#       ibasho-viewer-build bash /release/viewer/build-linux.sh
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential git ca-certificates curl xz-utils bzip2 file locales pkg-config ninja-build \
      python3 python3-venv python3-pip \
      libpulse-dev libunwind-dev libgl1-mesa-dev libglu1-mesa-dev libxinerama-dev libxcursor-dev libxfixes-dev \
      libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libxft-dev libpipewire-0.3-dev libdbus-1-dev \
      libosmesa6-dev \
 && locale-gen en_US.UTF-8 \
 && rm -rf /var/lib/apt/lists/*
# CMake from PyPI: 22.04's 3.22 is older than what Linden Lab's runners use
RUN python3 -m pip install --no-cache-dir "cmake>=3.28,<4"
