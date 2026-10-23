"""What the commands share: the context (config, API), id and name resolution, and the short formats events,
people and objects print in. Outputs stay terse: a model reads all of it."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

from .. import config as config_mod
from ..api import Api, ApiError
from ..eventlog import notification_time


class Fail(Exception):
    """A command can't do what was asked; the message says why."""


def in_session() -> bool:
    """Running inside one of the agent's sessions: its keeper (or harness) sets IBASHO_SESSION."""
    return bool(os.environ.get("IBASHO_SESSION"))


class Ctx:
    def __init__(self, args):
        self.args = args
        self._cfg = None
        self._api = None
        self.pictures: list[Path] = []   # what this command showed, for callers that pass pictures on (MCP)

    @property
    def cfg(self) -> config_mod.Config:
        if self._cfg is None:
            path = getattr(self.args, "config", None)
            try:
                self._cfg = config_mod.load(Path(path) if path else None)
            except config_mod.ConfigError as e:
                raise Fail(str(e)) from None
        return self._cfg

    @property
    def api(self) -> Api:
        if self._api is None:
            self._api = Api(self.cfg)
        return self._api

    @property
    def life(self) -> Path:
        from ..keeper import settings as keeper_settings
        return keeper_settings.life_dir_of(self.cfg)

    def agent_path(self, path: str | Path, what: str = "file") -> Path:
        """A path the agent gave for ibasho to read. In the agent's sessions only its life folder and the pictures
        folder: the session can't read other files itself, and ibasho mustn't do it for it. Relative paths are
        from where the command runs (the life folder, in a keeper session)."""
        if re.match(r"^[A-Za-z]:[^\\/]", str(path)):   # C:\a\b unquoted in a POSIX shell arrives as C:ab
            raise Fail(f"{what} {path}: no such file (did a shell eat the backslashes? write the path with /, or "
                       "quote it)")
        p = Path(os.path.abspath(path))
        if in_session() and not config_mod.inside(p, self.life, self.cfg.shots_dir):
            raise Fail(f"{what} {path}: in the agent's sessions ibasho reads only the life folder ({self.life}) and "
                       f"the pictures ({self.cfg.shots_dir})")
        return p

    def state_file(self, name: str) -> Path:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        return self.cfg.state_dir / name

    def picture(self, path: Path, label: str = ""):
        """Print a picture's path (what the CLI shows) and keep it."""
        self.pictures.append(Path(path))
        print(f"{label} {path}" if label else path)


def out_json(v):
    print(json.dumps(v, ensure_ascii=False, separators=(",", ":")))


def short(i) -> str:
    return str(i or "")[:8]


def is_uuid(s: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s or ""))


def vec(s: str | None):
    return [float(x) for x in s.replace(" ", "").split(",")] if s else None


def rounded(v, n=3):
    if isinstance(v, float):
        return round(v, n)
    if isinstance(v, list):
        return [rounded(x, n) for x in v]
    if isinstance(v, dict):
        return {k: rounded(x, n) for k, x in v.items()}
    return v


def need_ibasho(ctx: Ctx, what: str):
    if not ctx.api.ibasho:
        raise Fail(f"{what} needs the Ibasho viewer's LEAP APIs (this is a stock viewer)")


def position(ctx: Ctx) -> dict:
    """{region: [x,y,z], global: [...]}; region coordinates are in my current region."""
    return ctx.api.cmd("LLAgent", "getPosition")


# ---------- names and ids ----------

def names(ctx: Ctx, ids) -> dict:
    ids = sorted({str(i) for i in ids if i})
    if not ids or not ctx.api.ibasho:
        return {}
    try:
        return ctx.api.cmd("IbashoAgent", "names", {"ids": ids, "timeout": 3}, timeout=8).get("names") or {}
    except ApiError:
        return {}


def name_of(n: dict | None, fallback: str = "?") -> str:
    n = n or {}
    return n.get("display_name") or n.get("username") or fallback


