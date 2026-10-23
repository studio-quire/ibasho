"""The keeper's settings: the `[keeper]` table of the install's config.toml, and the paths that follow from it.

Read here rather than in config.py so the keeper's knobs live beside the code that uses them. Unknown keys are an
error (a typo in a cap should not silently leave the default in force). Tables inside it (`[keeper.NAME]`) are
left alone: none of these settings is a table, so they can't be typos, and other keepers keep their settings there.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from ..config import Config, ConfigError


@dataclass
class KeeperSettings:
    # who and where
    person: str = ""                # what the agent's notes and wake prompt call the person; "" = generic wording
    life: str = ""                  # the agent's memory folder; "" = a "life" folder beside config.toml
    handoff_note: str = "now.md"    # in life/: what a session leaves for the next one
    situation: str = ""             # what this run really is, if not an open-ended life; told at every wake
    # caps (0 = no cap): most people pay for tokens, so the defaults are modest
    hours_per_day: float = 4        # awake time per local day, all sessions together
    sessions_per_day: int = 8       # launches per local day (handoffs and restarts count)
    usd_per_day: float = 0          # API-key logins: stop for the day past this estimated spend
    max_plan_use: float = 0.8       # subscriptions: wind down when the plan's 5-hour or weekly window is this full
    allowed_hours: str = ""         # when the agent may be up, e.g. "09:00-23:00, 01:00-02:00"; "" = any time
    # Claude Code
    claude: str = ""                # path to the claude executable; "" = find it on PATH
    claude_config: str = "separate"  # "separate": its own Claude Code config folder; "shared": yours (development)
    auth: str = "login"             # "login": the config folder's login only; "env": API keys etc. from the environment
    model: str = ""
    effort: str = ""
    permission_mode: str = "acceptEdits"
    settings: str = ""              # Claude Code settings file for sessions; "" = the shipped template, filled in
    # session rhythm
    handoff_at: float = 0.8         # ask for a handoff at this fraction of the context window
    hard_handoff_at: float = 0.93   # past this, close the session without waiting
    context_window: int = 200_000   # assumed until Claude Code reports the real one
    max_session_hours: float = 0    # hand off after this long regardless (0 = never)
    nudge_after_s: int = 90         # a turn ended this long ago, while up: nudge (0 = never)
    nudge_limit: int = 3            # nudges in a row with nothing else happening, then quiet
    tool_wait_min: float = 15       # one tool call running this long = stuck
    stale_min: float = 20           # a turn with no output at all this long = hung
    handoff_grace_min: float = 15
    wind_down_grace_min: float = 10  # caps, hours, plan limits
    stop_grace_min: float = 3       # `ibasho keeper stop` without --now
    logout_grace_min: float = 3     # past a wind-down's deadline before the keeper closes the session
    restart_gap_s: int = 60
    default_nap_min: int = 120      # `ibasho sleep` with no --until
    wake_lead_min: int = 5          # wake this long before a booking starts
    crash_limit: int = 4            # this many crashes or stalls in an hour pauses the keeper

    def validate(self):
        if self.claude_config not in ("separate", "shared"):
            raise ConfigError('keeper.claude_config must be "separate" or "shared"')
        if self.auth not in ("login", "env"):
            raise ConfigError('keeper.auth must be "login" or "env"')
        if self.permission_mode not in ("acceptEdits", "auto", "manual", "dontAsk", "plan", "bypassPermissions"):
            raise ConfigError(f"keeper.permission_mode {self.permission_mode!r} isn't a Claude Code permission mode")
        if not 0 < self.handoff_at < self.hard_handoff_at <= 1:
            raise ConfigError("keeper: need 0 < handoff_at < hard_handoff_at <= 1")
        if not 0 < self.max_plan_use <= 1:
            raise ConfigError("keeper.max_plan_use must be in (0, 1] (1 = no cap)")
        for k in ("hours_per_day", "sessions_per_day", "usd_per_day", "max_session_hours", "nudge_after_s"):
            if getattr(self, k) < 0:
                raise ConfigError(f"keeper.{k} can't be negative")
        parse_hours(self.allowed_hours)
        if "/" in self.handoff_note or "\\" in self.handoff_note:
            raise ConfigError("keeper.handoff_note is a file name in the life folder, not a path")


def load(cfg: Config) -> KeeperSettings:
    try:
        raw = tomllib.loads(cfg.path.read_text(encoding="utf-8")).get("keeper", {})
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{cfg.path}: {e}") from None
    ks = KeeperSettings()
    known = {f.name: f for f in fields(ks)}
    for k, v in raw.items():
        if isinstance(v, dict):
            continue
        if k not in known:
            raise ConfigError(f"keeper.{k}: no such setting")
        # by the declared type: a float setting whose default is written as a whole number still takes 0.5
        want = {"float": float, "int": int, "str": str}.get(str(known[k].type), type(getattr(ks, k)))
        if want is float and isinstance(v, int) and not isinstance(v, bool):
            v = float(v)
        if not isinstance(v, want) or (want is int and isinstance(v, bool)):
            raise ConfigError(f"keeper.{k}: expected {want.__name__}, got {v!r}")
        setattr(ks, k, v)
    ks.validate()
    return ks


# ---------- paths ----------

def keeper_dir(cfg: Config) -> Path:
    return cfg.data_dir / "keeper"


def life_dir(cfg: Config, ks: KeeperSettings) -> Path:
    return cfg.home / Path(ks.life).expanduser() if ks.life else cfg.home / "life"   # relative: to config.toml


def life_dir_of(cfg: Config) -> Path:
    """The life folder, also when the `[keeper]` table has a mistake in it (then the default place): for checks that
    shouldn't fail on an unrelated typo."""
    try:
        return life_dir(cfg, load(cfg))
    except ConfigError:
        return cfg.home / "life"


def claude_config_dir(cfg: Config, ks: KeeperSettings) -> Path | None:
    """The agent's own Claude Code config folder (settings, login, transcripts), or None to share the person's."""
    return cfg.home / "claude" if ks.claude_config == "separate" else None


# ---------- the hours window ----------

HOURS_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_hours(spec: str) -> list[tuple[int, int]]:
    """'09:00-23:00, 23:30-01:00' -> [(540, 1380), (1410, 60)] in minutes of the day. A span may cross midnight."""
    out = []
    for part in [p for p in (spec or "").split(",") if p.strip()]:
        m = HOURS_RE.match(part)
        if not m:
            raise ConfigError(f"keeper.allowed_hours: can't read {part.strip()!r} (like \"18:00-23:30\")")
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        if h1 > 24 or h2 > 24 or m1 > 59 or m2 > 59:
            raise ConfigError(f"keeper.allowed_hours: {part.strip()!r} isn't a time of day")
        out.append(((h1 * 60 + m1) % 1440, (h2 * 60 + m2) % 1440))
    return out


def in_hours(spans: list[tuple[int, int]], minute_of_day: int) -> bool:
    if not spans:
        return True
    for a, b in spans:
        if a == b or (a < b and a <= minute_of_day < b) or (a > b and (minute_of_day >= a or minute_of_day < b)):
            return True
    return False
