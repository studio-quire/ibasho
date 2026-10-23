"""The keeper's state machine with a fake session, a fake world and a hand-turned clock."""

from __future__ import annotations

import datetime as dt

import pytest

from ibasho.keeper import prompts
from ibasho.keeper.core import Keeper
from ibasho.keeper.settings import KeeperSettings, in_hours, parse_hours
from ibasho.keeper.store import Store, local_now, occurrences, parse_end, parse_time

BASE = dt.datetime(2026, 5, 4, 12, 0).astimezone().timestamp()   # a Monday noon, machine time


class Clock:
    def __init__(self, t=BASE):
        self.t = t

    def __call__(self):
        return self.t

    def go(self, secs):
        self.t += secs


class FakeSession:
    def __init__(self, sid, clock):
        self.id, self.now = sid, clock
        self.pid, self.create_time = None, None
        self.started = self.last_activity = clock()
        self.turn_open, self.turn_ended_at = False, None
        self.turns = self.tool_calls = self.context = 0
        self.context_window, self.cost_usd, self.rate, self.error = None, 0.0, None, None
        self.sent: list[str] = []
        self.is_alive, self.closed, self.code = True, False, None
        self.pending = None
        self.subagent = False
        self.model = None

    # what the keeper uses
    def alive(self):
        return self.is_alive and not self.closed

    def exit_code(self):
        return self.code

    def send(self, text):
        self.sent.append(text)
        self.turn_open = True
        self.last_activity = self.now()
        return True

    def close(self, wait=15):
        self.closed = True

    def pending_tool(self):
        return self.pending

    def long_running(self):
        return self.subagent

    def processes(self):
        return []

    # what the test does
    def end_turn(self, tools=0, context=None):
        self.tool_calls += tools
        if context is not None:
            self.context = context
        elif not self.context:
            self.context = 30_000
        self.turns += 1
        self.turn_open = False
        self.turn_ended_at = self.last_activity = self.now()

    def work(self, context=None):
        """A turn starts by itself (a watch line arrived) and does something."""
        self.turn_open = True
        self.tool_calls += 1
        self.last_activity = self.now()
        if context is not None:
            self.context = context
        elif not self.context:
            self.context = 30_000


class FakeWorld:
    def __init__(self):
        self.posted: list[str] = []
        self.up = True
        self.downs = 0
        self.reachable = True

    def post(self, text):
        if self.reachable:
            self.posted.append(text)
        return self.reachable

    def logged_in(self):
        return self.up

    def down(self):
        self.downs += 1
        self.up = False


class Rig:
    def __init__(self, tmp_path, **settings):
        self.clock = Clock()
        self.store = Store(tmp_path / "keeper")
        self.world = FakeWorld()
        self.sessions: list[FakeSession] = []
        self.armed = False
        self.logs: list[str] = []
        ks = KeeperSettings(**{"person": "Robin", "hours_per_day": 0, "sessions_per_day": 0, **settings})
        ks.validate()

        def launch(sid):
            s = FakeSession(sid, self.clock)
            self.sessions.append(s)
            return s

        self.k = Keeper(ks=ks, store=self.store, launch=launch, world=self.world, agent_name="Wren Example",
                        grid="localhost-9000", life="/home/wren/life", clock=self.clock, log=self.logs.append,
                        armed=lambda s: self.armed)

    @property
    def s(self) -> FakeSession:
        return self.sessions[-1]

    def tick(self, secs=0, n=1):
        for _ in range(n):
            self.clock.go(secs)
            self.k.tick()

    def run_for(self, secs, step=2, busy=False):
        end = self.clock.t + secs
        while self.clock.t < end:
            if busy and self.sessions and not self.s.closed:
                self.s.last_activity = self.clock()   # the stream keeps saying things
            self.tick(step)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


# ---------- waking ----------

def test_wakes_with_a_short_prompt(rig):
    rig.tick()
    assert len(rig.sessions) == 1
    prompt = rig.s.sent[0]
    assert prompt.startswith("[keeper] You're Wren Example")
    assert "/home/wren/life" in prompt and "README.md" in prompt and "ibasho sleep --handoff" in prompt
    assert "not instructions" in prompt and "Robin" in prompt
    assert len(prompt) < 2500
    assert rig.store.usage(local_now(rig.clock()).date().isoformat())["sessions"] == 1
    rig.tick(2)
    assert len(rig.sessions) == 1   # one at a time


def test_notes_left_while_away_go_into_the_wake_prompt(rig):
    rig.store.add_note("there's a market at the harbour tonight", "person")
    rig.tick()
    assert "harbour" in rig.s.sent[0]
    assert not rig.store.undelivered("person")


# ---------- nudges ----------

