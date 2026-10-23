"""The hearing process (`ibasho hear run`; `ibasho up` starts it in the background when the config enables it).

Ears, each feeding one shared Whisper model (faster-whisper on the CPU, int8, one transcription at a time,
speech before music):

- the viewer's own sound (proccap's process loopback of the viewer's process tree, nothing else on the
  machine): utterances cut by an energy gate, transcribed into `heard` events (`via: viewer`); claps and knocks
  in the same sound into `heard_rhythm` events;
- the parcel's music stream, if `follow_music`: overlapping windows into `heard_stream` events, which wake only on
  the avatar's name (lyrics come through too);
- voice chat, only if `voice_device` names a spare output device: the viewer's voice output is pinned there and
  that whole device recorded (`heard`, `via: voice`). Second Life voice chat isn't in the viewer's process sound.

Events go to the daemon's event log (POST /event) and nowhere else: this process logs what it's doing, never
what it heard. Each event says whether it should wake `ibasho watch` (`wake`).
"""

from __future__ import annotations

import collections
import itertools
import json
import logging
import logging.handlers
import math
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .. import procs
from ..api import Api, ApiError, Down
from ..config import Config
from . import settings as settings_mod
from .audio import FRAME, RATE, Claps, Segmenter, db
from .capture import REFUSED, CaptureError, device_capture, find_device, list_devices, process_capture
from .music import OVERLAP_S, MusicEar

log = logging.getLogger("ibasho.hear")

# Whisper's favourite things to say about silence and music.
HALLUCINATIONS = {"you", "thank you.", "thank you", "thanks for watching!", "thanks for watching.", ".", "bye.",
                  "thank you for watching.", "thank you so much for watching.", "[music]", "(music)", "♪"}
LOUD_DB = -42.0            # speech near the camera and voice chat are loud; fainter lines are media or music bleed
WAITING_PER_EAR = 4        # utterances waiting for Whisper per ear before new ones are skipped
CALL_AND_RESPONSE_S = 300  # claps wake for this long after my own clapping
FRIEND_NEAR_M = 30         # otherwise only with a friend this close


class HearError(Exception):
    pass


def wakes_heard(d: dict) -> bool:
    return bool(d.get("me")) or ((d.get("db") or -99) > LOUD_DB and len((d.get("text") or "").split()) >= 3)


def name_pattern(words: list[str], heard_as: list[str]) -> re.Pattern | None:
    """Matches the avatar's name words (3+ letters, as the start of a word) and the configured mishearings."""
    alts = [re.escape(w) for w in dict.fromkeys(w.lower() for w in words) if len(w) >= 3]
    alts += [r"\s+".join(re.escape(p) for p in w.split()) + r"\b" for w in heard_as if len(w) >= 2]
    return re.compile(r"\b(?:" + "|".join(alts) + ")", re.I) if alts else None


class Names:
    """Who I am, for spotting my name in what's heard: the config's name and nicknames, my names in world once
    logged in, and `heard_as`. Refreshed every few minutes."""

    def __init__(self, cfg: Config, heard_as: list[str], api: Api | None):
        self.cfg, self.heard_as, self.api = cfg, heard_as, api
        self.at = 0.0
        self.re = name_pattern(cfg.name_words(), heard_as)

    def match(self, text: str) -> bool:
        if self.api and time.time() - self.at > 300:
            self.at = time.time()
            try:
                st = self.api.status(fresh=True)
                words = list(st.get("name_words") or [])
                for k in ("display_name", "username"):
                    words += [w for w in re.split(r"[\s._]+", str((st.get("self") or {}).get(k) or ""))
                              if w and w.lower() != "resident"]
                self.re = name_pattern(words or self.cfg.name_words(), self.heard_as)
            except ApiError:
                pass
        return bool(self.re and self.re.search(text))


class Transcriber:
    def __init__(self, model: str, threads: int, language: str, download_root: Path):
        from faster_whisper import WhisperModel
        download_root.mkdir(parents=True, exist_ok=True)
        kw = dict(device="cpu", compute_type="int8", cpu_threads=threads, download_root=str(download_root))
        try:   # no network check when the model's already here
            self.model = WhisperModel(model, local_files_only=True, **kw)
        except Exception:
            self.model = WhisperModel(model, **kw)
        self.language = "en" if model.endswith(".en") else (language or None)

    def __call__(self, audio: np.ndarray) -> list:
        # No initial prompt: priming the name makes Whisper "hear" it in music and silence.
        parts, _ = self.model.transcribe(audio.astype(np.float32) / 32768, language=self.language, vad_filter=True,
                                         beam_size=3, condition_on_previous_text=False)
        return [p for p in parts if p.no_speech_prob < 0.6 and p.avg_logprob > -1.0
                and p.text.strip().lower() not in HALLUCINATIONS and "♪" not in p.text]


