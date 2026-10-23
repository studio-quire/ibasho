"""The keeper's files, shared by the keeper process and the `ibasho` commands (the agent's and the person's).

All in data/keeper/:
  state.json     nap, crash pause, plan-limit wait (the keeper and the commands both write it)
  status.json    written by the keeper: what it's doing and why, the running session
  asleep.json    written by `ibasho sleep` inside a session: that session is done (or handing off)
  stop.json      written by `ibasho keeper stop`
  notes.jsonl    the notes channel between the person and the agent, append-only ops
  bookings.json  the agent's bookings: times it wants to be up
  usage.json     awake time, sessions and estimated spend per local day, for the caps
  keeper.log     what the keeper did (and the agent's notes, relayed)
  sessions/      one readable log per session (the agent's own words and tool calls, not what tools returned)

Times are epoch seconds in the files, except bookings, which keep the wall-clock time they were made with.
keeper.log, sessions/ and notes age out with the install's retention (runner.prune_logs); notes still waiting to
be delivered or read stay until they are.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

NOTE_MAX = 4000


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    _replace(tmp, path)


def _replace(tmp: Path, path: Path):
    for _ in range(40):  # Windows refuses to replace a file another process has open for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def _fold_notes(recs: list[dict]) -> list[dict]:
    """notes.jsonl's ops -> the notes, oldest first."""
    msgs: dict[str, dict] = {}
    for r in recs:
        op, i = r.get("op"), r.get("id")
        if op == "note":
            msgs[i] = {"id": i, "t": r["t"], "from": r.get("from"), "text": r.get("text", ""),
                       "delivered": None, "read": None}
        elif i in msgs and op == "delivered" and not msgs[i]["delivered"]:
            msgs[i]["delivered"] = {"t": r["t"], "how": r.get("how")}
        elif i in msgs and op == "read" and not msgs[i]["read"]:
            msgs[i]["read"] = r["t"]
    return sorted(msgs.values(), key=lambda m: m["t"])


