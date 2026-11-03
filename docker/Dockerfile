# Ibasho in a container, for a Linux server without a graphics card: the viewer drawing in software on a virtual
# display, Ibasho's daemon, and `ibasho mcp` over WebSocket for an MCP host on the same machine. docs/linux.md.
#
# Build from the repository's top folder, with the Linux viewer's tarball at docker/viewer.tar.xz:
#   docker build -f docker/Dockerfile -t ibasho .
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
      tini ca-certificates xz-utils procps python3 python3-venv \
      xvfb xauth libgl1 libglx-mesa0 libgl1-mesa-dri libegl1 libegl-mesa0 libglu1-mesa libgbm1 \
      libgtk-3-0t64 libnss3 libnspr4 libasound2t64 libpulse0 libxss1 libxcursor1 libxinerama1 libxrandr2 libxi6 \
      libxkbcommon0 libxkbcommon-x11-0 libdbus-1-3 libfontconfig1 libfreetype6 libdrm2 libxcomposite1 \
      libxdamage1 libxfixes3 libatk-bridge2.0-0t64 libcups2t64 libxshmfence1 libxtst6 libsm6 libice6 libvulkan1 \
      libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      x11vnc novnc websockify pulseaudio pulseaudio-utils \
 && rm -rf /var/lib/apt/lists/* \
 && (userdel -r ubuntu 2>/dev/null || true) \
 && useradd -m -u 1000 -s /bin/bash ibasho

COPY docker/viewer.tar.xz /tmp/viewer.tar.xz
RUN mkdir /opt/ibasho-viewer \
 && tar -xJf /tmp/viewer.tar.xz -C /opt/ibasho-viewer --strip-components=1 --no-same-owner \
 && rm /tmp/viewer.tar.xz \
 && test -x /opt/ibasho-viewer/bin/ibasho-viewer-bin

COPY pyproject.toml README.md LICENSE /tmp/ibasho/
COPY src /tmp/ibasho/src
COPY life-template /tmp/ibasho/life-template
# hearing's packages (faster-whisper) are 0.4 GB: --build-arg EXTRAS=mcp leaves them out
ARG EXTRAS=mcp,hearing
RUN python3 -m venv /opt/ibasho \
 && /opt/ibasho/bin/pip install --no-cache-dir "/tmp/ibasho[$EXTRAS]" \
 && rm -rf /tmp/ibasho

COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/ibasho-entrypoint
COPY --chmod=755 docker/login-view.sh /usr/local/bin/ibasho-login-view
# the login view's page: noVNC with pasting from the person's clipboard, served beside noVNC's own files
COPY --chmod=644 docker/ibasho.html /usr/share/novnc/ibasho.html
COPY docker/pulse.pa /etc/ibasho/pulse.pa

ENV PATH=/opt/ibasho/bin:$PATH \
    IBASHO_HOME=/home/ibasho/Ibasho \
    IBASHO_VIEWER=/opt/ibasho-viewer/ibasho-viewer \
    IBASHO_LOGIN_VIEW=/usr/local/bin/ibasho-login-view \
    PULSE_SERVER=unix:/tmp/ibasho-pulse/native XDG_RUNTIME_DIR=/tmp/ibasho-run \
    DISPLAY=:0
USER ibasho
WORKDIR /home/ibasho
# Everything that lasts: the config, the event log and pictures, the viewer's settings and saved login, its cache
VOLUME /home/ibasho
# 9280: `ibasho mcp` over WebSocket. 6080: the person's view of the screen while they log in (`ibasho login`)
EXPOSE 9280 6080
ENTRYPOINT ["tini", "--", "ibasho-entrypoint"]
