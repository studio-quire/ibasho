"""Feeling the ground ahead before walking: a height profile along a bearing, so a walk stops short of a drop or
open water instead of going over a dock's edge. Walls don't count (walking into one just stops you).

Two ways to get it:
- the Ibasho viewer's `IbashoAgent probeGround`: terrain, water and a downward ray against the scene the viewer
  has loaded, for a list of points; nothing to wear;
- on viewers without it, an optional worn attachment (lsl/probe_attachment.lsl) that casts rays in-world and
  owner-says the profile; set probe.attachment_channel to its channel.
With neither, walks go unprobed.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .api import Api, ApiError

UNKNOWN = "?"


@dataclass
class Profile:
    feet: float
    points: list[tuple[float, float | None | str]]   # (distance, height vs feet | None: no ground | "?")
    source: str


def first_drop(profile: Profile, limit: float = 1.5) -> float | None:
    """Distance of the first point with no ground or ground more than `limit` m below my feet."""
    for d, dz in profile.points:
        if dz is None or (dz != UNKNOWN and dz < -limit):
            return d
    return None


def available(api: Api) -> str | None:
    if api.has_op("IbashoAgent", "probeGround"):
        return "viewer"
    if api.status().get("probe_channel"):
        return "attachment"
    return None


def ground_profile(api: Api, here, bearing_deg: float | None, dist: float, step: float = 1.0) -> Profile | None:
    """here: my region position [x, y, z] and heading-free; bearing in compass degrees (None: the way I face,
    attachment only)."""
    how = available(api)
    if how == "viewer":
        return _viewer(api, here, bearing_deg, dist, step)
    if how == "attachment":
        return _attachment(api, bearing_deg, dist, step, int(api.status()["probe_channel"]))
    return None


def _ground(p: dict) -> float | None:
    """One point of `probeGround`'s reply, `{terrain, water, hit}` in region z: what I'd stand on there. `hit` is
    the highest object surface (or null: the viewer's cast passes through the terrain mesh, which comes as
    `terrain`), so the surface is the higher of the two; under water it's a drop."""
    terrain, hit, water = p.get("terrain"), p.get("hit"), p.get("water")
    heights = [v for v in (terrain, hit) if isinstance(v, (int, float))]
    ground = max(heights) if heights else None
    if isinstance(water, (int, float)) and (ground is None or water > ground):
        return water - 1.5   # open water reads as well below its surface, so rivers count as drops
    return ground


def _viewer(api: Api, here, bearing_deg, dist, step) -> Profile | None:
    if bearing_deg is None:
        return None
    b = math.radians(bearing_deg)
    ux, uy = math.sin(b), math.cos(b)
    ds = [round(step * i, 2) for i in range(1, int(dist / step + 1e-6) + 1)]
    pts = [[here[0] + ux * d, here[1] + uy * d] for d in ds]
    try:
        # Cast from ~4 m over my feet (the viewer's default is 10): under most ceilings, which would otherwise read
        # as the ground, and still above stairs and ramps rising ahead (a cast that starts inside one misses it).
        r = api.op("IbashoAgent", "probeGround", {"points": pts, "top": here[2] + 3, "depth": 45}, timeout=10)
    except ApiError:
        return None
    feet = float(r.get("feet", here[2] - 0.9))
    out = []
    for d, p in zip(ds, r.get("points") or []):
        g = _ground(p or {})
        out.append((d, None if g is None else round(g - feet, 2)))
    return Profile(feet, out, "viewer")


def _attachment(api: Api, bearing_deg, dist, step, channel: int, timeout: float = 8) -> Profile | None:
    b = "-" if bearing_deg is None else f"{float(bearing_deg):g}"
    since = api.seq()
    end, resend = time.time() + timeout, 0.0
    while time.time() < end:
        if time.time() >= resend:   # a quick second chat line is sometimes dropped; say it again after 3 s
            api.post("LLChatBar", "sendChat", {"message": f"probe {b} {dist:g} {step:g}", "channel": channel,
                                               "type": "normal"})
            resend = time.time() + 3
        for e in api.events(since, wait=2, kinds=["chat"]):
            since = max(since, e["seq"])
            msg = (e.get("data") or {}).get("message") or ""
            if isinstance(msg, str) and msg.startswith("IBASHO|probe|"):
                return parse_attachment(msg)
    return None


def parse_attachment(msg: str) -> Profile:
    _, _, feet, prof = msg.split("|", 3)
    out = []
    for pair in filter(None, prof.split(",")):
        d, dz = pair.split(":")
        out.append((float(d), None if dz == "X" else UNKNOWN if dz == "?" else float(dz)))
    return Profile(float(feet), out, "attachment")
