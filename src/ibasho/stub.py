"""The viewer's `--leap` child.

Moves whole LEAP frames between the viewer (our stdin/stdout) and the Ibasho daemon (TCP on 127.0.0.1), and
reconnects whenever the daemon restarts, so the daemon can be stopped or updated without restarting the viewer.
Whole frames only, so a reconnect never hands either side half a message; frames the viewer sends while the
daemon is away are dropped. Exits only when the viewer closes our stdin.

Env (set by `ibasho up`): IBASHO_LEAP_PORT, IBASHO_LEAP_TOKEN (proves to the daemon that we're its stub, not some
other local process), IBASHO_STUB_LOG. Stdlib only, so it starts fast under pythonw.
"""

import os
import socket
import sys
import threading
import time
import traceback

MAGIC = b"IBASHO-LEAP/1"
FRAME_MAX = 64 * 1024 * 1024


def log(text):
    path = os.environ.get("IBASHO_STUB_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


class FrameReader:
    """Reads `<len>:<payload>` frames from a recv-like function; returns the whole frame, prefix included."""

    def __init__(self, recv):
        self.recv = recv
        self.buf = b""

    def _fill(self):
        chunk = self.recv(1 << 16)
        if not chunk:
            raise EOFError
        self.buf += chunk

    def frame(self):
        while b":" not in self.buf:
            if len(self.buf) > 20:
                raise ValueError(f"no frame length in {self.buf[:20]!r}")
            self._fill()
        head, _, _ = self.buf.partition(b":")
        n = int(head)
        if not 0 <= n <= FRAME_MAX:
            raise ValueError(f"frame length {n}")
        need = len(head) + 1 + n
        while len(self.buf) < need:
            self._fill()
        out, self.buf = self.buf[:need], self.buf[need:]
        return out


def write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def main():
    if sys.platform == "win32":
        import msvcrt
        for fd in (0, 1):
            try:
                msvcrt.setmode(fd, os.O_BINARY)
            except OSError:
                pass
    port = int(os.environ.get("IBASHO_LEAP_PORT", "0"))
    token = os.environ.get("IBASHO_LEAP_TOKEN", "").encode()
    if not port or not token:
        log("IBASHO_LEAP_PORT / IBASHO_LEAP_TOKEN missing; exiting")
        return 2
    viewer = FrameReader(lambda n: os.read(0, n))
    try:
        handshake = viewer.frame()
    except (EOFError, ValueError, OSError):
        log("no handshake from the viewer; exiting")
        return 1
    hello = b"%s %s %d\n" % (MAGIC, token, os.getppid())
    lock = threading.Lock()
    current = {"sock": None}

    def viewer_to_daemon():
        while True:
            try:
                f = viewer.frame()
            except (EOFError, OSError):
                log("viewer closed stdin; exiting")
                os._exit(0)
            except ValueError as e:
                log(f"viewer stream out of frame ({e}); exiting")
                os._exit(1)
            with lock:
                s = current["sock"]
                if s is not None:
                    try:
                        s.sendall(f)
                    except OSError:
                        pass

    threading.Thread(target=viewer_to_daemon, daemon=True).start()

    while True:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
        except OSError:
            time.sleep(1)
            continue
        s.settimeout(None)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            with lock:
                s.sendall(hello + handshake)
                current["sock"] = s
            log("connected to the daemon")
            daemon = FrameReader(s.recv)
            while True:
                write_all(1, daemon.frame())
        except (EOFError, ConnectionError):
            pass
        except Exception:
            log("daemon link failed: " + traceback.format_exc())
        finally:
            with lock:
                current["sock"] = None
            s.close()
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
