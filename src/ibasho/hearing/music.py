"""Following the parcel's music stream: decoded here (PyAV, which faster-whisper already needs), cut into
overlapping windows, transcribed for the talk over it (DJs do talk, sometimes to you).

Only http(s) URLs, and only the network protocols they need: a parcel's music URL is set by whoever owns the
land, and a file: URL or a playlist pointing at local files must never be read, nor a stream on this machine or its
network (the viewer refuses those too).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

import numpy as np

from ..policy import LOCAL_URL
from .audio import RATE

log = logging.getLogger("ibasho.hear")

WINDOW_S, OVERLAP_S = 12.0, 2.0
ALLOWED_URL = re.compile(r"^https?://[^\s/]+", re.I)
PROTOCOLS = "http,https,tcp,tls,httpproxy"


def allowed(url: str | None) -> bool:
    return bool(url and ALLOWED_URL.match(url.strip()) and not LOCAL_URL.search(url))


def decode_stream(url: str, keep_going: Callable[[], bool] = lambda: True, timeout: float = 15.0,
                  local_file: bool = False):
    """Yield int16 mono 16 kHz chunks of a live stream, paced to real time (a plain file plays no faster than a
    stream would), until `keep_going()` turns false or the stream ends or stalls (an exception). `local_file`:
    a file on disk instead (the person's own, for checking the install), as fast as it decodes."""
    import av
    if local_file:
        container = av.open(url, options={"protocol_whitelist": "file"})
    else:
        container = av.open(url, timeout=(timeout, timeout),
                            options={"protocol_whitelist": PROTOCOLS, "reconnect": "1", "reconnect_streamed": "1",
                                     "reconnect_delay_max": "10", "user_agent": "Ibasho"})
    try:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise ValueError("no audio in the stream")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        start, played = time.monotonic(), 0
        for frame in container.decode(stream):
            if not keep_going():
                return
            for out in resampler.resample(frame):
                x = out.to_ndarray().reshape(-1).astype("<i2", copy=False)
                played += len(x)
                yield x
            ahead = played / RATE - (time.monotonic() - start) - 5.0   # live streams burst a few seconds at first
            if ahead > 0 and not local_file:
                time.sleep(ahead)
    finally:
        container.close()


class MusicEar:
    """Polls `current_url()` (the parcel's music URL) and follows it; `on_window(int16 window, url, first)` gets
    each window. Windows overlap by OVERLAP_S so words at a boundary aren't cut; the receiver keeps only segments
    that start after the overlap (all of the first window)."""

    def __init__(self, current_url: Callable[[], str | None], on_window, status: Callable[[str], None],
                 poll_s: float = 20.0):
        self.current_url, self.on_window, self.status, self.poll_s = current_url, on_window, status, poll_s
        self.wanted: str | None = None
        self.stopping = threading.Event()

    def start(self):
        threading.Thread(target=self._poll, daemon=True, name="music-poll").start()
        threading.Thread(target=self._run, daemon=True, name="music").start()

    def _poll(self):
        while not self.stopping.is_set():
            try:
                url = self.current_url()
                self.wanted = url.strip() if allowed(url) else None
                if url and not self.wanted:
                    self.status("not following the parcel's music URL (not http/https)")
            except Exception as e:   # the viewer's busy or away; keep the last answer
                log.debug("music url poll: %r", e)
            self.stopping.wait(self.poll_s)

    def _run(self):
        while not self.stopping.is_set():
            url = self.wanted
            if not url:
                self.status("on, no music here")
                self.stopping.wait(5)
                continue
            self.status(f"following {url}")
            log.info("music: following %s", url)
            try:
                need, step = int(WINDOW_S * RATE), int((WINDOW_S - OVERLAP_S) * RATE)
                parts, have, first = [], 0, True
                for chunk in decode_stream(url, lambda: self.wanted == url and not self.stopping.is_set()):
                    parts.append(chunk)
                    have += len(chunk)
                    if have >= need:
                        pending = np.concatenate(parts)
                        while len(pending) >= need:
                            self.on_window(pending[:need], url, first)
                            first = False
                            pending = pending[step:]
                        parts, have = [pending], len(pending)
                if self.wanted == url:      # it ended by itself: a file, or the station went off
                    self.status(f"{url} ended")
                    self.stopping.wait(60)
            except Exception as e:
                log.info("music: stream stopped (%s); retrying shortly", type(e).__name__)
                self.status(f"stream trouble ({type(e).__name__}), retrying")
                self.stopping.wait(10)
