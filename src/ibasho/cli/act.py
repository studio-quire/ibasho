"""Acting: say and IM, walking (looking for drops first, stepping round people), held-key moves, teleports,
touching and sitting, and answering dialogs and offers."""

from __future__ import annotations

import argparse
import json
import math
import re
import time
import urllib.parse
import urllib.request

from .. import geometry as geo, probe
from ..config import MATURITY
from ..api import ApiError
from ..places import looks_like_slurl, parse_slurl
from ..policy import button_base
from .common import Ctx, Fail, humanize, is_uuid, name_of, names, need_ibasho, position, resolve, rid, short

MAX_CHAT = 1000   # Second Life cuts chat lines at 1023 bytes


# In a shell, "$" and "`" inside double quotes start substitutions, and agents' harnesses refuse commands with them
# (Claude Code does without a prompt to ask): L$ needs writing as L\$ there, or the text single-quoted.
TEXT_HELP = ("the words; in a shell, inside double quotes write $ as \\$ (\"L\\$50\") and avoid backticks, or "
             "single-quote the text")


def register(sub):
    p = sub.add_parser("say", aliases=["chat"],
                       help="say something in local chat (or on a channel: --ch N, or a leading /N)")
    p.add_argument("text", help=TEXT_HELP)
    p.add_argument("--ch", type=int, default=None)
    p.add_argument("--type", default="normal", choices=["normal", "whisper", "shout"])
    p.set_defaults(fn=say)
    p = sub.add_parser("im", help="IM someone (name, id or id prefix), a group, or a session (s:ID)")
    p.add_argument("who", help="a name (display or user name, of someone near, a friend, or recent: IMs, payments), "
                               "an avatar id or its first characters, a group, or a session as printed (s:...)")
    p.add_argument("text", help=TEXT_HELP)
    p.set_defaults(fn=im)
    p = sub.add_parser("friend", help="offer someone friendship (a name, a username, or an id)")
    p.add_argument("who", help="as for `im`: someone near, known or recent, or anyone by username (first.last)")
    p.add_argument("--message", default="", help="a line to go with the offer")
    p.set_defaults(fn=friend)
    p = sub.add_parser("walk", help="walk to x y [z] (region coordinates); probes for drops, steps round people")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.add_argument("z", type=float, nargs="?")
    p.add_argument("--region", help="x y are in this region (default: the one I'm in)")
    p.add_argument("--wait", action="store_true", help="block until I arrive or stop; say where I ended up")
    p.add_argument("--stop", type=float, default=1.0, help="stop this far from the target")
    p.add_argument("--fly", action="store_true", help="let the autopilot fly")
    p.add_argument("--force", action="store_true", help="skip the drop probe")
    p.add_argument("--through", action="store_true", help="straight line, even through people")
    p.set_defaults(fn=walk)
    p = sub.add_parser("ahead", help="the ground ahead: heights vs my feet along a bearing")
    p.add_argument("bearing", nargs="?", help="compass degrees or N/NE/...; default the way I face")
    p.add_argument("--dist", type=float, default=8)
    p.add_argument("--step", type=float, default=1)
    p.set_defaults(fn=ahead)
    p = sub.add_parser("move", help="hold movement controls like keys (no window focus needed; steers vehicles)")
    p.add_argument("--forward", "-f", type=float, help="-1..1")
    p.add_argument("--strafe", "-s", type=float, help="-1..1, + right")
    p.add_argument("--up", "-u", type=float, help="-1..1: jump/fly up, crouch/down")
    p.add_argument("--turn", "-t", type=float, help="deg/s, + clockwise (a turn key is 90)")
    p.add_argument("--face", type=float, help="compass bearing to face first")
    p.add_argument("--run", action="store_true")
    p.add_argument("--fly", action=argparse.BooleanOptionalAction)
    p.add_argument("--secs", type=float, default=1.0)
    p.add_argument("--nowait", action="store_true")
    p.add_argument("--force", action="store_true", help="skip the drop probe")
    p.set_defaults(fn=move)
    sub.add_parser("stop", help="end a move now").set_defaults(fn=stop)
    p = sub.add_parser("tp", help="teleport to REGION X Y Z, or to a SLURL (maps.secondlife.com/secondlife/..., "
                                  "secondlife://...)")
    p.add_argument("region", help="a region's name, or a SLURL (its coordinates are used unless X Y Z follow)")
    p.add_argument("x", type=float, nargs="?", help="region coordinates; without them the region's middle")
    p.add_argument("y", type=float, nargs="?")
    p.add_argument("z", type=float, nargs="?")
    p.set_defaults(fn=tp)
    p = sub.add_parser("touch", help="touch an object")
    p.add_argument("id")
    p.add_argument("--face", type=int, default=0)
    p.set_defaults(fn=touch)
    p = sub.add_parser("sit", help="sit on an object (as right-click > Sit), or on the ground")
    p.add_argument("id", nargs="?")
    p.set_defaults(fn=sit)
    sub.add_parser("stand", help="stand up").set_defaults(fn=stand)
    p = sub.add_parser("respond", help="answer a notification (an offer, a request): respond ID BUTTON, or --cancel")
    p.add_argument("id", help="its id as printed (`notify NAME id=...`), or the first characters of it")
    p.add_argument("answer", nargs="?", default=None,
                   help="a button, by name or label (Keep, Discard, Accept...); or JSON for forms with fields: "
                        "'{\"BUTTON\": true, \"FIELD\": \"text\"}'")
    p.add_argument("--cancel", action="store_true", help="dismiss it without answering")
    p.set_defaults(fn=respond)
    p = sub.add_parser("pick", help="answer the latest script dialog with the button whose label contains TEXT")
    p.add_argument("text")
    p.set_defaults(fn=pick)
    p = sub.add_parser("textbox", help="answer the latest script text box with TEXT")
    p.add_argument("text")
    p.set_defaults(fn=textbox)