class _FileLock:
    """A cross-process lock held for a moment around notes.jsonl writes, so compacting the file can't drop a note
    another process appends meanwhile. Released when the process dies, however it dies."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+")
        if sys.platform == "win32":
            import msvcrt
            self.f.seek(0)
            for _ in range(200):   # up to 10 s
                try:
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                    return self
                except OSError:
                    time.sleep(0.05)
            self.f.close()
            raise TimeoutError(f"{self.path} stayed locked")
        import fcntl
        fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if sys.platform == "win32":
            import msvcrt
            self.f.seek(0)
            try:
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self.f.close()


class Store:
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def path(self, name: str) -> Path:
        return self.dir / name

    # ---- state ----

    STATE_DEFAULTS = {"nap_until": None, "paused": None, "limited_until": None, "limited_why": None,
                      "windows": {}}   # context window by model, as Claude Code reported them

    def state(self) -> dict:
        return {**self.STATE_DEFAULTS, **_read(self.path("state.json"), {})}

    def update_state(self, **kw) -> dict:
        s = self.state()
        s.update(kw)
        _write(self.path("state.json"), s)
        return s

    def status(self) -> dict:
        return _read(self.path("status.json"), {})

    def save_status(self, st: dict):
        _write(self.path("status.json"), st)

    # ---- sleep and stop ----

    def mark_asleep(self, session: str | None, until: float | None, handoff: bool):
        _write(self.path("asleep.json"), {"session": session, "t": time.time(), "until": until, "handoff": handoff})
        if until:
            self.update_state(nap_until=until)

    def asleep(self) -> dict | None:
        return _read(self.path("asleep.json"), None)

    def clear_asleep(self):
        self.path("asleep.json").unlink(missing_ok=True)

    def request_stop(self, now: bool):
        _write(self.path("stop.json"), {"t": time.time(), "now": now})

    def stop_request(self) -> dict | None:
        return _read(self.path("stop.json"), None)

    def clear_stop(self):
        self.path("stop.json").unlink(missing_ok=True)

    # ---- usage, for the caps ----

    def usage(self, day: str) -> dict:
        return {"awake_s": 0.0, "sessions": 0, "usd": 0.0, **_read(self.path("usage.json"), {}).get(day, {})}

    def add_usage(self, day: str, awake_s: float = 0, sessions: int = 0, usd: float = 0):
        all_days = _read(self.path("usage.json"), {})
        u = {"awake_s": 0.0, "sessions": 0, "usd": 0.0, **all_days.get(day, {})}
        u["awake_s"] = round(u["awake_s"] + awake_s, 1)
        u["sessions"] += sessions
        u["usd"] = round(u["usd"] + usd, 4)
        all_days[day] = u
        keep = sorted(all_days)[-60:]
        _write(self.path("usage.json"), {d: all_days[d] for d in keep})

    # ---- notes: the person and the agent ----
    # Append-only ops, so the keeper, the agent's `ibasho notes` and the person's can all write without clobbering
    # each other: note (from "person" or "agent"), delivered, read. Only the keeper's pruning rewrites the file, and
    # it holds notes.lock while it does, as every append does.

    def _notes_lock(self) -> _FileLock:
        return _FileLock(self.path("notes.lock"))

    def _note_op(self, **rec):
        with self._notes_lock():
            with open(self.path("notes.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), **rec}, ensure_ascii=False) + "\n")

    def _note_records(self) -> list[dict]:
        try:
            lines = self.path("notes.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
        return out

    def notes(self) -> list[dict]:
        """Oldest first: {id, t, from: person|agent, text, delivered: {t, how}|None, read: t|None}."""
        return _fold_notes(self._note_records())

    def prune_notes(self, cutoff: float) -> int:
        """Drop notes written before `cutoff` that have done their job: the person's once delivered or read, the
        agent's once the person read them. Notes still waiting stay. Returns how many were dropped."""
        if not self.path("notes.jsonl").exists():
            return 0
        with self._notes_lock():
            recs = self._note_records()
            notes = _fold_notes(recs)
            drop = {m["id"] for m in notes if m["t"] < cutoff and
                    (m["read"] or (m["from"] == "person" and m["delivered"]))}
            live = {m["id"] for m in notes} - drop
            keep = [r for r in recs if r.get("id") in live]   # also sheds ops for notes already gone
            if len(keep) == len(recs):
                return 0
            tmp = self.path(f"notes.jsonl.{os.getpid()}.tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
            _replace(tmp, self.path("notes.jsonl"))
        return len(drop)

    def add_note(self, text: str, sender: str) -> str:
        text = text.strip()
        if not text:
            raise ValueError("empty note")
        if len(text) > NOTE_MAX:
            raise ValueError(f"too long: {len(text)} characters, at most {NOTE_MAX}")
        if sender not in ("person", "agent"):
            raise ValueError(f"unknown sender {sender!r}")
        i = uuid.uuid4().hex[:8]
        self._note_op(op="note", id=i, **{"from": sender}, text=text)
        return i

    def mark_notes(self, ids, op: str, **extra):
        for i in ids:
            self._note_op(op=op, id=i, **extra)

    def undelivered(self, sender: str = "person") -> list[dict]:
        return [m for m in self.notes() if m["from"] == sender and not m["delivered"] and not m["read"]]

    def unread(self, sender: str) -> list[dict]:
        return [m for m in self.notes() if m["from"] == sender and not m["read"]]

    # ---- bookings ----

    def bookings(self) -> list[dict]:
        return _read(self.path("bookings.json"), [])

    def save_bookings(self, bs: list[dict]):
        bs.sort(key=lambda b: b["start"])
        _write(self.path("bookings.json"), bs)

    def add_booking(self, start: dt.datetime, end: dt.datetime, why: str, repeat: str | None = None,
                    until: str | None = None, now: dt.datetime | None = None) -> dict:
        if end <= start:
            raise ValueError("the booking ends before it starts")
        if repeat and end - start > dt.timedelta(days=REPEATS[repeat]):
            raise ValueError(f"a {repeat} booking can't be longer than its repeat")
        now = now or local_now()
        bs = [b for b in self.bookings() if is_live(b, now)]  # prune finished ones
        b = {"id": uuid.uuid4().hex[:6], "start": iso(start), "end": iso(end), "why": why.strip()}
        if repeat:
            b["repeat"] = repeat
            if until:
                b["until"] = until
        bs.append(b)
        self.save_bookings(bs)
        return b

    def remove_booking(self, key: str, now: dt.datetime | None = None) -> bool:
        """An id removes the booking (every day of a repeating one); id@YYYY-MM-DD drops one day of a repeating one."""
        bs = self.bookings()
        bid, _, day = key.partition("@")
        if day:
            for b in bs:
                if b["id"] == bid and b.get("repeat"):
                    if day in (b.get("skip") or []):
                        return False
                    today = (now or local_now()).date().isoformat()
                    b["skip"] = sorted(d for d in {*(b.get("skip") or []), day} if d >= today)
                    self.save_bookings(bs)
                    return True
            return False
        keep = [b for b in bs if b["id"] != bid]
        self.save_bookings(keep)
        return len(keep) != len(bs)


# ---------- time ----------

SLT = "America/Los_Angeles"   # Second Life time, which event listings use


def local_now(t: float | None = None) -> dt.datetime:
    return dt.datetime.fromtimestamp(time.time() if t is None else t).astimezone()


def iso(t: dt.datetime | None) -> str | None:
    return t.isoformat(timespec="minutes") if t else None


def from_iso(s: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(s).astimezone() if s else None


def _zone(tz: str | None):
    if not tz:
        return None
    from zoneinfo import ZoneInfo
    return ZoneInfo(tz)


def parse_time(s: str, tz: str | None = None, after: dt.datetime | None = None) -> dt.datetime:
    """'20:00' (the next one), '9:30pm', 'tomorrow 08:00', '10-24 20:00', '2026-10-24 20:00', or ISO. `tz` is a zone
    name the time is in (SLT for Second Life time); default the machine's own."""
    s = s.strip()
    zone = _zone(tz)
    base = (after or local_now())
    base = base.astimezone(zone) if zone else base
    try:
        t = dt.datetime.fromisoformat(s)
        if t.tzinfo:
            return t.astimezone()
        return (t.replace(tzinfo=zone) if zone else t.astimezone()).astimezone()
    except ValueError:
        pass
    m = re.fullmatch(r"(?:(tomorrow|today)\s+|(?:(\d{4})-)?(\d{1,2})-(\d{1,2})\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?", s, re.I)
    if not m:
        raise ValueError(f"can't read the time {s!r} (like '20:00', 'tomorrow 9:30pm', '2026-10-24 20:00')")
    word, y, mo, d, hh, mm, ap = m.groups()
    hh, mm = int(hh), int(mm)
    if ap:
        hh = hh % 12 + (12 if ap.lower() == "pm" else 0)
    t = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if mo:
        t = t.replace(year=int(y) if y else base.year, month=int(mo), day=int(d))
    elif word and word.lower() == "tomorrow":
        t += dt.timedelta(days=1)
    elif not word and t <= base:
        t += dt.timedelta(days=1)
    return t.astimezone()


def parse_end(s: str, start: dt.datetime, tz: str | None = None) -> dt.datetime:
    """An end time, or a length after `start`: '+90m', '+2h'."""
    m = re.fullmatch(r"\+\s*(\d+(?:\.\d+)?)\s*(m|min|h|hr|hours?)?", s.strip(), re.I)
    if m:
        n = float(m.group(1))
        return start + (dt.timedelta(hours=n) if (m.group(2) or "m").lower().startswith("h") else dt.timedelta(minutes=n))
    return parse_time(s, tz, after=start)


def parse_date(s: str, today: dt.date | None = None) -> str:
    """'2026-10-24' or '10-24' (the next one) -> 'YYYY-MM-DD'."""
    m = re.fullmatch(r"(?:(\d{4})-)?(\d{1,2})-(\d{1,2})", s.strip())
    if not m:
        raise ValueError(f"can't read the date {s!r} (like '10-24' or '2026-10-24')")
    y, mo, d = m.groups()
    today = today or local_now().date()
    day = dt.date(int(y) if y else today.year, int(mo), int(d))
    if not y and day < today:
        day = day.replace(year=day.year + 1)
    return day.isoformat()


# ---------- bookings ----------
# {id, start, end, why} plus, for a repeating one, "repeat" (daily|weekly), an optional "until" (last date, inclusive)
# and "skip" (dates dropped). Each occurrence has a key: the id for a one-off, id@YYYY-MM-DD for a repeating one.

REPEATS = {"daily": 1, "weekly": 7}


def days_later(t: dt.datetime, days: int) -> dt.datetime:
    """The same wall-clock time `days` later in machine time, so a daily booking doesn't drift across DST."""
    return (t.replace(tzinfo=None) + dt.timedelta(days=days)).astimezone()


def _spans(b: dict, t0: dt.datetime, t1: dt.datetime):
    start, end = from_iso(b["start"]), from_iso(b["end"])
    step = REPEATS.get(b.get("repeat"))
    if not step:
        yield b["id"], start, end
        return
    until, skip = b.get("until"), set(b.get("skip") or ())
    n = max(0, (t0 - end).days // step - 1)
    while True:
        s, e = days_later(start, n * step), days_later(end, n * step)
        d = s.date().isoformat()
        if s >= t1 or (until and d > until):
            return
        if d not in skip:
            yield f"{b['id']}@{d}", s, e
        n += 1


def occurrences(bs: list[dict], t0: dt.datetime, t1: dt.datetime) -> list[dict]:
    """Booked spans overlapping [t0, t1), soonest first: {key, id, start, end, why, repeat}."""
    out = [{"key": k, "id": b["id"], "start": s, "end": e, "why": b.get("why", ""), "repeat": b.get("repeat")}
           for b in bs for k, s, e in _spans(b, t0, t1) if e > t0 and s < t1]
    return sorted(out, key=lambda o: o["start"])


def is_live(b: dict, t: dt.datetime) -> bool:
    if b.get("repeat"):
        return not b.get("until") or b["until"] >= (t - dt.timedelta(days=1)).date().isoformat()
    return from_iso(b["end"]) > t - dt.timedelta(days=1)


def active_booking(bs: list[dict], t: dt.datetime, lead_min: float) -> dict | None:
    """The occurrence that wants the agent up at t (from lead_min before its start to its end)."""
    occ = occurrences(bs, t, t + dt.timedelta(minutes=lead_min))
    return occ[0] if occ else None
