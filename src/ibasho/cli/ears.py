"""Hearing's controls (`ibasho hear ...`) and `ibasho clap`. The hearing process itself is ibasho.hearing.ears;
docs/hearing.md has the whole story."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import sys
import time

from .. import procs
from ..api import ApiError
from ..config import ConfigError
from ..hearing import settings as settings_mod
from ..hearing.settings import HEARD_KINDS
from .common import Ctx, Fail


# What `hear on --model` takes in the agent's sessions: faster-whisper's own models up to medium. Anything else (a
# folder, another repository's model, the large ones) is the person's to set in hearing.model.
SESSION_MODELS = {"tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en",
                  "distil-small.en", "distil-medium.en"}


def register(sub):
    p = sub.add_parser("hear", help="hearing: speech in the viewer's sound turned into words, as heard events (words "
                                    "only: music and other sounds don't come through; docs/hearing.md)")
    hs = p.add_subparsers(dest="hear_command", required=True, metavar="ACTION")
    q = hs.add_parser("on", help="start hearing in the background (when the config enables it)")
    q.add_argument("--model", help="a faster-whisper model for this run (default: hearing.model); in the agent's "
                                   "sessions one of " + ", ".join(sorted(SESSION_MODELS)))
    q.set_defaults(fn=on)
    hs.add_parser("off", help="stop hearing (until `ibasho hear on` or the next `ibasho up`)").set_defaults(fn=off)
    hs.add_parser("status", help="which ears are on, and when something was last heard").set_defaults(fn=status)
    q = hs.add_parser("setup", help="for the person: check what hearing needs, and download the model")
    q.add_argument("--model", help="download this model instead of hearing.model")
    q.set_defaults(fn=setup)
    hs.add_parser("devices", help="output devices, for choosing hearing.voice_device").set_defaults(fn=devices)
    q = hs.add_parser("forget", help="delete what was heard from the event log (all of it, or --last MINUTES)")
    q.add_argument("--last", type=float, metavar="MINUTES")
    q.set_defaults(fn=forget)
    q = hs.add_parser("run", help="run hearing in this window (what `on` starts in the background)")
    q.add_argument("--model")
    q.add_argument("--pid", type=int, help="listen to this process instead of the viewer")
    q.add_argument("--print", action="store_true", help="print events instead of sending them to the daemon")
    q.add_argument("--file", help="transcribe an audio file instead (implies --print); for checking the install")
    q.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    q.set_defaults(fn=run)
    p = sub.add_parser("clap", help="clap a rhythm with a clap gesture: x = clap, . = a rest (hearing's notation)")
    p.add_argument("pattern", help='e.g. "x...x.x.x...x" (spaces ignored)')
    p.add_argument("--gesture", default="clap", help="an active gesture whose name contains this (default: clap)")
    p.add_argument("--grid", type=float, default=0.125, help="seconds per step (default 0.125)")
    p.set_defaults(fn=clap)


def hearing(ctx: Ctx) -> settings_mod.Hearing:
    try:
        return settings_mod.load(ctx.cfg)
    except ConfigError as e:
        raise Fail(str(e)) from None


def pid_file(ctx: Ctx):
    return ctx.cfg.state_dir / "hear.pid"


def state(ctx: Ctx) -> dict:
    try:
        return json.loads((ctx.cfg.state_dir / "hear.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def running(ctx: Ctx) -> int | None:
    return procs.from_file(pid_file(ctx))


def started_for(ctx: Ctx) -> list[str]:
    """What the running hearing process was started for: [pid, viewer pid, config mtime]."""
    try:
        return pid_file(ctx).read_text().split()
    except OSError:
        return []


def viewer_pid(ctx: Ctx) -> int | None:
    try:
        pid = (ctx.api.is_up() or {}).get("viewer_pid") if ctx.cfg.token_file.exists() else None
        if pid and procs.alive(int(pid)):
            return int(pid)
    except ApiError:
        pass
    return procs.from_file(ctx.cfg.state_dir / "viewer.pid")


def model_cached(ctx: Ctx, model: str) -> bool:
    try:
        from faster_whisper.utils import download_model
        download_model(model, local_files_only=True, cache_dir=str(settings_mod.models_dir(ctx.cfg)))
        return True
    except Exception:
        return False


# ---------- on / off ----------

def start(ctx: Ctx, model: str | None = None) -> str:
    """Start the hearing process for the running viewer (restarting one that's listening to an old viewer).
    Returns a line for the person or model; fails with the reason it can't."""
    cfg = ctx.cfg
    h = hearing(ctx)
    if not h.enabled:
        raise Fail("hearing is off in the config (hearing.enabled); turning it on is the person's call "
                   "(docs/hearing.md)")
    vpid = viewer_pid(ctx)
    if not vpid:
        raise Fail("no viewer running (`ibasho up` first)")
    mtime = str(cfg.path.stat().st_mtime)
    if cur := running(ctx):
        if started_for(ctx)[1:] == [str(vpid), mtime] and not model:
            return "hearing already on"
        procs.kill_tree(cur)   # listening to an old viewer, or the config changed
    if importlib.util.find_spec("faster_whisper") is None:
        raise Fail('hearing needs its extra packages: pip install ".[hearing]" in the Ibasho folder')
    if settings_mod.proccap_path(cfg, h) is None:
        raise Fail(f"proccap.exe not found: the person builds it with audio\\build.bat (or puts a release's copy in "
                   f"{cfg.home / 'bin'})")
    model = model or h.model
    fresh = "" if model_cached(ctx, model) else f"; first it downloads {model} (`ibasho hear setup` does that ahead)"
    from .lifecycle import _src_path
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "hear.json").unlink(missing_ok=True)
    args = procs.ibasho_args("--config", str(cfg.path), "hear", "run", "--background", "--pid", str(vpid))
    pid = procs.spawn(args + (["--model", model] if model != h.model else []), cfg.state_dir / "hear.out",
                      env=procs.env_with(PYTHONPATH=_src_path()))
    pid_file(ctx).write_text(f"{pid} {vpid} {mtime}")
    end = time.time() + 3
    while time.time() < end:
        time.sleep(0.3)
        if not procs.alive(pid):
            raise Fail(f"hearing stopped right away; see {cfg.state_dir / 'hear.log'} and hear.out")
    return f"hearing on (Whisper {model} on the CPU{fresh}): {WORDS_ONLY}"