# ---------- talking ----------

def say(ctx: Ctx, a):
    # Git Bash turns a leading "/me" into "C:/Program Files/Git/me"; undo that.
    text = re.sub(r"^[A-Za-z]:/Program Files/Git(?=/)", "", a.text)
    ch = a.ch
    m = re.match(r"^/(-?\d+)\s+(.*)$", text, re.S)
    if m and ch is None:   # "/7 hello" means channel 7, as in the viewer's chat bar
        ch, text = int(m.group(1)), m.group(2)
    if len(text.encode()) > MAX_CHAT:
        raise Fail(f"too long for one chat line ({len(text.encode())} bytes; {MAX_CHAT} max): split it")
    ctx.api.cmd("LLChatBar", "sendChat", {"message": text, "channel": ch or 0, "type": a.type}, wait=False)


def im(ctx: Ctx, a):
    need_ibasho(ctx, "im")
    api = ctx.api
    kind, i = resolve(ctx, a.who, ("session", "group", "avatar"))
    sessions = None
    if kind is None:   # a full id: a session if one has it, else an avatar
        sessions = api.op("IbashoIM", "sessions").get("sessions", [])
        kind = "session" if any(s["session_id"] == i for s in sessions) else "avatar"
    label = ""
    if kind == "avatar":
        # A typed-out id can be wrong past the part actually seen; a real avatar has a name.
        n = names(ctx, [i]).get(i)
        if not n:
            raise Fail(f"no avatar has id {i}; not sent")
        label = name_of(n)
    elif kind == "session":
        sessions = sessions or api.op("IbashoIM", "sessions").get("sessions", [])
        label = next((s["name"] for s in sessions if s["session_id"] == i), "")
    key = {"session": "session_id", "group": "group_id", "avatar": "to"}[kind]
    since = api.seq()
    r = api.op("IbashoIM", "send", {key: i, "message": a.text}, timeout=25)
    # The viewer's own daily cap answers like a send but says `refused` first (the event precedes the reply).
    if no := next((e for e in api.events(since, kinds=["refused"]) if e["data"].get("what") == "IM"), None):
        raise Fail("not sent: " + str(no["data"].get("reason") or "the viewer refused it").removeprefix("Ibasho: "))
    print(f"sent ({kind}{' ' + repr(label) if label else ''}) s:{short(r.get('session_id'))}")


