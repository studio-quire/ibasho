"""`ibasho mcp`: the tools through a real MCP client, against the daemon and the fake viewer."""

from __future__ import annotations

import asyncio
import base64
import io
import sys

import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters  # noqa: E402

from ibasho import mcp_server  # noqa: E402
from ibasho.cli import hear  # noqa: E402
from ibasho.mcp_server import argv, build  # noqa: E402

PERSONS = {"login", "init", "keeper", "uninstall", "config", "tool", "tool_setup", "check", "daemon", "raw", "post",
           "rpc", "hear", "notes", "sleep", "book"}


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def call(world, tool, **args):
    async with Client(build(str(world.cfg.path))) as c:
        return await c.call_tool(tool, args)


def later(world, secs, fn):
    loop = asyncio.get_running_loop()
    loop.call_later(secs, lambda: loop.run_in_executor(None, fn))


def snapshot(d):
    from PIL import Image
    Image.new("RGB", (64, 32), (40, 90, 160)).save(d["filename"], "BMP")
    return {"ok": True}


# ---------- without a daemon ----------

def test_argv():
    assert argv("say", "-_- hi", ch=7, type="normal") == ["say", "--ch=7", "--type=normal", "--", "-_- hi"]
    assert argv("prim-mod", "a", "b", pos=[1.0, 2.5, -3.0], fullbright=0, set={"hollow": 0.5}, name=None,
                force=False) == ["prim-mod", "--pos=1,2.5,-3", "--fullbright=0", '--set={"hollow": 0.5}', "--", "a", "b"]
    assert argv("look", noshot=True) == ["look", "--noshot"]
    assert argv("im-log", "s1", n=5) == ["im-log", "-n5", "--", "s1"]


async def test_the_persons_commands_arent_tools(home):
    from conftest import write_config
    cfg = write_config(home)
    async with Client(build(str(cfg.path))) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert {"look", "say", "im", "wait_events", "walk", "pay", "script_save", "up", "down"} <= names
    assert not names & PERSONS


async def test_daemon_down_says_so(home):
    from conftest import write_config
    cfg = write_config(home)
    async with Client(build(str(cfg.path))) as c:
        st = await c.call_tool("status", {})
        said = await c.call_tool("say", {"text": "hello"})
    assert not st.is_error and "daemon down" in text_of(st)
    assert said.is_error and "`ibasho up` starts it" in text_of(said)


# ---------- against the fake viewer ----------

async def test_status_with_balance(world_connected):
    w = world_connected
    w.viewer.handlers[("IbashoAgent", "balance")] = lambda d: {"balance": 42, "known": True}
    r = await call(w, "status")
    assert not r.is_error
    assert "in world as Test Avatar AI" in text_of(r) and "L$42" in text_of(r)


async def test_say_goes_through_the_daemon(world_connected):
    w = world_connected
    r = await call(w, "say", text="-_- hello", channel=7)
    assert not r.is_error and text_of(r) == "ok"
    w.until(lambda: w.viewer.sent("LLChatBar", "sendChat"))
    sent = w.viewer.sent("LLChatBar", "sendChat")[-1]
    assert (sent["channel"], sent["message"]) == (7, "-_- hello")


async def test_pay_over_the_cap_is_refused(world_connected):
    w = world_connected
    r = await call(w, "pay", to="12345678-1234-1234-1234-123456789abc", amount=500)
    assert r.is_error and "per-payment cap" in text_of(r)
    assert not w.viewer.sent("IbashoAgent", "pay")


async def test_the_daemons_refusal_reaches_the_agent(world_connected):
    """Not just the commands' own checks: the daemon's policy refuses, and says why (here: answering a notification
    it never saw, which could be a login or terms prompt that's the person's)."""
    w = world_connected
    r = await call(w, "respond", id="12345678-1234-1234-1234-123456789abc", response={"OK": True})
    assert r.is_error and "isn't one the daemon has seen" in text_of(r)
    assert not w.viewer.sent("LLNotifications", "respond")


async def test_respond_presses_a_button_by_name(world_connected):
    w = world_connected
    nid = "5ca1ab1e-0000-4000-8000-000000000002"
    w.emit("IbashoNotify", {"id": nid, "name": "ObjectGiveItem", "type": "offer", "message": "a teapot",
                            "form": [{"type": "button", "name": b, "text": b} for b in ("Keep", "Discard")]})
    w.until(lambda: nid in w.daemon.vstate.notifications)
    r = await call(w, "respond", id=nid[:8], button="Keep")
    assert not r.is_error and text_of(r) == "answered ObjectGiveItem: Keep"
    w.until(lambda: w.viewer.sent("LLNotifications", "respond"))
    sent = w.viewer.sent("LLNotifications", "respond")[-1]
    assert (sent["uuid"], sent["response"]) == (nid, {"Keep": True})
    r = await call(w, "respond", id=nid, button="Keep", response={"Keep": True})
    assert r.is_error and "not both" in text_of(r)


