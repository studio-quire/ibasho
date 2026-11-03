"""Seeing: look, shot, view, cam, overhead (with a coordinate grid, or as a map), --labels on pictures, my own sky,
nearby objects and people. Pictures are the viewer's own rendering, saved as PNG under the data dir; commands print
the path."""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

from .. import geometry as geo
from ..api import ApiError
from .common import (Ctx, Fail, clock, fmt_event, fmt_obj, fmt_person, is_uuid, name_of, names, need_ibasho, out_json,
                     position, resolve, rid, rounded, short)
from .hear import Quiet, collapse_repeats, notable, unread_line

OBJ_COLOR, AV_COLOR, ME_COLOR = (230, 230, 120), (0, 255, 255), (255, 60, 60)
# `sky`: the viewer's World > Environment menu function, and its parameter for each (midday is "noon" there)
SKY_FUNCTION = "World.EnvSettings"
SKIES = {"sunrise": "sunrise", "midday": "noon", "noon": "noon", "sunset": "sunset", "midnight": "midnight",
         "region": "region"}
# `overhead --map`: the viewer's own land overlays, on for the picture only. ShowParcelOwners tints the ground by who
# owns it (World > Show > Land Owners); ShowPropertyLines draws the parcels' edges.
MAP_SETTINGS = {"ShowParcelOwners": True, "ShowPropertyLines": True}
MAP_LEGEND = "ground: green mine, teal my groups', red others', orange for sale, purple at auction"