def friend(ctx: Ctx, a):
    need_ibasho(ctx, "friend")
    _, who = resolve(ctx, a.who, ("avatar",))
    n = names(ctx, [who]).get(who)
    if not n:
        raise Fail(f"no avatar has id {who}; no offer sent")
    if any(f.get("id") == who for f in ctx.api.op("IbashoAgent", "friends").get("friends", [])):
        print(f"already friends with {name_of(n)}")
        return
    args = {"to": who}
    if a.message:
        args["message"] = a.message
    ctx.api.op("IbashoAgent", "offerFriendship", args)
    user = n.get("username") or ""
    print(f"offered friendship to {name_of(n)}{f' ({user})' if user.lower() != name_of(n).lower() else ''}; their "
          "answer comes as a notification")


# ---------- moving ----------

def find_region(ctx: Ctx, name: str) -> dict | None:
    """The grid's map on a region (`IbashoAgent findRegion`): {found, region?: {name, global, access, maturity},
    similar?}; None when the viewer can't look regions up or the map didn't answer."""
    api = ctx.api
    if not api.has_op("IbashoAgent", "findRegion"):
        return None
    try:
        r = api.op("IbashoAgent", "findRegion", {"name": name, "timeout": 10}, timeout=25)
    except ApiError:
        return None
    return r if isinstance(r.get("found"), bool) else None


def no_region(name: str, r: dict) -> Fail:
    similar = [s for s in r.get("similar") or [] if s]
    return Fail(f"no region called {name!r} on this grid's map" +
                (f"; similar names: {', '.join(similar[:8])}" if similar else "") +
                " (region names are as the map spells them; spaces count)")


def region_origin(ctx: Ctx, name: str) -> tuple[int, int]:
    """Global metres of a region's south-west corner: from the grid's map through the viewer where it can, else
    the Second Life public map service."""
    if (r := find_region(ctx, name)) is not None:
        if not r["found"]:
            raise no_region(name, r)
        g = r["region"]["global"]
        return int(g[0]), int(g[1])
    url = ("https://cap.secondlife.com/cap/0/d661249b-2b5a-4436-966a-3d3b8d7a574f?var=coords&sim_name="
           + urllib.parse.quote(name))
    with urllib.request.urlopen(url, timeout=10) as r:
        m = re.search(r"'x'\s*:\s*(\d+).*'y'\s*:\s*(\d+)", r.read().decode())
    if not m:
        raise Fail(f"no region named {name!r}")
    return int(m.group(1)) * 256, int(m.group(2)) * 256


def probe_hint(ctx: Ctx) -> str:
    f = ctx.state_file("probe_hint")
    if f.exists():
        return " (no drop probe)"
    f.write_text("shown")
    return (" (no drop probe: this viewer has no IbashoAgent probeGround, and no probe attachment is configured; "
            "walks can't stop short of ledges or water)")


def probe_line(ctx: Ctx, here, bearing, dist):
    """(distance to keep, message) for a walk or move along bearing, or (None, note) if clear or unknown."""
    # The viewer's probe takes up to 256 points in one call, so sample finely: the first bad point is then at most
    # half a metre past the edge, and stopping a metre short of it stays on the near side.
    viewer = probe.available(ctx.api) == "viewer"
    step = 0.5 if viewer else 1.0 if dist <= 10 else 2.0
    prof = probe.ground_profile(ctx.api, here, bearing, dist, step)
    if prof is None:
        return None, probe_hint(ctx) if not probe.available(ctx.api) else " (probe didn't answer)"
    drop = probe.first_drop(prof)
    if drop is None:
        return None, ""
    return max(drop - step - 0.5, 0.0), f"drop or water {drop:.1f} m ahead"


