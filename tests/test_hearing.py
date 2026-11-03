"""Hearing: the pure parts always; proccap and Whisper when they're here.

proccap is looked for in IBASHO_TEST_PROCCAP, then ~/Ibasho/bin (audio/build.bat's default). The Whisper tests use
tiny.en, downloaded once (~75 MB) into pytest's cache. Sound for the capture tests plays on a spare output
device when the `soundcard` package is installed and one exists (so nobody hears it), else quietly on the
default speakers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from ibasho.cli import main
from ibasho.config import ConfigError, TEMPLATE
from ibasho.hearing import settings as settings_mod
from ibasho.hearing.audio import FRAME, RATE, Claps, Segmenter
from ibasho.hearing.ears import Names, Worker, name_pattern, rhythm_text, wakes_heard
from ibasho.hearing.music import allowed

WIN = sys.platform == "win32"


def run(world, capsys, *args) -> tuple[int, str, str]:
    code = main(["--config", str(world.cfg.path), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def frames(x):
    return [x[i:i + FRAME] for i in range(0, len(x) - FRAME + 1, FRAME)]


def tone(secs, freq=300.0, dbfs=-20.0):
    t = np.arange(int(RATE * secs)) / RATE
    return (10 ** (dbfs / 20) * 32767 * np.sin(2 * np.pi * freq * t)).astype("<i2")


def quiet(secs, dbfs=-75.0):
    rng = np.random.default_rng(1)
    return (rng.standard_normal(int(RATE * secs)) * 10 ** (dbfs / 20) * 32767).astype("<i2")


def add_hearing(cfg, **kw):
    lines = ["", "[hearing]"] + [f"{k} = {json.dumps(v)}" for k, v in kw.items()]
    with cfg.path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------- settings ----------

def test_template_hearing_defaults_match_the_code(home):
    import tomllib
    raw = tomllib.loads(TEMPLATE.format(name="A", grid="g", viewer="v", login_uri=""))["hearing"]
    d = settings_mod.Hearing()
    assert raw["enabled"] is False and raw["voice_device"] == "" and raw["model"] == ""
    for k, v in raw.items():
        assert getattr(d, k) == v, k


def test_settings_load_and_refuse(home):
    from conftest import write_config
    cfg = write_config(home)
    assert settings_mod.load(cfg).enabled is False
    add_hearing(cfg, enabled=True, heard_as=["Ivory", " "], gate_db=-50)
    h = settings_mod.load(cfg)
    assert h.enabled and h.heard_as == ["Ivory"] and h.gate_db == -50.0
    assert h.model == settings_mod.default_model() in ("small.en", "base.en")
    with cfg.path.open("a", encoding="utf-8") as f:
        f.write("voice = true\n")
    with pytest.raises(ConfigError, match="unknown setting"):
        settings_mod.load(cfg)


# ---------- cutting sound ----------

def test_segmenter_cuts_one_utterance_and_drops_blips():
    seg = Segmenter(-45)
    x = np.concatenate([quiet(1), tone(1.2), quiet(1.5), tone(0.15), quiet(1.5)])
    out = [u for f in frames(x) if (u := seg.feed(f)) is not None]
    assert len(out) == 1
    assert 1.2 <= len(out[0]) / RATE <= 2.2


def test_claps_become_a_rhythm_and_speech_does_not():
    got = []
    claps = Claps(got.append)

    def click():
        return (np.hanning(80) * 0.3 * 32767 * np.sign(np.sin(np.arange(80)))).astype("<i2")

    x = [quiet(0.5)]
    for _ in range(3):
        x += [click(), quiet(0.25 - 80 / RATE)]
    x += [quiet(2.5)]
    for f in frames(np.concatenate(x)):
        claps.feed(f)
    assert len(got) == 1 and got[0]["count"] == 3 and got[0]["pattern"] == "x.x.x" and got[0]["tempo"] == "fast"
    assert rhythm_text(dict(got[0], mine=False)).startswith("3 claps or knocks (fast) x.x.x")

    got.clear()
    for f in frames(np.concatenate([quiet(0.5), tone(1.0), quiet(2.5)])):
        claps.feed(f)
    assert got == []


def test_claps_inside_words_are_dropped():
    got = []
    claps = Claps(got.append)
    burst = (np.hanning(80) * 0.3 * 32767).astype("<i2")
    for f in frames(np.concatenate([quiet(0.5), burst, quiet(0.3)])):
        claps.feed(f)
    claps.submitted()
    claps.answered(claps.t, int(0.4 * RATE), True)   # Whisper found words around it
    for f in frames(quiet(2.5)):
        claps.feed(f)
    assert got == []


# ---------- names and waking ----------

def test_name_pattern():
    r = name_pattern(["Avery", "Example", "AJ"], ["Ivory", "the AI"])
    assert r.search("hey avery!") and r.search("Avery's here") and r.search("ivory, hi")
    assert r.search("ask the AI") and not r.search("they said brave things") and not r.search("bravery")
    assert not r.search("aj said") and not r.search("the aisle")


def test_names_from_config_without_a_daemon(home):
    from conftest import write_config
    n = Names(write_config(home), ["testee"], None)
    assert n.match("is Testy around") and n.match("testee?") and not n.match("nobody here")


def test_wake_rules():
    assert wakes_heard({"text": "hi", "me": True, "db": -60})
    assert wakes_heard({"text": "how is everyone", "db": -30})
    assert not wakes_heard({"text": "how is everyone", "db": -50})   # faint: media or music bleeding through
    assert not wakes_heard({"text": "okay", "db": -20})


def test_music_urls_only_over_http():
    assert allowed("http://radio.example:8000/stream") and allowed("https://radio.example/live")
    for bad in ("file:///C:/Users/me/secret.txt", "C:/x.mp3", "concat:a|b", "", None, "ftp://x/y",
                "subfile:,,start,0,end,0,:http://x", "http://127.0.0.1:8000/", "http://localhost/s",
                "http://192.168.1.20:8000/stream", "https://100.101.1.2/live", "http://[::1]/"):
        assert not allowed(bad)


def test_worker_skips_beyond_its_limit():
    gate = threading.Event()
    done = []

    def slow(audio):
        gate.wait(5)
        return []
    w = Worker(slow)
    took = [w.submit("viewer", 0, np.zeros(1), lambda p, t: done.append(1)) for _ in range(8)]
    assert took.count(True) in (4, 5) and not took[-1]   # 4 waiting (plus one maybe already started)
    gate.set()
    w.drain()
    assert len(done) == took.count(True)


# ---------- through the daemon ----------

def test_heard_events_wake_watch_only_when_they_say_so(world_connected, capsys):
    w = world_connected

    def emit():
        time.sleep(0.5)
        w.api.post_event("heard_stream", {"text": "la la la", "me": False, "wake": False})
        w.api.post_event("heard", {"text": "is anyone here building", "via": "viewer", "db": -30, "wake": True})
    threading.Thread(target=emit).start()
    code, out, _ = run(w, capsys, "watch", "--exit-on-wake", "--timeout", "20")
    assert code == 0 and out.strip().splitlines() == ["heard: is anyone here building"]


def test_forget_what_was_heard(world_connected, capsys):
    w = world_connected
    w.call(w.daemon.events.append, "heard", {"text": "old words", "wake": False}, time.time() - 3600)
    w.api.post_event("heard", {"text": "new words", "wake": False})
    w.api.post_event("heard_rhythm", {"text": "2 claps", "wake": False})
    w.emit("LLChat", {"from": "Someone", "from_id": "a", "message": "chat stays", "chat_type": 1, "source": 1})
    w.until(lambda: w.api.events(0, kinds=["chat"]))
    code, out, _ = run(w, capsys, "hear", "forget", "--last", "10")
    assert code == 0 and "forgot 2 heard events" in out
    left = [e["data"].get("text") or e["data"].get("message") for e in w.api.events(0, kinds=["heard", "chat"])]
    assert "new words" not in left and "old words" in left and "chat stays" in left
    code, out, _ = run(w, capsys, "hear", "forget")
    assert code == 0 and not w.api.events(0, kinds=["heard", "heard_rhythm", "heard_stream"])
    files = "".join(p.read_text(encoding="utf-8") for p in w.daemon.events.files())
    assert "words" not in files and "chat stays" in files


def test_hear_on_is_the_persons_choice(world_connected, capsys):
    w = world_connected
    code, _, err = run(w, capsys, "hear", "on")
    assert code == 1 and "hearing.enabled" in err
    code, out, _ = run(w, capsys, "hear", "status")
    assert code == 0 and "hearing off" in out
    code, _, err = run(w, capsys, "hear", "run")
    assert code == 1 and "hearing.enabled" in err


# ---------- Linux: the sound server's sinks (pactl faked) ----------

class FakePactl:
    """Answers capture._pactl like a PulseAudio with Ibasho's three sinks, and records what it's told to do."""

    def __init__(self, default="ibasho_world", voice_on=0):
        self.default, self.calls = default, []
        self.sinks = [{"index": i, "name": n, "description": n.capitalize()}
                      for i, n in enumerate(("ibasho_world", "ibasho_voice", "ibasho_silence"))]
        self.inputs = [{"index": 7, "sink": 0, "properties": {"application.name": "ibasho-viewer-bin"}},
                       {"index": 9, "sink": voice_on, "properties": {"application.name": "WEBRTC VoiceEngine"}}]

    def __call__(self, parec, *args):
        self.calls.append(args)
        if args == ("get-default-sink",):
            return self.default + "\n"
        if args[-2:] == ("list", "sinks"):
            return json.dumps(self.sinks)
        if args[-2:] == ("list", "sink-inputs"):
            return json.dumps(self.inputs)
        return ""


def test_linux_sinks_default_and_the_viewers_names_for_them(monkeypatch):
    from ibasho.hearing import capture
    fake = FakePactl()
    monkeypatch.setattr(capture, "_pactl", fake)
    monkeypatch.setattr(capture, "WINDOWS", False)
    devs = capture._sinks(Path("parec"))
    assert [(d.id, d.is_default) for d in devs] == [("ibasho_world", True), ("ibasho_voice", False),
                                                    ("ibasho_silence", False)]
    # the viewer's voice (WebRTC) names a sink by its description
    assert capture.find_device(devs, "ibasho_voice").viewer_id == "Ibasho_voice"
    assert capture.voice_device_for_viewer(Path("parec")) == "Ibasho_voice"
    assert capture.voice_device_for_viewer(None) is None
    assert capture.sink_exists(Path("parec"), capture.WORLD_SINK)


def test_linux_voice_is_kept_off_the_world_sink(monkeypatch):
    from ibasho.hearing import capture
    fake = FakePactl(voice_on=0)   # voice chat playing on the sink the viewer ear records
    monkeypatch.setattr(capture, "_pactl", fake)
    capture.keep_voice_off_world(Path("parec"))
    assert ("move-sink-input", "9", "ibasho_voice") in fake.calls
    assert not any("7" in c for c in fake.calls)   # the viewer's own sound stays
    fake = FakePactl(voice_on=1)
    monkeypatch.setattr(capture, "_pactl", fake)
    capture.keep_voice_off_world(Path("parec"))
    assert not any(c[0] in ("move-sink-input", "set-sink-input-mute") for c in fake.calls)
    fake = FakePactl(voice_on=0)   # no voice sink: muted instead
    fake.sinks = fake.sinks[:1]
    monkeypatch.setattr(capture, "_pactl", fake)
    capture.keep_voice_off_world(Path("parec"))
    assert ("set-sink-input-mute", "9", "1") in fake.calls


def test_trail_polls_only_when_woken_on_servers():
    from ibasho.hearing.ears import Trail

    class Api:
        n = 0

        def cmd(self, *a, **k):
            Api.n += 1
            return {"region": [1.0, 2.0, 3.0]}

    t = Trail(Api(), lazy=True)
    time.sleep(0.6)
    assert Api.n == 0   # no requests while nothing is heard: the viewer keeps its idle frame rate
    t.wake()
    time.sleep(1.2)
    assert Api.n >= 2


# ---------- native capture ----------

def proccap_exe() -> Path | None:
    for p in (os.environ.get("IBASHO_TEST_PROCCAP"), Path.home() / "Ibasho" / "bin" / "proccap.exe"):
        if p and Path(p).is_file():
            return Path(p)
    return None


needs_proccap = pytest.mark.skipif(not WIN or proccap_exe() is None, reason="proccap.exe not built")

PLAYER = textwrap.dedent("""
    import sys, time, wave
    import numpy as np
    path, device, delay = sys.argv[1], sys.argv[2], float(sys.argv[3])
    time.sleep(delay)
    if device:
        import soundcard as sc
        with wave.open(path) as w:
            rate, ch = w.getframerate(), w.getnchannels()
            x = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, ch).astype(np.float32) / 32768
        sc.get_speaker(device).play(x, samplerate=rate)
    else:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME)
    time.sleep(0.5)
