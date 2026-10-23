"""Making things: prims (create, modify, link), scripts (save with the contents check, read), what's really in
an object, reading notecards, inventory, rezzing and taking, giving with next-owner perms."""

from __future__ import annotations

import random
import re
import string
import time
from importlib import resources
from pathlib import Path

from ..api import ApiError
from .common import Ctx, Fail, is_uuid, need_ibasho, out_json, position, resolve, rid, rounded, short, vec

TYPES = {0: "texture", 1: "sound", 3: "landmark", 5: "clothing", 6: "object", 7: "notecard", 10: "script",
         13: "bodypart", 20: "animation", 21: "gesture", 49: "settings", 56: "material"}


def register(sub):
    for n in ("prim-create", "prim-mod"):
        p = sub.add_parser(n, help=("make a prim" if n == "prim-create" else "change prims") +
                           "; vectors as x,y,z; --set takes a JSON object of any modify keys")
        if n == "prim-mod":
            p.add_argument("ids", nargs="+")
        p.add_argument("--shape", default="box" if n == "prim-create" else None)
        for k in ("pos", "local-pos", "scale", "color", "texture", "name", "desc", "click-action", "set"):
            p.add_argument("--" + k, dest=k.replace("-", "_"))
        p.add_argument("--rot", dest="rot_euler", help="euler degrees x,y,z")
        p.add_argument("--alpha", type=float)
        p.add_argument("--glow", type=float)
        p.add_argument("--fullbright", type=int, choices=[0, 1])
        p.add_argument("--face", type=int)
        p.add_argument("--next-perms", help="what the next owner may do: any of M (modify) C (copy) T (transfer)")
        p.set_defaults(fn=prim_create if n == "prim-create" else prim_mod)
    p = sub.add_parser("link", help="link prims; the first is the root")
    p.add_argument("ids", nargs="+")
    p.set_defaults(fn=link)
    p = sub.add_parser("unlink", help="unlink prims (a root unlinks its whole linkset)")
    p.add_argument("ids", nargs="+")
    p.set_defaults(fn=unlink)
    p = sub.add_parser("dup", help="duplicate an object")
    p.add_argument("id")
    p.add_argument("--offset", help="x,y,z (default 0,0,0.5)")
    p.set_defaults(fn=dup)
    p = sub.add_parser("delete", help="delete objects (to Trash)")
    p.add_argument("ids", nargs="+")
    p.set_defaults(fn=delete)
    p = sub.add_parser("take", help="take objects into inventory")
    p.add_argument("ids", nargs="+")
    p.add_argument("--copy", action="store_true")
    p.set_defaults(fn=take)
    p = sub.add_parser("rez", help="rez an inventory object (id, or name in Objects; newest copy wins)")
    p.add_argument("item")
    p.add_argument("--pos", help="x,y,z")
    p.set_defaults(fn=rez)
    p = sub.add_parser("inv", help="inventory names containing WORDS, with copy counts and newest id")
    p.add_argument("words", nargs="*")
    p.add_argument("--folder", default="object", help="object, notecard, texture, lstndfnd, inbox, root_inv...")
    p.set_defaults(fn=inv)
    p = sub.add_parser("obj", help="an object's contents, from the script editor's listing")
    p.add_argument("id")
    p.set_defaults(fn=obj)
    p = sub.add_parser("contents", help="an object's real contents, straight from the region")
    p.add_argument("id")
    p.add_argument("--remove", nargs="+", default=[], metavar="NAME", help="remove these items first")
    p.set_defaults(fn=contents)
    p = sub.add_parser("script-save", help="write FILE into script NAME in object ID (made if missing); compiles")
    p.add_argument("id")
    p.add_argument("name")
    p.add_argument("file")
    p.add_argument("--stopped", action="store_true", help="save without running it")
    p.add_argument("--no-check", action="store_true", help="skip asking the region for a same-named item")
    p.add_argument("--vm", default="mono", choices=["mono", "luau", "lsl2"])
    p.set_defaults(fn=script_save)
    p = sub.add_parser("script-get", help="print a script's text")
    p.add_argument("id")
    p.add_argument("name")
    p.set_defaults(fn=script_get)
    p = sub.add_parser("notecard", help="read one of your notecards (by name or id), anywhere; without a name, list them")
    p.add_argument("name", nargs="?", default="", help="its name (or part of it), or its id")
    p.add_argument("--host", help="only for viewers that can't read notecards themselves: one of your objects within "
                                  "reach to run a reader script in")
    p.add_argument("--list", action="store_true")
    p.set_defaults(fn=notecard)
    p = sub.add_parser("give", help="give an inventory item (id or name), or one of your objects with --perms")
    p.add_argument("who")
    p.add_argument("item", help="inventory item id or name, or an in-world object of yours (with --perms)")
    p.add_argument("--perms", help="next-owner perms for an object you made, e.g. MCT or MC")
    p.add_argument("--folder", action="store_true", help="ITEM is a folder id")
    p.set_defaults(fn=give)