def walk(ctx: Ctx, a):
    api = ctx.api
    pos = position(ctx)
    if a.region:
        ox, oy = region_origin(ctx, a.region)
    else:
        ox, oy = pos["global"][0] - pos["region"][0], pos["global"][1] - pos["region"][1]
    z = a.z if a.z is not None else pos["global"][2]
    tx, ty = a.x, a.y
    asked = (ox + tx, oy + ty)
    here = pos["region"]
    span = math.hypot(tx - here[0], ty - here[1])
    note = ""
    if not (a.force or a.fly or a.region) and span > 1.5:
        keep, note = probe_line(ctx, here, geo.bearing(here, (tx, ty)), min(span, 16.0))
        if keep is not None:
            print(f"{note}; stopping {keep:.1f} m along (--force walks anyway)")
            if keep < 0.5:
                return
            tx, ty = here[0] + (tx - here[0]) * keep / span, here[1] + (ty - here[1]) * keep / span
            note = ""
    if not (a.fly or a.region or a.through) and span > 1.5 and api.ibasho:
        for _ in range(2):   # step round people instead of through them
            here = position(ctx)["region"]
            try:
                ppl = api.op("IbashoAgent", "people", {"dist": span + 3}, timeout=5).get("people", [])
            except ApiError:
                break
            side = geo.detour(here, (tx, ty), [p["pos"] for p in ppl if p.get("pos")])
            if not side:
                break
            print(f"going round someone via {side[0]:.1f},{side[1]:.1f}")
            api.post("LLAgent", "startAutoPilot", {"target_global": [ox + side[0], oy + side[1], z],
                                                   "stop_distance": 0.5, "allow_flying": False})
            for _ in range(20):
                time.sleep(0.5)
                g = position(ctx)["region"]
                if math.hypot(g[0] - side[0], g[1] - side[1]) < 1.0:
                    break
    target = [ox + tx, oy + ty, z]
    # The autopilot flies whenever the target's z is off the ground unless told not to.
    api.post("LLAgent", "startAutoPilot", {"target_global": target, "stop_distance": a.stop,
                                           "allow_flying": bool(a.fly)})
    if not a.wait:
        if note:
            print("walking" + note)
        return
    last, still = None, 0
    for _ in range(120):
        time.sleep(1)
        g = position(ctx)["global"]
        if math.hypot(g[0] - target[0], g[1] - target[1]) <= a.stop + 1.5:
            break
        if last and math.hypot(g[0] - last[0], g[1] - last[1]) < 0.2:
            still += 1
            if still >= 3:
                break
        else:
            still = 0
        last = g
    p = position(ctx)
    d = math.hypot(p["global"][0] - asked[0], p["global"][1] - asked[1])
    # standing still near the target is arriving (the autopilot ends a little off it, more so after going round
    # someone); only far from it is it stuck (seen as false alarms on Second Life, 2026-11-03)
    if still >= 3 and d > a.stop + 3:
        print("stopped short (stuck?)")
    print(f"at {[round(v, 1) for v in p['region']]}, {d:.0f} m from the target{note}")


def ahead(ctx: Ctx, a):
    here = position(ctx)["region"]
    from .see import compass_or_fail
    b = compass_or_fail(a.bearing)
    if b is None:
        if probe.available(ctx.api) == "viewer":
            raise Fail("give a bearing (N, NE, ... or degrees)")
    prof = probe.ground_profile(ctx.api, here, b, a.dist, a.step)
    if prof is None:
        raise Fail("no ground probe here: " + probe_hint(ctx).strip(" ()"))
    print(f"feet at z {prof.feet:.2f}; ground vs feet:")
    warn = None
    for d, dz in prof.points:
        if dz == probe.UNKNOWN:
            print(f"  {d:5.1f} m        ?")
            continue
        bad = dz is None or abs(dz) > 1.5
        if bad and warn is None:
            warn = d
        mark = ("  <- " + ("no ground" if dz is None else "drop" if dz < 0 else "wall")) if bad else ""
        print(f"  {d:5.1f} m  {'X' if dz is None else f'{dz:.2f}':>7}{mark}")
    print(f"first hazard at {warn:.1f} m" if warn is not None else "clear")


def fmt_where(r: dict) -> str:
    bits = [f"at {[round(v, 1) for v in r['pos']]}", f"heading {r['heading']}"]
    if r.get("region"):
        bits.append(f"in {r['region']}")
    return ", ".join(bits + [w for w in ("sitting", "flying") if r.get(w)])