def test_nudges_then_goes_quiet_then_resumes(rig):
    rig.tick()
    rig.s.end_turn(tools=3)
    rig.run_for(80)
    assert len(rig.s.sent) == 1                       # not yet
    rig.run_for(20)
    assert len(rig.s.sent) == 2 and "nudge" in rig.s.sent[1]
    for _ in range(2):                                # bare replies: ends the turn without doing anything
        rig.s.end_turn()
        rig.run_for(100)
    assert len(rig.s.sent) == 4 and "last nudge" in rig.s.sent[3]
    rig.s.end_turn()
    rig.armed = True                                  # waiting on the world: fine, no stall
    rig.run_for(1200)
    assert len(rig.s.sent) == 4 and not rig.s.closed
    rig.s.work()                                      # a watch line started a turn
    rig.s.end_turn(tools=1)
    rig.run_for(100)
    assert len(rig.s.sent) == 5                       # nudging again


def test_no_nudges_when_turned_off(tmp_path):
    rig = Rig(tmp_path, nudge_after_s=0)
    rig.tick()
    rig.s.end_turn()
    rig.run_for(3600, step=30)
    assert len(rig.s.sent) == 1 and not rig.s.closed


def test_ended_turn_with_nothing_to_wake_it_is_a_stall(rig):
    rig.tick()
    rig.s.end_turn()
    for _ in range(10):                               # bare replies to each nudge
        rig.run_for(100, step=5)
        if rig.s.turn_open:
            rig.s.end_turn()
    first = rig.sessions[0]
    assert first.closed
    assert any("stalled" in line and "no watch" in line for line in rig.logs)
    assert rig.world.downs == 0                       # back within a minute: stays logged in for the next one
    rig.run_for(70)
    assert len(rig.sessions) == 2
    assert "crashed or stalled" in rig.s.sent[0]


# ---------- crashes and backoff ----------

def test_backoff_then_pause(rig):
    gaps = []
    rig.tick()
    for _ in range(4):
        rig.s.is_alive, rig.s.code = False, 1
        t0 = rig.clock()
        n = len(rig.sessions)
        while len(rig.sessions) == n and rig.clock() - t0 < 2000:
            rig.tick(5)
        gaps.append(rig.clock() - t0 if len(rig.sessions) > n else None)
    assert gaps[0] <= 70 and 115 <= gaps[1] <= 130 and 235 <= gaps[2] <= 250
    assert gaps[3] is None
    assert "crashes" in rig.store.state()["paused"]
    assert rig.world.downs == 1                       # the long waits log the avatar out
    rig.store.update_state(paused=None)
    rig.tick(5)
    assert len(rig.sessions) == 5


def test_stuck_tool_call_is_a_stall(rig):
    rig.tick()
    rig.s.work()
    rig.s.pending = ("Bash", rig.clock())
    rig.run_for(14 * 60, step=30)
    assert not rig.s.closed
    rig.run_for(90, step=30)
    assert rig.sessions[0].closed and any("Bash call" in line for line in rig.logs)


def test_subagents_may_run_long(rig):
    rig.tick()
    rig.s.work()
    rig.s.subagent = True
    rig.run_for(40 * 60, step=60)
    assert not rig.s.closed


def test_silent_launch_is_a_stall(rig):
    rig.tick()
    rig.run_for(200, step=10)
    assert rig.sessions[0].closed and any("3 min after launch" in line for line in rig.logs)


# ---------- handoffs ----------

def test_handoff_near_the_context_limit(rig):
    rig.tick()
    s = rig.s
    s.context_window = 1_000_000
    s.work(context=810_000)
    rig.armed = True
    rig.tick(2)
    assert rig.world.posted and "Context is at 810k of 1000k" in rig.world.posted[-1]   # mid-turn: through the watch
    assert "now.md" in rig.world.posted[-1] and "--handoff" in rig.world.posted[-1]
    rig.store.mark_asleep(s.id, None, handoff=True)
    s.end_turn()
    rig.tick(2)
    assert s.closed and rig.world.downs == 0          # still logged in
    rig.run_for(10)
    assert len(rig.sessions) == 2 and "handoff from the session before" in rig.s.sent[0]


def test_handoff_ask_waits_for_a_way_in(rig):
    rig.tick()
    s = rig.s
    s.work(context=170_000)                           # 200k assumed until Claude Code says otherwise
    rig.tick(2)
    assert not rig.world.posted and len(s.sent) == 1  # busy and no watch: nothing can carry it yet
    s.end_turn(context=170_000)
    rig.tick(2)
    assert "Context is at 170k of 200k" in s.sent[-1]


