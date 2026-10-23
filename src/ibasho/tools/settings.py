"""The [tools] table of the install's config file (docs/tools.md), and the paths that follow from it."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..config import Config, ConfigError


@dataclass
class Tools:
    enabled: bool = False        # `ibasho tool NAME` runs at all; the person turns it on
    network: bool = False        # tools may reach the internet (and, depending on Windows, the local network)
    timeout_s: float = 100       # one run's limit (under Claude Code's default 2-minute command limit)
    max_output_kb: int = 64      # what a run may print before it's stopped (all of it goes into the model's context)
    memory_mb: int = 1024        # all of a run's processes together
    cpu_percent: int = 50        # share of the whole machine a run may use


KEYS = {"enabled": bool, "network": bool, "timeout_s": (int, float), "max_output_kb": int, "memory_mb": int,
        "cpu_percent": int}


def load(cfg: Config) -> Tools:
    try:
        raw = tomllib.loads(cfg.path.read_text(encoding="utf-8")).get("tools", {})
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{cfg.path}: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError("[tools] must be a table")
    t = Tools()
    for k, v in raw.items():
        kind = KEYS.get(k)
        if kind is None:
            raise ConfigError(f"tools.{k}: unknown setting (known: {', '.join(KEYS)})")
        if isinstance(v, bool) and kind is not bool or not isinstance(v, kind):
            raise ConfigError(f"tools.{k}: unexpected value {v!r}")
        setattr(t, k, v)
    t.timeout_s = float(t.timeout_s)
    if not 1 <= t.timeout_s <= 86400:
        raise ConfigError("tools.timeout_s must be between 1 and 86400 seconds")
    if not 1 <= t.max_output_kb <= 10_000:
        raise ConfigError("tools.max_output_kb must be between 1 and 10000")
    if not 64 <= t.memory_mb <= 1_000_000:
        raise ConfigError("tools.memory_mb must be at least 64")
    if not 1 <= t.cpu_percent <= 100:
        raise ConfigError("tools.cpu_percent must be between 1 and 100")
    return t


def runtime_dir(cfg: Config) -> Path:
    """The Python that tools run on, and the packages the person installed for them. Beside config.toml, never in
    the life folder: sessions can't write here, and tools can only read it."""
    return cfg.home / "tools-runtime"


def tools_dir(life: Path) -> Path:
    return life / "tools"