def move(ctx: Ctx, a):
    need_ibasho(ctx, "move")
    api = ctx.api
    if (a.forward or 0) > 0 and not a.force and not a.fly and (a.up or 0) <= 0:
        cur = api.op("IbashoAgent", "move", {"secs": 0.05, "wait": True}, timeout=10)
        if not cur.get("sitting"):   # vehicles steer while I sit; don't probe for them
            reach = min(a.secs * (5.5 if a.run else 3.0) * a.forward, 16.0)
            bearing = a.face if a.face is not None else cur.get("heading")
            if reach > 1.0 and bearing is not None:
                keep, msg = probe_line(ctx, cur["pos"], float(bearing), max(reach, 2.0))
                if keep is not None:
                    print(f"{msg}; shortening the move to ~{keep:.1f} m (--force goes anyway)")
                    if keep < 0.5:
                        return
                    a.secs = a.secs * keep / reach
    args = {"secs": a.secs, "wait": not a.nowait}
    for k in ("forward", "strafe", "turn", "up", "face"):
        if getattr(a, k) is not None:
            args[k] = getattr(a, k)
    if a.run:
        args["run"] = True
    if a.fly is not None:
        args["fly"] = a.fly
    r = api.op("IbashoAgent", "move", args, timeout=a.secs + 15)
    if a.nowait:
        print(f"moving for {a.secs:g}s from {fmt_where(r)}")
    else:
        print(f"{r['reason']} after {r['secs']:.1f}s, moved {r['moved']:.1f} m; {fmt_where(r)}")


def stop(ctx: Ctx, a):
    need_ibasho(ctx, "stop")
    r = ctx.api.op("IbashoAgent", "stop")
    print(("stopped; " if r.get("was_moving") else "wasn't moving; ") + fmt_where(r))


def tp_target(a) -> tuple[str, float, float, float]:
    """Region and coordinates from `tp`'s arguments: a region name and X Y Z, a SLURL, or a SLURL or name alone."""
    region, pos = a.region.strip(), (128.0, 128.0, 0.0)
    if looks_like_slurl(region):
        where = parse_slurl(region)
        if where is None:
            raise Fail(f"not a SLURL of a place: {region} (they look like "
                       "https://maps.secondlife.com/secondlife/REGION/X/Y/Z)")
        region, pos = where[0], where[1:]
    given = [v for v in (a.x, a.y, a.z) if v is not None]
    if given:
        if len(given) != 3:
            raise Fail("give all three of X Y Z, or none")
        pos = (a.x, a.y, a.z)
    return region, *pos


def tp(ctx: Ctx, a):
    api = ctx.api
    region, x, y, z = tp_target(a)
    # The viewer's teleport by name asks the map where the region is, and does nothing at all when the map knows
    # no such region: so ask first, and say so.
    if (r := find_region(ctx, region)) is not None:
        if not r["found"]:
            raise no_region(region, r)
        region = r["region"].get("name") or region
        caps = api.status().get("caps") or {}
        cap = MATURITY.get(str(caps.get("maturity") or ""))
        if cap and int(r["region"].get("access") or 0) > cap:
            print(f"({region} is rated {r['region'].get('maturity')}, above this install's maturity setting "
                  f"({caps.get('maturity')}): the grid will likely refuse the teleport)")
    since = api.seq()
    api.post("LLAgent", "requestTeleport", {"regionname": region, "x": x, "y": y, "z": z,
                                            "skip_confirmation": True})
    end = time.time() + 40
    while time.time() < end:
        for e in api.events(since, wait=2, kinds=["notify", "teleport"]):
            since = max(since, e["seq"])
            d = e["data"]
            if e["kind"] == "notify" and d.get("name") == "TeleportViaSLAPP":
                api.post("LLNotifications", "respond", {"uuid": d["id"], "response": {"OK_okcancelignore": True}})
            elif e["kind"] == "notify" and d.get("name") in ("CouldNotTeleportReason", "TeleportFailed"):
                # a refusal leaves a modal alert that swallows keyboard input; close it
                api.post("LLNotifications", "cancel", {"uuid": d["id"]})
                print("refused: " + humanize(ctx, d.get("message") or "").replace("\n", " ")[:200])
                return
            elif e["kind"] == "teleport" and d.get("success"):
                time.sleep(2)
                print("arrived")
                return
    print("no result yet (watch for it; a teleport to a name the map doesn't know never starts at all)")


def touch(ctx: Ctx, a):
    ctx.api.post("LLAgent", "requestTouch", {"obj_uuid": rid(ctx, a.id), "face": a.face})


def sit(ctx: Ctx, a):
    since = ctx.api.seq()
    r = ctx.api.cmd("LLAgent", "requestSit", {"obj_uuid": rid(ctx, a.id)} if a.id else {}, timeout=15)
    if isinstance(r, dict) and r.get("error"):
        raise Fail(str(r["error"]))
    # The region refuses a sit with a notification (CantSitNoRoom, CantSitTooFar...), not in the request's answer
    end = time.time() + 3
    while time.time() < end:
        for e in ctx.api.events(since, wait=1, kinds=["notify"]):
            since = max(since, e["seq"])
            d = e.get("data") or {}
            if str(d.get("name") or "").startswith(("CantSit", "SitFail")):
                raise Fail(f"the region refused the sit ({d.get('name')}): "
                           + humanize(ctx, str(d.get("message") or "")).replace("\n", " "))