""")


def spare_device(proccap: Path) -> str:
    """A non-default output to play test sound on, if soundcard is installed; '' = the default speakers."""
    try:
        import soundcard  # noqa: F401
    except ImportError:
        return ""
    from ibasho.hearing.capture import list_devices
    spare = [d for d in list_devices(proccap) if not d.is_default]
    return spare[0].name if spare else ""


def write_wav(path: Path, x, rate=RATE):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(x.astype("<i2").tobytes())


def play(path: Path, device: str, delay: float) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", PLAYER, str(path), device, str(delay)])


@needs_proccap
def test_proccap_lists_devices_and_refuses_a_default_one():
    from ibasho.hearing.capture import REFUSED, device_capture, list_devices
    devs = list_devices(proccap_exe())
    defaults = [d for d in devs if d.is_default]
    assert devs and defaults and all(d.id.startswith("{") for d in devs)
    cap = device_capture(proccap_exe(), defaults[0].id)
    assert list(cap.frames()) == [] and cap.wait() == REFUSED and "refusing" in cap.message


@needs_proccap
def test_process_capture_hears_only_that_process(tmp_path):
    from ibasho.hearing.capture import process_capture
    exe = proccap_exe()
    dev = spare_device(exe)
    write_wav(tmp_path / "a.wav", tone(2.5, 440, -26))
    write_wav(tmp_path / "b.wav", tone(2.5, 1000, -26))
    a, b = play(tmp_path / "a.wav", dev, 1.5), play(tmp_path / "b.wav", dev, 1.5)
    try:
        time.sleep(0.5)
        cap = process_capture(exe, a.pid)
        x = np.concatenate(list(cap.frames())).astype(np.float32)
    finally:
        a.wait(30)
        b.wait(30)
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    hz = np.fft.rfftfreq(len(x), 1 / RATE)
    at = lambda f: spec[(hz > f - 15) & (hz < f + 15)].max()  # noqa: E731
    assert at(440) > 100 * max(at(1000), np.median(spec))


# ---------- Whisper ----------

needs_whisper = pytest.mark.skipif(not WIN or __import__("importlib").util.find_spec("faster_whisper") is None,
                                   reason="faster-whisper not installed")
PHRASE = "Hello Avery, are you coming to the market tonight?"


@pytest.fixture(scope="session")
def speech_wav(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("speech") / "speech.wav"
    ps = ("Add-Type -AssemblyName System.Speech; $t = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$t.SetOutputToWaveFile('{path}'); $t.Speak('{PHRASE}'); $t.Dispose()")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    if r.returncode or not path.exists():
        pytest.skip("no Windows speech synthesis to make a test clip")
    return path


@pytest.fixture
def models(request, monkeypatch):
    d = Path(request.config.cache.mkdir("whisper-models"))
    monkeypatch.setattr(settings_mod, "models_dir", lambda cfg: d)
    return d


@needs_whisper
def test_whisper_transcribes_a_file(world, capsys, models, speech_wav):
    add_hearing(world.cfg, heard_as=["Avery"])
    code, out, err = run(world, capsys, "hear", "run", "--file", str(speech_wav), "--model", "tiny.en")
    assert code == 0, err
    ev = json.loads(out.strip().splitlines()[-1])
    assert ev["kind"] == "heard" and "market" in ev["data"]["text"].lower()
    assert ev["data"]["me"] is True and ev["data"]["wake"] is True


@needs_whisper
@needs_proccap
def test_live_hearing_reaches_watch(world_connected, capsys, models, speech_wav, tmp_path):
    """A process plays speech; hearing captures that process, transcribes it and posts to the daemon; the watch
    wakes on it."""
    from ibasho.hearing import ears
    w = world_connected
    add_hearing(w.cfg, enabled=True, heard_as=["Avery"], follow_music=False, mute_viewer_music=False,
                proccap=str(proccap_exe()), model="tiny.en")
    player = play(speech_wav, spare_device(proccap_exe()), 4.0)   # time for the model to load
    t = threading.Thread(target=ears.run, args=(w.cfg,), kwargs={"pid": player.pid}, daemon=True)
    t.start()
    try:
        code, out, _ = run(w, capsys, "watch", "--exit-on-wake", "--timeout", "90")
    finally:
        player.wait(60)
        t.join(60)
    assert code == 0 and "heard: " in out and "market" in out.lower()
    heard = w.api.events(0, kinds=["heard"])[-1]["data"]
    assert heard["via"] == "viewer" and heard["me"] and heard["wake"]
    status = json.loads((w.cfg.state_dir / "hear.json").read_text())
    assert status["ears"]["viewer"].startswith("stopped") and status["ears"]["voice"].startswith("off")
    assert "market" not in (w.cfg.state_dir / "hear.json").read_text().lower()


@needs_whisper
@needs_proccap
def test_hear_on_runs_in_the_background_and_off_stops_it(world_connected, capsys, models):
    from faster_whisper.utils import download_model
    w = world_connected
    local = download_model("tiny.en", cache_dir=str(models))   # a path, so the background process needn't download
    add_hearing(w.cfg, enabled=True, follow_music=False, proccap=str(proccap_exe()), model=local)
    code, out, err = run(w, capsys, "hear", "on")
    assert code == 0 and "hearing on" in out, err
    try:
        w.until(lambda: "on:" in json.loads((w.cfg.state_dir / "hear.json").read_text())["ears"]["viewer"], 60)
        code, out, _ = run(w, capsys, "hear", "status")
        assert "viewer: on: the viewer's sound" in out and "music: off" in out
        assert run(w, capsys, "hear", "on")[1].strip() == "hearing already on"
    finally:
        code, out, _ = run(w, capsys, "hear", "off")
    assert out.strip() == "hearing off"
    assert run(w, capsys, "hear", "status")[1].strip() == "hearing off"


# ---------- music streams ----------

@pytest.fixture
def web(tmp_path):
    """A local web server for tmp_path."""
    import functools
    import http.server
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_music_decodes_over_http_and_never_reads_local_files(tmp_path, web):
    pytest.importorskip("av")
    from ibasho.hearing.music import decode_stream
    write_wav(tmp_path / "song.wav", tone(3.0, 440, -20))
    x = np.concatenate(list(decode_stream(f"{web}/song.wav")))
    assert abs(len(x) / RATE - 3.0) < 0.1
    local = (tmp_path / "song.wav").as_uri()
    with pytest.raises(Exception):
        list(decode_stream(local))
    # a playlist on the web pointing at a file on this machine
    (tmp_path / "list.m3u8").write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3,\n{local}\n#EXT-X-ENDLIST\n")
    try:
        got = list(decode_stream(f"{web}/list.m3u8"))
    except Exception:
        got = []
    assert not got


def test_music_ear_follows_the_url_in_windows(tmp_path, web, monkeypatch):
    pytest.importorskip("av")
    from ibasho.hearing import music
    monkeypatch.setattr(music, "WINDOW_S", 2.0)
    monkeypatch.setattr(music, "OVERLAP_S", 0.5)
    monkeypatch.setattr(music, "LOCAL_URL", re.compile("(?!)"))   # the test's server is on 127.0.0.1
    write_wav(tmp_path / "song.wav", tone(4.6, 440, -20))
    wins, said = [], []
    ear = music.MusicEar(lambda: f"{web}/song.wav", lambda win, url, first: wins.append((len(win), first)),
                         said.append, poll_s=0.2)
    ear.start()
    end = time.time() + 15
    while time.time() < end and not any("ended" in s for s in said):
        time.sleep(0.1)
    ear.stopping.set()
    assert [w for w in wins] == [(2 * RATE, True), (2 * RATE, False)]   # 2 s windows, the second starts 0.5 s back
    assert any(s.startswith("following http://127.0.0.1") for s in said)


# ---------- voice ----------

@needs_whisper
@needs_proccap
def test_voice_ear_records_only_a_spare_device(world_connected, models, speech_wav):
    pytest.importorskip("soundcard")
    from ibasho.api import Api
    from ibasho.hearing import ears
    from ibasho.hearing.capture import list_devices
    w = world_connected
    devs = list_devices(proccap_exe())
    spare = [d for d in devs if not d.is_default]
    if not spare:
        pytest.skip("no spare output device")
    default = next(d for d in devs if d.is_default)

    def hearing_with(device):
        h = settings_mod.Hearing(enabled=True, voice_device=device, follow_music=False, claps=False,
                                 mute_viewer_music=False)
        api = Api(w.cfg)
        status = ears.Status(None, {})
        tr = ears.Transcriber("tiny.en", 2, "", models)
        return ears.Hearing(w.cfg, h, api, ears.Sink(api, False), status, tr, proccap_exe()), status

    refused, st = hearing_with(default.id)
    threading.Thread(target=refused.voice_ear, daemon=True).start()
    w.until(lambda: st.s["ears"].get("voice", "").startswith("refused"), 15)
    assert "default output" in st.s["ears"]["voice"] and refused.voice_id is None

    ok, st = hearing_with(spare[0].id)
    threading.Thread(target=ok.voice_ear, daemon=True).start()
    w.until(lambda: st.s["ears"].get("voice", "").startswith("on:"), 15)
    assert w.viewer.settings.get("VoiceOutputAudioDevice") == spare[0].id     # voice now plays there
    play(speech_wav, spare[0].name, 0.5).wait(30)
    w.until(lambda: any(e["data"].get("via") == "voice" for e in w.api.events(0, kinds=["heard"])), 60)
    heard = [e["data"] for e in w.api.events(0, kinds=["heard"]) if e["data"].get("via") == "voice"]
    assert "market" in heard[0]["text"].lower()
