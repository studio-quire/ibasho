"""The keeper's state machine: when the agent should be up, and what to do with the session that's running.

One tick every couple of seconds. With no session, start one if the agent should be up. With one:
- `ibasho sleep` from that session (asleep.json) ends it; `--handoff` keeps the avatar logged in and a fresh session
  follows in seconds, a plain sleep logs out and naps (until --until, or the default nap).
- It exiting on its own is a crash; a stuck tool call, a turn with no output for a long while, or a turn that ended
  with nothing left that could wake it are stalls. Both restart with backoff, and too many in an hour pause the
  keeper until the person resumes it.
- Near the context limit it asks for a handoff (no auto-compaction: the agent writes its own handoff note).
- When the agent shouldn't be up any more (a cap reached, outside the allowed hours, the plan's limits, a stop), it
  asks for a wind-down with some grace, then closes the session.
- A turn that ended while the agent is up gets a nudge after a while; a few in a row with nothing else happening and
  the keeper goes quiet until something does.
- Notes from the person go in: straight into the session between turns, or through the watch mid-turn.

Everything outside (the clock, starting a session, the daemon) is passed in, so tests drive it with fakes.
"""

from __future__ import annotations

import datetime as dt
import os
import time
import uuid
from typing import Callable, Protocol

from . import prompts
from .session import Session, watch_armed
from .settings import KeeperSettings, in_hours, parse_hours
from .store import Store, active_booking, local_now, occurrences

CRASH_WINDOW_S = 3600
MAX_BACKOFF_S = 900


class World(Protocol):
    def post(self, text: str) -> bool: ...        # into the daemon's event log, where the watch prints it
    def logged_in(self) -> bool: ...
    def down(self): ...                           # log the avatar out, stop viewer and daemon


