"""Hearing what happens: watch (one line per thing worth waking for, for Claude Code's Monitor tool), listen
(batch-read local chat in a talk or a crowd), events, IM sessions with unread counts, and quieting a chatty
object."""

from __future__ import annotations

import json
import re
import time

from ..api import ApiError, Down
from .common import Ctx, Fail, clock, fmt_event, is_uuid, mentions, need_ibasho, out_json, rid, short

QUIET_NOTIFY = {"ClothingLoading", "FailedToGetBenefits", "TeleportViaSLAPP", "PaymentSent"}
MONEY_NOTIFY = {"PaymentReceived", "PaymentSent"}   # the Ibasho viewer's `money` events say these, with the balance
OFFERS = ("TeleportOffered", "OfferFriendship", "TeleportRequest")
CROWD = 8                  # more people than this within 30 m: nearby chat wakes only when it's likely for me
GROUP_DIGEST_S = 120       # group chat comes as one line per group at most this often...
GROUP_LIVE_S = 300         # ...except for a while after I've spoken there
OBJECT_REPEAT = 4          # an object that says more than this many lines within OBJECT_WINDOW_S (a tram's stops,
OBJECT_WINDOW_S = 300      # a greeter, a game's scoreboard)...
OBJECT_DIGEST_S = 180      # ...comes as one digest line at most this often, until it's been quiet for a while
LOOK_REPEAT = 3            # `look`: an object with more lines than this since the last look shows as one line
QUIET_MINUTES = 60
# Room-wide hellos and goodbyes include me without naming me.
ROOM_SOCIAL = re.compile(
    r"\b(gn+|g'?nite|good ?night|nite|night night|bye+|cya|see (ya|you)|later|ttyl|take care|heading (out|off)"
    r"|off to bed|gotta go|have to go|gtg|g2g|hi|hiya|hello|hey|evening|morning|howdy|welcome|night"
    r"|have a (great|good|nice|lovely))\b.*\b(all|everyone|every1|everybody|y'?all|folks|peeps|friends|room)\b"
    r"|^\W*(gn+|g'?nite|good ?night|nite|night)\W*$", re.I)


def register(sub):
    p = sub.add_parser("watch", help="one line per event worth waking for, forever (run it under Monitor)")
    p.add_argument("--exit-on-wake", action="store_true",
                   help="return after the first such batch, with any group chat held for a digest (other harnesses)")
    p.add_argument("--timeout", type=float, default=0, help="with --exit-on-wake: give up after this many seconds")
    p.set_defaults(fn=watch)
    p = sub.add_parser("listen", help="collect local chat for a while without waking per line (talks, crowds)")
    p.add_argument("--secs", type=float, default=180)
    p.add_argument("--settle", type=float, default=None,
                   help="conversation mode: return this many seconds after the first new line from someone else")
    p.add_argument("--from", dest="only", metavar="REGEX", help="only speakers matching this (my name still wakes)")
    p.add_argument("--fresh", action="store_true", help="start from now, not where the last listen stopped")
    p.set_defaults(fn=listen)
    p = sub.add_parser("events", help="raw events after a sequence number, as JSON lines")
    p.add_argument("--since", "--after", dest="since", type=int, default=0,
                   help="events after this sequence number (the #N `--text` shows); 0, every one kept")
    p.add_argument("--wait", type=float, default=0)
    p.add_argument("--kinds", help="comma-separated")
    p.add_argument("--text", action="store_true", help="formatted lines instead of JSON")
    p.set_defaults(fn=events)
    p = sub.add_parser("ims", help="IM sessions, unread first")
    p.add_argument("--all", action="store_true", help="read ones too")
    p.set_defaults(fn=ims)
    p = sub.add_parser("im-log", help="a session's recent messages (marks it read)")
    p.add_argument("session")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=im_log)
    p = sub.add_parser("quiet", help="leave a chatty object's chat and IMs out of watch, listen and look for a while; "
                                     "without OBJECT, list the quieted ones")
    p.add_argument("object", nargs="?", help="its name (all objects of that name), id, or the id's first characters, "
                                             "as watch or look printed it")
    p.add_argument("--minutes", type=float, default=QUIET_MINUTES, help=f"how long (default {QUIET_MINUTES})")
    p.add_argument("--off", action="store_true", help="hear it again")
    p.set_defaults(fn=quiet)


