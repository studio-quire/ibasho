"""What the real-grid trial turned up once it resumed: notifications the viewer shows again at login, the natural
guesses (`chat`, a button's name, arguments swapped, a positional query, `--after`), reaching anyone by username,
friendship, profiles, a login the grid dropped, and the display-name reminder."""

from __future__ import annotations

import argparse
import datetime as dt
import threading
import time
import types

import pytest

from ibasho import config as config_mod
from ibasho.cli import lifecycle, main, make
from ibasho.cli.common import Ctx, explicit_username, looks_like_username, username_key
from ibasho.policy import Caps, Ledger, Policy, ViewerState

from conftest import write_config

FAR = "abcdef12-3456-4789-8abc-def012345678"     # someone not near, not a friend, never met
NOTE_ID = "5ca1ab1e-0000-4000-8000-000000000001"


def run(world_or_path, capsys, *args) -> tuple[int, str, str]:
    path = getattr(getattr(world_or_path, "cfg", None), "path", world_or_path)
    code = main(["--config", str(path), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def later(secs, fn):
    t = threading.Timer(secs, fn)
    t.start()
    return t


def utc(ago_s: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ago_s)).replace(tzinfo=None).isoformat()


def buttons(*pairs):
    return [{"type": "button", "name": n, "text": t} for n, t in pairs]


def nobody_around(w):
    """The lookups `resolve` makes, answered: no one near, no friends, no groups, no sessions."""
    h = w.viewer.handlers
    h[("IbashoAgent", "friends")] = lambda d: {"friends": []}
    h[("IbashoAgent", "people")] = lambda d: {"people": []}
    h[("LLAgent", "getGroups")] = lambda d: {"groups": []}
    h[("IbashoObjects", "nearby")] = lambda d: {"objects": []}
    h[("IbashoObjects", "find")] = lambda d: {"ids": []}


# ---------- notifications shown again at login ----------

def test_replayed_notifications_are_marked_and_the_ones_seen_stay_quiet(world_connected, capsys):
    w = world_connected
    seen, unseen = "11111111-0000-4000-8000-000000000001", "22222222-0000-4000-8000-000000000002"
    gift = lambda nid, t: {"id": nid, "name": "UserGiveItem", "type": "offer", "message": "a gift for you",
                           "form": buttons(("Keep", "Keep"), ("Discard", "Discard")), "time": t}
    w.emit("IbashoNotify", gift(seen, utc()))          # shown when it came, in the last session
    w.until(lambda: any(e["data"].get("id") == seen for e in w.api.events(0, kinds=["notify"])))

    def login_replays():
        w.emit("IbashoNotify", gift(seen, utc(4 * 3600)))
        w.emit("IbashoNotify", {"id": "33333333-0000-4000-8000-000000000003", "name": "PaymentReceived",
                                "type": "notify", "message": "someone paid you L$50", "form": [], "time": utc()})
        w.emit("IbashoNotify", gift(unseen, utc(3 * 3600)))
    later(0.5, login_replays)
    code, out, _ = run(w, capsys, "watch", "--exit-on-wake", "--timeout", "20")
    assert code == 0
    lines = out.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("notify UserGiveItem (from before this login, ")
    assert unseen in lines[0]
    evs = {e["data"]["id"]: e["data"] for e in w.api.events(0, kinds=["notify"])}
    assert evs[seen]["replayed"] and evs[seen]["shown_before"]
    assert evs[unseen]["replayed"] and not evs[unseen]["shown_before"]
    assert "replayed" not in evs["33333333-0000-4000-8000-000000000003"]
    # the daemon still knows them, to be answered
    assert seen in w.daemon.vstate.notifications and unseen in w.daemon.vstate.notifications


def test_seen_notification_ids_outlast_the_daemon(world_connected):
    w = world_connected
    w.emit("IbashoNotify", {"id": NOTE_ID, "name": "PaymentReceived", "type": "notify", "message": "x",
                            "form": [], "time": utc()})
    from ibasho.daemon import NotifyIds
    w.until(lambda: NOTE_ID in w.daemon.notify_ids)
    w.until(lambda: NOTE_ID in NotifyIds(w.cfg.state_dir / "notify_ids.json"))   # written just after


# ---------- the natural guesses ----------

def test_chat_is_say(world_connected, capsys):
    w = world_connected
    assert run(w, capsys, "chat", "hello there")[0] == 0
    w.until(lambda: w.viewer.sent("LLChatBar", "sendChat"))
    assert w.viewer.sent("LLChatBar", "sendChat")[-1]["message"] == "hello there"


def test_respond_takes_a_button_and_the_ids_first_characters(world_connected, capsys):
    w = world_connected
    w.emit("IbashoNotify", {"id": NOTE_ID, "name": "ObjectGiveItem", "type": "offer", "message": "a teapot",
                            "form": buttons(("Keep", "Keep"), ("Discard", "Discard"), ("Mute", "Block")),
                            "time": utc()})
    w.until(lambda: NOTE_ID in w.daemon.vstate.notifications)

    def answered():
        w.until(lambda: w.viewer.sent("LLNotifications", "respond"))
        return w.viewer.received.pop()[1] if w.viewer.received[-1][0] == "LLNotifications" else None

    code, out, err = run(w, capsys, "respond", NOTE_ID[:8], "discard")
    assert code == 0 and out.strip() == "answered ObjectGiveItem: Discard", err
    sent = answered()
    assert sent["uuid"] == NOTE_ID and sent["response"] == {"Discard": True}
    code, _, _ = run(w, capsys, "respond", NOTE_ID, "Block")          # by what the button says
    assert code == 0 and answered()["response"] == {"Mute": True}
    code, _, _ = run(w, capsys, "respond", NOTE_ID[:6], '{"Keep": true}')   # JSON still works
    assert code == 0 and answered()["response"] == {"Keep": True}
    code, _, err = run(w, capsys, "respond", NOTE_ID[:8], "Maybe")
    assert code == 1 and "no button 'Maybe'" in err and "Keep, Discard, Mute ('Block')" in err
    code, _, err = run(w, capsys, "respond", "fedcba98", "Keep")
    assert code == 1 and "no recent notification has an id starting 'fedcba98'" in err


def test_respond_matches_a_template_buttons_short_name(world_connected, capsys):
    w = world_connected
    nid = "44444444-0000-4000-8000-000000000004"
    w.emit("IbashoNotify", {"id": nid, "name": "TeleportOffered", "type": "offer", "message": "come over",
                            "form": buttons(("OK_okcancelbuttons", "Teleport"), ("Cancel_okcancelbuttons", "Cancel")),
                            "time": utc()})
    w.until(lambda: nid in w.daemon.vstate.notifications)
    assert run(w, capsys, "respond", nid[:8], "ok")[0] == 0
    w.until(lambda: w.viewer.sent("LLNotifications", "respond"))
    assert w.viewer.sent("LLNotifications", "respond")[-1]["response"] == {"OK_okcancelbuttons": True}


def test_script_save_takes_the_file_and_the_name_either_way(home, capsys, monkeypatch, tmp_path):
    cfg = write_config(home)
    f = tmp_path / "teapot.lsl"
    f.write_text("default { state_entry() {} }", encoding="utf-8")
    got = {}
    monkeypatch.setattr(make, "rid", lambda ctx, i, kinds=("object",): "the-object")
    monkeypatch.setattr(make, "save_script", lambda ctx, oid, name, src, **kw: got.update(oid=oid, name=name) or {})
    code, out, _ = run(cfg.path, capsys, "script-save", "abc", str(f), "teapot")
    assert code == 0 and got == {"oid": "the-object", "name": "teapot"}
    assert "script-save ID NAME FILE" in out
    got.clear()
    assert run(cfg.path, capsys, "script-save", "abc", "teapot", str(f))[0] == 0
    assert got == {"oid": "the-object", "name": "teapot"}


def test_objects_takes_the_query_as_a_word(world_connected, capsys):
    w = world_connected
    asked = []
    w.viewer.handlers[("IbashoObjects", "nearby")] = lambda d: asked.append(d) or {"objects": []}
    assert run(w, capsys, "objects", "teapot")[0] == 0
    assert asked[-1]["query"] == "teapot"
    assert run(w, capsys, "objects", "--query", "bench")[0] == 0
    assert asked[-1]["query"] == "bench"


def test_events_after_is_since(world_connected, capsys):
    w = world_connected
    code, out, _ = run(w, capsys, "events", "--after", "0", "--kinds", "daemon", "--text")
    assert code == 0 and "in world" in out
    seq = w.api.seq()
    code, out, _ = run(w, capsys, "events", "--after", str(seq), "--kinds", "daemon")
    assert code == 0 and out.strip() == ""


# ---------- anyone, by username ----------

def test_usernames():
    assert username_key("First.Last") == username_key("first last") == "first.last"
    assert username_key("Someone Resident") == username_key("someone") == "someone"
    assert looks_like_username("first.last") and looks_like_username("First Last") and looks_like_username("solo")
    assert not looks_like_username("a3c9e0f1") and not looks_like_username("s:1234") and \
        not looks_like_username("Example (AI)")
    assert explicit_username("first.last") and explicit_username("Solo Resident") and not explicit_username("solo")


def test_im_reaches_anyone_by_username_with_the_viewers_search(world, capsys):
    w = world
    w.viewer.extra_ops = {"IbashoAgent": ["findAvatars"]}
    w.connect()
    nobody_around(w)
    w.viewer.handlers[("IbashoAgent", "findAvatars")] = lambda d: {"avatars": [
        {"id": "99999999-0000-4000-8000-000000000009", "username": "some.oneelse", "display_name": "Other"},
        {"id": FAR, "username": "some.one", "display_name": "Some One"}]}
    code, out, err = run(w, capsys, "im", "Some.One", "hello")
    assert code == 0, err
    assert w.viewer.sent("IbashoIM", "send")[-1]["to"] == FAR
    assert w.viewer.sent("IbashoAgent", "findAvatars")[-1]["name"] == "Some.One"
    assert run(w, capsys, "im", "some one", "again")[0] == 0      # known now: no second search
    assert len(w.viewer.sent("IbashoAgent", "findAvatars")) == 1
    assert w.viewer.sent("IbashoIM", "send")[-1]["to"] == FAR
    w.viewer.handlers[("IbashoAgent", "findAvatars")] = lambda d: {"avatars": []}
    code, _, err = run(w, capsys, "im", "no.body", "hi")
    assert code == 1 and "no avatar has the username 'no.body'" in err


def test_without_the_search_a_temporary_script_looks_it_up(world_connected, capsys, monkeypatch):
    w = world_connected
    nobody_around(w)
    w.viewer.handlers[("IbashoBuild", "delete")] = lambda d: {"count": 1}
    scripts = []
    monkeypatch.setattr(make, "save_script", lambda ctx, oid, name, src, **kw: scripts.append((oid, src)) or {})
    monkeypatch.setattr(make, "wait_owner_say", lambda ctx, since, prefix, timeout, end_marks=(): [prefix + FAR])
    code, out, err = run(w, capsys, "pay", "some.one", "5")
    assert code == 0, err
    assert w.viewer.sent("IbashoAgent", "pay")[-1]["to"] == FAR
    host = w.viewer.sent("IbashoBuild", "create")[-1]
    assert host["temp"] and host["phantom"]
    assert w.viewer.sent("IbashoBuild", "delete")[-1]["ids"] == [scripts[0][0]]
    assert 'list NAMES = ["some one", "Some One", "some.one"];' in scripts[0][1]


def test_paying_someone_unknown_needs_the_username_in_full(world_connected, capsys):
    w = world_connected
    nobody_around(w)
    code, _, err = run(w, capsys, "pay", "tipjar", "5")
    assert code == 1 and "write their username in full" in err
    assert not w.viewer.sent("IbashoBuild", "create") and not w.viewer.sent("IbashoAgent", "pay")


# ---------- friendship and profiles ----------

def test_friend_offers_friendship(world_connected, capsys):
    w = world_connected
    nobody_around(w)
    w.viewer.handlers[("IbashoAgent", "offerFriendship")] = lambda d: {}
    code, out, err = run(w, capsys, "friend", FAR, "--message", "good to meet you")
    assert code == 0 and out.startswith("offered friendship to Someone"), err
    offer = w.viewer.sent("IbashoAgent", "offerFriendship")[-1]
    assert (offer["to"], offer["message"]) == (FAR, "good to meet you")
    w.viewer.handlers[("IbashoAgent", "friends")] = lambda d: {"friends": [{"id": FAR, "online": True}]}
    code, out, _ = run(w, capsys, "friend", FAR)
    assert code == 0 and out.startswith("already friends") and len(w.viewer.sent("IbashoAgent", "offerFriendship")) == 1


def test_profile_without_the_viewers_profile_op(world_connected, capsys):
    w = world_connected
    nobody_around(w)
    code, out, _ = run(w, capsys, "profile", FAR)
    assert code == 0 and out.startswith(f"Someone (user{FAR[:4]}) {FAR}")
    assert "need a newer Ibasho viewer" in out


def test_profile_reads_about_picks_and_groups(world, capsys):
    w = world
    w.viewer.extra_ops = {"IbashoAgent": ["profile"]}
    w.connect()
    nobody_around(w)
    w.viewer.handlers[("IbashoAgent", "profile")] = lambda d: {
        "id": d["id"], "about": "I build boats.\nSay hi.", "born": "2020-01-02",
        "partner_id": "00000000-0000-0000-0000-000000000000",
        "picks": [{"id": "p1", "name": "The harbour"}], "groups": [{"id": "g1", "name": "Sailors"}]}
    code, out, _ = run(w, capsys, "profile", FAR)
    assert code == 0
    assert 'about: "I build boats.\\nSay hi."' in out and "born 2020-01-02" in out
    assert "picks (1): The harbour" in out and "groups (1): Sailors" in out and "partner" not in out


def test_the_new_ops_have_their_arguments_checked(tmp_path):
    pol = Policy(Caps(), Ledger(tmp_path / "ledger.json"))
    st = ViewerState(logged_in=True, notifications={})
    ok = lambda op, **kw: pol.check("IbashoAgent", dict(op=op, **kw), st).ok
    assert ok("findAvatars", name="some.one", timeout=10) and ok("profile", id=FAR, timeout=10)
    assert not ok("findAvatars", name="") and not ok("findAvatars", name="x" * 65)
    assert not ok("findAvatars", name="a\nb") and not ok("findAvatars", name=5)
    assert not ok("findAvatars", name="x", timeout=1e9) and not ok("findAvatars", name="x", timeout=True)
    assert not ok("profile", id="someone") and not ok("profile", id=FAR + "/../x") and not ok("profile")


# ---------- a login the grid dropped ----------

def ctx_of(w) -> Ctx:
    return Ctx(argparse.Namespace(config=str(w.cfg.path)))


def test_a_login_the_grid_dropped_is_seen(world_connected, capsys):
    w = world_connected
    w.emit("IbashoNotify", {"id": NOTE_ID, "name": "YouHaveBeenLoggedOut", "type": "alertmodal",
                            "message": "You have been logged out.\nThe region went away.", "form": buttons(("OK", "OK")),
                            "time": utc()})
    w.until(lambda: w.api.status(fresh=True).get("logged_out"))
    ctx = ctx_of(w)
    assert lifecycle.stranded(ctx, ctx.api.status(fresh=True)).startswith("the grid logged me out (You have been")
    code, out, _ = run(w, capsys, "status")
    assert "LOGGED OUT by the grid" in out and "`ibasho up` logs back in" in out


def test_in_no_region_is_seen(world_connected, monkeypatch):
    w = world_connected
    monkeypatch.setattr(lifecycle, "time", types.SimpleNamespace(sleep=lambda s: None, time=time.time))
    w.viewer.handlers[("IbashoAgent", "parcel")] = lambda d: {"parcel": {"name": "somewhere"}}
    ctx = ctx_of(w)
    assert "not in any region" in lifecycle.stranded(ctx, ctx.api.status(fresh=True))
    w.viewer.handlers[("IbashoAgent", "parcel")] = lambda d: {"region": {"name": "Somewhere"}, "parcel": {}}
    assert lifecycle.stranded(ctx, ctx.api.status(fresh=True)) is None


# ---------- the display name ----------

def test_check_leaves_the_display_name_to_the_agent(home, capsys):
    """The display name and profile are the agent's to choose (2026-11-03): no wording is asked of them."""
    cfg = write_config(home, grid="secondlife", confirmed=True)
    with pytest.raises(SystemExit):     # no viewer here: a PROBLEM, after the notes
        main(["--config", str(cfg.path), "check"])
    assert "NOTE: the scripted-agent flag hasn't been checked" in capsys.readouterr().out
    config_mod.save_flag_check(cfg, cfg.grid_arg, {"t": time.time(), "flagged": True, "display_name": "Sunny"})
    with pytest.raises(SystemExit):
        main(["--config", str(cfg.path), "check"])
    assert "NOTE" not in capsys.readouterr().out


# ---------- setting my own profile (0.2) ----------

TEX_ITEM = "7e570000-0000-4000-8000-000000000001"
TEX_ASSET = "a55e7000-0000-4000-8000-000000000002"


def profile_world(world):
    w = world
    w.viewer.extra_ops = {"IbashoAgent": ["profile", "setProfile", "setDisplayName"]}
    w.connect()
    nobody_around(w)
    h = w.viewer.handlers
    h[("IbashoAgent", "setProfile")] = lambda d: {}
    h[("IbashoAgent", "setDisplayName")] = lambda d: {"display_name": d["name"], "next_change": "2026-11-10T12:00:00Z"}
    return w


def test_set_profile_passes_on_whatever_the_agent_writes(world, capsys):
    w = profile_world(world)
    about = "I'm a bot. Or not; my call.\nSecond line"
    code, out, err = run(w, capsys, "set-profile", "--about", about, "--display-name", "Wren")
    assert code == 0, err
    assert w.viewer.sent("IbashoAgent", "setProfile")[-1]["about"] == about
    assert w.viewer.sent("IbashoAgent", "setDisplayName")[-1]["name"] == "Wren"
    assert "profile saved" in out and "display name now 'Wren'" in out and "2026-11-10" in out
    code, out, err = run(w, capsys, "set-profile", "--display-name", "")   # back to the username
    assert code == 0 and w.viewer.sent("IbashoAgent", "setDisplayName")[-1]["name"] == "", err
    code, _, err = run(w, capsys, "set-profile")
    assert code == 1 and "nothing to set" in err


def test_set_profile_finds_the_pictures_asset(world, capsys):
    w = profile_world(world)
    h = w.viewer.handlers
    h[("LLInventory", "getBasicFolderID")] = lambda d: {"id": "f0000000-0000-4000-8000-000000000000"}
    h[("LLInventory", "collectDescendantsIf")] = lambda d: {"items": {TEX_ITEM: {
        "id": TEX_ITEM, "name": "me at the harbour", "creation_date": 5}}}
    h[("LLInventory", "getItemsInfo")] = lambda d: {"items": {TEX_ITEM: {
        "id": TEX_ITEM, "name": "me at the harbour", "asset_id": TEX_ASSET}} if TEX_ITEM in d["item_ids"] else {}}
    code, _, err = run(w, capsys, "set-profile", "--image", "me at the harbour")
    assert code == 0, err
    sent = w.viewer.sent("IbashoAgent", "setProfile")[-1]
    assert sent["image_id"] == TEX_ASSET and "about" not in sent
    code, _, err = run(w, capsys, "set-profile", "--image", TEX_ASSET)     # not an item: an asset id as given
    assert code == 0 and w.viewer.sent("IbashoAgent", "setProfile")[-1]["image_id"] == TEX_ASSET, err


def test_a_display_name_change_too_soon_says_when(world, capsys):
    w = profile_world(world)
    w.viewer.handlers[("IbashoAgent", "setDisplayName")] = lambda d: {
        "error": "the display name can change about once a week; the next change can be made from "
                 "2026-11-09T08:00:00Z (UTC)", "next_change": "2026-11-09T08:00:00Z"}
    code, _, err = run(w, capsys, "set-profile", "--display-name", "Wren")
    assert code == 1 and "once a week" in err and "2026-11-09" in err


def test_set_profile_with_an_older_viewer_says_so(world_connected, capsys):
    code, _, err = run(world_connected, capsys, "set-profile", "--about", "hello")
    assert code == 1 and "0.2 or newer" in err


def test_profile_set_ops_check_types_not_words(tmp_path):
    pol = Policy(Caps(), Ledger(tmp_path / "ledger.json"))
    st = ViewerState(logged_in=True, notifications={})
    ok = lambda op, **kw: pol.check("IbashoAgent", dict(op=op, **kw), st).ok
    assert ok("setProfile", about="anything at all, no AI in it") and ok("setProfile", about="")
    assert ok("setProfile", image_id=TEX_ASSET) and ok("setDisplayName", name="Wren") and ok("setDisplayName", name="")
    assert not ok("setProfile", about=5) and not ok("setProfile", image_id="me.png")
    assert not ok("setDisplayName") and not ok("setDisplayName", name=["x"])


# ---------- where the next login starts ----------

def test_the_start_place_is_only_given_on_second_lifes_main_grid(home):
    """secondlife://REGION/X/Y/Z means a place on Second Life's main grid to the viewer, which then logs in there
    whatever --grid said: on a test grid (or aditi) it would send the login to Linden Lab's."""
    import json as json_mod
    for grid, expect in (("secondlife", True), ("aditi", False), ("localhost-9000", False)):
        cfg = write_config(home, grid=grid, confirmed=True)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / "last_place.json").write_text(json_mod.dumps(
            {"grid": cfg.grid_arg, "region": "Ahern", "pos": [10, 20, 30], "t": time.time()}), encoding="utf-8")
        got = lifecycle.start_slurl(cfg)
        assert got == (["--url", "secondlife://Ahern/10/20/30"] if expect else []), grid


def test_the_persons_login_can_start_somewhere_other_than_home(home):
    """A new account's home can keep sending it somewhere that refuses it (seen 2026-11-03): `login --start`."""
    from ibasho.cli.common import Fail
    cfg = write_config(home, grid="secondlife", confirmed=True)
    assert lifecycle.start_slurl(cfg, "Sandbox Island") == ["--url", "secondlife://Sandbox%20Island/128/128/30"]
    cfg = write_config(home, grid="localhost-9000", confirmed=True)
    with pytest.raises(Fail):
        lifecycle.start_slurl(cfg, "Sandbox Island")