def register(sub):
    p = sub.add_parser("look", help="where I am, who and what is near, what happened since the last look, a picture")
    p.add_argument("--noshot", action="store_true")
    p.add_argument("--ui", action="store_true", help="include the viewer's UI in the picture (chat and IM toasts "
                   "show there, and pictures aren't touched by `ibasho forget`)")
    p.add_argument("--all", action="store_true", help="every event in memory, not just since the last look")
    p.add_argument("--labels", type=float, nargs="?", const=30.0, default=0, metavar="M",
                   help="write names of things within M m (30) onto the picture")
    p.set_defaults(fn=look)
    p = sub.add_parser("shot", help="a picture of what the viewer shows, without its UI unless --ui")
    p.add_argument("--ui", action="store_true", help="include the viewer's UI in the picture (chat and IM toasts "
                   "show there, and pictures aren't touched by `ibasho forget`)")
    p.add_argument("--max", type=int, default=1280, help="largest width in pixels")
    p.add_argument("--labels", type=float, nargs="?", const=30.0, default=0, metavar="M")
    p.set_defaults(fn=shot)
    p = sub.add_parser("view", help="look a compass way from my eyes (N, NE..., or degrees: 0 N, 90 E), or at an "
                                    "object (as `cam`, from my side); no UI")
    p.add_argument("dir", help="a bearing (N, NE... or degrees), or an object (name, id or its first characters)")
    p.add_argument("--pitch", type=float, default=-5, help="degrees, + up")
    p.add_argument("--back", type=float, default=0.5, help="metres behind my eyes")
    p.add_argument("--wait", type=float, default=2.0, help="seconds for the scene to settle")
    p.add_argument("--max", type=int, default=1280)
    p.add_argument("--keep", action="store_true", help="leave the camera there")
    p.add_argument("--labels", type=float, nargs="?", const=30.0, default=0, metavar="M")
    p.set_defaults(fn=view)
    p = sub.add_parser("cam", help="orbit the camera on a point x,y,z or an object; `cam release` hands it back")
    p.add_argument("target")
    p.add_argument("--dist", type=float, default=4)
    p.add_argument("--az", type=float, default=180, help="0: camera north of the target, looking south")
    p.add_argument("--elev", type=float, default=30)
    p.add_argument("--wait", type=float, default=2.0)
    p.add_argument("--max", type=int, default=1280)
    p.add_argument("--keep", action="store_true")
    p.add_argument("--labels", type=float, nargs="?", const=30.0, default=0, metavar="M")
    p.set_defaults(fn=cam)
    p = sub.add_parser("overhead", help="a north-up picture from above with a region-coordinate grid")
    p.add_argument("--radius", type=float, default=40)
    p.add_argument("--center", help="x,y (default: me)")
    p.add_argument("--z", type=float, help="the ground plane for the grid (default: my feet)")
    p.add_argument("--grid", type=float, help="grid step in m (default from the radius)")
    p.add_argument("--wait", type=float, default=3.0)
    p.add_argument("--max", type=int, default=1280)
    p.add_argument("--labels", type=float, nargs="?", const=0.0, default=None, metavar="M",
                   help="name objects too (within M m of the centre; default the radius)")
    p.add_argument("--map", action="store_true",
                   help="map-style: the ground tinted by who owns it and parcel edges drawn (the viewer's own land "
                        "overlays), avatars named; the legend says the colours")
    p.set_defaults(fn=overhead)
    p = sub.add_parser("sky", help="my own sky: sunrise, midday, sunset, midnight, or back to the region's (it changes "
                                   "only what this viewer shows)")
    p.add_argument("which", choices=list(SKIES), metavar="{sunrise,midday,sunset,midnight,region}")
    p.set_defaults(fn=sky)
    p = sub.add_parser("objects", help="nearby objects: name, flags, hover text, owner, sale")
    p.add_argument("words", nargs="?", metavar="QUERY", help="only names containing this (as --query)")
    p.add_argument("--dist", type=float, default=20)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--query", help="only names containing this")
    p.add_argument("--perms", action="store_true", help="show permissions (yours, next owner's)")
    p.add_argument("--att", action="store_true", help="include attachments")
    p.add_argument("--timeout", type=float, default=4)
    p.set_defaults(fn=objects)
    p = sub.add_parser("object", help="everything about some objects, as JSON")
    p.add_argument("ids", nargs="+")
    p.add_argument("--full", action="store_true", help="also creator and creation time (selects briefly)")
    p.add_argument("--children", action="store_true")
    p.set_defaults(fn=object_)
    p = sub.add_parser("people", help="avatars in the region, nearest first")
    p.add_argument("--dist", type=float, default=96)
    p.set_defaults(fn=people)
    sub.add_parser("friends", help="friends, online first").set_defaults(fn=friends)
    p = sub.add_parser("profile", help="someone's profile: about, picks, groups (a name, username or id); "
                                       "without one, my own")
    p.add_argument("who", nargs="?", default="me",
                   help="as for `im`: someone near, known or recent, or anyone by username (first.last)")
    p.set_defaults(fn=profile)
    p = sub.add_parser("set-profile", help="my own profile: the about text, its picture, my display name")
    p.add_argument("--about", help="the about text, which people read to see who I am")
    p.add_argument("--about-file", help="the about text from a file (UTF-8)")
    p.add_argument("--image", help="the profile picture: a texture in my inventory (name or item id) or an asset id")
    p.add_argument("--display-name", help="the name over my head and in chat (\"\" goes back to my username); "
                                          "Second Life allows a change about once a week")
    p.set_defaults(fn=set_profile)
    sub.add_parser("parcel", help="the region and parcel I'm on").set_defaults(fn=parcel)


# ---------- pictures ----------

def take_shot(ctx: Ctx, noui: bool = False, max_w: int = 1280):
    """Returns (png path, window size). The viewer writes BMP whatever the extension; convert and downscale."""
    from PIL import Image
    shots = ctx.cfg.shots_dir
    shots.mkdir(parents=True, exist_ok=True)
    stem = shots / time.strftime("%Y%m%d-%H%M%S")
    raw = stem.with_suffix(".bmp")
    # A snapshot renders a frame of its own: seconds normally, much longer with the graphics card busy elsewhere
    r = ctx.api.cmd("LLViewerWindow", "saveSnapshot", {"filename": str(raw), "showui": not noui, "showhud": True,
                                                       "type": "COLOR"}, timeout=90)
    if not r.get("ok"):
        raise Fail(f"snapshot failed: {r}")
    im = Image.open(raw).convert("RGB")
    win = im.size
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    png = stem.with_suffix(".png")
    im.save(png)
    raw.unlink(missing_ok=True)
    ctx.state_file("lastshot.json").write_text(json.dumps({"win": list(win), "img": list(im.size)}))
    return png