def stand(ctx: Ctx, a):
    ctx.api.post("LLAgent", "requestStand")


# ---------- dialogs and offers ----------

def notification(ctx: Ctx, ident: str) -> tuple[str, dict | None]:
    """(full id, the notification as last shown, if it's among the recent events) for an id or its first
    characters."""
    ident = ident.strip().lower().removeprefix("id=")
    seen: dict[str, dict] = {}
    for e in ctx.api.recent(kinds=["notify"]):
        nid = str((e.get("data") or {}).get("id") or "").lower()
        if nid.startswith(ident):
            seen[nid] = e["data"]
    if is_uuid(ident):
        return ident, seen.get(ident)
    if len(seen) == 1:
        return next(iter(seen.items()))
    if seen:
        raise Fail(f"{ident!r} starts more than one notification's id: " + ", ".join(
            f"{d.get('name')} {i}" for i, d in list(seen.items())[:6]))
    raise Fail(f"no recent notification has an id starting {ident!r}; give its whole id, as `watch` printed it")


def buttons_of(note: dict | None) -> list[dict]:
    return [b for b in (note or {}).get("form") or [] if isinstance(b, dict) and b.get("type") == "button"]


def respond(ctx: Ctx, a):
    nid, note = notification(ctx, a.id)
    what = (note or {}).get("name") or "the notification"
    if a.cancel:
        ctx.api.post("LLNotifications", "cancel", {"uuid": nid})
        print(f"dismissed {what}")
        return
    answer = "{}" if a.answer is None else a.answer
    try:
        resp = json.loads(answer)
    except ValueError:
        resp = None
    if not isinstance(resp, dict):   # a button: its name ("Keep", "OK_okcancelbuttons") or what it says
        btns = buttons_of(note)
        want = answer.strip().lower()
        hit = next((b["name"] for b in btns if str(b.get("name")).lower() == want), None) or \
            next((b["name"] for b in btns if button_base(str(b.get("name"))).lower() == want
                  or str(b.get("text") or "").strip().lower() == want), None)
        if btns and not hit:
            raise Fail(f"{what} has no button {answer!r}; its buttons: " + ", ".join(
                f"{b.get('name')}" + (f" ({b.get('text')!r})" if b.get("text") and b.get("text") != b.get("name")
                                      else "") for b in btns))
        resp = {hit or answer.strip(): True}
    ctx.api.post("LLNotifications", "respond", {"uuid": nid, "response": resp})
    pressed = [k for k, v in resp.items() if v is True]
    print(f"answered {what}" + (f": {', '.join(pressed)}" if pressed else ""))


def latest_dialog(ctx: Ctx, textbox: bool = False) -> dict:
    for e in reversed(ctx.api.recent(kinds=["notify"])):
        d = e["data"]
        if d.get("name") != "ScriptDialog":
            continue
        btns = [b.get("name") for b in d.get("form") or [] if isinstance(b, dict) and b.get("type") == "button"]
        if textbox == ("!!llTextBox!!" in btns):
            return d
    raise Fail("no script " + ("text box" if textbox else "dialog") + " seen")


def pick(ctx: Ctx, a):
    dlg = latest_dialog(ctx)
    buttons = [b["name"] for b in dlg.get("form") or [] if isinstance(b, dict) and b.get("type") == "button"]
    hit = next((b for b in buttons if a.text.lower() == b.strip().lower()), None) or \
        next((b for b in buttons if a.text.lower() in b.lower()), None)
    if not hit:
        raise Fail(f"no button like {a.text!r} in {buttons}")
    ctx.api.post("LLNotifications", "respond", {"uuid": dlg["id"], "response": {hit: True}})
    print(f"picked {hit.strip()!r}")


def textbox(ctx: Ctx, a):
    dlg = latest_dialog(ctx, textbox=True)
    ctx.api.post("LLNotifications", "respond", {"uuid": dlg["id"], "response": {"!!llTextBox!!": a.text}})
