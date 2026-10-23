"""The event log: everything the daemon hears, one JSON line per event, one file per local day.

Events are `{seq, t, kind, data}` with `seq` increasing across daemon restarts (its high-water mark is kept beside
the files, so pruning or forgetting never winds it back). Days age out after the retention period, pruned while
the daemon runs, and `forget` takes one person's events out of every file (their words, and ours to them).
The newest events are also kept in memory for the HTTP API.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Iterable

FILE_RE = re.compile(r"^events-(\d{4}-\d{2}-\d{2})\.jsonl$")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# Names count only under these keys, at any depth (notifications nest theirs in substitutions and payloads).
NAME_KEYS = {"from", "to", "username", "conversation", "FROM", "NAME", "USERNAME", "FIRST", "OBJECTFROMNAME"}


class EventLog:
    def __init__(self, directory: Path, retention_days: int = 7, memory: int = 5000):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.recent: collections.deque[dict] = collections.deque(maxlen=memory)
        self._changed = asyncio.Event()
        self._seq_file = self.dir / "seq"
        for ev in self._tail(memory):
            self.recent.append(ev)
        try:
            saved = int(self._seq_file.read_text())
        except (OSError, ValueError):
            saved = 0
        self.seq = max(saved, self.recent[-1]["seq"] if self.recent else 0)

    # ---- files ----

    def files(self) -> list[Path]:
        return sorted(p for p in self.dir.iterdir() if FILE_RE.match(p.name))

    def _file_for(self, t: float) -> Path:
        return self.dir / f"events-{dt.date.fromtimestamp(t).isoformat()}.jsonl"

    @staticmethod
    def _read(path: Path) -> list[dict]:
        out = []
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
        return out

    def _tail(self, n: int) -> list[dict]:
        out: list[dict] = []
        for path in reversed(self.files()):
            out[:0] = self._read(path)
            if len(out) >= n:
                break
        return out[-n:]

    # ---- writing ----

    def append(self, kind: str, data, t: float | None = None) -> dict:
        self.seq += 1
        ev = {"seq": self.seq, "t": round(t if t is not None else time.time(), 3), "kind": kind, "data": data}
        with self._file_for(ev["t"]).open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self._seq_file.write_text(str(self.seq))
        self.recent.append(ev)
        self._changed.set()
        self._changed = asyncio.Event()
        return ev

    # ---- reading ----

    def since(self, seq: int, kinds: Iterable[str] | None = None, limit: int | None = None) -> list[dict]:
        if not self.recent or seq + 1 >= self.recent[0]["seq"]:
            evs = [e for e in self.recent if e["seq"] > seq]
        else:  # older than memory holds: read the files
            evs = [e for p in self.files() for e in self._read(p) if e.get("seq", 0) > seq]
        if kinds:
            ks = set(kinds)
            evs = [e for e in evs if e["kind"] in ks]
        return evs[:limit] if limit else evs

    async def wait(self, seq: int, timeout: float):
        """Return once there's an event after `seq`, or after `timeout` seconds."""
        if self.seq > seq or timeout <= 0:
            return
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    # ---- retention and forgetting ----

    def cutoff(self, now: float | None = None) -> dt.date:
        """The oldest day kept: today and the days before it, retention_days in all."""
        return dt.date.fromtimestamp(now or time.time()) - dt.timedelta(days=self.retention_days - 1)

    def prune(self, now: float | None = None) -> int:
        """Delete day files older than the retention period. Returns how many went."""
        cutoff = self.cutoff(now)
        gone = 0
        for p in self.files():
            if dt.date.fromisoformat(FILE_RE.match(p.name).group(1)) < cutoff:
                p.unlink(missing_ok=True)
                gone += 1
        if gone:
            keep = [e for e in self.recent if dt.date.fromtimestamp(e["t"]) >= cutoff]
            self.recent.clear()
            self.recent.extend(keep)
        return gone

    def forget(self, match: Callable[[dict], bool]) -> int:
        """Remove every event `match` accepts, from the files and from memory. Returns how many."""
        removed = 0
        for p in self.files():
            evs = self._read(p)
            keep = [e for e in evs if not match(e)]
            if len(keep) != len(evs):
                removed += len(evs) - len(keep)
                tmp = p.with_suffix(".tmp")
                with tmp.open("w", encoding="utf-8") as f:
                    for e in keep:
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
                os.replace(tmp, p)
        keep = [e for e in self.recent if not match(e)]
        self.recent.clear()
        self.recent.extend(keep)
        return removed


def notification_time(v) -> float | None:
    """A notification's `time` (the viewer's clock, UTC, ISO 8601) as a timestamp."""
    try:
        t = dt.datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp()


def involves(ids: Iterable[str], names: Iterable[str]) -> Callable[[dict], bool]:
    """A matcher for events from, to or about one person: their avatar id anywhere in the event (notifications
    nest it, and offers and IM toasts carry their words), or one of their exact names (case-insensitive) under a
    name key. P2P sessions with them count; group chat only for their own lines."""
    idset = {str(i).lower() for i in ids if i}
    nameset = {n.strip().lower() for n in names if n and n.strip()}

    def hit(v, key=None) -> bool:
        if isinstance(v, dict):
            if v.get("session_type") == "p2p" and str(v.get("session_name") or "").strip().lower() in nameset:
                return True
            return any(hit(x, k) for k, x in v.items())
        if isinstance(v, list):
            return any(hit(x, key) for x in v)
        if not isinstance(v, str):
            return False
        if idset and any(u.lower() in idset for u in UUID_RE.findall(v)):
            return True
        return key in NAME_KEYS and v.strip().lower() in nameset

    return lambda ev: hit(ev.get("data"))