def fov(ctx: Ctx) -> float:
    return float(ctx.api.cmd("LLViewerControl", "get", {"group": "Global", "key": "CameraAngle"}).get("value") or 1.047)


def label_items(ctx: Ctx, max_dist: float):
    items = []
    if ctx.api.ibasho:
        for o in ctx.api.op("IbashoObjects", "nearby", {"dist": max_dist + 10, "limit": 200}).get("objects", []):
            items.append((o.get("pos"), o.get("name") or "?", OBJ_COLOR))
    else:   # a stock viewer knows where objects are but not their names: label them by id
        for o in ctx.api.cmd("LLAgent", "getNearbyObjectsList", {"dist": max_dist + 10}).get("result", []):
            items.append((o.get("region_pos"), short(o.get("id")), OBJ_COLOR))
    for av in ctx.api.cmd("LLAgent", "getNearbyAvatarsList", {"dist": max_dist + 10}).get("result", []):
        items.append((av.get("region_pos"), "@" + str(av.get("name", "?")), AV_COLOR))
    return items


def draw_labels(png, labels):
    from PIL import Image, ImageDraw
    im = Image.open(png).convert("RGB")
    dr = ImageDraw.Draw(im)
    for lb in labels:
        w = 6 * len(lb.text) + 4
        dr.ellipse([lb.x - 3, lb.y - 3, lb.x + 3, lb.y + 3], outline=lb.color, width=2)
        dr.rectangle([lb.x + 5, lb.ty, lb.x + 6 + w, lb.ty + 12], fill=(0, 0, 0))
        dr.text((lb.x + 8, lb.ty + 1), lb.text, fill=lb.color)
    out = png.with_name(png.stem + "-labels.png")
    im.save(out)
    return out


def labelled(ctx: Ctx, png, cam: geo.Camera, max_dist: float):
    from PIL import Image
    w, h = Image.open(png).size
    cam.width, cam.height = w, h
    return draw_labels(png, geo.place_labels(cam, label_items(ctx, max_dist), max_dist))


def viewer_camera(ctx: Ctx) -> geo.Camera:
    c = ctx.api.cmd("LLAgent", "getCamera")
    return geo.Camera.from_quat(c["position"], c["rotation"], float(c.get("fov") or 1.047), 0, 0)


def place_camera(ctx: Ctx, pos, focus):
    ctx.api.post("LLAgent", "setCameraParams", {"camera_pos": list(pos), "focus_pos": list(focus),
                                                "camera_locked": True, "focus_locked": True})


def release_camera(ctx: Ctx):
    ctx.api.post("LLAgent", "removeCameraParams")
    ctx.api.post("LLAgent", "setFollowCamActive", {"active": False})


def shot(ctx: Ctx, a):
    cam = viewer_camera(ctx) if a.labels else None
    png = take_shot(ctx, not a.ui, a.max)
    ctx.picture(labelled(ctx, png, cam, a.labels) if cam else png)


def view(ctx: Ctx, a):
    p = position(ctx)["region"]
    eye = (p[0], p[1], p[2] + 0.7)
    deg = bearing_or_none(a.dir)
    if deg is None:   # an object: look at it as `cam` does, from where I stand
        try:
            oid = rid(ctx, a.dir)
        except Fail as e:
            raise Fail(f"{a.dir!r} is neither a bearing (N, NE... or degrees: 0 north, 90 east) nor an object here "
                       f"({e})") from None
        tx, ty, tz = object_pos(ctx, oid, a.dir)
        az = geo.bearing((tx, ty), p) if math.hypot(p[0] - tx, p[1] - ty) > 0.5 else 180.0
        print(f"(looking at {short(oid)} from my side, as `cam {short(oid)}` does)")
        _orbit_shot(ctx, a, (tx, ty, tz), 4.0, az, 20.0)
        return
    b = math.radians(deg)
    dx, dy = math.sin(b), math.cos(b)
    pitch = math.radians(a.pitch)
    back = a.back
    if back > 0 and ctx.api.ibasho:
        # Behind my eyes while I face the other way is in front of my face: the picture was all face (Second Life,
        # 2026-11-03). Then the camera goes just ahead of my head instead.
        try:
            heading = ctx.api.op("IbashoAgent", "move", {"secs": 0.05, "wait": True}, timeout=10).get("heading")
        except ApiError:
            heading = None
        if heading is not None and abs((float(heading) - deg + 180) % 360 - 180) > 90:
            back = -0.4
    pos = (eye[0] - back * dx, eye[1] - back * dy, eye[2])
    focus = (eye[0] + 20 * dx * math.cos(pitch), eye[1] + 20 * dy * math.cos(pitch), eye[2] + 20 * math.sin(pitch))
    _posed_shot(ctx, a, pos, focus)