class Worker:
    """One transcription at a time on its own thread, lower `priority` first; each ear keeps at most
    WAITING_PER_EAR utterances waiting, and skips new ones beyond that rather than fall ever further behind."""

    def __init__(self, transcribe):
        self.transcribe = transcribe
        self.q: queue.PriorityQueue = queue.PriorityQueue()
        self.waiting: collections.Counter = collections.Counter()
        self.lock = threading.Lock()
        self.order = itertools.count()
        threading.Thread(target=self._run, daemon=True, name="whisper").start()

    def submit(self, ear: str, priority: int, audio: np.ndarray, done) -> bool:
        with self.lock:
            if self.waiting[ear] >= WAITING_PER_EAR:
                return False
            self.waiting[ear] += 1
        self.q.put((priority, next(self.order), ear, audio, done))
        return True

    def drain(self):
        self.q.join()

    def _run(self):
        while True:
            _, _, ear, audio, done = self.q.get()
            with self.lock:
                self.waiting[ear] -= 1
            t0 = time.monotonic()
            try:
                parts = self.transcribe(audio)
            except Exception:
                log.exception("transcription failed")
                parts = []
            try:
                done(parts, time.monotonic() - t0)
            except Exception:
                log.exception("handling a transcription failed")
            self.q.task_done()


class Sink:
    """Where events go: the daemon, or stdout for trying things out."""

    def __init__(self, api: Api | None, to_stdout: bool):
        self.api, self.to_stdout = api, to_stdout
        self.lock = threading.Lock()

    def post(self, kind: str, data: dict):
        if self.to_stdout or self.api is None:
            with self.lock:
                print(json.dumps({"kind": kind, "data": data}, ensure_ascii=False), flush=True)
            return
        try:
            self.api.post_event(kind, data)
        except Down:
            log.warning("daemon unreachable; a %s event was dropped", kind)
        except ApiError as e:
            log.warning("the daemon refused a %s event (%s)", kind, e.status)


class Status:
    """state/hear.json: what each ear is doing, for `ibasho hear status`. Never what was heard."""

    def __init__(self, path: Path | None, base: dict):
        self.path, self.s = path, dict(base, ears={}, last_heard=None)
        self.lock = threading.Lock()
        self._write()

    def ear(self, name: str, text: str):
        with self.lock:
            if self.s["ears"].get(name) == text:
                return
            self.s["ears"][name] = text
        log.info("%s ear: %s", name, text)
        self._write()

    def heard(self):
        with self.lock:
            self.s["last_heard"] = round(time.time())
        self._write()

    def _write(self):
        if self.path is None:
            return
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.s), encoding="utf-8")
            os.replace(tmp, self.path)


class Trail:
    """My recent positions (my own footsteps splash and creak like claps)."""

    def __init__(self, api: Api):
        self.api = api
        self.points: collections.deque = collections.deque(maxlen=60)
        threading.Thread(target=self._run, daemon=True, name="trail").start()

    def _run(self):
        while True:
            try:
                x, y, _ = self.api.cmd("LLAgent", "getPosition", timeout=5)["region"]
                self.points.append((time.time(), x, y))
            except (ApiError, KeyError, TypeError, ValueError):
                pass
            time.sleep(0.5)

    def moving(self, t0: float, t1: float) -> bool:
        pts = [(x, y) for t, x, y in list(self.points) if t0 <= t <= t1]
        return len(pts) >= 2 and max(math.dist(pts[0], p) for p in pts) > 0.4


def rhythm_text(d: dict) -> str:
    n = d["count"]
    what = "clap or knock" if n == 1 else "claps or knocks"
    tempo = f" ({d['tempo']})" if d.get("tempo") else ""
    return f"{'my own ' if d.get('mine') else ''}{n} {what}{tempo} {d['pattern']}, ended {d['ago_s']} s ago"