# ---------- what's worth waking for ----------

TOOL_OBJECTS = {"ibasho name lookup", "ibasho self-check"}   # temporary prims of Ibasho's own


def tool_line(d: dict) -> bool:
    msg = d.get("message") or ""
    return msg == "Hello, Avatar!" or (d.get("chat_type") == 8 and msg.startswith("IBASHO|")) or \
        (d.get("source") == 2 and d.get("from") in TOOL_OBJECTS)


def wake_worthy(e: dict, me: str | None, money_events: bool = False) -> bool:
    """`money_events`: the viewer reports payments as `money` events (the Ibasho viewer), so its payment
    notifications would say the same twice."""
    k, d = e["kind"], e.get("data") or {}
    if k == "chat":
        if tool_line(d) or d.get("chat_type") in (4, 5):   # new scripts, my tools' protocols, typing on/off
            return False
        return d.get("from_id") != me
    if k == "im":
        return d.get("dir") == "in"
    if k == "im_transcript":
        return True
    if k == "object_im":
        return True
    if k == "money":
        return d.get("dir") == "in"
    if k == "notify":
        if d.get("name") == "IMToast":
            return False   # the im event already said it
        if d.get("replayed") and d.get("shown_before"):
            return False   # shown again at login, and already seen when it came
        if money_events and d.get("name") in MONEY_NOTIFY:
            return False
        return d.get("name") not in QUIET_NOTIFY and d.get("type") != "notifytip"
    if k == "teleport":
        return not d.get("success", True)
    if k == "refused":
        return True
    if k == "editor":   # errors from scripts in my objects (some regions send them as debug-channel lines)
        return d.get("method") == "runtime.error" or \
            (d.get("method") == "runtime.debug" and (d.get("params") or {}).get("channel") == "debug")
    if k == "daemon":
        msg = str(d.get("msg") or "")
        return "disconnected" in msg or "closing" in msg or "flag" in msg
    if k == "startup":
        return bool(d.get("after_failure"))
    if k in ("autopilot", "object_created", "object_properties", "move_done"):
        return False
    return bool(isinstance(d, dict) and d.get("wake"))   # side channels (hearing, the keeper) say so themselves


def notable(e: dict, me: str | None, money_events: bool = False) -> bool:
    """For `look`: wake-worthy things plus my own words, minus tool chatter."""
    d = e.get("data") or {}
    if e["kind"] == "chat":
        return not tool_line(d) and d.get("chat_type") not in (4, 5)
    return wake_worthy(e, me, money_events) or e["kind"] == "im"


# ---------- quieted objects ----------