class Keeper:
    TICK_S = 2

    def __init__(self, *, ks: KeeperSettings, store: Store, launch: Callable[[str], Session], world: World,
                 agent_name: str, grid: str, life: str, handoff_exists: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = print,
                 armed: Callable[[Session], bool] | None = None, extra_prompt: str = "",
                 tools_note: Callable[[], str] = lambda: ""):
        self.ks, self.store, self.launch, self.world = ks, store, launch, world
        self.agent_name, self.grid, self.life = agent_name, grid, life
        self.handoff_exists = handoff_exists
        self.now = clock
        self.log = log
        self.armed = armed or (lambda s: watch_armed(s.processes()))
        self.extra_prompt = extra_prompt
        self.tools_note = tools_note
        self.hours = parse_hours(ks.allowed_hours)
        self.sess: Session | None = None
        self.info: dict = {}
        self.crashes: list[float] = []
        self.next_start = 0.0
        self.avatar_ours = False       # the avatar was last up under one of our sessions
        self.stopping = False          # winding down to exit
        self.exiting = False
        self.unflushed = 0.0           # awake seconds not yet in usage.json
        self.next_reason: str | None = None
        self.why = ""
        self.started_at = clock()
        self.agent_notes_seen = {n["id"] for n in store.notes() if n["from"] == "agent"}

    # ---------- when to be up ----------

    def day(self, t: float) -> str:
        return local_now(t).date().isoformat()

    def usage(self, t: float) -> dict:
        u = self.store.usage(self.day(t))
        u["awake_s"] += self.unflushed
        if self.sess:
            u["usd"] += self.sess.cost_usd
        return u

    def cap_reached(self, t: float, starting: bool) -> str | None:
        ks, u = self.ks, self.usage(t)
        if ks.hours_per_day and u["awake_s"] >= ks.hours_per_day * 3600:
            return f"today's {ks.hours_per_day:g} h awake are used up (keeper.hours_per_day)"
        if starting and ks.sessions_per_day and u["sessions"] >= ks.sessions_per_day:
            return f"today's {ks.sessions_per_day} sessions are used up (keeper.sessions_per_day)"
        if ks.usd_per_day and u["usd"] >= ks.usd_per_day:
            return f"today's estimated spend reached ${ks.usd_per_day:g} (keeper.usd_per_day)"
        return None

    def decide(self, t: float) -> tuple[bool, str, dict | None, str | None]:
        """(want the agent up, why, the booking that wants it, what's keeping it down)."""
        st = self.store.state()
        lt = local_now(t)
        if self.stopping:
            return False, "the keeper is stopping", None, "stop"
        if st["paused"]:
            return False, f"paused: {st['paused']} (`ibasho keeper resume`)", None, "paused"
        if st["limited_until"] and st["limited_until"] > t:
            return False, f"{st['limited_why']}; waiting until {local_now(st['limited_until']):%a %H:%M}", None, "limited"
        if cap := self.cap_reached(t, starting=self.sess is None):
            return False, cap, None, "cap"
        if not in_hours(self.hours, lt.hour * 60 + lt.minute):
            return False, f"outside the allowed hours ({self.ks.allowed_hours})", None, "hours"
        if b := active_booking(self.store.bookings(), lt, self.ks.wake_lead_min):
            return True, f"booking: {b['why']}", b, None
        nap = st["nap_until"]
        if nap and nap > t:
            return False, f"napping (its own choice) until {local_now(nap):%a %H:%M}", None, "nap"
        return True, "free time", None, None

    # ---------- the tick ----------

    def tick(self):
        t = self.now()
        self.handle_stop(t)
        s = self.sess
        if s:
            self.count_awake(t)
            self.check_rate(s, t)
        want, why, booking, kind = self.decide(t)
        if s:
            info = self.info
            asleep = self.store.asleep()
            if asleep and asleep.get("session") == s.id:
                info.setdefault("asleep_at", t)
                if not s.alive() or not s.turn_open or t - info["asleep_at"] > 30:  # let it say its last words
                    handoff = bool(asleep.get("handoff"))
                    self.end(t, "went to sleep" + (" (handoff)" if handoff else ""), handoff=handoff, want=want)
            elif limit := self.hit_limit(s, t):
                self.end(t, limit, want=False)
            elif not s.alive():
                w = info.get("wind_down")
                self.end(t, "the session closed during its wind-down" if w else
                         f"the session exited unexpectedly (exit code {s.exit_code()})", crash=not w, want=want)
            elif stalled := self.stalled(s, t):
                self.end(t, f"stalled: {stalled}", crash=True, want=want)
            else:
                self.manage(s, t, want, why, kind)
        self.relay_agent_notes()
        if s and not self.sess:  # it just ended, which may have changed things (a nap, a pause, a limit)
            want, why, booking, kind = self.decide(t)
        if not self.sess:
            if self.stopping:
                self.exiting = True
            elif want and t >= self.next_start:
                self.start(t, f"{why}; {self.next_reason}" if self.next_reason else why, booking)
                self.next_reason = None
            elif not want and self.avatar_ours:
                self.avatar_down()
        self.why = why
        self.write_status(t, want, why)

    def manage(self, s: Session, t: float, want: bool, why: str, kind: str | None):
        ks, info = self.ks, self.info
        w = info.get("wind_down")
        if not want:
            grace = ks.stop_grace_min if kind == "stop" else ks.wind_down_grace_min
            self.wind_down(t, self.agent_why(kind, why), grace, kind or "down")
        elif w and w["cancellable"]:
            self.cancel_wind_down(s)
        window = self.window(s)
        if s.context >= ks.hard_handoff_at * window:
            self.end(t, f"context at {s.context // 1000}k of {window // 1000}k, past the hard limit; closed it",
                     stay_up=want, want=want)
            return
        if s.context >= ks.handoff_at * window:
            self.wind_down(t, f"Context is at {s.context // 1000}k of {window // 1000}k", ks.handoff_grace_min,
                           "context", cancellable=False, handoff=want)
        if want and ks.max_session_hours and t - s.started > ks.max_session_hours * 3600:
            self.wind_down(t, f"This session has run {ks.max_session_hours:g} h", ks.handoff_grace_min,
                           "max_hours", cancellable=False, handoff=True)
        self.deliver_notes(s, t)
        w = info.get("wind_down")
        if w and not w["delivered"]:
            w["delivered"] = self.say(s, w["msg"])
        if w and t > w["deadline"] + ks.logout_grace_min * 60:
            self.end(t, "it didn't finish winding down in time; closed it", stay_up=w["handoff"], want=want)
        elif not w:
            self.maybe_nudge(s, t)

    def agent_why(self, kind: str | None, why: str) -> str:
        """The reason for a wind-down, as the agent hears it."""
        if kind == "stop":
            who = prompts.person_label(self.ks.person)
            return f"{who[:1].upper()}{who[1:]} is stopping the keeper"
        if kind == "nap":
            nap = self.store.state()["nap_until"]
            return f"The booking that woke you is over, and your nap runs until {local_now(nap):%a %H:%M}"
        return why[:1].upper() + why[1:]

    def window(self, s: Session) -> int:
        """The session's context window. Claude Code reports it only when a turn ends, and a session may live in one
        long turn, so remember what each model reported before."""
        known = self.store.state().get("windows") or {}
        if s.context_window:
            if s.model and known.get(s.model) != s.context_window:
                self.store.update_state(windows={**known, s.model: s.context_window})
            return s.context_window
        return known.get(s.model or "") or self.ks.context_window

    # ---------- talking to the session ----------

    def say(self, s: Session, text: str, watch_text: str | None = None) -> bool:
        """Between turns, straight in as a user message. Mid-turn, through the watch if one is running (a user
        message sent mid-turn lands beside a tool result, where it reads like an injection). Else not yet."""
        if not s.turn_open:
            return s.send(text)
        if self.armed(s):
            return self.world.post(watch_text or text)
        return False

    def deliver_notes(self, s: Session, t: float):
        notes = self.store.undelivered("person")
        if not notes:
            return
        text = "\n\n".join(prompts.note_line(self.ks.person, n) for n in notes)
        who = prompts.person_label(self.ks.person)
        watch_text = "\n".join(f"NOTE from {who}: {prompts.flat(n['text'])} (`ibasho notes` replies)" for n in notes)
        how = "session" if not s.turn_open else "watch"
        if self.say(s, text, watch_text):
            self.store.mark_notes([n["id"] for n in notes], "delivered", how=how)
            self.log(f"delivered {len(notes)} note{'s' if len(notes) > 1 else ''} ({how})")

    def maybe_nudge(self, s: Session, t: float):
        ks, info = self.ks, self.info
        mark = info.get("nudge_mark")
        if mark and (s.turns > mark[0] + 1 or s.tool_calls > mark[1]):
            info["nudges"] = 0          # something besides a bare reply to the last nudge has happened
            info["nudge_mark"] = None
        if not ks.nudge_after_s or s.turn_open or s.turn_ended_at is None:
            return
        idle = t - max(s.turn_ended_at, info.get("nudged_at") or 0)
        if idle < ks.nudge_after_s or info.get("nudges", 0) >= ks.nudge_limit:
            return
        n = info.get("nudges", 0) + 1
        if s.send(prompts.nudge(n, ks.nudge_limit, t - s.turn_ended_at)):
            info.update(nudges=n, nudged_at=t, nudge_mark=(s.turns, s.tool_calls))
            info["nudges_total"] = info.get("nudges_total", 0) + 1
            self.log(f"nudged (turn ended {t - s.turn_ended_at:.0f} s ago; {n} in a row)")

    def wind_down(self, t: float, why: str, grace_min: float, kind: str, cancellable: bool = True,
                  handoff: bool = False):
        w = self.info.get("wind_down")
        deadline = t + grace_min * 60
        if w and w["deadline"] <= deadline:
            return  # an earlier (or the same) request stands
        msg = prompts.wind_down(why, grace_min, handoff, self.ks.handoff_note)
        self.info["wind_down"] = {"why": why, "kind": kind, "t": t, "deadline": deadline, "cancellable": cancellable,
                                  "handoff": handoff, "msg": msg, "delivered": False}
        self.log(f"wind-down asked: {why} ({grace_min:g} min)")

    def cancel_wind_down(self, s: Session):
        w = self.info.pop("wind_down")
        if w["delivered"]:
            self.say(s, prompts.CANCEL)
        self.log("wind-down cancelled")

    # ---------- trouble ----------

    def stalled(self, s: Session, t: float) -> str | None:
        ks = self.ks
        if s.turns == 0 and s.context == 0 and t - s.started > 180:
            return "no reply from Claude Code 3 min after launch"
        p = s.pending_tool()
        if p and t - p[1] > ks.tool_wait_min * 60:
            return f"a {p[0]} call has run {(t - p[1]) / 60:.0f} min"
        if s.turn_open and not p and not s.long_running() and t - s.last_activity > ks.stale_min * 60:
            return f"no output for {(t - s.last_activity) / 60:.0f} min mid-turn"
        if (not s.turn_open and ks.nudge_after_s and s.turn_ended_at is not None
                and self.info.get("nudges", 0) >= ks.nudge_limit
                and t - s.last_activity > ks.nudge_after_s * (ks.nudge_limit + 1) and not self.armed(s)):
            return "its turn ended, nudges went unanswered and no watch is running that could wake it"
        return None

    def check_rate(self, s: Session, t: float):
        """The plan's own limits, which the person's own use of Claude shares: stop short of them."""
        r = s.rate or {}
        windows = r.get("unifiedWindows") or {}
        over = [(name, w) for name, w in windows.items()
                if isinstance(w, dict) and (w.get("utilization") or 0) >= self.ks.max_plan_use and w.get("resetsAt")]
        if over and self.ks.max_plan_use < 1:
            name, w = max(over, key=lambda x: x[1]["resetsAt"])
            until = float(w["resetsAt"])
            if until > t and (self.store.state()["limited_until"] or 0) < until:
                label = {"five_hour": "5-hour", "seven_day": "weekly"}.get(name, name.replace("_", " "))
                why = (f"the plan's {label} window is {w['utilization']:.0%} used "
                       f"(keeper.max_plan_use {self.ks.max_plan_use:g})")
                self.store.update_state(limited_until=until, limited_why=why)
                self.log(why)

    def hit_limit(self, s: Session, t: float) -> str | None:
        """The plan refused: end the session (not a crash) and wait for the reset."""
        r = s.rate or {}
        refused = r.get("status") not in (None, "allowed", "allowed_warning")
        err = (s.error or "").lower()
        if not refused and not ("limit" in err and ("usage" in err or "rate" in err)):
            return None
        until = float(r.get("resetsAt") or 0) or t + 1800
        self.store.update_state(limited_until=until, limited_why="the plan's usage limit was reached")
        return f"the plan's usage limit was reached; waiting until {local_now(until):%a %H:%M}"

    # ---------- starting and ending ----------

    def start(self, t: float, why: str, booking: dict | None):
        sid = str(uuid.uuid4())
        notes = self.store.undelivered("person")
        self.store.clear_asleep()
        s = self.launch(sid)
        self.store.add_usage(self.day(t), sessions=1)
        prompt = prompts.wake(name=self.agent_name, grid=self.grid, person=self.ks.person, reason=why, session=sid,
                              life=self.life, handoff_note=self.ks.handoff_note, has_handoff=self.handoff_exists(),
                              allowance=self.allowance(t), notes=notes, tools=self.tools_note(), situation=self.ks.situation,
                              extra=self.extra_prompt)
        s.send(prompt)
        if notes:
            self.store.mark_notes([n["id"] for n in notes], "delivered", how="wake")
        self.sess = s
        self.info = {"reason": why, "booking": booking and booking["key"], "counted_at": t, "nudges": 0}
        self.avatar_ours = True
        self.log(f"started session {sid[:8]} ({why})")

    def allowance(self, t: float) -> str | None:
        ks = self.ks
        if not ks.hours_per_day:
            return None
        left = max(0.0, ks.hours_per_day * 3600 - self.usage(t)["awake_s"])
        who = prompts.person_label(ks.person)
        return (f"Today's allowance: about {left / 3600:.1f} h of the {ks.hours_per_day:g} h a day {who} set "
                "(tokens cost them money); the keeper asks for a wind-down when it runs out.")

    def end(self, t: float, why: str, crash: bool = False, handoff: bool = False, stay_up: bool = False,
            want: bool = True):
        s, self.sess = self.sess, None
        self.count_awake(t, flush=True)
        s.close()
        if s.cost_usd:
            self.store.add_usage(self.day(t), usd=s.cost_usd)
        self.log(f"session {s.id[:8]} ended: {why}")
        keep_up = want and (handoff or stay_up)
        if crash:
            self.crashes = [c for c in self.crashes if t - c < CRASH_WINDOW_S] + [t]
            if len(self.crashes) >= self.ks.crash_limit:
                self.store.update_state(paused=f"{len(self.crashes)} crashes or stalls within an hour")
                self.log("paused: too many crashes or stalls; `ibasho keeper resume` when it's sorted")
                self.crashes = []
            self.next_start = t + min(MAX_BACKOFF_S, 60 * 2 ** (len(self.crashes) - 1)) if self.crashes else t
            keep_up = want and self.next_start - t <= 120 and not self.store.state()["paused"]
        else:
            self.next_start = t + (5 if keep_up else self.ks.restart_gap_s)
        self.info = {}
        self.next_reason = ("a handoff from the session before" if keep_up and not crash else
                            "a restart after the last session crashed or stalled" if crash else None)
        if not keep_up:
            self.avatar_down()

    def avatar_down(self):
        if not self.avatar_ours:
            return
        self.avatar_ours = False
        try:
            if self.world.logged_in():
                self.log("logging the avatar out")
                self.world.down()
        except Exception as e:  # the daemon going away mid-call isn't the keeper's problem
            self.log(f"logging out failed: {e!r}")

    def count_awake(self, t: float, flush: bool = False):
        start = self.info.get("counted_at", t)
        self.unflushed += max(0.0, t - start)
        self.info["counted_at"] = t
        if self.unflushed and (flush or self.unflushed >= 60):
            self.store.add_usage(self.day(t), awake_s=self.unflushed)
            self.unflushed = 0.0

    # ---------- the person ----------

    def handle_stop(self, t: float):
        req = self.store.stop_request()
        if not req:
            return
        self.store.clear_stop()
        if req.get("now") or not self.sess:
            self.log("stopping now (the person asked)")
            if self.sess:
                self.end(t, "the person stopped the keeper", want=False)
            self.avatar_down()
            self.stopping = self.exiting = True
        elif not self.stopping:
            self.log("stopping after a short wind-down (the person asked)")
            self.stopping = True

    def relay_agent_notes(self):
        for n in self.store.notes():
            if n["from"] == "agent" and n["id"] not in self.agent_notes_seen:
                self.agent_notes_seen.add(n["id"])
                self.log(f"note from {self.agent_name}: {prompts.flat(n['text'])}")

    def write_status(self, t: float, want: bool, why: str):
        s = self.sess
        sess = None
        if s:
            sess = {"id": s.id, "pid": s.pid, "create_time": getattr(s, "create_time", None), "started": s.started,
                    "turn_open": s.turn_open, "turns": s.turns, "context": s.context,
                    "context_window": self.window(s), "model": s.model, "cost_usd": round(s.cost_usd, 4),
                    "last_activity": s.last_activity, "nudges": self.info.get("nudges", 0),
                    "reason": self.info.get("reason"),
                    "wind_down": {k: v for k, v in (self.info.get("wind_down") or {}).items() if k != "msg"} or None}
        u = self.usage(t)
        lt = local_now(t)
        nxt = next(iter(occurrences(self.store.bookings(), lt, lt + dt.timedelta(days=14))), None)
        self.store.save_status({
            "t": t, "keeper_pid": os.getpid(), "want": want, "why": why, "session": sess,
            "next_start": self.next_start if not s and self.next_start > t else None,
            "stopping": self.stopping, "avatar_ours": self.avatar_ours,
            "crashes_last_hour": len([c for c in self.crashes if t - c < CRASH_WINDOW_S]),
            "today": {"awake_h": round(u["awake_s"] / 3600, 2), "sessions": u["sessions"], "usd": round(u["usd"], 2)},
            "rate": s.rate if s else None,
            "next_booking": nxt and {"key": nxt["key"], "start": nxt["start"].isoformat(timespec="minutes"),
                                     "end": nxt["end"].isoformat(timespec="minutes"), "why": nxt["why"]},
        })

    def shutdown(self, why: str = "the keeper stopped"):
        if self.sess:
            self.end(self.now(), why, want=False)
        self.avatar_down()
        self.write_status(self.now(), False, why)