def recent_ids(ctx: Ctx, kinds) -> list[tuple[str, str, list[str]]]:
    """(kind, id, names) for the avatars and objects in recent events (IMs, payments, chat, object IMs), so an id
    or name a command printed earlier resolves even once its owner has left. Avatars get their current names."""
    out: list[tuple[str, str, list[str]]] = []
    for e in ctx.api.recent(kinds=["im", "money", "chat", "object_im"]):
        d = e.get("data") or {}
        k = e["kind"]
        if k == "im" and d.get("dir") == "in" and d.get("session_type") == "p2p":
            out.append(("avatar", d.get("from_id"), [d.get("from")]))
        elif k == "money":
            for side in ("source", "dest"):
                if not d.get(f"{side}_group"):
                    out.append(("avatar", d.get(f"{side}_id"), []))   # or an object: names() tells which
        elif k == "chat" and d.get("source") in (1, 2):
            out.append(("avatar" if d["source"] == 1 else "object", d.get("from_id"), [d.get("from")]))
        elif k == "object_im":
            out.append(("object", d.get("object_id"), [d.get("from")]))
            out.append(("avatar", d.get("owner_id"), []))
    me = str((ctx.api.status().get("self") or {}).get("id") or "")
    out = [(k, str(i), ns) for k, i, ns in out
           if k in kinds and is_uuid(str(i or "")) and str(i) != me and not str(i).startswith("00000000-")]
    avatars = {i for k, i, _ in out if k == "avatar"}
    known = names(ctx, avatars) if avatars else {}
    return [(k, i, ns + [known[i].get("display_name"), known[i].get("username")] if i in known else ns)
            for k, i, ns in out if k != "avatar" or not ctx.api.ibasho or i in known]