class Quiet:
    """Objects whose chat and IMs the watch, listen and look leave out: by id, or by name (every object called
    that: the announcer in each car of a tram). Kept in state/quiet.json with an end time each."""

    def __init__(self, ctx: Ctx):
        self.path = ctx.state_file("quiet.json")
        self.mtime = None
        self.ids: dict[str, dict] = {}
        self.names: dict[str, dict] = {}
        self.reload()

    def reload(self):
        try:
            m = self.path.stat().st_mtime
        except OSError:
            m = None
        if m == self.mtime:
            return
        self.mtime = m
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d = d if isinstance(d, dict) else {}
        self.ids = d.get("ids") if isinstance(d.get("ids"), dict) else {}
        self.names = d.get("names") if isinstance(d.get("names"), dict) else {}

    def save(self):
        now = time.time()
        d = {"ids": {k: v for k, v in self.ids.items() if v.get("until", 0) > now},
             "names": {k: v for k, v in self.names.items() if v.get("until", 0) > now}}
        self.path.write_text(json.dumps(d), encoding="utf-8")
        self.mtime = None

    def active(self) -> list[tuple[str, str, dict]]:
        now = time.time()
        return [("id", k, v) for k, v in self.ids.items() if v.get("until", 0) > now] + \
            [("name", k, v) for k, v in self.names.items() if v.get("until", 0) > now]

    def match(self, e: dict) -> str | None:
        """The quieted object's name if `e` is chat or an IM from one, else None."""
        k, d = e["kind"], e.get("data") or {}
        if k == "chat" and d.get("source") == 2:
            oid, name = str(d.get("from_id") or ""), str(d.get("from") or "")
        elif k == "object_im":
            oid, name = str(d.get("object_id") or ""), str(d.get("from") or "")
        else:
            return None
        now = time.time()
        hit = self.ids.get(oid.lower()) or self.names.get(name.strip().lower())
        return (hit.get("name") or name or short(oid)) if hit and hit.get("until", 0) > now else None


def object_speakers(ctx: Ctx) -> list[tuple[str, str]]:
    """(id, name) of objects that spoke lately (chat and IMs in the events held in memory) or are near."""
    seen: dict[str, str] = {}
    for e in ctx.api.recent(kinds=["chat", "object_im"]):
        d = e.get("data") or {}
        if e["kind"] == "chat" and d.get("source") == 2 and d.get("from_id"):
            seen[str(d["from_id"]).lower()] = str(d.get("from") or "")
        elif e["kind"] == "object_im" and d.get("object_id"):
            seen[str(d["object_id"]).lower()] = str(d.get("from") or "")
    if ctx.api.ibasho:
        try:
            for o in ctx.api.op("IbashoObjects", "nearby", {"dist": 96, "limit": 300, "fetch": True,
                                                            "timeout": 3}, timeout=35).get("objects", []):
                seen.setdefault(str(o["id"]).lower(), str(o.get("name") or ""))
        except ApiError:
            pass
    return list(seen.items())


def quiet(ctx: Ctx, a):
    q = Quiet(ctx)
    if not a.object:
        rows = q.active()
        for how, key, v in rows:
            left = max(1, round((v["until"] - time.time()) / 60))
            what = f"{v.get('name')!r} ({short(key)})" if how == "id" else f"everything called {v.get('name')!r}"
            print(f"quiet: {what}, {left} min more")
        if not rows:
            print("nothing quieted")
        return
    want = a.object.strip()
    low = want.lower()
    if a.off:
        gone = [k for k in q.ids if k.startswith(low) or (q.ids[k].get("name") or "").lower() == low]
        for k in gone:
            q.ids.pop(k)
        # by name, or by the id of an object quieted by its name
        names = {low} | ({n.lower() for i, n in object_speakers(ctx) if i.startswith(low)} if len(low) >= 6 else set())
        for n in names & set(q.names):
            q.names.pop(n)
            gone.append(n)
        if not gone:
            raise Fail(f"nothing quieted matches {want!r} (`ibasho quiet` lists them)")
        q.save()
        print(f"hearing {want!r} again")
        return
    if not 0 < a.minutes <= 24 * 60:
        raise Fail("--minutes: more than 0, at most a day (1440)")
    until = time.time() + a.minutes * 60
    speakers = object_speakers(ctx)
    if is_uuid(want) or (len(low) >= 6 and all(c in "0123456789abcdef-" for c in low)):
        ids = [(i, n) for i, n in speakers if i.startswith(low)]
        if is_uuid(want) and not ids:
            ids = [(low, "")]
        if len(ids) > 1:
            raise Fail(f"{want!r} starts more than one object's id: " + ", ".join(f"{n!r} {i}" for i, n in ids[:6]))
        if not ids:
            raise Fail(f"no object near or heard lately has an id starting {want!r}")
        oid, name = ids[0]
        q.ids[oid] = {"name": name, "until": until}
        q.save()
        print(f"quiet: {name or short(oid)!r} ({short(oid)}) for {a.minutes:g} min (`ibasho quiet {short(oid)} --off` "
              "to hear it again)")
        return
    names = sorted({n for _, n in speakers if n and n.lower() == low}) or \
        sorted({n for _, n in speakers if n and low in n.lower()})
    if not names:
        raise Fail(f"no object near or heard lately is called {want!r}")
    if len({n.lower() for n in names}) > 1:
        raise Fail(f"{want!r} could be any of: " + ", ".join(repr(n) for n in names[:8]) + "; give the whole name")
    name = names[0]
    q.names[name.lower()] = {"name": name, "until": until}
    q.save()
    print(f"quiet: everything called {name!r} for {a.minutes:g} min (`ibasho quiet {json.dumps(name)} --off` to "
          "hear it again)")


