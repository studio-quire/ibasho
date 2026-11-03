#!/bin/bash
# The person's view of the virtual display, for the one login they do by hand (`ibasho login` runs this; it's
# IBASHO_LOGIN_VIEW in the image). x11vnc shows the display, and noVNC serves it to a browser on port 6080 (our
# ibasho.html, which adds pasting from the browser's clipboard, or noVNC's vnc.html), which
# the container publishes on the server's loopback only: the person reaches it through an SSH tunnel, with a
# password made afresh each time. Nothing listens until a login starts, and the view closes by itself when the
# viewer it was opened for exits.
#
#   ibasho-login-view start VIEWER_PID     prints what the person does
#   ibasho-login-view stop
set -u
RUN=/tmp/ibasho-login-view
PORT="${IBASHO_VIEW_PORT:-6080}"

stop() {
  [ -f "$RUN/websockify.pid" ] && kill "$(cat "$RUN/websockify.pid")" 2>/dev/null
  pkill -u "$(id -u)" -f "x11vnc -display :0" 2>/dev/null
  rm -rf "$RUN"
}

case "${1:-}" in
start)
  viewer="${2:-}"
  stop
  mkdir -m 700 -p "$RUN"
  pass="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 8)"
  x11vnc -storepasswd "$pass" "$RUN/passwd" >/dev/null 2>&1
  # X11VNC_AVOID_WINDOWS=never: x11vnc otherwise makes the window it pastes from only 45 s after someone connects
  # (to wait out a display manager we don't have), and pasting does nothing until then
  X11VNC_AVOID_WINDOWS=never x11vnc -display :0 -rfbauth "$RUN/passwd" -localhost -rfbport 5900 -forever -shared -bg -quiet \
    -o "$RUN/x11vnc.log" >/dev/null 2>&1 || { echo "the screen view didn't start (x11vnc)" >&2; exit 1; }
  websockify --web /usr/share/novnc "0.0.0.0:$PORT" 127.0.0.1:5900 >"$RUN/websockify.log" 2>&1 &
  echo $! >"$RUN/websockify.pid"
  if [ -n "$viewer" ]; then
    (while kill -0 "$viewer" 2>/dev/null; do sleep 2; done; "$0" stop) >/dev/null 2>&1 &
    disown
  fi
  cat <<EOF
The viewer is on this server's virtual screen. The person sees it in their own browser:
  1. On their computer: ssh -N -L $PORT:127.0.0.1:$PORT USER@SERVER   (the server's SSH login; leave it running)
  2. In a browser there: http://localhost:$PORT/ibasho.html#password=$pass
     (Ctrl+V pastes into the viewer from there. If that page doesn't work, noVNC's own does, without pasting:
     http://localhost:$PORT/vnc.html?autoconnect=1&resize=scale&password=$pass)
The view closes when the viewer does.
EOF
  ;;
stop)
  stop
  ;;
*)
  echo "usage: $0 start VIEWER_PID | stop" >&2
  exit 2
  ;;
esac
