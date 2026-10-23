"""The last fixes from the real-grid trial: reading a notecard anywhere, my own sky, a teleport to a region the map
doesn't know, `view` given an object, a chatty object quieted or digested, and hearing saying what it is."""

from __future__ import annotations

import json
import time

import pytest

from ibasho.cli import see
from ibasho.cli.hear import OBJECT_REPEAT
from ibasho.policy import Caps, Ledger, Policy, ViewerState

from conftest import write_config
from test_resumed_trial import nobody_around, run

CARD = "c0ffee00-0000-4000-8000-00000000000c"
ROOT, NOTES = "f0000000-0000-4000-8000-000000000001", "f0000000-0000-4000-8000-000000000002"
OBJ = "0b1ec700-0000-4000-8000-000000000001"
OBJ2 = "0b1ec700-0000-4000-8000-000000000002"
IN = ViewerState(logged_in=True, notifications={})


def newer(world, *ops):
    """Connect the fake viewer as one that has these IbashoAgent ops."""
    world.viewer.extra_ops = {"IbashoAgent": list(ops)}
    world.connect()
    nobody_around(world)
    return world


def inventory(w, cards):
    h = w.viewer.handlers
    h[("LLInventory", "getBasicFolderID")] = lambda d: {"id": NOTES}
    h[("LLInventory", "getItemsInfo")] = lambda d: {"categories": {NOTES: {"parent_id": ROOT}}}
    h[("LLInventory", "collectDescendantsIf")] = lambda d: {"items": {c["id"]: c for c in cards}}


def card(name, item_id=CARD, created=1, asset="a5e7a5e7-0000-4000-8000-000000000001"):
    return {"id": item_id, "name": name, "asset_type": "notecard", "is_link": False, "creation_date": created,
            "asset_id": asset}


# ---------- the allowlist ----------

def test_the_new_ops_and_the_sky_have_their_arguments_checked(tmp_path):
    pol = Policy(Caps(), Ledger(tmp_path / "ledger.json"))
    ok = lambda pump, **kw: pol.check(pump, kw, IN)
    assert ok("IbashoAgent", op="readNotecard", item_id=CARD, timeout=20).ok
    assert not ok("IbashoAgent", op="readNotecard", item_id="welcome card").ok
    assert not ok("IbashoAgent", op="readNotecard", item_id=CARD, timeout=1e6).ok
    assert ok("IbashoAgent", op="findRegion", name="Sandbox Island").ok
    assert not ok("IbashoAgent", op="findRegion", name="x/../app/openfloater/web").ok
    assert not ok("IbashoAgent", op="findRegion", name="").ok
    for param in ("sunrise", "noon", "sunset", "midnight", "region"):
        assert ok("UI", op="call", function="World.EnvSettings", parameter=param).ok
    v = ok("UI", op="call", function="World.EnvSettings", parameter="midday")     # the menu's word is "noon"
    assert not v.ok and v.rule == "arguments" and "noon" in v.reason
    assert not ok("UI", op="call", function="World.EnvSettings", parameter="my_environs").ok   # opens a window
    assert not ok("UI", op="call", function="World.EnvSettings", parameter="noon", extra=1).ok
    v = ok("UI", op="call", function="Advanced.ShowDebugSettings", parameter="all")
    assert not v.ok and v.rule == "allowlist" and 'allow = ["UI call Advanced.ShowDebugSettings"]' in v.reason
    wide = Policy(Caps(), Ledger(tmp_path / "l2.json"), allow=["UI call"])
    assert wide.check("UI", {"op": "call", "function": "World.EnvPreset", "parameter": "x"}, IN).ok
    assert not wide.check("UI", {"op": "call", "function": "Advanced.ShowDebugSettings"}, IN).ok   # still named


# ---------- the sky ----------

