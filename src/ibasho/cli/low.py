"""Low level: any LEAP op (through the same policy as everything else), the viewer's API list, the script
editor's RPC."""

from __future__ import annotations

import json

from ..api import ApiError
from ..policy import ALLOWED, Widening
from .common import Ctx, Fail, is_uuid, out_json


def register(sub):
    for n in ("raw", "post"):
        p = sub.add_parser(n, help=("call" if n == "raw" else "send without waiting") +
                           " a LEAP op: PUMP OP [JSON args]; PUMP _command for LEAP's own ops")
        p.add_argument("pump")
        p.add_argument("op")
        p.add_argument("json", nargs="?", default="{}")
        p.add_argument("--timeout", type=float, default=15)
        p.set_defaults(fn=raw, wait=n == "raw")
    p = sub.add_parser("apis", help="the viewer's LEAP APIs, or one API's ops")
    p.add_argument("name", nargs="?")
    p.set_defaults(fn=apis)
    p = sub.add_parser("rpc", help="call the viewer's script-editor JSON-RPC: METHOD [JSON params]")
    p.add_argument("method")
    p.add_argument("json", nargs="?")
    p.set_defaults(fn=rpc)


def _args(s: str) -> dict:
    try:
        v = json.loads(s)
    except ValueError:
        raise Fail("args must be a JSON object") from None
    if not isinstance(v, dict):
        raise Fail("args must be a JSON object")
    return v


def raw(ctx: Ctx, a):
    try:
        out_json(ctx.api.cmd(a.pump, a.op, _args(a.json), timeout=a.timeout, wait=a.wait))
    except ApiError as e:
        if e.status != 504:
            raise
        # Many of the viewer's stock ops never answer, even when asked to (sendChat, startAutoPilot...); the Ibasho
        # APIs always do. `getAPI` doesn't say which are which.
        raise Fail(f"no answer in {a.timeout:g} s. Many of the viewer's stock ops are fire-and-forget: they never "
                   "reply, even when they ran (`ibasho post` sends without waiting for one). Or the viewer is slow "
                   "right now; --timeout waits longer") from None


def apis(ctx: Ctx, a):
    """What the viewer offers, marking what the daemon doesn't pass on (docs/api.md, "The safety rules")."""
    widen = Widening(ctx.cfg.policy_allow)
    added = widen.ops | {e[:2] for e in widen.exact}    # (pump, op) the person allowed, some or all of

    def passed(api, op=None):
        if op is None:
            return api in ALLOWED or any(p == api for p, _ in added)
        return op in ALLOWED.get(api, {}) or (api, op) in added or (api, "*") in added

    if a.name:
        r = ctx.api.cmd("_command", "getAPI", {"api": a.name})
        for o in r.get("ops") or []:
            desc = " ".join(str(o.get("desc", "")).split())
            mark = "" if passed(a.name, str(o.get("name"))) else " [not passed on]"
            print(f"{o.get('name')}{mark}: {desc[:300]}")
        return
    for k, v in sorted(ctx.api.cmd("_command", "getAPIs").items()):
        if isinstance(v, dict):
            k = "_command" if is_uuid(k) else k   # LEAP's own command pump, named afresh at every launch
            mark = "" if passed(k) else " [not passed on]"
            print(f"{k}{mark}: {' '.join(str(v.get('desc', '')).split())[:100]}")


def rpc(ctx: Ctx, a):
    try:
        params = json.loads(a.json) if a.json else None
    except ValueError:
        raise Fail("params must be JSON") from None
    out_json(ctx.api.rpc(a.method, params))
