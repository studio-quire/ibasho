"""Sound into pieces worth transcribing: levels, an energy-gated utterance cutter, and the clap ear.

Everything here works on 16 kHz mono signed 16-bit PCM in 30 ms frames, as proccap writes it.
"""

from __future__ import annotations

import math
import queue

import numpy as np

RATE = 16000
FRAME = 480          # 30 ms
FRAME_BYTES = FRAME * 2


def db(x: np.ndarray) -> float:
    """Level in dBFS."""
    if not len(x):
        return -180.0
    rms = math.sqrt(float(np.mean(x.astype(np.float32) ** 2)))
    return 20 * math.log10(rms / 32768 + 1e-9)


class Segmenter:
    """Cuts a frame stream into utterances: contiguous audio louder than both `gate_db` and a slowly rising
    noise floor + 12 dB, ending after `hang_s` of quiet or at `max_s`, dropped if shorter than `min_s`."""

    def __init__(self, gate_db: float = -45.0, hang_s: float = 0.8, max_s: float = 20.0, min_s: float = 0.4):
        self.gate, self.hang, self.max, self.min = gate_db, hang_s, max_s, min_s
        self.floor = -70.0
        self.buf: list[np.ndarray] = []
        self.quiet = 0

    @property
    def busy(self) -> bool:
        return bool(self.buf)

    def feed(self, x: np.ndarray) -> np.ndarray | None:
        """One frame in; a finished utterance out, or None."""
        level = db(x)
        self.floor = min(level, self.floor + 0.02) if level > self.floor else level   # tracks the quiet bits
        loud = level > max(self.gate, self.floor + 12)
        if not (loud or self.buf):
            return None
        self.buf.append(x)
        self.quiet = 0 if loud else self.quiet + 1
        n = len(self.buf) * FRAME / RATE
        quiet_s = self.quiet * FRAME / RATE
        if quiet_s >= self.hang or n >= self.max:
            return self._cut(n - quiet_s)
        return None

    def flush(self) -> np.ndarray | None:
        n = len(self.buf) * FRAME / RATE
        return self._cut(n - self.quiet * FRAME / RATE) if self.buf else None

    def _cut(self, voiced_s: float) -> np.ndarray | None:
        seg = np.concatenate(self.buf) if voiced_s >= self.min else None
        self.buf, self.quiet = [], 0
        return seg


class Claps:
    """Percussive onsets (gesture claps, knocks) -> one rhythm per phrase, through `emit(dict)`.

    Speech recognition never hears a clap, and in some places claps are a language of their own (one for "are you
    there?", fast for cheering, rhythms traded back and forth). A gesture clap in the viewer's sound is a ~30 ms
    burst out of near silence, so: 10 ms hops; an onset is a jump of `rise` dB over the previous hop that clears
    the noise floor, and it counts only if it has died away within `short_ms` and stays quiet for `after_ms`
    (speech and music sustain; claps don't). A phrase ends after `gap_s` without onsets. Phrases wait while an
    utterance is being cut or transcribed, and onsets inside audio that turned out to be words are dropped
    (plosives look like claps).
    """
    HOP = 160  # 10 ms

    def __init__(self, emit, rise=12.0, above_floor=18.0, short_ms=150, after_ms=90, gap_s=1.6, max_onsets=40):
        self.emit, self.rise, self.above, self.short, self.gap, self.max = emit, rise, above_floor, short_ms, gap_s, max_onsets
        self.after = after_ms // 10
        self.t = 0                    # hops seen
        self.prev, self.floor = -90.0, -70.0
        self.cand = None              # (hop, peak dB, quiet hops or None) awaiting the decay check
        self.onsets: list[int] = []
        self.peaks: list[float] = []
        self.speech: list[tuple[int, int]] = []   # hop ranges that held words
        self.pending = 0              # utterances handed to the transcriber, not yet answered
        self.answers: queue.SimpleQueue = queue.SimpleQueue()

    # The transcriber's side (any thread): an utterance went off, and later whether it held words.

    def submitted(self) -> None:
        self.answers.put(("sent", 0, 0))

    def answered(self, end_hop: int, n_samples: int, words: bool) -> None:
        self.answers.put(("done", end_hop, n_samples if words else 0))

    # The capture side (one thread).

    def feed(self, x: np.ndarray, busy: bool = False) -> None:
        """busy: an utterance is being cut right now."""
        while not self.answers.empty():
            what, end, n = self.answers.get()
            if what == "sent":
                self.pending += 1
            else:
                self.pending -= 1
                if n:
                    self.speech.append((end - n // self.HOP - 10, end))
        for i in range(0, len(x) - self.HOP + 1, self.HOP):
            self.hop(db(x[i:i + self.HOP]), busy or self.pending > 0)

    def hop(self, level: float, busy: bool = False) -> None:
        self.t += 1
        self.floor = min(level, self.floor + 0.01) if level > self.floor else level
        if self.cand:
            h, peak, quiet = self.cand
            if quiet is None:
                if level > peak:
                    self.cand = (h, level, None)
                elif level < peak - 10:                       # decayed; now it has to stay quiet
                    self.cand = (h, peak, 0)
                elif (self.t - h) * 10 > self.short:          # still ringing: speech, music, a held note
                    self.cand = None
            elif level >= peak - 10:                          # something right after: a plosive into its vowel
                self.cand = None
            elif quiet + 1 >= self.after:
                self.onsets.append(h)
                self.peaks.append(peak)
                self.cand = None
            else:
                self.cand = (h, peak, quiet + 1)
        elif level - self.prev >= self.rise and level > self.floor + self.above and level > -65:
            if not self.onsets or (self.t - self.onsets[-1]) * 10 >= 80:   # refractory
                self.cand = (self.t, level, None)
        self.prev = level
        if self.onsets and (self.t - self.onsets[-1]) * 10 > self.gap * 1000 and not self.cand and not busy:
            self.flush()

    def flush(self) -> None:
        on, peaks = self.onsets, self.peaks
        self.onsets, self.peaks = [], []
        if not on or len(on) > self.max:   # a flood of hits is rain or a drum loop, not someone clapping
            return
        keep = [(h, p) for h, p in zip(on, peaks) if not any(a <= h <= b for a, b in self.speech)]
        self.speech = [r for r in self.speech if r[1] > self.t - 3000]
        if not keep:
            return
        loud = max(p for _, p in keep)     # faint ticks among the claps are tails, echoes or something far off
        on = [h for h, p in keep if p >= loud - 9]
        iv = [(b - a) * 10 for a, b in zip(on, on[1:])]
        grid = 125
        pattern = "x" + "".join("." * max(0, round(g / grid) - 1) + "x" for g in iv)
        mean = sum(iv) / len(iv) if iv else 0
        tempo = "" if not iv else "fast" if mean <= 330 else "slow" if mean >= 650 else "steady"
        self.emit({"count": len(on), "intervals_ms": iv, "pattern": pattern, "tempo": tempo,
                   "db": round(loud, 1), "ago_s": round((self.t - on[-1]) / 100, 1)})