class ObjectDigest:
    """An object repeating itself (a tram announcing every stop) comes as one line every few minutes instead of a
    line each time, until it has been quiet for a while. Lines that name me still come at once."""

    def __init__(self, me_re):
        self.me_re = me_re
        self.said: dict[str, list[float]] = {}
        self.buf: dict[str, list[dict]] = {}
        self.first: dict[str, float] = {}

    @staticmethod
    def is_object_chat(e) -> bool:
        return e["kind"] == "chat" and (e.get("data") or {}).get("source") == 2

    def take(self, e) -> bool:
        """True: print now. False: held for the digest."""
        d = e["data"]
        oid = str(d.get("from_id") or "")
        now = time.time()
        times = [t for t in self.said.get(oid, []) if now - t < OBJECT_WINDOW_S] + [now]
        self.said[oid] = times
        if self.me_re and self.me_re.search(d.get("message") or ""):
            return True
        if oid in self.buf or len(times) > OBJECT_REPEAT:
            self.buf.setdefault(oid, []).append(d)
            self.first.setdefault(oid, now)
            return False
        return True

    def next_due(self) -> float | None:
        return min(self.first.values()) + OBJECT_DIGEST_S if self.first else None

    def flush(self, force=False) -> list[str]:
        lines, now = [], time.time()
        for oid in list(self.buf):
            if force or now - self.first[oid] >= OBJECT_DIGEST_S:
                msgs = self.buf.pop(oid)
                mins = max(1, round((now - self.first.pop(oid)) / 60))
                last = " | ".join((m.get("message") or "")[:160] for m in msgs[-3:])
                lines.append(f"object digest {msgs[-1].get('from')!r} ({short(oid)}) said {len(msgs)} "
                             f"line{'s' if len(msgs) != 1 else ''} in {mins} min, the last: {last} (it keeps "
                             f"talking: `ibasho quiet {short(oid)}` leaves it out)")
        for oid in [o for o, ts in self.said.items() if not ts or now - ts[-1] > OBJECT_WINDOW_S]:
            self.said.pop(oid)
        return lines


def collapse_repeats(evs: list[dict], q: Quiet | None) -> list[tuple[dict, str | None]]:
    """For `look`: (event, None) to show as it is, or (event, line) to show as that line instead: an object's lines
    beyond LOOK_REPEAT since the last look become one line where its last one was, and quieted objects' lines one
    line at the end."""
    by_obj: dict[str, list[int]] = {}
    hushed: dict[str, int] = {}
    keep: list[tuple[dict, str | None]] = []
    for e in evs:
        if q and (name := q.match(e)):
            hushed[name] = hushed.get(name, 0) + 1
            continue
        if ObjectDigest.is_object_chat(e):
            by_obj.setdefault(str(e["data"].get("from_id") or ""), []).append(len(keep))
        keep.append((e, None))
    drop = set()
    for oid, idx in by_obj.items():
        if len(idx) > LOOK_REPEAT:
            last = keep[idx[-1]][0]
            d = last["data"]
            keep[idx[-1]] = (last, f"chat {d.get('from')} (object, {short(oid)}) said {len(idx)} lines, the last: "
                                   f"{d.get('message')} (`ibasho quiet {short(oid)}` leaves it out)")
            drop.update(idx[:-1])
    out = [k for i, k in enumerate(keep) if i not in drop]
    if hushed:
        out.append(({"t": evs[-1]["t"], "kind": "quiet", "data": {}},
                    "quieted: " + ", ".join(f"{n!r} {c} line{'s' if c != 1 else ''}" for n, c in hushed.items())))
    return out