def test_sky_calls_the_menu_function_with_its_own_word_for_midday(world_connected, capsys):
    w = world_connected
    w.viewer.handlers[("UI", "call")] = lambda d: {}
    code, out, err = run(w, capsys, "sky", "midday")
    assert code == 0 and out.startswith("sky: midday, in my view only"), err
    sent = w.viewer.sent("UI", "call")[-1]
    assert (sent["function"], sent["parameter"]) == ("World.EnvSettings", "noon")
    code, out, _ = run(w, capsys, "sky", "region")
    assert code == 0 and w.viewer.sent("UI", "call")[-1]["parameter"] == "region" and "region's own" in out
    with pytest.raises(SystemExit):                    # argparse: not one of the skies
        run(w, capsys, "sky", "dusk")


# ---------- notecards ----------

def test_notecard_is_read_by_the_viewer_anywhere(world, capsys):
    w = newer(world, "readNotecard")
    inventory(w, [card("Welcome card", created=1), card("Welcome card (new)", "d0ffee00-0000-4000-8000-0000000000dd",
                                                        created=5)])
    w.viewer.handlers[("IbashoAgent", "readNotecard")] = lambda d: {
        "item_id": d["item_id"], "name": "Welcome card", "text": "Hello!\nThe [landmark: Garden] is lovely.",
        "items": [{"name": "Garden", "type": "landmark"}]}
    code, out, err = run(w, capsys, "notecard", "welcome card")
    assert code == 0, err
    assert w.viewer.sent("IbashoAgent", "readNotecard")[-1]["item_id"] == CARD    # the whole name wins
    assert out.splitlines() == ["# Welcome card", "Hello!", "The [landmark: Garden] is lovely.",
                                "# items in it: landmark 'Garden'"]
    assert run(w, capsys, "notecard", "new")[0] == 0                             # a part of the name
    assert w.viewer.sent("IbashoAgent", "readNotecard")[-1]["item_id"].endswith("dd")
    assert run(w, capsys, "notecard", CARD[:8])[0] == 0                          # the id's first characters
    assert w.viewer.sent("IbashoAgent", "readNotecard")[-1]["item_id"] == CARD
    w.viewer.handlers[("IbashoAgent", "readNotecard")] = lambda d: {"error": '"Welcome card" is no-copy: ...'}
    code, _, err = run(w, capsys, "notecard", "Welcome card")
    assert code == 1 and "no-copy" in err
    code, _, err = run(w, capsys, "notecard", "menu")
    assert code == 1 and "no notecard like 'menu'" in err
    code, out, _ = run(w, capsys, "notecard")
    assert code == 0 and out.splitlines()[0].startswith("Welcome card (new)")


def test_notecard_without_the_viewers_reader_needs_a_host(world_connected, capsys):
    w = world_connected
    inventory(w, [card("Welcome card")])
    code, _, err = run(w, capsys, "notecard", "Welcome card")
    assert code == 1 and "--host" in err and "a newer Ibasho viewer" in err


# ---------- teleports and regions ----------

def test_tp_to_a_region_the_map_doesnt_know_says_so(world, capsys):
    w = newer(world, "findRegion")
    w.viewer.handlers[("IbashoAgent", "findRegion")] = lambda d: {"found": False, "similar": ["Harbourside", "Harbor"]}
    code, _, err = run(w, capsys, "tp", "Harbour", "128", "128", "30")
    assert code == 1 and "no region called 'Harbour'" in err and "similar names: Harbourside, Harbor" in err
    assert not w.viewer.sent("LLAgent", "requestTeleport")


def test_tp_uses_the_maps_spelling_and_warns_of_a_rating_above_the_cap(world, capsys):
    w = newer(world, "findRegion")
    w.viewer.handlers[("IbashoAgent", "findRegion")] = lambda d: {
        "found": True, "region": {"name": "Harbour Lights", "global": [256000.0, 256256.0], "access": 42,
                                  "maturity": "Adult"}}
    w.viewer.handlers[("LLAgent", "requestTeleport")] = lambda d: (w.viewer.emit("LLTeleport", {"success": True}), None)[1]
    code, out, err = run(w, capsys, "tp", "harbour lights", "10", "20", "30")
    assert code == 0, err
    assert w.viewer.sent("LLAgent", "requestTeleport")[-1]["regionname"] == "Harbour Lights"
    assert "rated Adult, above this install's maturity setting" in out