def cam(ctx: Ctx, a):
    if a.target == "release":
        release_camera(ctx)
        return
    if "," in a.target:
        try:
            tx, ty, tz = (float(v) for v in a.target.split(","))
        except ValueError:
            raise Fail("cam takes x,y,z (three numbers), an object, or release") from None
    else:
        tx, ty, tz = object_pos(ctx, rid(ctx, a.target), a.target)
    _orbit_shot(ctx, a, (tx, ty, tz), a.dist, a.az, a.elev)


def object_pos(ctx: Ctx, oid: str, said: str) -> tuple[float, float, float]:
    if ctx.api.ibasho:
        o = (ctx.api.op("IbashoObjects", "get", {"ids": [oid], "fetch": False}).get("objects") or [None])[0]
        if not o:
            raise Fail(f"{said} isn't in view")
        return tuple(o["pos"])
    r = ctx.api.cmd("LLAgent", "getNearbyObjectsList", {"dist": 96})
    hit = next((o for o in r.get("result", []) if o.get("id") == oid), None)
    if not hit:
        raise Fail(f"{said} isn't in view")
    return tuple(hit["region_pos"])


def _orbit_shot(ctx: Ctx, a, target, dist: float, az_deg: float, elev_deg: float):
    """A picture of `target` from `dist` metres away, the camera at compass bearing `az_deg` from it (0: north of it,
    looking south), `elev_deg` above it."""
    tx, ty, tz = target
    az, el = math.radians(az_deg), math.radians(elev_deg)
    pos = (tx + dist * math.cos(el) * math.sin(az), ty + dist * math.cos(el) * math.cos(az), tz + dist * math.sin(el))
    _posed_shot(ctx, a, pos, (tx, ty, tz))


def compass_or_fail(s):
    try:
        return geo.compass(s)
    except ValueError:
        raise Fail(f"{s!r}: a bearing is N, NE, E... or degrees (0 north, 90 east)") from None


def bearing_or_none(s: str) -> float | None:
    """A bearing if `s` reads as one; None for anything else (an object's name or id). Digits alone, longer than a
    bearing and past 360, are an id's first characters."""
    try:
        deg = geo.compass(s)
    except ValueError:
        return None
    return None if len(s.strip()) >= 6 and abs(float(s)) > 360 else deg


def sky(ctx: Ctx, a):
    param = SKIES[a.which]
    # The Ibasho viewer's menu calls reply when asked; a stock viewer's never do.
    r = ctx.api.cmd("UI", "call", {"function": SKY_FUNCTION, "parameter": param}, wait=ctx.api.ibasho)
    if isinstance(r, dict) and r.get("error"):
        raise Fail(f"the viewer couldn't change the sky: {r['error']}")
    if param == "region":
        print("sky: the region's own again")
    else:
        print(f"sky: {a.which}, in my view only (others still see the region's) until I log out or "
              "`ibasho sky region`")


def _posed_shot(ctx: Ctx, a, pos, focus):
    place_camera(ctx, pos, focus)
    time.sleep(a.wait)
    try:
        png = take_shot(ctx, True, a.max)
        if a.labels:
            png = labelled(ctx, png, geo.Camera.look_at(pos, focus, fov(ctx), 0, 0), a.labels)
        ctx.picture(png)
    finally:
        if not a.keep:
            release_camera(ctx)