def resolve(ctx: Ctx, target: str, kinds=("avatar", "session", "group", "object"),
            lookup: str = "any") -> tuple[str | None, str]:
    """A full UUID passes through as (None, id). Otherwise match an id prefix or a name among open IM sessions,
    groups, avatars (nearby, friends, and anyone in recent IMs, payments and chat) and objects (nearby, and those
    in recent events); returns (kind, id), or fails with the candidates. A session may be written as the CLI prints
    it, `s:` and all. Failing all that, a username is looked up grid-wide (`lookup`: "any" form; "explicit", only
    one written out in full, for paying and giving, so an object's name out of view doesn't reach a stranger;
    "none")."""
    target = target.strip()
    if target[:2].lower() == "s:" and "session" in kinds:
        target = target[2:]
    if is_uuid(target):
        return None, target.lower()
    api = ctx.api
    want = target.lower()
    hexish = all(c in "0123456789abcdef-" for c in want)
    if kinds == ("object",) and len(want) >= 6 and hexish and api.ibasho:
        ids = api.op("IbashoObjects", "find", {"prefix": want}).get("ids", [])
        if len(ids) == 1:
            return "object", ids[0]
    cands: dict[str, tuple[str, list[str]]] = {}

    def add(kind, i, *ns):
        if i and str(i) not in cands:
            cands[str(i)] = (kind, [n.lower() for n in ns if n])

    p2p_other = {}
    if "session" in kinds and api.ibasho:
        heard: dict[str, set] = {}   # IM events carry usernames; sessions are named by display name
        for e in api.recent(kinds=["im"]):
            d = e["data"]
            if d.get("dir") == "in" and d.get("from"):
                heard.setdefault(d.get("session_id"), set()).add(d["from"])
        for s in api.op("IbashoIM", "sessions").get("sessions", []):
            extra = heard.get(s["session_id"], ()) if s.get("type") == "p2p" else ()
            add("session", s["session_id"], s.get("name"), *extra)
            if s.get("type") == "p2p":
                p2p_other[s["session_id"]] = s.get("other_id")
                if "avatar" in kinds:   # `ims` shows the other person's short id
                    add("avatar", s.get("other_id"), s.get("name"), *extra)
    if "group" in kinds:
        for g in api.cmd("LLAgent", "getGroups").get("groups", []):
            add("group", g["id"], g.get("name"))
    if "avatar" in kinds:
        if api.ibasho:
            people = api.op("IbashoAgent", "people", {"dist": 4096}).get("people", []) + \
                api.op("IbashoAgent", "friends").get("friends", [])
            for p in people:
                add("avatar", p["id"], p.get("display_name"), p.get("username"))
        else:
            for p in api.cmd("LLAgent", "getNearbyAvatarsList", {"dist": 256}).get("result", []):
                add("avatar", p.get("id"), p.get("name"))
    if "object" in kinds:
        if api.ibasho:
            if hexish:
                for i in api.op("IbashoObjects", "find", {"prefix": want}).get("ids", []):
                    add("object", i)
            for o in api.op("IbashoObjects", "nearby", {"dist": 256, "limit": 2000, "fetch": False}).get("objects", []):
                add("object", o["id"], o.get("name"))
        else:
            r = api.cmd("LLAgent", "getNearbyObjectsList", {"dist": 96})
            for o in (r.get("result", []) if isinstance(r, dict) else r):
                add("object", o.get("id"))

    def matches():
        hits = [(k, i) for i, (k, ns) in cands.items() if i.startswith(want) or want in ns]
        if not hits:
            hits = [(k, i) for i, (k, ns) in cands.items() if any(n.startswith(want) for n in ns)]
        ids = {i for _, i in hits}
        return [(k, i) for k, i in hits if not (k == "session" and p2p_other.get(i) in ids)]  # same person twice

    hits = matches()
    if not hits and ({"avatar", "object"} & set(kinds)):   # people and things from earlier: payers, past IMs
        for k, i, ns in recent_ids(ctx, kinds):
            add(k, i, *ns)
        hits = matches()
    if not hits and "avatar" in kinds and lookup != "none" and looks_like_username(target):
        # anyone at all, by the username people pass on to each other
        if lookup == "explicit" and not explicit_username(target):
            raise Fail(f"no {'/'.join(kinds)} near, known or recent matches {target!r}; to reach someone else, "
                       "write their username in full (first.last, or name Resident)")
        if i := lookup_username(ctx, target):
            return "avatar", i
        raise Fail(f"no one near, known or recent matches {target!r}, and no avatar has the username "
                   f"{username_key(target)!r}")
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise Fail(f"no {'/'.join(kinds)} matches {target!r}")
    raise Fail(f"{target!r} is ambiguous: " + ", ".join(f"{k} {i}" for k, i in hits[:8]))


USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[. ][A-Za-z0-9]+)?")


def username_key(name: str) -> str:
    """A username as Second Life writes it: "first.last", or just "first" for a Resident. Legacy names ("First
    Last", "First Resident") come out the same."""
    n = re.sub(r"\s+", ".", name.strip().lower())
    return n[:-len(".resident")] if n.endswith(".resident") else n


def explicit_username(s: str) -> bool:
    """Written as a username can only be one: "first.last", "First Last", "name Resident"."""
    return bool(USERNAME.fullmatch(s.strip())) and bool(re.search(r"[. ]", s.strip()))


def looks_like_username(s: str) -> bool:
    s = s.strip()
    if not USERNAME.fullmatch(s):
        return False
    return not (re.fullmatch(r"[0-9a-f]+", s.lower()) and any(c.isdigit() for c in s))   # an id's first characters