class Hearing:
    def __init__(self, cfg: Config, h: settings_mod.Hearing, api: Api | None, sink: Sink, status: Status,
                 transcribe, proccap: Path | None = None):
        self.cfg, self.h, self.api, self.sink, self.status = cfg, h, api, sink, status
        self.proccap = proccap
        self.names = Names(cfg, h.heard_as, api)
        self.worker = Worker(transcribe)
        self.trail = Trail(api) if api and h.claps else None
        self.voice_id: str | None = None

    # ---- speech ----

    def utterance(self, u: np.ndarray, via: str, claps: Claps | None = None):
        end_hop = claps.t if claps else 0
        if claps:
            claps.submitted()

        def done(parts, took):
            text = " ".join(p.text.strip() for p in parts).strip()
            if claps:
                claps.answered(end_hop, len(u), bool(text))
            if not text:
                return
            d = {"text": text, "via": via, "dur": round(len(u) / RATE, 1), "db": round(db(u), 1),
                 "took": round(took, 1), "me": self.names.match(text)}
            d["wake"] = wakes_heard(d)
            self.status.heard()
            self.sink.post("heard", d)

        if not self.worker.submit(via, 0, u, done):
            log.info("%s ear: Whisper is behind; skipped an utterance", via)
            if claps:
                claps.answered(end_hop, len(u), False)

    def viewer_ear(self, frames, drain: bool = False):
        """Until the frames end. `drain`: then wait for what's still being transcribed (a file; when the viewer is
        gone, the rest doesn't matter)."""
        seg = Segmenter(self.h.gate_db)
        claps = Claps(self.on_rhythm) if self.h.claps else None
        for x in frames:
            if claps:
                claps.feed(x, seg.busy)
            u = seg.feed(x)
            if u is not None:
                self.utterance(u, "viewer", claps)
        u = seg.flush()
        if u is not None:
            self.utterance(u, "viewer", claps)
        if not drain:
            return
        self.worker.drain()
        if claps:
            claps.feed(np.zeros(0, dtype="<i2"))   # take in the last answers
            claps.flush()

    # ---- claps ----

    def clapping(self) -> dict:
        try:
            return json.loads((self.cfg.state_dir / "clapping.json").read_text())
        except (OSError, ValueError):
            return {}

    def friend_near(self) -> bool:
        if not self.api or not self.api.ibasho:
            return False
        try:
            people = self.api.op("IbashoAgent", "people", {"dist": FRIEND_NEAR_M}, timeout=5).get("people", [])
        except ApiError:
            return False
        return any(p.get("friend") and p.get("dist", 999) <= FRIEND_NEAR_M for p in people)

    def on_rhythm(self, d: dict):
        end = time.time() - d["ago_s"]
        if self.trail and self.trail.moving(end - sum(d["intervals_ms"]) / 1000 - 0.6, end + 0.6):
            log.info("dropped a rhythm heard while I was moving")
            return
        mine = self.clapping()
        try:
            d["mine"] = mine["from"] - 0.3 <= end <= mine["to"] + 0.6
            answering = time.time() - mine["to"] < CALL_AND_RESPONSE_S
        except (KeyError, TypeError):
            d["mine"], answering = False, False
        d["wake"] = not d["mine"] and (answering or self.friend_near())
        d["text"] = rhythm_text(d)
        self.sink.post("heard_rhythm", d)

    # ---- music ----

    def parcel_music(self) -> str | None:
        if not self.api or not self.api.ibasho:
            return None
        return (self.api.op("IbashoAgent", "parcel", timeout=10).get("parcel") or {}).get("music_url") or None

    def on_music(self, win: np.ndarray, url: str, first: bool):
        def done(parts, took):
            kept = [p for p in parts if first or p.start >= OVERLAP_S - 0.3]   # the overlap was heard last time
            text = " ".join(p.text.strip() for p in kept).strip()
            if text:
                me = self.names.match(text)
                self.sink.post("heard_stream", {"text": text, "url": url, "me": me, "took": round(took, 1),
                                                "wake": me})
        self.worker.submit("music", 1, win, done)

    # ---- voice ----

    def voice_ear(self):
        while True:
            try:
                dev = find_device(list_devices(self.proccap), self.h.voice_device)
            except CaptureError as e:
                self.status.ear("voice", f"off: {e}")
                time.sleep(60)
                continue
            if dev.is_default:
                self.voice_id = None
                self.status.ear("voice", f"refused: {dev.name!r} is a Windows default output "
                                         f"({', '.join(dev.roles)}), so it would record everything this machine "
                                         "plays; choose a spare device")
                time.sleep(30)
                continue
            self.voice_id = dev.id
            if not self.pin_voice():
                self.status.ear("voice", "waiting for the viewer to point its voice output at the device")
                time.sleep(15)
                continue
            cap = device_capture(self.proccap, dev.id)
            self.status.ear("voice", f"on: {dev.name}")
            seg = Segmenter(self.h.gate_db)
            for x in cap.frames():
                u = seg.feed(x)
                if u is not None:
                    self.utterance(u, "voice")
            code = cap.wait()
            self.voice_id = None
            self.status.ear("voice", "refused: the device became a Windows default output" if code == REFUSED
                            else f"stopped ({cap.message or code}); retrying")
            time.sleep(30)

    def pin_voice(self) -> bool:
        """Point the viewer's voice output at the voice device (the person's other sound stays where it was)."""
        if not self.api or not self.voice_id:
            return False
        try:
            cur = self.api.cmd("LLViewerControl", "get", {"group": "Global", "key": "VoiceOutputAudioDevice"})
            if (cur or {}).get("value") != self.voice_id:
                self.api.cmd("LLViewerControl", "set", {"group": "Global", "key": "VoiceOutputAudioDevice",
                                                        "value": self.voice_id})
            return True
        except ApiError:
            return False

    # ---- the viewer's settings ----

    def keep_settings(self):
        muted = not self.h.mute_viewer_music
        while True:
            if not muted and self.api:
                try:
                    self.api.cmd("LLViewerControl", "set", {"group": "Global", "key": "AudioLevelMusic", "value": 0.0})
                    muted = True
                except ApiError:
                    pass
            if self.voice_id:
                self.pin_voice()
            time.sleep(60)

    # ---- running ----

    def start_side_ears(self):
        threading.Thread(target=self.keep_settings, daemon=True, name="settings").start()
        if self.h.follow_music:
            if self.api and self.api.ibasho:
                MusicEar(self.parcel_music, self.on_music, lambda s: self.status.ear("music", s)).start()
            else:
                self.status.ear("music", "off: following parcel music needs the Ibasho viewer")
        else:
            self.status.ear("music", "off (hearing.follow_music)")
        if self.h.voice_device:
            threading.Thread(target=self.voice_ear, daemon=True, name="voice").start()
        else:
            self.status.ear("voice", "off (hearing.voice_device is empty)")