class GroupDigest:
    """Group chat as one line per group every couple of minutes, live when I'm named or have just spoken there."""

    def __init__(self, me_re):
        self.me_re = me_re
        self.buf, self.first, self.my_last = {}, {}, {}

    @staticmethod
    def is_group(e) -> bool:
        return e["kind"] == "im" and (e.get("data") or {}).get("session_type") == "group"

    def take(self, e) -> bool:
        """True: print now. False: buffered (or my own line)."""
        d = e["data"]
        sid = d.get("session_id")
        if d.get("dir") == "out":
            self.my_last[sid] = time.time()
            self.buf.pop(sid, None)
            self.first.pop(sid, None)
            return False
        if (self.me_re and self.me_re.search(d.get("message") or "")) or \
                time.time() - self.my_last.get(sid, 0) < GROUP_LIVE_S:
            return True
        self.buf.setdefault(sid, []).append(d)
        self.first.setdefault(sid, time.time())
        return False

    def next_due(self) -> float | None:
        return min(self.first.values()) + GROUP_DIGEST_S if self.first else None

    def flush(self, force=False) -> list[str]:
        lines = []
        for sid in list(self.buf):
            if force or time.time() - self.first[sid] >= GROUP_DIGEST_S:
                msgs = self.buf.pop(sid)
                self.first.pop(sid)
                name = msgs[0].get("session_name") or short(sid)
                body = " | ".join(f"{m.get('from')}: {(m.get('message') or '')[:160]}" for m in msgs[-12:])
                more = f" (+{len(msgs) - 12} earlier)" if len(msgs) > 12 else ""
                lines.append(f"group digest [{name}] {len(msgs)} msgs{more}: {body}")
        return lines


def unread_line(ctx: Ctx) -> str | None:
    if not ctx.api.ibasho:
        return None
    try:
        ss = ctx.api.op("IbashoIM", "sessions").get("sessions", [])
    except ApiError:
        return None
    direct = [s for s in ss if s.get("unread") and s.get("type") != "group"]
    groups = sum(s.get("unread") or 0 for s in ss if s.get("type") == "group")
    if not direct and not groups:
        return None
    n = sum(s["unread"] for s in direct)
    bits = []
    if direct:
        bits.append(f"{n} IM{'s' if n != 1 else ''} in {len(direct)} conversation{'s' if len(direct) != 1 else ''} "
                    f"({', '.join(repr(s.get('name')) for s in direct[:4])})")
    if groups:
        bits.append(f"{groups} in groups")
    return "unread: " + "; ".join(bits) + " (`ibasho ims`)"


# ---------- watch ----------