def lookup_username(ctx: Ctx, name: str) -> str | None:
    """The avatar id for a username, anywhere on the grid; None if no one has it. Usernames never change, so
    what's found is kept (state/usernames.json). With the viewer's avatar search (`IbashoAgent findAvatars`) it's
    one request; without, a script in a temporary prim asks the region (llRequestUserKey), which needs somewhere
    I can build."""
    key = username_key(name)
    known_f = ctx.state_file("usernames.json")
    try:
        known = json.loads(known_f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    if isinstance(known, dict) and is_uuid(str(known.get(key) or "")):
        return known[key]
    api = ctx.api
    if not api.ibasho:
        raise Fail(f"looking up {name!r} grid-wide needs the Ibasho viewer; their avatar id works")
    if api.has_op("IbashoAgent", "findAvatars"):
        r = api.op("IbashoAgent", "findAvatars", {"name": name.strip(), "timeout": 10}, timeout=25)
        found = next((a.get("id") for a in r.get("avatars") or []
                      if username_key(str(a.get("username") or "")) == key), None)
    else:
        found = script_lookup(ctx, name)
    if found and is_uuid(str(found)) and not str(found).startswith("00000000-"):
        known = known if isinstance(known, dict) else {}
        known[key] = str(found).lower()
        known_f.write_text(json.dumps(known), encoding="utf-8")
        print(f"(the username {key} is {known[key]})")   # so a name meant for someone near shows going elsewhere
        return known[key]
    return None


def script_lookup(ctx: Ctx, name: str) -> str | None:
    """llRequestUserKey in a throwaway script, in a temporary phantom prim over my head (deleted after). Second
    Life takes a username or a legacy name in any case; other grids may want "First Last" spelt as registered,
    so the script tries a few ways of writing it."""
    import random
    import string
    from . import make
    api = ctx.api
    key = username_key(name)
    spaced = " ".join(re.split(r"[.\s]+", name.strip()))
    forms = list(dict.fromkeys([spaced, spaced.title(), name.strip(), key]))
    here = position(ctx)["region"]
    try:
        host = api.op("IbashoBuild", "create", {"shape": "box", "scale": [0.2, 0.2, 0.2], "temp": True,
                                                "phantom": True, "name": "ibasho name lookup",
                                                "pos": [here[0], here[1], here[2] + 1.5]}, timeout=20)["id"]
    except ApiError as e:
        raise Fail(f"can't look up {key!r} from here: without the viewer's avatar search a script in a temporary "
                   f"prim does it, and building isn't allowed here ({e}). Somewhere I can build (a sandbox), "
                   "or their avatar id, works") from None
    make.unselect_on_sim(ctx, host)
    try:
        tag = f"{random.randrange(16 ** 6):06x}"
        src = string.Template(make.lsl("name_lookup.lsl")).substitute(
            names=", ".join(make.lsl_str(f) for f in forms), tag=tag)
        since = api.seq()
        r = make.save_script(ctx, host, f"name lookup {tag}", src, check_contents=False, quiet=True)
        if r.get("compiled") is False:
            raise Fail("the name lookup script didn't compile on this grid")
        got = make.wait_owner_say(ctx, since, f"IBASHO|user|{tag}|", 30 + 20 * len(forms))
    finally:
        try:
            api.op("IbashoBuild", "delete", {"ids": [host]})
        except ApiError:
            pass
    if not got:
        raise Fail(f"no answer from the name lookup for {key!r} (are scripts allowed here?)")
    answer = got[0].rsplit("|", 1)[-1].strip()
    if answer == "TIMEOUT":
        raise Fail(f"the region didn't answer the name lookup for {key!r}; try again in a moment")
    return answer if is_uuid(answer) else None


def rid(ctx: Ctx, target: str, kinds=("object",)) -> str:
    return resolve(ctx, target, kinds)[1]


# ---------- formats ----------

def fmt_person(p: dict) -> str:
    name = p.get("display_name") or "?"
    user = f" ({p['username']})" if p.get("username") and p["username"].lower() != name.lower() else ""
    where = f" {p['dist']:.0f}m" if "dist" in p else (" online" if p.get("online") else " offline")
    if isinstance(p.get("pos"), list) and len(p["pos"]) >= 3:
        where += " at " + ",".join(f"{v:.0f}" for v in p["pos"][:3])
    return f"{name}{user}{where}{' friend' if p.get('friend') else ''} {p['id']}"


def fmt_perms(p: dict | None) -> str:
    if not p:
        return ""
    return f"owner:{p.get('owner', '')} next:{p.get('next', '')}"   # the owner's perms, and the next owner's


def fmt_obj(o: dict, owners: dict | None = None, perms: bool = False) -> str:
    bits = [f"{o.get('dist', 0):5.1f}m", o.get("name") or "?"]
    tags = [f for f in o.get("flags", []) if f in ("touch", "pay", "mine", "scripted")]
    if o.get("click_action"):
        tags.append(o["click_action"])
    if tags:
        bits.append("[" + ",".join(tags) + "]")
    if o.get("text"):
        bits.append(json.dumps(o["text"].strip(), ensure_ascii=False))
    if o.get("sale"):
        bits.append(f"for sale L${o['sale']['price']} ({o['sale']['type']})")
    if o.get("prims", 1) > 1:
        bits.append(f"{o['prims']}p")
    if o.get("seated"):
        bits.append(f"{len(o['seated'])} seated")
    if o.get("attached_to"):
        bits.append(f"worn by {short(o['attached_to'])} on {o.get('attach_point')}")
    if owners is not None and o.get("owner_id") and "mine" not in o.get("flags", []):
        bits.append("owner " + name_of(owners.get(o["owner_id"]), short(o["owner_id"])))
    if perms and o.get("perms"):
        bits.append(fmt_perms(o["perms"]))
    bits.append(short(o["id"]))
    return " ".join(bits)


AGENT_LINK = re.compile(r"(?:secondlife|x-grid-location-info)://\S*?/app/agent/([0-9a-f-]{36})/\w+")
INVENTORY_LINK = re.compile(r"(?:secondlife|x-grid-location-info)://\S*?/app/inventory/[0-9a-f-]{36}/select\?name=(\S+)")


def humanize(ctx: Ctx, text: str) -> str:
    """Notifications link people and items as SLURLs; show names instead."""
    ids = set(AGENT_LINK.findall(text))
    ns = names(ctx, ids) if ids else {}
    text = AGENT_LINK.sub(lambda m: name_of(ns.get(m.group(1)), m.group(1)), text)
    text = INVENTORY_LINK.sub(lambda m: urllib.parse.unquote(m.group(1)), text)
    return re.sub(r"</?nolink>", "", text)


CHAT_TYPES = {0: " whispers", 2: " shouts", 8: " (to you)"}


def fmt_event(ctx: Ctx | None, e: dict) -> str:
    k, d = e["kind"], e.get("data") or {}
    if k == "chat":
        who = d.get("from") or "?"
        if d.get("source") == 2:
            who += " (object)"
        return f"chat {who}{CHAT_TYPES.get(d.get('chat_type'), '')}: {d.get('message')}"
    if k == "im":
        st = d.get("session_type")
        where = st + (f" {d.get('session_name')!r}" if st != "p2p" else "")
        who = "me" if d.get("dir") == "out" else f"{d.get('from')} ({short(d.get('from_id'))})"
        return f"IM[{where} s:{short(d.get('session_id'))}]{' [offline]' if d.get('offline') else ''} {who}: {d.get('message')}"
    if k == "im_transcript":
        return f"IM <{d.get('conversation')}> {d.get('line')}"
    if k == "object_im":
        return f"objIM {d.get('from')!r} ({short(d.get('object_id'))}, owner {short(d.get('owner_id'))}): {d.get('message')}"
    if k == "money":
        if "amount" not in d:
            return f"L$ balance {d.get('balance')} {d.get('desc', '')}".rstrip()
        what = f"{d.get('item') or d.get('desc') or ''!r} balance {d.get('balance')}"
        side = {"in": "source", "out": "dest"}.get(d.get("dir"))
        if not side:
            return f"L$ ? {d.get('amount')} {short(d.get('source_id'))}->{short(d.get('dest_id'))} {what}"
        other = str(d.get(f"{side}_id") or "")
        if d.get(f"{side}_group"):
            who = f"a group ({short(other)})"
        else:   # an avatar has a name; an object paid or paid to doesn't
            n = names(ctx, [other]).get(other) if ctx else None
            who = f"{name_of(n)} ({short(other)})" if n else short(other)
        return f"L$ {d['dir']} {d.get('amount')} {'from' if side == 'source' else 'to'} {who} {what}"
    if k == "object_created":
        err = f" ({d['modify_error']})" if d.get("modify_error") else ""
        return f"created {d.get('id')} at {[round(v, 2) for v in d.get('pos', [])]}{err}"
    if k == "refused":   # one of the viewer's own limits (daily messages, spending, land, L$)
        reason = re.sub(r"^Ibasho:\s*", "", str(d.get("reason") or ""))
        return f"refused ({d.get('what')}): {reason[:200]}"
    if k == "move_done":
        return f"move ended ({d.get('reason')}) after {d.get('secs', 0):.1f}s, {d.get('moved', 0):.1f} m"
    if k == "notify":
        form = d.get("form") or []
        btns = [f"{b.get('name')}={b.get('text')!r}" for b in form if isinstance(b, dict) and b.get("type") == "button"]
        fields = [b.get("name") for b in form if isinstance(b, dict) and b.get("type") not in ("button", None)]
        msg = humanize(ctx, d.get("message") or "") if ctx else d.get("message") or ""
        when = ""
        if d.get("replayed"):   # the viewer shows saved notifications again at login
            born = notification_time(d.get("time"))
            at = time.strftime("%Y-%m-%d %H:%M", time.localtime(born)) if born else "earlier"
            when = f" (from before this login, {at})"
        return (f"notify {d.get('name')}{when} id={d.get('id')} buttons=[{', '.join(btns)}]"
                f"{f' fields={fields}' if fields else ''}: {msg!r}")
    if k == "teleport":
        return "teleport " + ("ok" if d.get("success", True) else f"failed: {d.get('reason') or d}")
    if k == "daemon":
        return f"IBASHO: {d.get('msg')}"
    if k == "startup":
        return f"viewer at its login screen ({d.get('state')}){' after a failed login' if d.get('after_failure') else ''}"
    if k == "editor":
        m, p = d.get("method"), d.get("params") or {}
        if m in ("object.publish", "object.update"):
            o = p.get("object", p)
            return f"editor {m} {o.get('object_name', '')} {o.get('object_id', '')}"
        if m in ("runtime.error", "runtime.debug") and isinstance(p, dict):
            what = "script error" if m == "runtime.error" or p.get("channel") == "debug" else f"script {p.get('channel')}"
            item = (p.get("item") or {}).get("name") or ""
            where = f"{p.get('object_name', '')!r}{' ' + repr(item) if item else ''} ({short(p.get('object_id'))})"
            return f"{what} in {where}: {str(p.get('message') or '').strip()[:300]}"
        return f"editor {m} {json.dumps(p, ensure_ascii=False)[:300]}"
    if isinstance(d, dict) and d.get("text"):
        return f"{k}: {d['text']}"
    return f"{k} {json.dumps(d, ensure_ascii=False)[:300]}"


def clock(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t))


def mentions(status: dict) -> re.Pattern | None:
    words = list(status.get("name_words") or [])
    me = status.get("self") or {}
    for k in ("display_name", "username"):
        if me.get(k):
            words += [w for w in re.split(r"[\s._]+", me[k]) if w.lower() != "resident"]
    words = [w for w in dict.fromkeys(w.lower() for w in words) if len(w) >= 3]
    if not words:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")", re.I)


def stdout_utf8():
    for f in (sys.stdout, sys.stderr):  # world text is full of symbols the Windows console codepage lacks
        if f is not None and hasattr(f, "reconfigure"):
            f.reconfigure(encoding="utf-8", errors="replace")