# What hearing is and isn't, for whoever reads `hear on` and `hear status` (the model, mostly).
WORDS_ONLY = ("speech near me comes as `heard` lines, in words; music, songs' tunes and other sounds don't come "
              "through (lyrics sometimes do, as words), and names are often misheard")


def start_if_enabled(ctx: Ctx) -> str | None:
    """For `ibasho up`: start hearing when the config enables it; never fails the caller."""
    try:
        if not hearing(ctx).enabled:
            return None
        return start(ctx)
    except Fail as e:
        return f"hearing not started: {e}"


def stop(ctx: Ctx) -> bool:
    pid = running(ctx)
    if pid:
        procs.kill_tree(pid)
    pid_file(ctx).unlink(missing_ok=True)
    (ctx.cfg.state_dir / "hear.json").unlink(missing_ok=True)
    return bool(pid)


def on(ctx: Ctx, a):
    print(start(ctx, a.model))


def off(ctx: Ctx, a):
    print("hearing off" if stop(ctx) else "hearing wasn't on")


def status(ctx: Ctx, a):
    h = hearing(ctx)
    pid = running(ctx)
    if not pid:
        print("hearing off" + ("" if h.enabled else " (and disabled in the config: hearing.enabled)"))
        return
    s = state(ctx)
    print(f"hearing on: Whisper {s.get('model') or h.model}" + ("" if s else " (starting)") + f"; {WORDS_ONLY}")
    for ear, what in (s.get("ears") or {}).items():
        print(f"  {ear}: {what}")
    if s.get("last_heard"):
        print(f"  last heard something {int(time.time() - s['last_heard'])} s ago")


# ---------- the person's side ----------