# ---------- prims ----------

def modify_args(a) -> dict:
    import json
    m = json.loads(a.set) if a.set else {}
    for key in ("pos", "local_pos", "scale", "rot_euler", "color"):
        if getattr(a, key, None):
            m[key] = vec(getattr(a, key))
    for key in ("shape", "texture", "name", "desc", "click_action", "alpha", "glow", "face"):
        if getattr(a, key, None) is not None:
            m[key] = getattr(a, key)
    if a.fullbright is not None:
        m["fullbright"] = bool(a.fullbright)
    if a.next_perms is not None:
        m["next_perms"] = perms_arg(a.next_perms)
    return m


def perms_arg(s: str) -> str:
    s = s.upper()
    if set(s) - set("MCT"):
        raise Fail("perms: letters M (modify), C (copy), T (transfer)")
    return s


def unselect_on_sim(ctx: Ctx, oid: str):
    """Objects made with the create-selected flag stay selected by us on the region, and a selected physical
    object is frozen; a select/deselect clears it (what a right-click in a normal viewer does). The Ibasho viewer
    deselects its new objects itself since its tenth patch; builds from before it have no probeGround either,
    which came later in the series, so that's the tell."""
    api = ctx.api
    if oid and api.ibasho and not api.has_op("IbashoAgent", "probeGround"):
        api.cmd("IbashoObjects", "select", {"ids": [oid], "add": True})
        time.sleep(0.3)
        api.cmd("IbashoObjects", "deselect", {"ids": [oid]})


def made(ctx, r):
    unselect_on_sim(ctx, r.get("id"))
    print(r.get("id"))


def prim_create(ctx: Ctx, a):
    need_ibasho(ctx, "prim-create")
    made(ctx, ctx.api.op("IbashoBuild", "create", modify_args(a), timeout=20))


def prim_mod(ctx: Ctx, a):
    need_ibasho(ctx, "prim-mod")
    m = modify_args(a)
    for i in a.ids:
        ctx.api.op("IbashoBuild", "modify", dict(m, id=rid(ctx, i)))


def link(ctx: Ctx, a):
    ctx.api.op("IbashoBuild", "link", {"ids": [rid(ctx, i) for i in a.ids]})


def unlink(ctx: Ctx, a):
    ctx.api.op("IbashoBuild", "unlink", {"ids": [rid(ctx, i) for i in a.ids]})


def dup(ctx: Ctx, a):
    args = {"id": rid(ctx, a.id)}
    if a.offset:
        args["offset"] = vec(a.offset)
    made(ctx, ctx.api.op("IbashoBuild", "duplicate", args, timeout=20))


def delete(ctx: Ctx, a):
    print(ctx.api.op("IbashoBuild", "delete", {"ids": [rid(ctx, i) for i in a.ids]}).get("count"), "deleted")


def take(ctx: Ctx, a):
    print(ctx.api.op("IbashoBuild", "take", {"ids": [rid(ctx, i) for i in a.ids], "copy": a.copy}).get("count"),
          "taken")


# ---------- inventory ----------

def folder_items(ctx: Ctx, ft_name: str, name: str = "") -> list[dict]:
    api = ctx.api
    folder = api.cmd("LLInventory", "getBasicFolderID", {"ft_name": ft_name})["id"]
    found = api.cmd("LLInventory", "collectDescendantsIf", {"folder_id": folder, "name": name}).get("items") or {}
    return list(found.values())


