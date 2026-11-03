#!/bin/bash
# The container's main process (under tini): the virtual display the viewer draws on, then `ibasho mcp` over
# WebSocket, started again if it stops. The daemon and viewer start when the agent (or anyone) runs `ibasho up`;
# stopping the container logs the avatar out first (give `docker stop` 30 s: --stop-timeout, or compose's
# stop_grace_period).
set -u
SCREEN="${IBASHO_SCREEN:-1024x768}"
mkdir -p "$IBASHO_HOME"

# Sound: a PulseAudio server of the container's own, with null sinks only (docker/pulse.pa); PULSE_SERVER points
# every process here at it
mkdir -m 700 -p /tmp/ibasho-pulse "${XDG_RUNTIME_DIR:-/tmp/ibasho-run}"
pulseaudio -n -F /etc/ibasho/pulse.pa --daemonize=no --exit-idle-time=-1 --disallow-module-loading=no \
  --realtime=no --high-priority=no --log-target=stderr >/tmp/pulse.log 2>&1 &
PULSE=$!

rm -f /tmp/.X0-lock /tmp/.X11-unix/X0   # left by this container's last run, if it was stopped hard
Xvfb :0 -screen 0 "${SCREEN}x24" -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB=$!
for _ in $(seq 100); do [ -S /tmp/.X11-unix/X0 ] && break; sleep 0.1; done
[ -S /tmp/.X11-unix/X0 ] || { echo "ibasho: the virtual display didn't start" >&2; cat /tmp/xvfb.log >&2; exit 1; }

stopping=""
shut_down() {
  stopping=1
  [ -f "$IBASHO_HOME/config.toml" ] && ibasho down >/dev/null 2>&1
  ibasho-login-view stop >/dev/null 2>&1
  kill "$MCP" "$XVFB" "$PULSE" 2>/dev/null
  exit 0
}
trap shut_down TERM INT

while [ -z "$stopping" ]; do
  ibasho mcp --websocket "${IBASHO_MCP_LISTEN:-0.0.0.0:9280}" &
  MCP=$!
  wait "$MCP"
  [ -n "$stopping" ] || sleep 2
done