def setup(ctx: Ctx, a):
    cfg = ctx.cfg
    h = hearing(ctx)
    model = a.model or h.model
    ok = True
    print(f"hearing.enabled = {str(h.enabled).lower()}" + ("" if h.enabled else " (set it in the config to use hearing)"))
    build = tuple(int(x) for x in (platform.version().split(".") + ["0", "0", "0"])[:3] if x.isdigit())
    if sys.platform != "win32" or build < (10, 0, 19041):
        print("PROBLEM: capturing one program's sound needs Windows 10 version 2004 or later")
        ok = False
    proccap = settings_mod.proccap_path(cfg, h)
    if proccap:
        print(f"proccap: {proccap}")
    else:
        ok = False
        print(f"PROBLEM: proccap.exe not found. Build it with audio\\build.bat (Visual Studio Build Tools, as for "
              f"the viewer); it goes to {cfg.home / 'bin'}")
    if importlib.util.find_spec("faster_whisper") is None:
        print('PROBLEM: the hearing packages aren\'t installed: pip install ".[hearing]" in the Ibasho folder')
        raise SystemExit(1)
    # The model hub's warnings (no symlinks without Windows developer mode; anonymous downloads) don't matter here.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_VERBOSITY", "error")
    import warnings
    warnings.filterwarnings("ignore", module="huggingface_hub")
    from faster_whisper.utils import download_model
    where = settings_mod.models_dir(cfg)
    print(f"model {model}: " + ("already here" if model_cached(ctx, model) else f"downloading into {where} ..."),
          flush=True)
    try:
        download_model(model, cache_dir=str(where))
    except Exception as e:
        ok = False
        print(f"PROBLEM: couldn't get {model}: {e}")
    else:
        print(f"model {model}: ready ({settings_mod.threads(h)} CPU threads)")
    print("music: " + ("following the parcel's stream (needs the Ibasho viewer)" if h.follow_music else "off"))
    if h.voice_device:
        if proccap:
            from ..hearing.capture import CaptureError, find_device, list_devices
            try:
                d = find_device(list_devices(proccap), h.voice_device)
                if d.is_default:
                    ok = False
                    print(f"PROBLEM: voice device {d.name!r} is a Windows default output ({', '.join(d.roles)}); "
                          "voice capture refuses it. Choose a spare device (docs/hearing.md)")
                else:
                    print(f"voice: on, from {d.name!r} (voice chat will play there, not on your speakers)")
            except CaptureError as e:
                ok = False
                print(f"PROBLEM: voice device: {e}")
    else:
        print("voice: off (opt-in: hearing.voice_device)")
    if not ok:
        raise SystemExit(1)


def devices(ctx: Ctx, a):
    h = hearing(ctx)
    proccap = settings_mod.proccap_path(ctx.cfg, h)
    if not proccap:
        raise Fail("proccap.exe not found (audio\\build.bat builds it)")
    from ..hearing.capture import CaptureError, list_devices
    try:
        devs = list_devices(proccap)
    except CaptureError as e:
        raise Fail(str(e)) from None
    want = h.voice_device.lower()
    for d in devs:
        mark = "*" if want and (want == d.id.lower() or want in d.name.lower()) else " "
        roles = f"  [default: {', '.join(d.roles)}; can't be the voice device]" if d.roles else ""
        print(f"{mark} {d.name}{roles}\n    {d.id}")
    print("(* = hearing.voice_device)" if want else "hearing.voice_device is empty: voice chat isn't captured")


def forget(ctx: Ctx, a):
    after = time.time() - a.last * 60 if a.last else 0
    r = ctx.api.http("POST", "/forget", {"kinds": list(HEARD_KINDS), "after": after})
    span = f"from the last {a.last:g} min" if a.last else "of all time kept"
    print(f"forgot {r['events_removed']} heard events ({span})")


def run(ctx: Ctx, a):
    from ..hearing import ears
    try:
        ears.run(ctx.cfg, model=a.model, pid=a.pid, to_stdout=a.print, file=a.file, foreground=not a.background)
    except ears.HearError as e:
        raise Fail(str(e)) from None


# ---------- clapping ----------

def clap(ctx: Ctx, a):
    pat = a.pattern.replace(" ", "")
    if not pat or set(pat) - set("x.") or "x" not in pat:
        raise Fail("pattern: x for a clap, . for a rest")
    if not 0.05 <= a.grid <= 2:
        raise Fail("--grid: between 0.05 and 2 seconds")
    api = ctx.api
    gs = api.cmd("LLGesture", "getActiveGestures").get("gestures") or {}
    items = gs.items() if isinstance(gs, dict) else [(g.get("id"), g) for g in gs if isinstance(g, dict)]
    gid = next((i for i, g in items if a.gesture.lower() in str(g.get("name", "")).lower()), None)
    if not gid:
        raise Fail(f"no active gesture named like {a.gesture!r} (activate one in the viewer's Gestures window)")
    times = [i * a.grid for i, ch in enumerate(pat) if ch == "x"]
    t0 = time.time()
    # so hearing knows these claps are mine
    ctx.state_file("clapping.json").write_text(json.dumps({"from": t0, "to": t0 + times[-1] + 0.3}))
    for at in times:
        while time.time() - t0 < at:
            time.sleep(0.004)
        api.cmd("LLGesture", "startGesture", {"id": gid}, wait=False)
    print(f"clapped {len(times)} in {times[-1]:.2f} s")