def object_item(ctx: Ctx, item: str) -> str:
    """An inventory item id as given, or the newest object named exactly ITEM in Objects or Lost and Found."""
    if is_uuid(item):
        return item
    for folder in ("object", "lstndfnd"):   # things returned to you land in Lost and Found
        hits = sorted((it for it in folder_items(ctx, folder, item) if it["name"] == item),
                      key=lambda it: it.get("creation_date", 0))
        if hits:
            return hits[-1]["id"]
    raise Fail(f"no object named {item!r} in Objects or Lost and Found")


def inv(ctx: Ctx, a):
    want = " ".join(a.words).lower()
    groups: dict[str, list] = {}
    for it in folder_items(ctx, a.folder):
        if want in it["name"].lower():
            groups.setdefault(it["name"], []).append(it)
    for name in sorted(groups, key=str.lower):
        its = sorted(groups[name], key=lambda it: it.get("creation_date", 0))
        print(f"{name}{f' x{len(its)}' if len(its) > 1 else ''}  {its[-1]['id']}")
    if not groups:
        print("(nothing)")


def rez(ctx: Ctx, a):
    args = {"item_id": object_item(ctx, a.item)}
    if a.pos:
        args["pos"] = vec(a.pos)
    made(ctx, ctx.api.op("IbashoBuild", "rez", args, timeout=20))


def give(ctx: Ctx, a):
    _, who = resolve(ctx, a.who, ("avatar",), lookup="explicit")
    api = ctx.api
    if a.folder:
        api.op("IbashoAgent", "give", {"to": who, "folder_id": a.item})
        print("gave the folder")
        return
    if a.perms is not None:
        # Perms live on the object in-world: set them, take a copy, give that copy.
        oid = rid(ctx, a.item)
        o = (api.op("IbashoObjects", "get", {"ids": [oid]}).get("objects") or [None])[0]
        if not o or "mine" not in o.get("flags", []):
            raise Fail("--perms needs one of your own objects in view")
        api.op("IbashoBuild", "modify", {"id": oid, "next_perms": perms_arg(a.perms)})
        started = time.time() - 5
        api.op("IbashoBuild", "take", {"ids": [oid], "copy": True})
        item = None
        for _ in range(30):
            hits = [it for it in folder_items(ctx, "object", o.get("name") or "")
                    if it["name"] == o.get("name") and it.get("creation_date", 0) >= started]
            if hits:
                item = max(hits, key=lambda it: it.get("creation_date", 0))["id"]
                break
            time.sleep(0.5)
        if not item:
            raise Fail("took a copy but couldn't find it in Objects yet; give it by name in a moment")
        api.op("IbashoAgent", "give", {"to": who, "item_id": item})
        print(f"gave a copy of {o.get('name')!r} (next owner: {a.perms.upper() or 'none'})")
        return
    item = a.item if is_uuid(a.item) else _any_item(ctx, a.item)
    api.op("IbashoAgent", "give", {"to": who, "item_id": item})
    print("gave it")


def _any_item(ctx: Ctx, name: str) -> str:
    hits = [it for it in folder_items(ctx, "root_inv", name) if it["name"] == name and not it.get("is_link")]
    if not hits:
        raise Fail(f"no inventory item named exactly {name!r} (`ibasho inv WORDS --folder root_inv` to look)")
    return max(hits, key=lambda it: it.get("creation_date", 0))["id"]


# ---------- scripts ----------

def lsl(name: str) -> str:
    return resources.files("ibasho").joinpath("lsl", name).read_text(encoding="utf-8")


def published(ctx: Ctx, object_id: str) -> list[dict]:
    return [o for o in ctx.api.rpc("object.list")["objects"] if o["object_id"] == object_id]


def wait_for(pred, timeout: float, step: float = 0.5):
    end = time.time() + timeout
    while True:
        v = pred()
        if v or time.time() >= end:
            return v
        time.sleep(step)


def find_item(ctx: Ctx, object_id: str, name: str):
    for o in published(ctx, object_id):
        prims = [(o["object_id"], o["inventory"])] + [(l["link_id"], l["inventory"]) for l in o.get("linked_objects") or []]
        for pid, items in prims:
            for it in items:
                if it["name"] == name or it["item_id"] == name:
                    return pid, it["item_id"]
    return None


def publish(ctx: Ctx, object_id: str):
    if not published(ctx, object_id):  # re-requesting a published object can reset its listing to a stale one
        ctx.api.rpc("object.request", {"object_id": object_id})
        wait_for(lambda: published(ctx, object_id), 15)