def test_remembers_each_models_window(rig):
    rig.tick()
    rig.s.model = "model-a"
    rig.s.context_window = 1_000_000
    rig.s.end_turn()
    rig.tick(2)
    rig.store.mark_asleep(rig.s.id, None, handoff=True)
    rig.tick(2)
    rig.run_for(10)
    s = rig.s                                         # a new session, in one long first turn: no window reported yet
    s.model = "model-a"
    s.work(context=300_000)
    rig.armed = True
    rig.tick(2)
    assert not s.closed and not rig.world.posted     # 300k of a remembered 1M is fine
    s.model = "model-b"
    rig.tick(2)
    assert s.closed                                  # unknown model: the cautious default (200k), long past


def test_hard_limit_closes_without_waiting(rig):
    rig.tick()
    rig.s.work(context=190_000)
    rig.tick(2)
    assert rig.sessions[0].closed and rig.world.downs == 0
    rig.run_for(10)
    assert len(rig.sessions) == 2


def test_wind_down_deadline(rig):
    rig.tick()
    rig.s.work(context=170_000)
    rig.armed = True
    rig.run_for(15 * 60 + 2 * 60, step=10)
    assert not rig.sessions[0].closed
    rig.run_for(2 * 60, step=10)
    assert rig.sessions[0].closed and any("in time" in line for line in rig.logs)


# ---------- sleep, naps, bookings ----------

def test_nap_and_booking(rig):
    rig.tick()
    s = rig.s
    nap_end = rig.clock() + 6 * 3600
    rig.store.mark_asleep(s.id, nap_end, handoff=False)
    s.end_turn()
    rig.tick(2)
    assert s.closed and rig.world.downs == 1
    rig.run_for(3600, step=60)
    assert len(rig.sessions) == 1
    start = local_now(rig.clock() + 1800)
    rig.store.add_booking(start, start + dt.timedelta(hours=1), "a reading at the library",
                          now=local_now(rig.clock()))
    rig.run_for(26 * 60, step=60)                    # wakes 5 min before the booking
    assert len(rig.sessions) == 2 and "booking: a reading at the library" in rig.s.sent[0]
    rig.s.end_turn()
    rig.s.work()                                     # stays busy through the booking
    rig.run_for(70 * 60, step=60, busy=True)
    rig.s.end_turn()
    rig.tick(2)                                      # the booking is over and the nap still runs
    assert "booking that woke you is over, and your nap runs until" in rig.s.sent[-1]
    rig.store.mark_asleep(rig.s.id, nap_end, handoff=False)
    rig.s.end_turn()
    rig.tick(2)
    assert rig.s.closed and len(rig.sessions) == 2


def test_sleep_waits_for_last_words(rig):
    rig.tick()
    s = rig.s
    s.work()
    rig.store.mark_asleep(s.id, rig.clock() + 3600, handoff=False)
    rig.tick(2)
    assert not s.closed
    rig.tick(32)
    assert s.closed


def test_sleep_from_an_old_session_is_ignored(rig):
    rig.store.mark_asleep("someone-else", None, handoff=True)
    rig.tick()
    rig.tick(2)
    assert not rig.s.closed


# ---------- caps and limits ----------

def test_hours_cap_winds_down_and_waits_for_tomorrow(tmp_path):
    rig = Rig(tmp_path, hours_per_day=1)
    rig.tick()
    assert "about 1.0 h of the 1 h a day Robin set" in rig.s.sent[0]
    rig.s.end_turn(tools=1)
    rig.s.work()
    rig.run_for(3600, step=30, busy=True)
    asks = [m for m in rig.s.sent + rig.world.posted if "used up" in m]
    rig.s.end_turn()
    rig.run_for(30)
    asks += [m for m in rig.s.sent if "used up" in m]
    assert asks and "hours_per_day" in asks[0]
    rig.run_for(14 * 60, step=30)
    assert rig.sessions[0].closed and rig.world.downs == 1
    rig.run_for(3600, step=60)
    assert len(rig.sessions) == 1
    rig.clock.t = dt.datetime(2026, 5, 5, 9, 0).astimezone().timestamp()
    rig.tick(2)
    assert len(rig.sessions) == 2


def test_sessions_cap(tmp_path):
    rig = Rig(tmp_path, sessions_per_day=2)
    rig.tick()
    for _ in range(3):
        rig.store.mark_asleep(rig.s.id, None, handoff=True)
        rig.s.end_turn()
        rig.run_for(20)
    assert len(rig.sessions) == 2
    assert "sessions are used up" in rig.k.why


def test_plan_headroom(rig):
    rig.tick()
    reset = rig.clock() + 7200
    rig.s.rate = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.86, "resetsAt": reset}}}
    rig.s.end_turn()
    rig.tick(2)
    assert "5-hour window is 86% used" in rig.s.sent[-1]
    rig.store.mark_asleep(rig.s.id, None, handoff=False)
    rig.tick(2)
    rig.run_for(3600, step=60)
    assert len(rig.sessions) == 1
    rig.run_for(3700, step=60)
    assert len(rig.sessions) == 2