async def test_friend_and_profile_are_tools(world_connected):
    w = world_connected
    who = "abcdef12-3456-4789-8abc-def012345678"
    w.viewer.handlers[("IbashoAgent", "friends")] = lambda d: {"friends": []}
    w.viewer.handlers[("IbashoAgent", "offerFriendship")] = lambda d: {}
    r = await call(w, "friend", who=who)
    assert not r.is_error and text_of(r).startswith("offered friendship to Someone")
    assert w.viewer.sent("IbashoAgent", "offerFriendship")[-1]["to"] == who
    r = await call(w, "profile", who=who)
    assert not r.is_error and text_of(r).startswith(f"Someone (user{who[:4]}) {who}")


async def test_sky_and_quiet_are_tools(world_connected):
    w = world_connected
    w.viewer.handlers[("UI", "call")] = lambda d: {}
    r = await call(w, "sky", which="midday")
    assert not r.is_error and text_of(r).startswith("sky: midday")
    assert w.viewer.sent("UI", "call")[-1]["parameter"] == "noon"
    r = await call(w, "sky", which="dusk")
    assert r.is_error
    w.viewer.handlers[("IbashoObjects", "nearby")] = lambda d: {"objects": [
        {"id": "0b1ec700-0000-4000-8000-000000000001", "name": "Line 3 Announcer"}]}
    r = await call(w, "quiet", object="Line 3 Announcer", minutes=5)
    assert not r.is_error and "everything called 'Line 3 Announcer' for 5 min" in text_of(r)
    r = await call(w, "quiet")
    assert "Line 3 Announcer" in text_of(r)


async def test_prim_create_returns_the_id(world_connected):
    w = world_connected
    r = await call(w, "create_prim", name="block", pos=[128, 128, 25], color=[1, 0.5, 0], more={"phantom": True})
    assert text_of(r) == "00000000-0000-0000-0000-000000000077"
    made = w.viewer.sent("IbashoBuild", "create")[-1]
    assert made["pos"] == [128, 128, 25] and made["color"] == [1, 0.5, 0] and made["phantom"] is True


async def test_look_returns_the_picture_inline(world_connected):
    w = world_connected
    w.viewer.handlers[("LLViewerWindow", "saveSnapshot")] = snapshot
    w.viewer.handlers[("IbashoAgent", "parcel")] = lambda d: {"region": {"name": "Testplace"}, "parcel": {}}
    w.viewer.handlers[("IbashoObjects", "nearby")] = lambda d: {"objects": []}
    r = await call(w, "look")
    assert not r.is_error
    assert text_of(r).startswith("at Testplace [128.0, 128.0, 22.0]") and "\nshot " in text_of(r)
    img = [c for c in r.content if c.type == "image"]
    assert len(img) == 1 and img[0].mime_type == "image/jpeg"
    from PIL import Image
    assert Image.open(io.BytesIO(base64.b64decode(img[0].data))).size == (64, 32)


async def test_wait_events_returns_what_watch_would_print(world_connected):
    w = world_connected
    async with Client(build(str(w.cfg.path))) as c:
        quiet = await c.call_tool("wait_events", {"timeout": 1})
        assert text_of(quiet) == "(nothing in 1 s)"
        later(w, 0.5, lambda: w.emit("IbashoEvents", {
            "event": "im", "dir": "in", "session_type": "p2p", "from": "ann.example", "from_id": "aaaaaaaa",
            "session_id": "s1", "message": "are you around?"}))
        r = await c.call_tool("wait_events", {"timeout": 20})
    assert text_of(r) == "IM[p2p s:s1] ann.example (aaaaaaaa): are you around?"


async def test_group_chat_held_for_a_digest_carries_over_between_waits(world_connected, monkeypatch):
    w = world_connected
    monkeypatch.setattr(hear, "GROUP_DIGEST_S", 1.5)
    async with Client(build(str(w.cfg.path))) as c:
        await c.call_tool("wait_events", {"timeout": 1})
        w.emit("IbashoEvents", {"event": "im", "dir": "in", "session_type": "group", "session_name": "Builders",
                               "from": "x", "session_id": "g1", "message": "anyone seen the sandbox?"})
        first = await c.call_tool("wait_events", {"timeout": 1})
        second = await c.call_tool("wait_events", {"timeout": 5})
    assert text_of(first) == "(nothing in 1 s)"
    assert text_of(second) == "group digest [Builders] 1 msgs: x: anyone seen the sandbox?"


