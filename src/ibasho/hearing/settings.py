"""The [hearing] table of the install's config file (docs/hearing.md)."""

from __future__ import annotations

import os
import sys
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config, ConfigError

HEARD_KINDS = ("heard", "heard_stream", "heard_rhythm")   # the events hearing writes


@dataclass
class Hearing:
    enabled: bool = False             # `ibasho up` starts hearing; `ibasho hear on` works at all
    model: str = ""                   # faster-whisper model (tiny.en, base.en, small.en, medium.en...); "" = by CPUs
    language: str = ""                # multilingual models only: a code like "de", or empty to detect
    threads: int = 0                  # Whisper's CPU threads; 0 = half the cores, at most 4
    retention_days: float = 2         # heard events are deleted after this long (the daemon prunes them)
    follow_music: bool = False        # transcribe the parcel's music stream (DJs talk); wakes only on my name
    mute_viewer_music: bool = True    # the viewer's own music volume to 0 while hearing runs
    claps: bool = True                # claps and knocks as rhythm events
    heard_as: list[str] = field(default_factory=list)   # how speech recognition mishears my name
    gate_db: float = -45.0            # dBFS a sound must pass to count as an utterance
    voice_device: str = ""            # opt-in: the spare output device voice chat plays on (docs/hearing.md)
    proccap: str = ""                 # Windows: path to proccap.exe; empty = IBASHO_HOME/bin, then PATH


KEYS = {"enabled": bool, "model": str, "language": str, "threads": int, "retention_days": (int, float),
        "follow_music": bool,
        "mute_viewer_music": bool, "claps": bool, "heard_as": list, "gate_db": (int, float), "voice_device": str,
        "proccap": str}


def load(cfg: Config) -> Hearing:
    try:
        raw = tomllib.loads(cfg.path.read_text(encoding="utf-8")).get("hearing", {})
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{cfg.path}: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError("[hearing] must be a table")
    h = Hearing()
    for k, v in raw.items():
        kind = KEYS.get(k)
        if kind is None:
            raise ConfigError(f"hearing.{k}: unknown setting (known: {', '.join(KEYS)})")
        if isinstance(v, bool) and kind is not bool or not isinstance(v, kind):
            raise ConfigError(f"hearing.{k}: unexpected value {v!r}")
        setattr(h, k, v)
    h.model = h.model.strip() or default_model()
    h.gate_db = float(h.gate_db)
    h.retention_days = float(h.retention_days)
    if h.retention_days <= 0:
        raise ConfigError("hearing.retention_days must be more than 0 (days; 0.5 is twelve hours)")
    h.heard_as = [str(w).strip() for w in h.heard_as if str(w).strip()]
    h.voice_device = h.voice_device.strip()
    if h.threads < 0:
        raise ConfigError("hearing.threads can't be negative")
    if not -80 <= h.gate_db <= 0:
        raise ConfigError("hearing.gate_db is in dBFS: between -80 and 0")
    return h


def default_model() -> str:
    """small.en with 4 CPUs or more; base.en below, where small.en beside a software-drawn viewer takes longer than
    the speech itself (docs/hearing.md, "How much CPU it takes")."""
    from ..launch import cpus_available
    return "small.en" if cpus_available() >= 4 else "base.en"


def threads(h: Hearing) -> int:
    from ..launch import cpus_available   # a container's CPU quota, not the host's cores
    return h.threads or max(1, min(4, cpus_available() // 2))


def models_dir(cfg: Config) -> Path:
    return cfg.home / "models"


def proccap_path(cfg: Config, h: Hearing) -> Path | None:
    if h.proccap:
        p = Path(h.proccap)
        return p if p.is_file() else None
    p = cfg.home / "bin" / "proccap.exe"
    if p.is_file():
        return p
    found = shutil.which("proccap")
    return Path(found) if found else None


def capture_tool(cfg: Config, h: Hearing) -> Path | None:
    """What captures sound here: proccap.exe on Windows, the sound server's parec elsewhere."""
    if sys.platform == "win32":
        return proccap_path(cfg, h)
    found = shutil.which("parec")
    return Path(found) if found else None


def missing_tool() -> str:
    """Why capture_tool found nothing, and what the person does about it."""
    if sys.platform == "win32":
        return ("proccap.exe not found: the person builds it with audio\\build.bat (or puts a release's copy in "
                "IBASHO_HOME\\bin), or sets hearing.proccap")
    return "parec not found: hearing on Linux needs a sound server's tools (pulseaudio-utils; the container has them)"