def overhead(ctx: Ctx, a):
    """Top-down, north up, with the region grid projected through the real camera (a pinhole, using the viewer's
    field of view) onto the plane z = --z (my feet by default): read positions off it for walk and tp. Things far
    above or below that plane sit a little off their grid square."""
    from PIL import Image, ImageDraw
    api = ctx.api
    me = position(ctx)["region"]
    cx, cy = (float(v) for v in a.center.split(",")) if a.center else (me[0], me[1])
    gz = a.z if a.z is not None else me[2] - 0.9
    f = fov(ctx)
    pos, focus = geo.overhead_pose(cx, cy, gz, a.radius, f)
    far = float(api.cmd("LLViewerControl", "get", {"group": "Global", "key": "RenderFarClip"}).get("value") or 128)
    need = (pos[2] - gz) * 1.15 + 20
    avs = api.cmd("LLAgent", "getNearbyAvatarsList", {"dist": 256}).get("result", [])
    if need > far:
        api.post("LLViewerControl", "set", {"group": "Global", "key": "RenderFarClip", "value": need})
    was = {}
    if a.map:
        for key, value in MAP_SETTINGS.items():
            was[key] = api.cmd("LLViewerControl", "get", {"group": "Global", "key": key}).get("value")
            if was[key] != value:
                api.post("LLViewerControl", "set", {"group": "Global", "key": key, "value": value})
    place_camera(ctx, pos, focus)
    time.sleep(a.wait)
    try:
        png = take_shot(ctx, True, a.max)
    finally:
        release_camera(ctx)
        if need > far:
            api.post("LLViewerControl", "set", {"group": "Global", "key": "RenderFarClip", "value": far})
        for key, value in was.items():
            if value != MAP_SETTINGS[key]:
                api.post("LLViewerControl", "set", {"group": "Global", "key": key, "value": bool(value)})
    im = Image.open(png).convert("RGB")
    cam = geo.Camera.look_at(pos, focus, f, im.width, im.height)
    dr = ImageDraw.Draw(im)

    def px(x, y, z=gz):
        p = cam.project((x, y, z))
        return (p[0], p[1]) if p else (-1, -1)

    half_w = a.radius * im.width / im.height
    step = a.grid or geo.grid_step(a.radius)
    x0, x1, y0, y1 = cx - half_w * 1.1, cx + half_w * 1.1, cy - a.radius * 1.1, cy + a.radius * 1.1
    gx = math.ceil(x0 / step) * step
    while gx <= x1:
        if 0 <= gx <= 256:
            dr.line([px(gx, y0), px(gx, y1)], fill=(255, 255, 255), width=1)
            tx, ty = px(gx, cy + a.radius * 0.97)
            dr.text((tx + 3, max(4, ty)), f"x{gx:g}", fill=(255, 255, 160))
        gx += step
    gy = math.ceil(y0 / step) * step
    while gy <= y1:
        if 0 <= gy <= 256:
            dr.line([px(x0, gy), px(x1, gy)], fill=(255, 255, 255), width=1)
            tx, ty = px(cx - half_w * 0.98, gy)
            dr.text((max(2, tx), ty + 2), f"y{gy:g}", fill=(255, 255, 160))
        gy += step
    if a.labels is not None:
        reach = a.labels or a.radius
        for o in api.op("IbashoObjects", "nearby", {"dist": reach + 10, "limit": 120}).get("objects", []) \
                if api.ibasho else []:
            if o.get("pos") and math.hypot(o["pos"][0] - cx, o["pos"][1] - cy) <= reach:
                x, y = px(*o["pos"])
                dr.rectangle([x - 2, y - 2, x + 2, y + 2], outline=OBJ_COLOR)
                dr.text((x + 4, y - 5), (o.get("name") or "?")[:20], fill=OBJ_COLOR)
    shown = []
    for av in avs:
        p = av.get("region_pos")
        if p:
            x, y = px(*p)
            if 0 <= x < im.width and 0 <= y < im.height:
                dr.ellipse([x - 4, y - 4, x + 4, y + 4], outline=AV_COLOR, width=2)
                dr.text((x + 6, y - 6), str(av.get("name", "?"))[:16], fill=AV_COLOR)
                shown.append(f"{av.get('name', '?')} at {p[0]:.0f},{p[1]:.0f}")
    x, y = px(*me)
    dr.ellipse([x - 5, y - 5, x + 5, y + 5], outline=ME_COLOR, width=3)
    dr.text((x + 7, y + 2), "me", fill=ME_COLOR)
    head = f"N up | grid {step:g} m | centre {cx:.0f},{cy:.0f} | plane z {gz:.1f}"
    if a.map:
        dr.rectangle([0, im.height - 36, 8 + 6 * len(MAP_LEGEND), im.height - 18], fill=(0, 0, 0))
        dr.text((6, im.height - 33), MAP_LEGEND, fill=(255, 255, 160))
    dr.rectangle([0, im.height - 18, 8 + 6 * len(head), im.height], fill=(0, 0, 0))
    dr.text((6, im.height - 15), head, fill=(255, 255, 160))
    out = png.with_name(png.stem + ("-map.png" if a.map else "-overhead.png"))
    im.save(out)
    png.unlink(missing_ok=True)
    if a.map:
        print("avatars: " + "; ".join(shown) if shown else "no avatars in view")
        print(MAP_LEGEND)
    ctx.picture(out)