async def test_concurrent_tools_keep_their_own_output(world_connected):
    """A wait and other calls at once: each result has only its own command's words."""
    w = world_connected
    w.viewer.handlers[("IbashoAgent", "balance")] = lambda d: {"balance": 42, "known": True}
    async with Client(build(str(w.cfg.path))) as c:
        later(w, 1.5, lambda: w.emit("IbashoEvents", {
            "event": "im", "dir": "in", "session_type": "p2p", "from": "ann.example", "from_id": "aaaaaaaa",
            "session_id": "s1", "message": "hi"}))
        waited, st, ppl = await asyncio.gather(c.call_tool("wait_events", {"timeout": 20}),
                                               c.call_tool("status", {}), c.call_tool("people", {}))
    assert text_of(waited) == "IM[p2p s:s1] ann.example (aaaaaaaa): hi"
    assert "in world as" in text_of(st) and "IM[" not in text_of(st)
    assert text_of(ppl) == "ok"


async def test_a_second_wait_at_once_is_refused(world_connected):
    w = world_connected
    async with Client(build(str(w.cfg.path))) as c:
        a, b = await asyncio.gather(c.call_tool("wait_events", {"timeout": 2}),
                                    c.call_tool("wait_events", {"timeout": 2}))
    assert sorted([a.is_error, b.is_error]) == [False, True]
    assert "still running" in text_of(a if a.is_error else b)


async def test_dialog_needs_one_of_button_or_text(world_connected):
    r = await call(world_connected, "dialog")
    assert r.is_error and "either button or text" in text_of(r)


async def test_over_stdio(world_connected):
    """The real thing: `python -m ibasho --config ... mcp` as a subprocess, over its stdin and stdout."""
    w = world_connected
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "ibasho", "--config", str(w.cfg.path), "mcp"])
    async with Client(params) as c:
        r = await c.call_tool("say", {"text": "over stdio"})
        st = await c.call_tool("status", {})
    assert not r.is_error and text_of(r) == "ok"
    assert "in world as Test Avatar AI" in text_of(st)
    w.until(lambda: any(d["message"] == "over stdio" for d in w.viewer.sent("LLChatBar", "sendChat")))


def test_missing_extra_message(monkeypatch, home):
    """Without the mcp package, `ibasho mcp` says how to get it."""
    import builtins
    real = builtins.__import__

    def no_mcp(name, *a, **k):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError("No module named 'mcp'")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_mcp)
    from ibasho.cli.common import Fail
    with pytest.raises(Fail, match=r"ibasho\[mcp\]"):
        mcp_server.serve(None, type("A", (), {"config": None})())


async def test_set_profile_and_outfit_are_tools(world):
    w = world
    w.viewer.extra_ops = {"IbashoAgent": ["setProfile", "setDisplayName"]}
    w.connect()
    w.viewer.handlers[("IbashoAgent", "setProfile")] = lambda d: {}
    w.viewer.handlers[("IbashoAgent", "setDisplayName")] = lambda d: {"display_name": d["name"]}
    r = await call(w, "set_profile", about="-- starts with dashes\nand goes on", display_name="")
    assert not r.is_error and "profile saved" in text_of(r), text_of(r)
    assert w.viewer.sent("IbashoAgent", "setProfile")[-1]["about"] == "-- starts with dashes\nand goes on"
    assert w.viewer.sent("IbashoAgent", "setDisplayName")[-1]["name"] == ""
    r = await call(w, "set_profile")
    assert r.is_error
    outfits = {"0f0f0000-0000-4000-8000-000000000001": "Harbour clothes",
               "0f0f0000-0000-4000-8000-000000000002": "Rain gear"}
    w.viewer.handlers[("LLAppearance", "getOutfitsList")] = lambda d: {"outfits": outfits}
    w.viewer.handlers[("LLAppearance", "wearOutfit")] = lambda d: {}
    r = await call(w, "outfit")
    assert not r.is_error and "Harbour clothes" in text_of(r) and "Rain gear" in text_of(r)
    r = await call(w, "outfit", name="rain")
    assert not r.is_error and text_of(r).startswith("wearing it")
    sent = w.viewer.sent("LLAppearance", "wearOutfit")[-1]
    assert sent["folder_id"].endswith("0002") and sent["append"] is False
    r = await call(w, "outfit", name="Casual", library=True, add=True)
    sent = w.viewer.sent("LLAppearance", "wearOutfit")[-1]
    assert sent["folder_name"] == "Casual" and sent["append"] is True