def obj(ctx: Ctx, a):
    oid = rid(ctx, a.id)
    publish(ctx, oid)
    for o in published(ctx, oid):
        print(f"{o['object_name']!r} {o['object_id']} desc={o.get('object_description')!r}")
        prims = [(1, o["object_id"], o["object_name"], o["inventory"])] + \
            [(l["link_number"], l["link_id"], l["link_name"], l["inventory"]) for l in o.get("linked_objects") or []]
        for n, pid, name, items in prims:
            print(f"  link {n} {short(pid)} {name!r}")
            for it in items:
                flags = "".join(x for x, on in (("R", it.get("running")), ("F", it.get("faulted"))) if on)
                print(f"    {it['type']:8} {it['name']!r} {short(it['item_id'])} {flags}".rstrip())


def save_script(ctx: Ctx, object_id: str, name: str, src: str, running: bool = True, vm: str = "mono",
                check_contents: bool = True, quiet: bool = False) -> dict:
    """Write src into script NAME in the object, creating it if missing; returns the compile result.

    The editor's listing of an object goes stale (more so on the Second Life grid than on OpenSim), and creating a
    script that's really there already makes "NAME 1" beside it. So: select the object (that asks the region for a fresh
    inventory serial), find the item by name, and if it isn't listed, ask the region itself and remove any
    same-named item first, so the save replaces rather than duplicates. A new script starts as the region's default
    (which says "Hello, Avatar!" once) until the save lands; the text is read back to make sure it did."""
    api = ctx.api
    sel = api.ibasho
    if sel:
        api.cmd("IbashoObjects", "select", {"ids": [object_id]})
    try:
        if sel and published(ctx, object_id):
            time.sleep(1.5)   # the select's answer marks the listing out of date; fetch it afresh (see below)
            api.rpc("object.request", {"object_id": object_id})
        else:
            publish(ctx, object_id)
        hit = wait_for(lambda: find_item(ctx, object_id, name), 4 if sel else 0.1)
        if not hit and check_contents:
            region_contents(ctx, object_id, remove=[name])
        if not hit:
            r = {}
            try:
                r = api.rpc("object.item.create", {"prim_id": object_id, "type": "script", "name": name, "vm": vm})
            except ApiError as e:
                if not any(s in str(e) for s in ("Timed out", "not found in updated inventory")):
                    raise
            hit = wait_for(lambda: find_item(ctx, object_id, name), 15)
            if not hit and (r or {}).get("name") == name:
                hit = (object_id, r["item_id"])
            for _ in range(3):
                if hit:
                    break
                # Regions without the newer inventory caps (OpenSim) don't update the editor's listing after a
                # create. A fresh select marks the viewer's copy of the inventory out of date (the region's answer
                # carries a new inventory serial); asking for the object again then fetches it afresh.
                if sel:
                    api.cmd("IbashoObjects", "deselect", {"ids": [object_id]})
                    api.cmd("IbashoObjects", "select", {"ids": [object_id]})
                    time.sleep(1.5)
                api.rpc("object.request", {"object_id": object_id})
                hit = wait_for(lambda: find_item(ctx, object_id, name), 5)
            if not hit:
                raise Fail(f"made {name!r} (it holds the default script for now) but can't find it in the listing; "
                           "run the same script-save again: it'll find it by name and save over it")
    finally:
        if sel:
            api.cmd("IbashoObjects", "deselect", {"ids": [object_id]})
    prim, item = hit
    r = api.rpc("object.content.save", {"prim_id": prim, "item_id": item, "content": src, "running": running,
                                        "vm": vm}, timeout=60)
    if not quiet:
        if r.get("compiled") is False:
            for d in r.get("diagnostics", []):
                print(f"COMPILE {d.get('level', 'ERROR')} line {d.get('row')} col {d.get('column')}: "
                      f"{str(d.get('message', '')).strip()}")
            print("COMPILE FAILED: saved but not running")
        else:
            print(f"saved {name!r} in {short(object_id)}" + ("" if running else " (stopped)"))
    try:
        got = api.rpc("object.content.get", {"prim_id": prim, "item_id": item}).get("content", "")
        if got.strip() != src.strip() and not quiet:
            # where the listing isn't kept up to date (OpenSim), the read goes to the old text until it's fetched again
            api.cmd("IbashoObjects", "select", {"ids": [object_id]})
            time.sleep(1.5)
            api.cmd("IbashoObjects", "deselect", {"ids": [object_id]})
            api.rpc("object.request", {"object_id": object_id})
            hit = wait_for(lambda: find_item(ctx, object_id, name), 5) or hit
            got = api.rpc("object.content.get", {"prim_id": hit[0], "item_id": hit[1]}).get("content", "")
        if got.strip() != src.strip() and not quiet:
            print(f"WARNING: {name!r} doesn't hold this file after the save; run it again")
    except ApiError as e:
        if not quiet:
            print(f"(couldn't read it back to check: {e})")
    return r