# ---------- lists ----------

def nearby_objects(ctx: Ctx, dist: float, limit: int, query=None, att=False, timeout=4.0):
    args = {"dist": dist, "limit": limit, "fetch": True, "attachments": att, "timeout": timeout}
    if query:
        args["query"] = query
    return ctx.api.op("IbashoObjects", "nearby", args, timeout=timeout + 30)   # the viewer may be slow (api.slow_viewer)


def objects(ctx: Ctx, a):
    if not ctx.api.ibasho:
        r = ctx.api.cmd("LLAgent", "getNearbyObjectsList", {"dist": a.dist})
        for o in r.get("result", [])[:a.limit]:
            print(f"{short(o.get('id'))} at {rounded(o.get('region_pos'), 1)}")
        return
    if a.words and a.query and a.words != a.query:
        raise Fail("give the name to look for once (QUERY or --query)")
    r = nearby_objects(ctx, a.dist, a.limit, a.query or a.words, a.att, a.timeout)
    objs = r.get("objects", [])
    owners = names(ctx, {o.get("owner_id") for o in objs if o.get("owner_id")})
    for o in objs:
        print(fmt_obj(o, owners, a.perms))
    if r.get("timed_out"):
        print("(some names still loading)")


def object_(ctx: Ctx, a):
    need_ibasho(ctx, "object")
    r = ctx.api.op("IbashoObjects", "get", {"ids": [rid(ctx, i) for i in a.ids], "children": a.children,
                                           "full": a.full}, timeout=20)
    for o in r.get("objects", []):
        kids = o.pop("children", [])
        for k in ("local_id", "full"):
            o.pop(k, None)
        out_json(rounded(o))
        for c in kids:
            print(f"  child {c['id']} {c.get('name', '?')!r} pos={rounded(c['pos'], 2)} scale={rounded(c['scale'], 3)}")
    if r.get("missing"):
        print("not in view:", r["missing"])


def people(ctx: Ctx, a):
    if not ctx.api.ibasho:
        for p in ctx.api.cmd("LLAgent", "getNearbyAvatarsList", {"dist": a.dist}).get("result", []):
            print(f"{p.get('name')} {p.get('id')}")
        return
    for p in ctx.api.op("IbashoAgent", "people", {"dist": a.dist}).get("people", []):
        print(fmt_person(p))


def friends(ctx: Ctx, a):
    need_ibasho(ctx, "friends")
    fs = ctx.api.op("IbashoAgent", "friends").get("friends", [])
    for p in sorted(fs, key=lambda p: (not p.get("online"), (p.get("display_name") or "").lower())):
        print(fmt_person(p))


