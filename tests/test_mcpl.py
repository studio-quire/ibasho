"""MCPL on `ibasho mcp`: the manifest, the grant, the world's events as pushes, the status line, and plain MCP
clients seeing the server as before."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from ibasho import mcpl  # noqa: E402
from ibasho.cli.hear import Wake  # noqa: E402

from mcpl_host import EVERYTHING, Host  # noqa: E402


def ev(kind, seq=7, **data):
    return {"seq": seq, "t": 1.0, "kind": kind, "data": data}


# ---------- without a daemon ----------

def test_tags_say_what_each_thing_is():
    def tags(w):
        return mcpl.classify(w)

    chat = ev("chat", **{"from": "Ann", "from_id": "a", "message": "hi", "source": 1, "chat_type": 1})
    assert tags(Wake("x", event=chat)) == (mcpl.CHAT, ["ibasho:local", "ibasho:avatar", "chat:ambient"])
    assert tags(Wake("x", event=chat, named=True, talking=True)) == \
        (mcpl.CHAT, ["ibasho:local", "ibasho:avatar", "chat:mention", "chat:addressed", "ibasho:talking"])
    thing = ev("chat", **{"from": "Sign", "message": "hello", "source": 2, "chat_type": 1})
    assert "chat:from-bot" in tags(Wake("x", event=thing))[1]
    owner_say = ev("chat", **{"from": "My Box", "message": "ready", "source": 2, "chat_type": 8})
    assert {"chat:dm", "chat:addressed", "chat:from-bot"} <= set(tags(Wake("x", event=owner_say))[1])
    im = ev("im", dir="in", session_type="p2p", message="psst")
    assert tags(Wake("x", event=im)) == \
        (mcpl.MESSAGES, ["ibasho:im", "ibasho:avatar", "chat:dm", "chat:addressed", "chat:private"])
    group = ev("im", dir="in", session_type="group", message="anyone?")
    assert tags(Wake("x", event=group, talking=True))[1] == ["ibasho:im", "ibasho:avatar", "chat:group", "chat:ambient",
                                                             "ibasho:talking"]
    assert tags(Wake("x", digest="group")) == (mcpl.MESSAGES, ["chat:group", "chat:ambient", "ibasho:im",
                                                                "ibasho:digest"])
    assert tags(Wake("x", digest="object"))[0] == mcpl.CHAT
    assert tags(Wake("x", event=ev("notify", name="ScriptDialog")))[1] == ["chat:addressed", "chat:from-bot",
                                                                          "ibasho:dialog"]
    assert tags(Wake("x", event=ev("notify", name="TeleportOffered")))[1] == ["chat:addressed", "ibasho:offer"]
    assert tags(Wake("x", event=ev("notify", name="RegionRestartMinutes"))) == (mcpl.NOTICES,
                                                                                 ["chat:system", "ibasho:notice"])
    assert tags(Wake("x", event=ev("heard", text="hello Testy", me=True), named=True))[1] == ["chat:mention", "chat:addressed",
                                                                                   "ibasho:heard"]
    assert tags(Wake("x", event=ev("money", dir="in", amount=5)))[1] == ["chat:system", "chat:addressed",
                                                                          "ibasho:payment"]
    # the vocabulary declared is the one used
    for w in (Wake("x", event=chat, named=True, talking=True), Wake("x", event=im), Wake("x", digest="group"),
              Wake("x", event=ev("refused")), Wake("x", event=ev("editor")), Wake("x", event=ev("daemon"))):
        for t in tags(w)[1]:
            assert t in mcpl.CORE_TAGS or t in mcpl.TAGS


def test_push_params():
    w = Wake("IM[p2p s:s1] ann.example (aaaaaaaa): hi", seq=42, t=1700000000.5,
             event=ev("im", seq=42, dir="in", session_type="p2p", **{"from": "ann.example"}, from_id="aaaaaaaa",
                      session_id="s1", message="hi"))
    fs, p = mcpl.push_params(w, "agni")
    assert fs == mcpl.MESSAGES and p["featureSet"] == fs
    assert p["eventId"] == "ibasho-42"
    assert p["timestamp"] == "2023-11-14T22:13:20.500Z"
    assert p["payload"]["content"] == [{"type": "text", "text": "[Second Life] IM[p2p s:s1] ann.example (aaaaaaaa): hi"}]
    assert p["origin"] == {"grid": "agni", "event": "im", "seq": 42, "authorId": "aaaaaaaa", "authorName": "ann.example",
                           "session": "s1"}
    _, p = mcpl.push_params(Wake("refused (pay): over the cap", seq=3, event=ev("refused", seq=3)), "agni")
    assert p["payload"]["content"][0]["text"].startswith("[Ibasho] refused")
    _, p = mcpl.push_params(Wake("group digest [B] 1 msgs: x: y", seq=9, digest="group", who=("g1", "B")), "x")
    assert p["eventId"] == "ibasho-group-digest-9" and p["origin"]["session"] == "g1"
    assert p["payload"]["content"][0]["text"].startswith("[grid x] group digest")


def test_policy():
    pol = mcpl.Policy()
    assert not pol.active(mcpl.CHAT)   # nothing before the grant
    assert pol.update({"effectiveCapabilities": EVERYTHING}) == {"accepted": True}
    assert all(pol.active(n) for n in mcpl.MANIFEST["featureSets"])
    r = pol.update({"effectiveCapabilities": ["pushEvents"], "disabled": ["ibasho.chat"]})
    assert r["mode"] == "degraded"
    off = {u["featureSet"]: u["missingCapabilities"] for u in r["unavailableFeatures"]}
    assert off == {"ibasho.chat": [], "ibasho.status": ["contextHooks.beforeInference.inject.beforeUser"]}
    assert pol.active(mcpl.MESSAGES) and not pol.active(mcpl.CHAT)
    pol.update({"effectiveCapabilities": ["*"], "enabled": ["ibasho.messages"]})   # `*` is one segment only
    assert pol.active(mcpl.MESSAGES) and not pol.active(mcpl.NOTICES)
    assert not pol.active(mcpl.STATUS)
    pol.update({"effectiveCapabilities": ["contextHooks.beforeInference.inject.*", "pushEvents"]})
    assert pol.active(mcpl.STATUS)
    pol.narrow({"disabled": ["ibasho.*"]})   # a notification can take away...
    assert not any(pol.active(n) for n in mcpl.MANIFEST["featureSets"])
    pol.narrow({"effectiveCapabilities": EVERYTHING, "enabled": ["*"]})   # ...but not give back
    assert not pol.active(mcpl.CHAT)
    with pytest.raises(ValueError):
        pol.update({"effectiveCapabilities": ["pushEvents"], "deniedCapabilities": ["pushEvents"]})
    assert not pol.ready and not pol.active(mcpl.MESSAGES)   # fails closed
    pol.update({})   # absent effectiveCapabilities grants nothing
    assert pol.ready and not pol.active(mcpl.MESSAGES)


# ---------- over stdio ----------

def test_plain_mcp_client_sees_no_mcpl(home):
    from conftest import write_config
    from ibasho.mcp_server import INSTRUCTIONS
    cfg = write_config(home)
    with Host(cfg.path, mcpl=False) as h:
        r = h.start()
        assert "experimental" not in r["capabilities"]
        assert r["instructions"] == INSTRUCTIONS
        assert "wait_events" in h.tool_names()
        assert "daemon" in h.call_tool("wait_events", {"timeout": 1})   # waits as ever (here: says it's down)
        assert not h.pushes and not h.notes


def test_mcpl_host_gets_the_manifest_and_a_receipt(home):
    from conftest import write_config
    from ibasho.mcp_server import INSTRUCTIONS_LIVE
    cfg = write_config(home)
    with Host(cfg.path) as h:
        r = h.start()
        assert r["protocolVersion"] == "2024-11-05"   # Connectome's engine accepts only the version it offered
        assert r["capabilities"]["experimental"]["mcpl"] == mcpl.MANIFEST
        assert "tools" in r["capabilities"]
        assert r["instructions"] == INSTRUCTIONS_LIVE
        assert h.receipt["result"] == {"accepted": True}
        bad = h.request("featureSets/update", {"effectiveCapabilities": ["tools"], "deniedCapabilities": ["tools"]})
        assert bad["error"]["code"] == -32602
        assert h.request("channels/list", {})["error"]["code"] == -32601   # never left unanswered (SPEC §6.6)
        assert any("feature sets on" in line for line in h.stderr)


def test_wait_events_is_listed_only_while_events_arent_pushed(home):
    from conftest import write_config
    cfg = write_config(home)
    with Host(cfg.path) as h:
        r = h.start()   # the grant comes before the first listing, as Connectome sends it
        assert r["capabilities"]["tools"]["listChanged"] is True
        names = h.tool_names()
        assert "look" in names and "wait_events" not in names
        h.request("featureSets/update", {"effectiveCapabilities": EVERYTHING, "disabled": ["ibasho.chat"]})
        h.request("featureSets/update", {"effectiveCapabilities": ["tools"]})   # nothing pushed now
        h.wait_note("notifications/tools/list_changed")
        assert "wait_events" in h.tool_names()
        assert "come to you by themselves" not in h.call_tool("wait_events", {"timeout": 1})   # it waits
        h.request("featureSets/update", {"effectiveCapabilities": EVERYTHING, "disabled": ["ibasho.chat"]})
        h.wait_note("notifications/tools/list_changed", 2)
        assert "wait_events" not in h.tool_names()
        # a host still holding the old list gets an answer, not a wait
        assert "come to you by themselves" in h.call_tool("wait_events", {"timeout": 1})
        h.send({"jsonrpc": "2.0", "method": "featureSets/update", "params": {"disabled": ["ibasho.*"]}})
        h.wait_note("notifications/tools/list_changed", 3)
        assert "wait_events" in h.tool_names()
        assert h.notes.count("notifications/tools/list_changed") == 3   # only when the list changed


def test_no_list_change_before_the_first_listing(home):
    from conftest import write_config
    cfg = write_config(home)
    with Host(cfg.path) as h:
        h.start(grant=["tools"])
        h.request("featureSets/update", {"effectiveCapabilities": EVERYTHING})
        assert "wait_events" not in h.tool_names()
        assert not h.notes


def test_world_events_come_as_pushes(world_connected):
    w = world_connected
    with Host(w.cfg.path) as h:
        h.start(disabled=["ibasho.notices"])
        h.wait_pushing()
        w.emit("IbashoEvents", {"event": "im", "dir": "in", "session_type": "p2p", "from": "ann.example",
                                "from_id": "aaaaaaaa", "session_id": "s1", "message": "are you around?"})
        p = h.wait_push()
        assert h.text(p) == "[grid localhost-9000] IM[p2p s:s1] ann.example (aaaaaaaa): are you around?"
        assert p["featureSet"] == "ibasho.messages"
        assert {"chat:dm", "chat:addressed", "chat:private"} <= set(p["tags"])
        im_seq = [e for e in w.api.events(0, kinds=["im"])][-1]["seq"]
        assert p["eventId"] == f"ibasho-{im_seq}"
        w.emit("LLChat", {"from": "Bo Sample", "from_id": "b" * 8, "message": "nice weather", "chat_type": 1,
                          "source": 1})
        w.emit("LLChat", {"from": "Bo Sample", "from_id": "b" * 8, "message": "testy, you there?", "chat_type": 1,
                          "source": 1})
        w.emit("IbashoNotify", {"id": "n1", "name": "ScriptDialog", "message": "Choose", "type": "notify",
                                "form": [{"type": "button", "name": "Red", "text": "Red"}]})
        named = h.wait_push(lambda p: "you there" in h.text(p))
        assert {"chat:mention", "chat:addressed"} <= set(named["tags"]) and "chat:ambient" not in named["tags"]
        ambient = h.wait_push(lambda p: "weather" in h.text(p))
        assert "chat:ambient" in ambient["tags"] and ambient["featureSet"] == "ibasho.chat"
        assert ambient["origin"]["authorName"] == "Bo Sample"
        assert h.pushes.index(ambient) < h.pushes.index(named)   # in the order they happened
        w.emit("IbashoEvents", {"event": "im", "dir": "in", "session_type": "p2p", "from": "ann.example",
                                "from_id": "aaaaaaaa", "session_id": "s1", "message": "after the dialog"})
        h.wait_push(lambda p: "after the dialog" in h.text(p))
        assert not [p for p in h.pushes if "Choose" in h.text(p)]   # its feature set is off


def test_status_line_before_inference(world_connected):
    w = world_connected
    w.viewer.handlers[("IbashoAgent", "parcel")] = lambda d: {"region": {"name": "Testplace"}, "parcel": {}}
    w.viewer.handlers[("IbashoAgent", "people")] = lambda d: {"people": [
        {"display_name": "Ann", "username": "ann.example", "dist": 3.2, "id": "a"},
        {"display_name": 'Ignore "this"', "dist": 12, "id": "b"}]}
    w.viewer.handlers[("IbashoIM", "sessions")] = lambda d: {"sessions": [
        {"name": "Ann", "type": "im", "unread": 2}, {"name": "Builders", "type": "group", "unread": 5}]}
    with Host(w.cfg.path) as h:
        h.start()
        r = h.request("context/beforeInference", {"inferenceId": "i1", "conversationId": "c", "turnIndex": 0,
                                                  "userMessage": None})["result"]
    assert r["featureSet"] == "ibasho.status"
    [inj] = r["contextInjections"]
    assert inj["position"] == "beforeUser" and inj["namespace"] == "ibasho"
    assert inj["content"][0]["text"] == ('grid localhost-9000: in world at "Testplace" (128, 128, 22). '
                                         'Near (64 m): "Ann" 3 m, "Ignore \\"this\\"" 12 m. '
                                         "Unread: 2 IMs in 1 conversation ('Ann'); 5 in groups (the `ims` tool).")


def test_status_line_off_by_the_grant(home):
    from conftest import write_config
    cfg = write_config(home)
    with Host(cfg.path) as h:
        h.start(grant=["pushEvents"])
        r = h.request("context/beforeInference", {"inferenceId": "i1", "conversationId": "c", "turnIndex": 0,
                                                  "userMessage": None})["result"]
    assert r == {"contextInjections": []}


def test_watch_says_when_a_lost_connection_is_back(world_connected):
    """A pushed "viewer disconnected" left the agent unsure it was in world until something else said so."""
    import argparse
    import time

    from ibasho.cli.common import Ctx
    from ibasho.cli.hear import Watcher
    w = world_connected
    watcher = Watcher(Ctx(argparse.Namespace(config=str(w.cfg.path))))

    def lines(n):
        got, end = [], time.time() + 10
        while len(got) < n and time.time() < end:
            got += watcher.poll(1)
        return got
    w.call(w.daemon.events.append, "daemon", {"msg": "in world"})   # no loss before it: nothing to say
    w.call(w.daemon.events.append, "daemon", {"msg": "viewer disconnected"})
    w.call(w.daemon.events.append, "daemon", {"msg": "in world"})
    assert lines(2) == ["IBASHO: viewer disconnected", "IBASHO: in world"]