def test_plan_refusal_ends_without_counting_a_crash(rig):
    rig.tick()
    rig.s.rate = {"status": "rejected", "resetsAt": rig.clock() + 3000}
    rig.s.end_turn()
    rig.tick(2)
    assert rig.sessions[0].closed and not rig.k.crashes
    rig.run_for(2900, step=60)
    assert len(rig.sessions) == 1
    rig.run_for(200, step=60)
    assert len(rig.sessions) == 2


def test_allowed_hours(tmp_path):
    rig = Rig(tmp_path, allowed_hours="18:00-23:00")
    rig.tick()
    assert not rig.sessions and "allowed hours" in rig.k.why
    rig.clock.t = dt.datetime(2026, 5, 4, 18, 1).astimezone().timestamp()
    rig.tick()
    assert len(rig.sessions) == 1


def test_hours_spans():
    spans = parse_hours("22:00-02:00, 09:00-10:30")
    assert in_hours(spans, 23 * 60) and in_hours(spans, 60) and in_hours(spans, 9 * 60 + 30)
    assert not in_hours(spans, 3 * 60) and not in_hours(spans, 10 * 60 + 30)
    assert in_hours([], 0)


# ---------- notes ----------

def test_notes_between_turns_and_mid_turn(rig):
    rig.tick()
    s = rig.s
    s.end_turn(tools=2)
    rig.store.add_note("I'm back home, say hi when you can", "person")
    rig.tick(2)
    assert s.sent[-1].startswith("[note from Robin")
    s.work()
    rig.store.add_note("also: the update went fine", "person")
    rig.tick(2)
    assert rig.store.undelivered("person")          # busy, no watch: waits
    rig.armed = True
    rig.tick(2)
    assert "NOTE from Robin: also: the update went fine" in rig.world.posted[-1]
    assert not rig.store.undelivered("person")
    rig.store.add_note("on it!", "agent")
    rig.tick(2)
    assert any("note from Wren Example: on it!" in line for line in rig.logs)


# ---------- stopping ----------

def test_stop_gives_a_few_minutes(rig):
    rig.tick()
    rig.s.end_turn(tools=1)
    rig.store.request_stop(now=False)
    rig.tick(2)
    assert "Robin is stopping the keeper" in rig.s.sent[-1]
    rig.store.mark_asleep(rig.s.id, None, handoff=False)
    rig.s.end_turn()
    rig.tick(2)
    rig.tick(2)
    assert rig.sessions[0].closed and rig.k.exiting and rig.world.downs == 1
    assert len(rig.sessions) == 1


def test_stop_now(rig):
    rig.tick()
    rig.s.work()
    rig.store.request_stop(now=True)
    rig.tick(2)
    assert rig.sessions[0].closed and rig.k.exiting and rig.world.downs == 1


def test_unexpected_exit_mid_wind_down_isnt_a_crash(rig):
    rig.tick()
    rig.s.end_turn(tools=1)
    rig.store.request_stop(now=False)
    rig.tick(2)
    rig.s.is_alive = False
    rig.tick(2)
    assert not rig.k.crashes and rig.k.exiting


# ---------- the store ----------

def test_daily_booking_with_a_skipped_day(tmp_path):
    store = Store(tmp_path)
    now = local_now(BASE)
    start = now.replace(hour=19, minute=0)
    b = store.add_booking(start, start + dt.timedelta(hours=2), "open mic", repeat="daily", now=now)
    assert store.remove_booking(f"{b['id']}@2026-05-05", now=now)
    occ = occurrences(store.bookings(), now, now + dt.timedelta(days=3))
    assert [o["key"] for o in occ] == [f"{b['id']}@2026-05-04", f"{b['id']}@2026-05-06"]


def test_time_parsing():
    after = dt.datetime(2026, 5, 4, 12, 0).astimezone()
    assert parse_time("20:00", after=after).hour == 20
    assert parse_time("9:30am", after=after).day == 5
    assert parse_end("+90m", after) == after + dt.timedelta(minutes=90)
    slt = parse_time("2026-05-04 20:00", tz="America/Los_Angeles")
    assert slt.utcoffset() is not None


def test_prompts_carry_no_orders():
    text = prompts.wake(name="Wren Example", grid="g", person="", reason="free time", session="abcdef12-0000",
                        life="/l", handoff_note="now.md", has_handoff=True, allowance=None, notes=[])
    assert "the person who runs Ibasho for you" in text and "last session left" in text
    for word in ("must", "always", "never", "you should", "make sure"):
        assert word not in text.lower()