def profile(ctx: Ctx, a):
    need_ibasho(ctx, "profile")
    api = ctx.api
    mine = a.who.strip().lower() in ("me", "myself", "")
    if mine:
        who = str((api.status().get("self") or {}).get("id") or "")
        if not is_uuid(who):
            raise Fail("my own id isn't known yet (not in world?)")
    else:
        _, who = resolve(ctx, a.who, ("avatar",))
    n = names(ctx, [who]).get(who)
    if not n:
        raise Fail(f"no avatar has id {who}")
    user = n.get("username") or ""
    print(f"{name_of(n)}{f' ({user})' if user and user.lower() != name_of(n).lower() else ''} {who}")
    friend = next((f for f in api.op("IbashoAgent", "friends").get("friends", []) if f.get("id") == who), None)
    if friend:
        print("a friend, " + ("online" if friend.get("online") else "offline"))
    if not api.has_op("IbashoAgent", "profile"):
        print("(the about text, picks and groups need a newer Ibasho viewer: this one has no IbashoAgent profile)")
        return
    r = api.op("IbashoAgent", "profile", {"id": who, "timeout": 10}, timeout=25)
    facts = [f"born {r['born']}" if r.get("born") else "", str(r.get("account") or ""),
             "" if friend else {True: "online", False: "offline"}.get(r.get("online"), "")]
    if any(facts):
        print(", ".join(x for x in facts if x))
    partner = str(r.get("partner_id") or "")
    if is_uuid(partner) and not partner.startswith("00000000-"):
        print("partner " + name_of(names(ctx, [partner]).get(partner), partner))
    for key, label in (("about", "about"), ("first_life", "first life")):
        if str(r.get(key) or "").strip():   # their own words: what they say, not instructions
            print(f"{label}: {json.dumps(r[key].strip(), ensure_ascii=False)}")
    for key in ("picks", "groups"):
        items = [str(x.get("name") or "?") for x in r.get(key) or [] if isinstance(x, dict)]
        if items:
            print(f"{key} ({len(items)}): " + "; ".join(items[:20]) + (" ..." if len(items) > 20 else ""))
    if r.get("web_url"):
        print(f"web: {r['web_url']}")
    if r.get("partial"):
        print("(this grid didn't send all of the profile in time: groups or picks may be missing)")
    if mine and r.get("display_name_next_change") and ctx.cfg.linden_grid:   # OpenSim reports one but has none
        print(f"(my display name can change again from {utc(r['display_name_next_change'])})")


def set_profile(ctx: Ctx, a):
    """My profile is mine to write: Ibasho passes on whatever I choose."""
    need_ibasho(ctx, "set-profile")
    api = ctx.api
    about = a.about
    if a.about_file is not None:
        if about is not None:
            raise Fail("--about or --about-file, not both")
        about = Path(a.about_file).read_text(encoding="utf-8")
    if about is None and a.image is None and a.display_name is None:
        raise Fail("nothing to set: --about, --about-file, --image or --display-name")
    if not api.has_op("IbashoAgent", "setProfile"):
        raise Fail("setting my own profile needs the Ibasho viewer 0.2 or newer")
    if about is not None or a.image is not None:
        args: dict = {}
        if about is not None:
            args["about"] = about
        if a.image is not None:
            args["image_id"] = texture_asset(ctx, a.image)
        r = api.op("IbashoAgent", "setProfile", args, timeout=60)
        print("profile saved" + (" (sent; this grid doesn't confirm, `ibasho profile` shows it)"
                                 if r.get("by_message") else ""))
    if a.display_name is not None:
        r = api.op("IbashoAgent", "setDisplayName", {"name": a.display_name}, timeout=45)
        nxt = f"; the next change can be made from {utc(r['next_change'])}" if r.get("next_change") else ""
        print(f"display name now {r.get('display_name')!r}{nxt} (others see it within a few minutes)")


def utc(stamp) -> str:
    """Second Life's ISO time (2026-11-10T18:54:25.78Z) as 2026-11-10 18:54 UTC."""
    m = re.match(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d)", str(stamp))
    return f"{m[1]} {m[2]} UTC" if m else f"{stamp} UTC"