def test_tp_without_a_map_answer_still_goes(world, capsys, monkeypatch):
    w = newer(world, "findRegion")
    w.viewer.handlers[("IbashoAgent", "findRegion")] = lambda d: {"error": "the world map didn't answer in time"}
    w.viewer.handlers[("LLAgent", "requestTeleport")] = lambda d: (w.viewer.emit("LLTeleport", {"success": True}), None)[1]
    code, out, err = run(w, capsys, "tp", "Harbour", "1", "2", "3")
    assert code == 0 and "arrived" in out, err
    assert w.viewer.sent("LLAgent", "requestTeleport")[-1]["regionname"] == "Harbour"


def test_walk_to_another_region_finds_it_on_the_map(world, capsys):
    w = newer(world, "findRegion")
    w.viewer.handlers[("IbashoAgent", "findRegion")] = lambda d: {
        "found": True, "region": {"name": "Next Door", "global": [256256.0, 256000.0], "access": 13}}
    w.viewer.handlers[("LLAgent", "startAutoPilot")] = lambda d: None
    code, _, err = run(w, capsys, "walk", "10", "20", "--region", "Next Door")
    assert code == 0, err
    w.until(lambda: w.viewer.sent("LLAgent", "startAutoPilot"))
    assert w.viewer.sent("LLAgent", "startAutoPilot")[-1]["target_global"][:2] == [256266.0, 256020.0]


# ---------- view, given an object ----------

def test_view_tells_a_bearing_from_an_object():
    assert see.bearing_or_none("NE") == 45 and see.bearing_or_none("270") == 270
    assert see.bearing_or_none("-90") == 270
    assert see.bearing_or_none("0b1ec700") is None and see.bearing_or_none(OBJ) is None
    assert see.bearing_or_none("12345678") is None          # an id's first characters, digits only
    assert see.bearing_or_none("Blue Kite") is None


def test_view_of_an_object_looks_at_it_from_my_side(world_connected, capsys, monkeypatch):
    w = world_connected
    w.viewer.handlers[("IbashoObjects", "find")] = lambda d: {"ids": [OBJ] if OBJ.startswith(d["prefix"]) else []}
    w.viewer.handlers[("IbashoObjects", "get")] = lambda d: {"objects": [{"id": OBJ, "pos": [128.0, 138.0, 22.0]}]}
    posed = []
    monkeypatch.setattr(see, "_posed_shot", lambda ctx, a, pos, focus: posed.append((pos, focus)))
    code, out, err = run(w, capsys, "view", OBJ[:8])
    assert code == 0 and "as `cam 0b1ec700` does" in out, err
    pos, focus = posed[0]
    assert focus == (128.0, 138.0, 22.0)
    assert abs(pos[0] - 128.0) < 1e-6 and pos[1] < 138.0     # I'm south of it (at 128,128): the camera is too


# ---------- a chatty object ----------

def look_ready(w):
    nobody_around(w)
    w.viewer.handlers[("IbashoAgent", "parcel")] = lambda d: {"region": {"name": "Somewhere"}, "parcel": {}}


def object_says(w, oid, name, msg):
    w.emit("LLChat", {"from": name, "from_id": oid, "message": msg, "chat_type": 1, "source": 2})


def avatar_says(w, msg):
    w.emit("LLChat", {"from": "Ann Example", "from_id": "a0000000-0000-4000-8000-000000000001", "message": msg,
                      "chat_type": 1, "source": 1})