def script_save(ctx: Ctx, a):
    path = ctx.agent_path(a.file, "script file")
    name = a.name
    if not path.exists():
        try:
            swapped = ctx.agent_path(a.name, "script file")
        except Fail:
            swapped = None
        if swapped is not None and swapped.is_file():   # `script-save ID FILE NAME`: the order's easy to swap, and only one is a file
            print(f"(taking {a.name} as the file and {a.file!r} as the script's name: script-save ID NAME FILE)")
            path, name = swapped, a.file
        else:
            raise Fail(f"no such file: {path} (script-save ID NAME FILE: the object, the script's name in it, "
                       "then the file)")
    src = path.read_text(encoding="utf-8")
    save_script(ctx, rid(ctx, a.id), name, src, running=not a.stopped, vm=a.vm, check_contents=not a.no_check)


def script_get(ctx: Ctx, a):
    oid = rid(ctx, a.id)
    publish(ctx, oid)
    hit = find_item(ctx, oid, a.name)
    if not hit:
        raise Fail(f"no item {a.name!r} in {short(oid)}'s listing")
    print(ctx.api.rpc("object.content.get", {"prim_id": hit[0], "item_id": hit[1]})["content"])


def lsl_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def wait_owner_say(ctx: Ctx, since: int, prefix: str, timeout: float, end_marks=()) -> list[str]:
    """Owner-say lines starting with prefix, until one ends with an end mark (or the first, without marks)."""
    lines, end = [], time.time() + timeout
    while time.time() < end:
        for e in ctx.api.events(since, wait=2, kinds=["chat"]):
            since = max(since, e["seq"])
            msg = (e.get("data") or {}).get("message") or ""
            if isinstance(msg, str) and msg.startswith(prefix):
                lines.append(msg)
                if not end_marks or msg[len(prefix):] in end_marks:
                    return lines
    return lines


def mine_or_fail(ctx: Ctx, oid: str):
    """Only my own objects: with someone's edit rights a probe would save into theirs, report to them, and stay."""
    if not ctx.api.ibasho:
        return
    o = (ctx.api.op("IbashoObjects", "get", {"ids": [oid]}).get("objects") or [None])[0]
    me = (ctx.api.status().get("self") or {}).get("id")
    if o and o.get("owner_id") and me and o["owner_id"] != me:
        raise Fail(f"{short(oid)} isn't yours: not putting a script into someone else's object")


def region_contents(ctx: Ctx, object_id: str, remove=()) -> list[tuple[str, int, str]]:
    mine_or_fail(ctx, object_id)
    tag = f"{random.randrange(16 ** 6):06x}"
    src = string.Template(lsl("contents_probe.lsl")).substitute(gone=", ".join(lsl_str(n) for n in remove), tag=tag)
    since = ctx.api.seq()
    r = save_script(ctx, object_id, f"contents {tag}", src, check_contents=False, quiet=True)
    if r.get("compiled") is False:
        raise Fail("the contents probe didn't compile")
    prefix = f"IBASHO|inv|{tag}|"
    rows = []
    for line in wait_owner_say(ctx, since, prefix, 30, end_marks=("EOF",)):
        rest = line[len(prefix):]
        if rest == "EOF":
            return rows
        name, t, run = rest.rsplit("|", 2)
        rows.append((name, int(t), run))
    raise Fail("no report from the contents probe within 30 s (is the object in reach, and yours?)")