def texture_asset(ctx: Ctx, image: str) -> str:
    """A texture's asset id, from an item in my inventory (its id, or its name in Textures), else as given."""
    api = ctx.api
    if is_uuid(image):
        info = api.cmd("LLInventory", "getItemsInfo", {"item_ids": [image]}).get("items") or {}
        item = info.get(image.lower()) or info.get(image)
        if not item:
            return image   # not an item of mine: an asset id
        image = str(item.get("name") or image)
        if is_uuid(str(item.get("asset_id"))) and not str(item["asset_id"]).startswith("00000000-"):
            return str(item["asset_id"])
        raise Fail(f"{image!r}: the grid doesn't show me that texture's asset id (it needs full permissions)")
    found = api.cmd("LLInventory", "collectDescendantsIf",
                    {"folder_id": api.cmd("LLInventory", "getBasicFolderID", {"ft_name": "texture"})["id"],
                     "name": image}).get("items") or {}
    hits = sorted((it for it in found.values() if it.get("name") == image), key=lambda it: it.get("creation_date", 0))
    if not hits:
        raise Fail(f"no texture named {image!r} in my Textures folder (or give its id)")
    return texture_asset(ctx, str(hits[-1]["id"]))


def parcel(ctx: Ctx, a):
    need_ibasho(ctx, "parcel")
    r = ctx.api.op("IbashoAgent", "parcel")
    reg, p = r.get("region", {}), r.get("parcel", {})
    print(f"region {reg.get('name')!r} {reg.get('maturity')}{' sandbox' if reg.get('sandbox') else ''}")
    owner = names(ctx, [p.get("owner_id")]).get(p.get("owner_id")) if not p.get("group_owned") else None
    who = (owner or {}).get("display_name") or short(p.get("owner_id")) + (" (group)" if p.get("group_owned") else "")
    print(f"parcel {p.get('name')!r} {p.get('area')}m2 owner {who}")
    print(f"  allowed: {' '.join(p.get('allowed', []))}")
    if "my_voice" in r:
        allowed = "voice" in p.get("allowed", [])
        print(f"  voice chat: {'allowed' if allowed else 'off'} here; mine "
              + ("on (people shows who's in voice and speaking)" if r["my_voice"] else
                 "off (the person running me turns it on: docs/hearing.md, Voice chat)"))
    if p.get("desc"):
        print(f"  desc: {p['desc']!r}")


# ---------- look ----------

def look(ctx: Ctx, a):
    api = ctx.api
    st = api.status()
    seq_file = ctx.state_file("look.cursor")
    try:
        since = 0 if a.all else int(seq_file.read_text())
    except (OSError, ValueError):
        since = max(0, st["seq"] - 50)
    if since > st["seq"]:   # a cursor from before the log was reset
        since = max(0, st["seq"] - 50)
    pos = position(ctx)
    region = ""
    if api.ibasho:
        try:
            region = f"{api.op('IbashoAgent', 'parcel').get('region', {}).get('name')} "
        except ApiError:
            pass
    print(f"at {region}{rounded(pos['region'], 1)}")
    if api.ibasho:
        for p in api.op("IbashoAgent", "people", {"dist": 64}).get("people", [])[:12]:
            print("avatar", fmt_person(p))
        r = nearby_objects(ctx, 20, 12, timeout=3)
        objs = r.get("objects", [])
        owners = names(ctx, {o.get("owner_id") for o in objs if o.get("owner_id")})
        for o in objs:
            print("obj", fmt_obj(o, owners))
        if line := unread_line(ctx):
            print(line)
    else:
        for p in api.cmd("LLAgent", "getNearbyAvatarsList", {"dist": 64}).get("result", []):
            print("avatar", p.get("name"), p.get("id"))
    me = (st.get("self") or {}).get("id")
    evs = [e for e in api.events(since) if notable(e, me, api.ibasho)]
    for e, line in collapse_repeats(evs, Quiet(ctx))[-40:] if evs else []:
        print(f"{clock(e['t'])} {line or fmt_event(ctx, e)}")
    seq_file.write_text(str(api.seq()))
    if not a.noshot:
        cam = viewer_camera(ctx) if a.labels else None
        png = take_shot(ctx, not a.ui)
        ctx.picture(labelled(ctx, png, cam, a.labels) if cam else png, "shot")
