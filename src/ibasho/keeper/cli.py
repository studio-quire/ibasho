"""The keeper's commands: `ibasho keeper ...` for the person; sleep, awake, book, unbook, bookings and notes for the
agent (notes for both: whoever runs it inside a keeper session is the agent)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time

import psutil

from .. import procs
from ..config import ConfigError
from ..cli.common import Ctx, Fail
from . import prompts, settings as settings_mod
from .store import SLT, Store, active_booking, from_iso, local_now, occurrences, parse_date, parse_end, parse_time


def register(sub):
    p = sub.add_parser("keeper", help="run the agent's Claude Code sessions for you (`ibasho keeper -h`)")
    ks = p.add_subparsers(dest="keeper_command", required=True, metavar="WHAT")
    q = ks.add_parser("run", help="run the keeper in this window (Ctrl+C stops it and logs the avatar out)")
    q.add_argument("--quiet", action="store_true", help="log to keeper.log only")
    q.add_argument("--extra-prompt", default="", help="a paragraph added to every wake prompt (for test runs)")
    q.set_defaults(fn=keeper_run)
    q = ks.add_parser("start", help="run the keeper in the background")
    q.set_defaults(fn=keeper_start)
    q = ks.add_parser("stop", help="stop the keeper: the agent gets a few minutes to wrap up, unless --now")
    q.add_argument("--now", action="store_true")
    q.set_defaults(fn=keeper_stop)
    q = ks.add_parser("status", help="what the keeper is doing, today's use against the caps")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=keeper_status)
    ks.add_parser("wake", help="cancel the agent's nap, so the keeper wakes it now").set_defaults(fn=keeper_wake)
    ks.add_parser("resume", help="clear a crash pause or a plan-limit wait").set_defaults(fn=keeper_resume)
    ks.add_parser("setup", help="make the life folder, check Claude Code, show what sessions may do") \
        .set_defaults(fn=keeper_setup)
    ks.add_parser("login", help="log the agent's own Claude Code folder in (once)").set_defaults(fn=keeper_login)
    q = ks.add_parser("log", help="the keeper's log, or the latest session's")
    q.add_argument("-n", type=int, default=30)
    q.add_argument("--session", action="store_true", help="the latest session's log (its words and tool calls)")
    q.set_defaults(fn=keeper_log)

    p = sub.add_parser("sleep", aliases=["asleep"],
                       help="end this session: log out and nap, or --handoff to a fresh session")
    p.add_argument("--until", metavar="TIME", help="nap until then ('08:00', 'tomorrow 9:30pm', '+3h'); bookings "
                                                   "can still wake me")
    p.add_argument("--slt", action="store_true", help="TIME is Second Life time")
    p.add_argument("--handoff", action="store_true",
                   help="stay logged in; a fresh session takes over from the handoff note")
    p.set_defaults(fn=sleep)
    sub.add_parser("awake", help="cancel my nap (if a booking woke me and I'd like to stay up after it)") \
        .set_defaults(fn=awake)
    p = sub.add_parser("book", help="be up at a time: book START END WHY (END may be '+2h')")
    p.add_argument("start")
    p.add_argument("end")
    p.add_argument("why", nargs="+")
    p.add_argument("--slt", action="store_true", help="times are Second Life time")
    rep = p.add_mutually_exclusive_group()
    rep.add_argument("--daily", action="store_true")
    rep.add_argument("--weekly", action="store_true")
    p.add_argument("--until", metavar="DATE", help="last day of a repeating booking ('10-31')")
    p.set_defaults(fn=book)
    p = sub.add_parser("unbook", help="unbook ID, or ID@YYYY-MM-DD for one day of a repeating booking")
    p.add_argument("key")
    p.set_defaults(fn=unbook)
    p = sub.add_parser("bookings", help="the next two weeks of bookings")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(fn=bookings)
    p = sub.add_parser("notes", help="notes between the person and the agent: no TEXT shows them, TEXT sends one")
    p.add_argument("text", nargs="*")
    p.add_argument("-n", type=int, default=10, help="how many recent ones to show")
    p.set_defaults(fn=notes)


# ---------- shared ----------

def ks_of(ctx: Ctx) -> settings_mod.KeeperSettings:
    try:
        return settings_mod.load(ctx.cfg)
    except ConfigError as e:
        raise Fail(str(e)) from None


def store_of(ctx: Ctx) -> Store:
    return Store(settings_mod.keeper_dir(ctx.cfg))


def in_session() -> str | None:
    return os.environ.get("IBASHO_SESSION") or None


def keeper_running(store: Store) -> dict | None:
    st = store.status()
    pid = st.get("keeper_pid")
    if not st or time.time() - st.get("t", 0) > 30 or not pid:
        return None
    try:
        p = psutil.Process(pid)
        return st if "keeper" in " ".join(p.cmdline()) else None
    except psutil.Error:
        return None


def when(t: float | None) -> str:
    return f"{local_now(t):%a %d %b %H:%M}" if t else "-"


# ---------- the person's commands ----------

def keeper_run(ctx: Ctx, a):
    from .runner import KeeperError, run
    try:
        run(ctx.cfg, echo=not a.quiet, extra_prompt=a.extra_prompt)
    except (KeeperError, ConfigError) as e:
        raise Fail(str(e)) from None


def keeper_start(ctx: Ctx, a):
    from ..cli.lifecycle import _src_path
    store = store_of(ctx)
    if keeper_running(store):
        raise Fail("the keeper is already running (`ibasho keeper status`)")
    ks_of(ctx)
    kdir = settings_mod.keeper_dir(ctx.cfg)
    kdir.mkdir(parents=True, exist_ok=True)
    before = store.status().get("t", 0)
    pid = procs.spawn(procs.ibasho_args("--config", str(ctx.cfg.path), "keeper", "run", "--quiet"),
                      kdir / "keeper.out", env=procs.env_with(PYTHONPATH=_src_path()))
    end = time.time() + 90   # the first tick waits on `claude auth status`
    while time.time() < end:
        time.sleep(0.5)
        if store.status().get("t", 0) > before and (st := keeper_running(store)):
            print(f"keeper running in the background (pid {st['keeper_pid']}); `ibasho keeper status`, "
                  "`ibasho keeper stop`")
            return
        if not procs.alive(pid):
            break
    tail = (kdir / "keeper.out").read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
    raise Fail("the keeper didn't start" + (": " + " / ".join(tail) if tail else f"; see {kdir / 'keeper.log'}"))


def keeper_stop(ctx: Ctx, a):
    store = store_of(ctx)
    if not keeper_running(store):
        print("the keeper isn't running")
        return
    store.request_stop(now=a.now)
    if not a.now:
        ks = ks_of(ctx)
        print(f"asked the keeper to stop: the agent has about {ks.stop_grace_min:g} min to wrap up, then the session "
              "closes and the avatar logs out (`--now` doesn't wait)")
        return
    end = time.time() + 60
    while time.time() < end and keeper_running(store) and not store.status().get("stopping"):
        time.sleep(0.5)
    while time.time() < end and keeper_running(store):
        time.sleep(0.5)
    print("keeper stopped" if not keeper_running(store) else "asked the keeper to stop; it's still finishing")


def keeper_status(ctx: Ctx, a):
    cfg, ks, store = ctx.cfg, ks_of(ctx), store_of(ctx)
    st = store.status()
    running = keeper_running(store)
    state = store.state()
    if a.json:
        print(json.dumps({"running": bool(running), "status": st, "state": state}, ensure_ascii=False))
        return
    print(f"keeper: {'running' if running else 'not running'}" + (f"; {st.get('why')}" if running else ""))
    s = running and st.get("session")
    if s:
        w = s.get("context_window") or ks.context_window
        print(f"session {s['id'][:8]} up since {when(s['started'])}: {'working' if s['turn_open'] else 'between turns'}, "
              f"context {s['context'] // 1000}k of {w // 1000}k ({s['context'] / w:.0%}), {s['turns']} turns, "
              f"{s['nudges']} nudges in a row" + (f", est. ${s['cost_usd']:.2f}" if s.get("cost_usd") else ""))
        if s.get("wind_down"):
            print(f"  winding down: {s['wind_down']['why']} (closes {when(s['wind_down']['deadline'])})")
    elif running and st.get("next_start"):
        print(f"next session from {when(st['next_start'])}")
    today = (st.get("today") if running else None) or store.usage(local_now().date().isoformat())
    hours = today.get("awake_h", round(today.get("awake_s", 0) / 3600, 2))
    caps = [f"{hours:.1f} of {ks.hours_per_day:g} h" if ks.hours_per_day else f"{hours:.1f} h (no cap)",
            f"{today.get('sessions', 0)} of {ks.sessions_per_day} sessions" if ks.sessions_per_day else
            f"{today.get('sessions', 0)} sessions"]
    if ks.usd_per_day:
        caps.append(f"est. ${today.get('usd', 0):.2f} of ${ks.usd_per_day:g}")
    print("today: " + ", ".join(caps))
    if state.get("nap_until") and state["nap_until"] > time.time():
        print(f"napping until {when(state['nap_until'])} (its choice; `ibasho keeper wake` cancels)")
    if state.get("paused"):
        print(f"PAUSED: {state['paused']} (`ibasho keeper resume`)")
    if state.get("limited_until") and state["limited_until"] > time.time():
        print(f"waiting: {state['limited_why']} until {when(state['limited_until'])}")
    if running and st.get("next_booking"):
        b = st["next_booking"]
        print(f"next booking: {when(from_iso(b['start']).timestamp())} {b['why']}")
    unread = store.unread("agent")
    if unread:
        print(f"{len(unread)} unread note{'s' if len(unread) > 1 else ''} from {cfg.name} (`ibasho notes`)")


def keeper_wake(ctx: Ctx, a):
    store_of(ctx).update_state(nap_until=None)
    print("nap cleared; the keeper wakes the agent at its next tick (if the caps and hours allow)")


def keeper_resume(ctx: Ctx, a):
    store_of(ctx).update_state(paused=None, limited_until=None, limited_why=None)
    print("cleared; the keeper starts a session at its next tick if the agent should be up")


def keeper_setup(ctx: Ctx, a):
    from .runner import KeeperError, claude_env, ensure_life, session_settings
    from .session import auth_status, find_claude
    cfg, ks = ctx.cfg, ks_of(ctx)
    life = settings_mod.life_dir(cfg, ks)
    try:
        made = ensure_life(life)
        settings = session_settings(cfg, ks, settings_mod.keeper_dir(cfg))
    except KeeperError as e:
        raise Fail(str(e)) from None
    print(f"life folder: {life}" + (" (made from the template)" if made else ""))
    claude = find_claude(ks.claude)
    if not claude:
        print("PROBLEM: Claude Code (`claude`) isn't on PATH; install it or set keeper.claude")
    else:
        st = auth_status(claude, claude_env(cfg, ks))
        where = "the agent's own Claude Code folder" if ks.claude_config == "separate" else "your Claude Code"
        if st.get("loggedIn"):
            print(f"{where}: logged in ({st.get('authMethod')}{', ' + st['subscriptionType'] if st.get('subscriptionType') else ''})")
        else:
            print(f"{where}: NOT logged in; run `ibasho keeper login`" if ks.claude_config == "separate" else
                  f"{where}: NOT logged in; run `claude` and log in")
    if ks.claude_config == "shared":
        print("NOTE: keeper.claude_config = \"shared\" is for developing Ibasho: sessions load your own Claude Code "
              "settings, hooks, plugins and CLAUDE.md, and their transcripts go to your own Claude Code folder with "
              "your retention (docs/keeper.md, Isolation)")
    from .session import AUTH_ENV
    held = sorted(k for k in os.environ if AUTH_ENV.match(k))
    if held:
        print(f"your environment sets {', '.join(held)}: " + (
            "sessions get them (keeper.auth = \"env\"), so they may bill an API key" if ks.auth == "env" else
            "sessions don't get them and use the login above (keeper.auth = \"env\" would pass them on)"))
    allow = json.loads(settings.read_text(encoding="utf-8")).get("permissions", {})
    print(f"sessions run in {life} with permission mode {ks.permission_mode} and {settings}:")
    print("  allowed without asking: " + ", ".join(allow.get("allow", [])))
    print("  denied: " + ", ".join(allow.get("deny", [])))
    print("  plus Claude Code's defaults for the mode (reading and, in acceptEdits, editing files in the life folder; "
          "reading the shots folder). Anything else is refused, since nobody is there to approve it. "
          "docs/keeper.md has the whole picture.")
    caps = [f"{ks.hours_per_day:g} h awake a day" if ks.hours_per_day else "no daily hours cap",
            f"{ks.sessions_per_day} sessions a day" if ks.sessions_per_day else "no sessions cap"]
    if ks.usd_per_day:
        caps.append(f"est. ${ks.usd_per_day:g} a day")
    if ks.max_plan_use < 1:
        caps.append(f"stops at {ks.max_plan_use:.0%} of the plan's usage windows")
    print("caps: " + ", ".join(caps) + f" (raise them in [keeper] of {cfg.path})")


def keeper_login(ctx: Ctx, a):
    from .runner import claude_env
    from .session import auth_status, find_claude
    cfg, ks = ctx.cfg, ks_of(ctx)
    claude = find_claude(ks.claude)
    if not claude:
        raise Fail("can't find Claude Code (`claude`)")
    env = claude_env(cfg, ks)
    if ks.claude_config == "shared":
        print("keeper.claude_config is \"shared\": sessions use your own Claude Code login")
    else:
        # Claude Code's own login flow (the same as /login inside it), run in the agent's folder. Each folder logs in
        # itself: copying another folder's credentials file would share one login's tokens between two installs.
        print(f"logging in the agent's own Claude Code folder ({settings_mod.claude_config_dir(cfg, ks)}); "
              "a browser opens")
        subprocess.run([claude, "auth", "login"], env=env)
    st = auth_status(claude, env)
    print("logged in" if st.get("loggedIn") else "not logged in yet")


def keeper_log(ctx: Ctx, a):
    kdir = settings_mod.keeper_dir(ctx.cfg)
    if a.session:
        logs = sorted((kdir / "sessions").glob("*.log"))
        if not logs:
            raise Fail("no session logs yet")
        path = logs[-1]
    else:
        path = kdir / "keeper.log"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        raise Fail(f"no log at {path}") from None
    print("\n".join(lines[-a.n:]))


# ---------- the agent's commands ----------

def sleep(ctx: Ctx, a):
    cfg, ks, store = ctx.cfg, ks_of(ctx), store_of(ctx)
    sid = in_session()
    if a.handoff and not sid:
        raise Fail("--handoff only means something inside a session the keeper started")
    if a.handoff and a.until:
        raise Fail("--handoff hands over to a fresh session right away; --until is for napping")
    until = None
    if not a.handoff:
        try:
            until = (parse_end(a.until, local_now(), SLT if a.slt else None) if a.until else
                     local_now() + dt.timedelta(minutes=ks.default_nap_min)).timestamp()
        except ValueError as e:
            raise Fail(str(e)) from None
        from ..api import Down
        try:
            if ctx.api.is_up():
                from ..cli.lifecycle import down
                down(ctx, argparse.Namespace(keep_daemon=False))
        except Down:
            pass
    store.mark_asleep(sid, until, a.handoff)
    if a.handoff:
        print("handing off: still logged in; a fresh session takes over in a few seconds")
        return
    print(f"asleep until {when(until)}" + ("; the keeper closes this session" if sid else ""))
    b = active_booking(store.bookings(), local_now(), ks.wake_lead_min)
    if b:
        print(f"note: booking {b['key']} ({b['why']}) runs until {b['end']:%H:%M}, so the keeper wakes me again "
              f"for it; `ibasho unbook {b['key']}` drops it")


def awake(ctx: Ctx, a):
    store_of(ctx).update_state(nap_until=None)
    print("nap cleared")


def book(ctx: Ctx, a):
    ks, store = ks_of(ctx), store_of(ctx)
    tz = SLT if a.slt else None
    try:
        start = parse_time(a.start, tz)
        end = parse_end(a.end, start, tz)
        until = parse_date(a.until) if a.until else None
        repeat = "daily" if a.daily else "weekly" if a.weekly else None
        b = store.add_booking(start, end, " ".join(a.why), repeat=repeat, until=until)
    except ValueError as e:
        raise Fail(str(e)) from None
    print(f"booked {b['id']}: {start:%a %d %b %H:%M} to {from_iso(b['end']):%H:%M} (this machine's time)"
          + (f", {repeat}" + (f" until {until}" if until else "") if repeat else "") + f": {b['why']}")
    spans = settings_mod.parse_hours(ks.allowed_hours)
    if not settings_mod.in_hours(spans, start.hour * 60 + start.minute):
        print(f"note: that starts outside the hours the keeper may wake you ({ks.allowed_hours}), so it won't")


def unbook(ctx: Ctx, a):
    print("removed" if store_of(ctx).remove_booking(a.key) else "no such booking")


def bookings(ctx: Ctx, a):
    store = store_of(ctx)
    t0 = local_now()
    occ = occurrences(store.bookings(), t0, t0 + dt.timedelta(days=a.days))
    if not occ:
        print("no bookings")
    for o in occ:
        print(f"{o['key']:<18} {o['start']:%a %d %b %H:%M}-{o['end']:%H:%M}  {o['why']}"
              + (f"  ({o['repeat']})" if o["repeat"] else ""))


def notes(ctx: Ctx, a):
    cfg, ks, store = ctx.cfg, ks_of(ctx), store_of(ctx)
    me = "agent" if in_session() else "person"
    other = "person" if me == "agent" else "agent"
    text = " ".join(a.text).strip()
    if text:
        try:
            store.add_note(text, me)
        except ValueError as e:
            raise Fail(str(e)) from None
        print("sent" + ("" if me == "agent" else " (it reaches the agent at its next turn or through its watch)"))
        return
    them = prompts.person_label(ks.person) if me == "agent" else cfg.name
    msgs = store.notes()
    msgs = [m for i, m in enumerate(msgs) if i >= len(msgs) - a.n or (m["from"] == other and not m["read"])]
    if not msgs:
        print("no notes yet")
    for m in msgs:
        new = m["from"] == other and not m["read"]
        who = them if m["from"] == other else "me"
        print(f"{when(m['t'])}  {who}: {m['text']}" + ("  [new]" if new else ""))
    store.mark_notes([m["id"] for m in msgs if m["from"] == other and not m["read"]], "read")