def contents(ctx: Ctx, a):
    oid = rid(ctx, a.id)
    for name, t, run in region_contents(ctx, oid, a.remove):
        state = {"1": "  running", "0": "  STOPPED"}.get(run, "")
        print(f"{TYPES.get(t, t):9} {name}{state}")
    if a.remove:
        print(f"(removed if present: {', '.join(a.remove)})")


# ---------- notecards ----------

def my_notecards(ctx: Ctx) -> list[dict]:
    """Every notecard in my inventory (all folders, Received Items too), as LLInventory lists items."""
    api = ctx.api
    f = api.cmd("LLInventory", "getBasicFolderID", {"ft_name": "notecard"})["id"]
    root = api.cmd("LLInventory", "getItemsInfo", {"item_ids": [f]})["categories"][f]["parent_id"]
    items = (api.cmd("LLInventory", "collectDescendantsIf", {"folder_id": root, "name": ""}).get("items") or {}).values()
    return [i for i in items if i.get("asset_type") == "notecard" and not i.get("is_link")]


def find_notecard(cards: list[dict], want: str) -> dict:
    """A notecard by its item id (or its first characters), its asset id, or its name: the whole name first, else a
    part of it; the newest of several."""
    w = want.strip().lower()
    for key in ("id", "asset_id"):
        hits = [c for c in cards if str(c.get(key) or "").lower() == w]
        if hits:
            return hits[0]
    hits = [c for c in cards if c["name"].lower() == w] or [c for c in cards if w in c["name"].lower()]
    if not hits and len(w) >= 6 and all(ch in "0123456789abcdef-" for ch in w):
        hits = [c for c in cards if str(c.get("id") or "").lower().startswith(w)]
    if not hits:
        raise Fail(f"no notecard like {want!r} in my inventory (`ibasho notecard --list` lists them)")
    return max(hits, key=lambda c: c.get("creation_date", 0))


def notecard(ctx: Ctx, a):
    """The Ibasho viewer reads a notecard in my inventory itself, as its notecard window does (`IbashoAgent
    readNotecard`). Older viewers can't hand the text over, but the LSL function llGetNotecardLine takes the card's
    asset id, and the inventory listing has those: so there, a throwaway reader in one of my objects reads it out."""
    api = ctx.api
    cards = my_notecards(ctx)
    if a.list or not a.name:
        for c in sorted(cards, key=lambda c: -c.get("creation_date", 0))[:50]:
            print(f"{c['name']}  {c.get('id')}")
        if not cards:
            print("(no notecards)")
        return
    card = find_notecard(cards, a.name)
    if api.has_op("IbashoAgent", "readNotecard"):
        r = api.op("IbashoAgent", "readNotecard", {"item_id": card["id"], "timeout": 30}, timeout=45)
        print(f"# {r.get('name') or card['name']}")
        print(r.get("text") or "(empty)")
        if r.get("items"):
            print("# items in it: " + "; ".join(f"{i.get('type')} {i.get('name')!r}" for i in r["items"]))
        if r.get("truncated"):
            print("# (cut at 64 KB)")
        return
    asset = card.get("asset_id")
    if not asset or str(asset).startswith("00000000"):
        raise Fail(f"{card['name']!r}: its asset id is hidden (no-copy or no-modify?), so it can't be read this way")
    if not a.host:
        raise Fail("this viewer can't read notecards itself (a newer Ibasho viewer can): give --host OBJECT, one of "
                   "your objects within reach, to run a reader script in")
    host = rid(ctx, a.host)
    mine_or_fail(ctx, host)
    tag = f"{random.randrange(16 ** 6):06x}"
    since = api.seq()
    r = save_script(ctx, host, f"nc reader {tag}", string.Template(lsl("notecard_reader.lsl")).substitute(
        card=asset, tag=tag), check_contents=False, quiet=True)
    if r.get("compiled") is False:
        raise Fail("the reader didn't compile")
    prefix = f"IBASHO|nc|{tag}|"
    got = {}
    end_mark = None
    for line in wait_owner_say(ctx, since, prefix, 70, end_marks=("EOF", "TIMEOUT")):
        rest = line[len(prefix):]
        if rest in ("EOF", "TIMEOUT"):
            end_mark = rest
            break
        n, _, text = rest.partition("|")
        got[int(n)] = text
    print(f"# {card['name']}")
    for i in sorted(got):
        print(got[i])
    if end_mark != "EOF":
        print("# (timed out before the end)")