def test_quiet_leaves_an_object_out_of_watch_and_look(world_connected, capsys):
    w = world_connected
    look_ready(w)
    object_says(w, OBJ, "Line 3 Announcer", "Next stop: the harbour")
    w.until(lambda: any(e["data"].get("from_id") == OBJ for e in w.api.events(0, kinds=["chat"])))
    code, out, err = run(w, capsys, "quiet", "line 3 announcer", "--minutes", "30")
    assert code == 0 and "everything called 'Line 3 Announcer' for 30 min" in out, err
    code, out, _ = run(w, capsys, "quiet")
    assert "quiet: everything called 'Line 3 Announcer', 30 min more" in out

    def chatter():
        object_says(w, OBJ2, "Line 3 Announcer", "Next stop: the market")   # another car, the same name
        avatar_says(w, "hello there")
    t = __import__("threading").Timer(0.5, chatter)
    t.start()
    code, out, _ = run(w, capsys, "watch", "--exit-on-wake", "--timeout", "20")
    t.join()
    assert code == 0 and out.strip().splitlines() == ["chat Ann Example: hello there"]
    code, out, _ = run(w, capsys, "look", "--noshot", "--all")
    assert "Next stop" not in out and "quieted: 'Line 3 Announcer' 2 lines" in out
    assert run(w, capsys, "quiet", "Line 3 Announcer", "--off")[0] == 0
    assert run(w, capsys, "quiet")[1].strip() == "nothing quieted"
    code, _, err = run(w, capsys, "quiet", "the moon")
    assert code == 1 and "no object near or heard lately is called 'the moon'" in err


def test_an_object_repeating_itself_comes_as_a_digest(world_connected, capsys):
    w = world_connected
    look_ready(w)

    def tram():
        for i in range(OBJECT_REPEAT + 3):
            object_says(w, OBJ, "Line 3 Announcer", f"Stop {i}")
            time.sleep(0.05)
    t = __import__("threading").Timer(0.5, tram)
    t.start()
    code, out, _ = run(w, capsys, "watch", "--timeout", "4")
    t.join()
    lines = out.strip().splitlines()
    live = [x for x in lines if x.startswith("chat Line 3 Announcer (object): Stop")]
    assert len(live) == OBJECT_REPEAT
    assert lines[-1].startswith("object digest 'Line 3 Announcer' (0b1ec700) said 3 lines in 1 min, the last: "
                                f"Stop {OBJECT_REPEAT} | Stop {OBJECT_REPEAT + 1} | Stop {OBJECT_REPEAT + 2}")
    assert "`ibasho quiet 0b1ec700`" in lines[-1]
    code, out, _ = run(w, capsys, "look", "--noshot", "--all")
    said = [x for x in out.splitlines() if "Line 3 Announcer" in x]
    assert len(said) == 1 and f"said {OBJECT_REPEAT + 3} lines, the last: Stop {OBJECT_REPEAT + 2}" in said[0]


def test_listen_leaves_quieted_objects_out(world_connected, capsys):
    w = world_connected
    object_says(w, OBJ, "Line 3 Announcer", "Next stop: the harbour")
    w.until(lambda: any(e["data"].get("from_id") == OBJ for e in w.api.events(0, kinds=["chat"])))
    look_ready(w)
    assert run(w, capsys, "quiet", OBJ[:8])[0] == 0
    q = json.loads((w.cfg.state_dir / "quiet.json").read_text())
    assert OBJ in q["ids"] and q["ids"][OBJ]["name"] == "Line 3 Announcer"

    def chatter():
        object_says(w, OBJ, "Line 3 Announcer", "Next stop: the market")
        avatar_says(w, "lovely day")
    t = __import__("threading").Timer(0.5, chatter)
    t.start()
    code, out, _ = run(w, capsys, "listen", "--secs", "3", "--fresh")
    t.join()
    assert code == 0 and "Ann Example: lovely day" in out and "market" not in out
    assert "(1 line from quieted objects left out)" in out


# ---------- hearing says what it is ----------

def test_the_wake_prompt_says_hearing_is_words_only(home):
    from ibasho.keeper.runner import hearing_note
    cfg = write_config(home)
    assert hearing_note(cfg) == ""
    text = cfg.path.read_text(encoding="utf-8") + "\n[hearing]\nenabled = true\n"
    cfg.path.write_text(text, encoding="utf-8")
    from ibasho import config as config_mod
    note = hearing_note(config_mod.load(cfg.path))
    assert "Hearing is on" in note and "music" in note and "words" in note