class Watcher:
    """What `watch` keeps between polls: where it's up to in the event log, group chat being gathered into digests,
    dialogs and refusals already shown, whether it's crowded. `watch` polls one in a loop; a harness that can't keep
    a process running between turns (the MCP server) keeps one and polls it on each wait."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        st = ctx.api.status(fresh=True)
        self.since = st["seq"]
        self.cursor = ctx.state_file("watch.cursor")
        try:  # a re-armed watch picks up where the last one stopped, if that was recent
            prev, t = (float(v) for v in self.cursor.read_text().split())
            if time.time() - t < 600 and prev <= self.since:
                self.since = int(prev)
        except (OSError, ValueError):
            pass
        self.me = (st.get("self") or {}).get("id")
        self.me_re = mentions(st)
        self.groups = GroupDigest(self.me_re)
        self.objects = ObjectDigest(self.me_re)
        self.quiet = Quiet(ctx)
        self.crowd, self.crowd_at, self.last_said = False, 0.0, 0.0
        self.shown: dict[str, float] = {}

    def poll(self, wait: float = 25) -> list[str]:
        """Lines worth waking for: group digests now due, else what the next batch of events (within `wait` seconds)
        holds, which may be nothing. Down and ApiError pass through."""
        if lines := self.groups.flush() + self.objects.flush():
            return lines
        if due := min((d for d in (self.groups.next_due(), self.objects.next_due()) if d), default=None):
            wait = min(wait, max(0.1, due - time.time()))
        api = self.ctx.api
        evs = api.events(self.since, wait=wait)
        self.quiet.reload()
        if not self.me:
            st = api.status(fresh=True)
            self.me = (st.get("self") or {}).get("id")
            self.me_re = self.groups.me_re = self.objects.me_re = mentions(st)
        if api.ibasho and time.time() - self.crowd_at > 60:
            self.crowd_at = time.time()
            try:
                self.crowd = len(api.op("IbashoAgent", "people", {"dist": 30}, timeout=5).get("people", [])) > CROWD
            except ApiError:
                pass
        if evs:
            self.cursor.write_text(f"{evs[-1]['seq']} {time.time()}")
        lines = []
        for e in evs:
            self.since = max(self.since, e["seq"])
            if self.wakes(e):
                lines.append(fmt_event(self.ctx, e).replace("\n", " "))
        return lines

    def wakes(self, e: dict) -> bool:
        d = e.get("data") or {}
        if e["kind"] == "chat" and d.get("from_id") == self.me:
            self.last_said = time.time()
        if GroupDigest.is_group(e):
            return self.groups.take(e)
        if not wake_worthy(e, self.me, self.ctx.api.ibasho):
            return False
        if self.quiet.match(e):
            return False
        if ObjectDigest.is_object_chat(e) and not self.objects.take(e):
            return False
        if self.crowd and e["kind"] == "chat":
            msg = d.get("message") or ""
            if not ((self.me_re and self.me_re.search(msg)) or ROOM_SOCIAL.search(msg)
                    or time.time() - self.last_said < 90):
                return False
        if e["kind"] == "notify" and d.get("name") == "ScriptDialog":
            if self.seen(d.get("message") or "", 600):
                return False   # menus re-send themselves after every choice
        if e["kind"] == "refused":
            if self.seen(f"refused {d.get('what')}", 60):
                return False   # one line a minute per kind of refusal is enough to know
        return True

    def seen(self, key: str, within: float) -> bool:
        if time.time() - self.shown.get(key, 0) < within:
            return True
        self.shown[key] = time.time()
        return False

    def drain(self) -> list[str]:
        """Group chat and objects' lines still being gathered, as digests now (before stopping, so they aren't
        lost)."""
        return self.groups.flush(force=True) + self.objects.flush(force=True)


def watch(ctx: Ctx, a):
    w = Watcher(ctx)
    if line := unread_line(ctx):
        print(line, flush=True)
    end = time.time() + a.timeout if a.timeout else None
    while True:
        left = end - time.time() if end else 25
        if left <= 0:
            for line in w.drain():
                print(line, flush=True)
            return
        try:
            lines = w.poll(min(25.0, left))
        except Down:
            print("watch: daemon unreachable", flush=True)
            time.sleep(min(10.0, left))
            continue
        except ApiError as e:
            print(f"watch: {e}", flush=True)
            time.sleep(min(5.0, left))
            continue
        if a.exit_on_wake and lines:
            # group chat and objects' lines held back for a digest would be lost with this process: they come along now
            lines += w.drain()
        for line in lines:
            print(line, flush=True)
        if a.exit_on_wake and lines:
            return


# ---------- listen ----------

def listen(ctx: Ctx, a):
    api = ctx.api
    st = api.status(fresh=True)
    me = (st.get("self") or {}).get("id")
    me_re = mentions(st)
    only = re.compile(a.only, re.I) if a.only else None
    cur = ctx.state_file("listen.cursor")
    since = st["seq"]
    try:
        prev, t = (float(v) for v in cur.read_text().split())
        if not a.fresh and time.time() - t < 600 and prev <= since:
            since = int(prev)
    except (OSError, ValueError):
        pass
    start = time.time()
    end = start + a.secs
    out, urgent, first_other = [], None, None
    q, hushed = Quiet(ctx), 0
    while time.time() < end and not urgent:
        if first_other and time.time() > first_other + a.settle:
            break
        try:
            evs = api.events(since, wait=min(5.0, max(0.1, end - time.time())))
        except Down:
            out.append("(daemon unreachable)")
            break
        for e in evs:
            since = max(since, e["seq"])
            k, d = e["kind"], e.get("data") or {}
            fresh = e["t"] >= start - 2          # older ones the watch already showed
            if k == "im" and d.get("dir") == "in" and d.get("session_type") != "group" and fresh:
                urgent = f"IM {d.get('from')}: {d.get('message')}"
            elif k == "notify" and d.get("name") in OFFERS and fresh:
                urgent = f"{d.get('name')} (respond {d.get('id')}): {(d.get('message') or '')[:160]}"
            elif k not in ("chat", "im", "notify") and wake_worthy(e, me, api.ibasho) and fresh:
                urgent = fmt_event(ctx, e)
            elif k == "chat" and q.match(e):
                hushed += 1
            elif k == "chat" and d.get("chat_type") not in (4, 5, 8) and not tool_line(d):
                who, msg = d.get("from") or "?", (d.get("message") or "").strip()
                mine = d.get("from_id") == me
                named = bool(me_re and me_re.search(msg)) and not mine
                if only and not only.search(who) and not named and not mine:
                    continue
                out.append(f"{who}: {msg}")
                if a.settle is not None and fresh and not mine and d.get("source") == 1 and first_other is None:
                    first_other = time.time()
                if named and fresh:
                    urgent = "(my name came up)"
            if urgent:
                break
    cur.write_text(f"{since} {time.time()}")
    print("\n".join(out) if out else "(quiet)")
    if hushed:
        print(f"({hushed} line{'s' if hushed != 1 else ''} from quieted objects left out)")
    if urgent:
        print("URGENT: " + urgent)


# ---------- events and IMs ----------

def events(ctx: Ctx, a):
    kinds = a.kinds.split(",") if a.kinds else None
    for e in ctx.api.events(a.since, a.wait, kinds):
        if a.text:
            print(f"#{e['seq']} {clock(e['t'])} {fmt_event(ctx, e)}")
        else:
            out_json(e)


def ims(ctx: Ctx, a):
    need_ibasho(ctx, "ims")
    ss = ctx.api.op("IbashoIM", "sessions").get("sessions", [])
    ss.sort(key=lambda s: -(s.get("unread") or 0))
    shown = 0
    for s in ss:
        if not a.all and not s.get("unread"):
            continue
        shown += 1
        print(f"{s['type']:10} {s['name']!r} unread={s.get('unread') or 0} msgs={s.get('messages')} "
              f"s:{s['session_id']}{' with ' + short(s.get('other_id')) if s.get('other_id') else ''}")
    rest = len(ss) - shown
    if rest:
        print(f"({rest} read session{'s' if rest != 1 else ''}; --all lists them)")
    if not ss:
        print("(no IM sessions)")


def im_log(ctx: Ctx, a):
    need_ibasho(ctx, "im-log")
    sid = rid(ctx, a.session, ("session",))
    r = ctx.api.op("IbashoIM", "history", {"session_id": sid, "count": a.n})
    print(f"{r['session']['type']} {r['session']['name']!r}")
    for m in r.get("messages", []):
        print(f"  [{m.get('time')}] {m.get('from')}: {m.get('message')}")
    ctx.api.post("IbashoIM", "markRead", {"session_id": sid})