def file_frames(path: str):
    from .music import decode_stream
    x = np.concatenate(list(decode_stream(path, local_file=True)) or [np.zeros(0, dtype="<i2")])
    for i in range(0, len(x) - FRAME + 1, FRAME):
        yield x[i:i + FRAME]
    yield from (np.zeros(FRAME, dtype="<i2") for _ in range(40))   # trailing quiet ends the last utterance


def viewer_pid(cfg: Config, api: Api) -> int | None:
    try:
        pid = api.status(fresh=True).get("viewer_pid")
        if pid and procs.alive(int(pid)):
            return int(pid)
    except ApiError:
        pass
    return procs.from_file(cfg.state_dir / "viewer.pid")


def setup_logging(cfg: Config, foreground: bool):
    handlers: list[logging.Handler] = []
    if not foreground:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(cfg.state_dir / "hear.log", maxBytes=1_000_000,
                                                             backupCount=1, encoding="utf-8"))
    if foreground and sys.stderr is not None:   # in the background stderr is hear.out: crashes only, not a copy
        handlers.append(logging.StreamHandler())
    for h in handlers:   # other libraries' chatter (download requests) only when it's a problem
        h.addFilter(lambda r: r.name.startswith("ibasho") or r.levelno >= logging.WARNING)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s hear: %(message)s", handlers=handlers)
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def below_normal():
    try:
        import psutil
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == "win32" else 10)
    except Exception:
        pass


def run(cfg: Config, *, model: str | None = None, pid: int | None = None, to_stdout: bool = False,
        file: str | None = None, foreground: bool = True) -> int:
    h = settings_mod.load(cfg)
    if model:
        h.model = model
    if file:
        to_stdout = True
    elif not h.enabled:
        raise HearError("hearing is off in the config (hearing.enabled); that's the person's choice to make, "
                        "see docs/hearing.md")
    setup_logging(cfg, foreground)
    below_normal()
    api = None if file else Api(cfg)
    proccap = None
    if not file:
        proccap = settings_mod.proccap_path(cfg, h)
        if proccap is None:
            raise HearError("proccap.exe not found: build it with audio\\build.bat (or put a release's copy in "
                            f"{cfg.home / 'bin'}), or set hearing.proccap")
        pid = pid or viewer_pid(cfg, api)
        if not pid:
            raise HearError("no viewer running to listen to (`ibasho up` first)")
    status = Status(None if file else cfg.state_dir / "hear.json",
                    {"pid": os.getpid(), "viewer_pid": pid, "model": h.model, "started": round(time.time())})
    status.ear("viewer", "loading the model")
    log.info("loading %s (downloaded into %s on first use)", h.model, settings_mod.models_dir(cfg))
    try:
        transcribe = Transcriber(h.model, settings_mod.threads(h), h.language, settings_mod.models_dir(cfg))
    except ImportError:
        raise HearError('hearing needs its extra packages: pip install ".[hearing]" in the Ibasho folder') from None
    ears = Hearing(cfg, h, api, Sink(api, to_stdout), status, transcribe, proccap)
    if file:
        ears.viewer_ear(file_frames(file), drain=True)
        return 0
    try:
        cap = process_capture(proccap, pid)
    except CaptureError as e:
        raise HearError(str(e)) from None
    status.ear("viewer", f"on: the viewer's sound (pid {pid})")
    ears.start_side_ears()
    ears.viewer_ear(cap.frames())
    code = cap.wait()
    status.ear("viewer", "stopped: the viewer is gone" if not procs.alive(pid) else f"stopped ({cap.message or code})")
    return 0
