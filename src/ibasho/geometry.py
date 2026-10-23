"""Plain geometry for seeing and moving: pinhole projection of a camera I placed or read back, the overhead
grid, and stepping round people. Region coordinates: x east, y north, z up, metres."""

from __future__ import annotations

import math
from dataclasses import dataclass

Vec = tuple[float, float, float]

COMPASS = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}


def sub(p, q) -> Vec:
    return (p[0] - q[0], p[1] - q[1], p[2] - q[2])


def dot(p, q) -> float:
    return p[0] * q[0] + p[1] * q[1] + p[2] * q[2]


def norm(p) -> Vec:
    m = math.sqrt(dot(p, p))
    return (p[0] / m, p[1] / m, p[2] / m)


def cross(p, q) -> Vec:
    return (p[1] * q[2] - p[2] * q[1], p[2] * q[0] - p[0] * q[2], p[0] * q[1] - p[1] * q[0])


def quat_rotate(q, v) -> Vec:
    """Rotate v by unit quaternion q = [x, y, z, w]."""
    x, y, z, w = q
    u = (x, y, z)
    t = cross(u, v)
    t = (2 * t[0], 2 * t[1], 2 * t[2])
    c = cross(u, t)
    return (v[0] + w * t[0] + c[0], v[1] + w * t[1] + c[1], v[2] + w * t[2] + c[2])


def compass(s: str | float | None) -> float | None:
    """A compass bearing in degrees (0 north, 90 east) from N/NE/... or a number."""
    if s is None:
        return None
    if isinstance(s, str) and s.upper() in COMPASS:
        return float(COMPASS[s.upper()])
    return float(s) % 360


def bearing(frm, to) -> float:
    return math.degrees(math.atan2(to[0] - frm[0], to[1] - frm[1])) % 360


@dataclass
class Camera:
    pos: Vec
    fwd: Vec
    right: Vec
    up: Vec
    fov: float          # vertical, radians
    width: int
    height: int

    @classmethod
    def look_at(cls, pos, focus, fov, width, height) -> "Camera":
        f = norm(sub(focus, pos))
        world_up = (0.0, 0.0, 1.0)
        u = norm(sub(world_up, tuple(dot(world_up, f) * c for c in f)))
        return cls(tuple(pos), f, cross(f, u), u, fov, width, height)

    @classmethod
    def from_quat(cls, pos, q, fov, width, height) -> "Camera":
        """The viewer's camera (LLAgent getCamera): it looks along its local +x, with +y left and +z up."""
        left = quat_rotate(q, (0.0, 1.0, 0.0))
        return cls(tuple(pos), quat_rotate(q, (1.0, 0.0, 0.0)), (-left[0], -left[1], -left[2]),
                   quat_rotate(q, (0.0, 0.0, 1.0)), fov, width, height)

    @property
    def k(self) -> float:
        return (self.height / 2) / math.tan(self.fov / 2)

    def project(self, p) -> tuple[float, float, float] | None:
        """Image pixel (x right, y down) and depth of a world point, or None behind the camera."""
        d = sub(p, self.pos)
        depth = dot(d, self.fwd)
        if depth < 0.3:
            return None
        return (self.width / 2 + dot(d, self.right) / depth * self.k,
                self.height / 2 - dot(d, self.up) / depth * self.k, depth)


@dataclass
class Label:
    x: float
    y: float
    ty: float           # where the text box goes after nudging
    text: str
    color: tuple
    dist: float


def place_labels(cam: Camera, items, max_dist: float = 30.0, limit: int = 30) -> list[Label]:
    """items: (pos, name, color). Nearest first, one label per name per 40-px cell, boxes nudged down past ones
    already placed. No occlusion test: something behind a wall gets a label too (its distance tells)."""
    seen, placed = set(), []
    for pos, name, color in items:
        if not pos:
            continue
        pr = cam.project(pos)
        if pr is None:
            continue
        x, y, _ = pr
        dist = math.sqrt(dot(sub(pos, cam.pos), sub(pos, cam.pos)))
        if dist > max_dist or not (0 <= x < cam.width and 0 <= y < cam.height):
            continue
        key = (name, round(x / 40), round(y / 40))
        if key in seen:
            continue
        seen.add(key)
        placed.append((dist, x, y, name, color))
    placed.sort(key=lambda t: t[0])
    out, boxes = [], []
    for dist, x, y, name, color in placed[:limit]:
        text = f"{name[:28]} {dist:.0f}m"
        w = 6 * len(text) + 4
        ty = y - 7
        for bx0, by0, bx1, by1 in boxes:
            if bx0 < x + 6 + w and x + 6 < bx1 and abs(ty - by0) < 13:
                ty = by1 + 1
        boxes.append((x + 5, ty, x + 6 + w, ty + 12))
        out.append(Label(x, y, ty, text, color, dist))
    return out


def overhead_pose(cx: float, cy: float, ground_z: float, radius: float, fov: float) -> tuple[Vec, Vec]:
    """A camera straight above (cx, cy) showing +-radius north-south, nudged a hair south so its up is north."""
    h = radius / math.tan(fov / 2)
    return (cx, cy - h * 0.002, ground_z + h), (cx, cy, ground_z)


def grid_step(radius: float) -> float:
    return 5 if radius <= 25 else 10 if radius <= 60 else 20 if radius <= 130 else 50


def detour(here, goal, people, clear: float = 1.3, off: float = 1.9) -> tuple[float, float] | None:
    """A side point that takes the straight line here -> goal round the nearest person standing within `clear` m
    of it, or None. people: (x, y, z). Anyone at the goal itself (I'm walking to them), right beside me, or on
    another floor doesn't count."""
    hx, hy = here[0], here[1]
    dx, dy = goal[0] - hx, goal[1] - hy
    length = math.hypot(dx, dy)
    if length < 1.5:
        return None
    ux, uy = dx / length, dy / length
    pts = [(p[0], p[1]) for p in people if abs(p[2] - here[2]) < 3]
    worst = None
    for px, py in pts:
        along = (px - hx) * ux + (py - hy) * uy
        side = (px - hx) * -uy + (py - hy) * ux          # + = left of the line
        if 1.0 < along < length - 1.5 and abs(side) < clear and (worst is None or along < worst[0]):
            worst = (along, side, px, py)
    if not worst:
        return None
    _, side, px, py = worst
    sign = -1 if side > 0 else 1                         # pass on the side they're further from
    for s in (sign, -sign):
        sx, sy = px - uy * off * s, py + ux * off * s
        if all(math.hypot(sx - qx, sy - qy) > 1.2 for qx, qy in pts):
            return sx, sy
    return None
