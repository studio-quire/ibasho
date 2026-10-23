"""The `ibasho` command against the daemon and the fake viewer."""

from __future__ import annotations

import threading
import time

import pytest

from ibasho.cli import main


def run(world, capsys, *args) -> tuple[int, str, str]:
    code = main(["--config", str(world.cfg.path), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def later(secs, fn):
    t = threading.Timer(secs, fn)
    t.start()
    return t


def test_status(world_connected, capsys):
    code, out, _ = run(world_connected, capsys, "status")
    assert code == 0 and "in world as Test Avatar AI" in out and "0/3000 messages" in out


def test_say_counts_and_handles_channels(world_connected, capsys):
    w = world_connected
    assert run(w, capsys, "say", "/7 lights on")[0] == 0
    assert run(w, capsys, "say", "C:/Program Files/Git/me waves")[0] == 0
    w.until(lambda: len(w.viewer.sent("LLChatBar", "sendChat")) == 2)
    a, b = w.viewer.sent("LLChatBar", "sendChat")
    assert (a["channel"], a["message"]) == (7, "lights on")
    assert (b["channel"], b["message"]) == (0, "/me waves")
    assert w.api.status(fresh=True)["messages_today"] == 1   # chat to scripts on channel 7 isn't a message


def test_watch_wakes_on_an_im_but_not_on_tool_chatter(world_connected, capsys):
    w = world_connected
    def emit():
        w.emit("LLChat", {"from": "My Thing", "from_id": "o", "message": "IBASHO|probe|1|1:0", "chat_type": 8, "source": 2})
        w.emit("LLChat", {"from": "Test Avatar", "from_id": w.viewer.self_id, "message": "me talking", "chat_type": 1})
        w.emit("IbashoEvents", {"event": "im", "dir": "in", "session_type": "p2p", "from": "ann.example",
                               "from_id": "aaaaaaaa", "session_id": "s1", "message": "are you around?"})
    later(0.5, emit)
    code, out, _ = run(w, capsys, "watch", "--exit-on-wake", "--timeout", "20")
    assert code == 0
    assert out.strip().splitlines() == ["IM[p2p s:s1] ann.example (aaaaaaaa): are you around?"]


def test_group_chat_is_digested_unless_im_named(world_connected, capsys):
    w = world_connected
    def emit():
        w.emit("IbashoEvents", {"event": "im", "dir": "in", "session_type": "group", "session_name": "Builders",
                               "from": "x", "session_id": "g1", "message": "anyone seen the sandbox?"})
        w.emit("IbashoEvents", {"event": "im", "dir": "in", "session_type": "group", "session_name": "Builders",
                               "from": "y", "session_id": "g1", "message": "testy, you there?"})
    later(0.5, emit)
    code, out, _ = run(w, capsys, "watch", "--exit-on-wake", "--timeout", "20")
    # named: at once; the rest of the group's chat as a digest, which comes along when the watch returns
    assert out.strip().splitlines() == ["IM[group 'Builders' s:g1] y (): testy, you there?",
                                        "group digest [Builders] 1 msgs: x: anyone seen the sandbox?"]


def test_watch_with_a_timeout_hands_over_gathered_group_chat(world_connected, capsys):
    """Group chat waits to be digested; a watch that stops (--timeout) says it rather than dropping it."""
    w = world_connected
    later(0.5, lambda: w.emit("IbashoEvents", {"event": "im", "dir": "in", "session_type": "group",
                                              "session_name": "Builders", "from": "x", "session_id": "g1",
                                              "message": "anyone seen the sandbox?"}))
    t = time.time()
    code, out, _ = run(w, capsys, "watch", "--exit-on-wake", "--timeout", "3")
    assert code == 0 and time.time() - t < 10
    assert out.strip().splitlines() == ["group digest [Builders] 1 msgs: x: anyone seen the sandbox?"]


def test_listen_collects_then_wakes_on_my_name(world_connected, capsys):
    w = world_connected
    def emit():
        w.emit("LLChat", {"from": "Speaker", "from_id": "s", "message": "welcome to the talk", "chat_type": 1, "source": 1})
        w.emit("LLChat", {"from": "Speaker", "from_id": "s", "message": "any questions, Test?", "chat_type": 1, "source": 1})
    later(0.5, emit)
    code, out, _ = run(w, capsys, "listen", "--secs", "15", "--fresh")
    assert "Speaker: welcome to the talk" in out and "URGENT: (my name came up)" in out


def test_listen_settles(world_connected, capsys):
    w = world_connected
    later(0.3, lambda: w.emit("LLChat", {"from": "Ann", "from_id": "a", "message": "hi", "chat_type": 1, "source": 1}))
    t = time.time()
    code, out, _ = run(w, capsys, "listen", "--secs", "30", "--settle", "1", "--fresh")
    assert "Ann: hi" in out and time.time() - t < 10


def test_pay_over_the_cap_is_refused_before_the_viewer(world_connected, capsys):
    w = world_connected
    code, _, err = run(w, capsys, "pay", "12345678-1234-1234-1234-123456789abc", "500")
    assert code == 1 and "per-payment cap" in err
    assert not w.viewer.sent("IbashoAgent", "pay")


def test_im_refuses_an_unknown_avatar(world_connected, capsys):
    w = world_connected
    w.viewer.handlers[("IbashoAgent", "names")] = lambda d: {"names": {}}
    code, _, err = run(w, capsys, "im", "12345678-1234-1234-1234-123456789abc", "hello")
    assert code == 1 and "no avatar has id" in err
    assert not w.viewer.sent("IbashoIM", "send")


def test_walk_stops_short_of_a_drop(world_connected, capsys):
    w = world_connected
    starts = []
    w.viewer.handlers[("LLAgent", "startAutoPilot")] = lambda d: starts.append(d) or {}

    def probe(d):   # flat for 5 m, then the edge of a dock
        return {"feet": 21.0, "points": [{"terrain": 21.0 if (p[1] - 128) < 5.5 else 10.0, "water": 15.0, "hit": None}
                                         for p in d["points"]]}
    w.viewer.handlers[("IbashoAgent", "probeGround")] = probe
    code, out, _ = run(w, capsys, "walk", "128", "140")
    assert code == 0 and "drop or water 5.5 m ahead" in out
    w.until(lambda: starts)
    target = starts[-1]["target_global"]
    assert abs(target[1] - (256000 + 128 + 4.5)) < 0.01   # a metre short of the first point past the edge
    assert starts[-1]["allow_flying"] is False


def test_forget_command(world_connected, capsys):
    w = world_connected
    seq = w.api.seq()
    w.emit("LLChat", {"from": "Ann Example", "from_id": "aaaaaaaa-0000-0000-0000-000000000001", "message": "x",
                      "chat_type": 1, "source": 1})
    w.until(lambda: w.api.events(seq))
    code, out, _ = run(w, capsys, "forget", "Ann Example")
    assert code == 0 and "1 events" in out and "aaaaaaaa" in out and "useraaaa" in out
    assert any(e["kind"] == "daemon" for e in w.api.events(0))   # my own events stay


def test_daemon_down_message(home, capsys):
    from conftest import write_config
    cfg = write_config(home)
    code = main(["--config", str(cfg.path), "look", "--noshot"])
    assert code == 1 and "ibasho up" in capsys.readouterr().err


def test_watch_says_what_the_viewer_refused(world_connected, capsys):
    w = world_connected
    later(0.5, lambda: w.emit("IbashoEvents", {"event": "refused", "what": "land",
                                              "reason": "Ibasho: buying land is off in this viewer."}))
    code, out, _ = run(w, capsys, "watch", "--exit-on-wake", "--timeout", "20")
    assert out.strip().splitlines() == ["refused (land): buying land is off in this viewer."]


def test_im_over_the_viewers_own_cap(world_connected, capsys):
    w = world_connected

    def send(d):   # what the viewer does: the refusal first, then an ordinary reply
        w.viewer.emit("IbashoEvents", {"event": "refused", "what": "IM",
                                      "reason": "Ibasho: this IM wasn't sent. Today's limit is reached."})
        return {"session_id": "s1"}
    w.viewer.handlers[("IbashoIM", "send")] = send
    code, out, err = run(w, capsys, "im", "12345678-1234-1234-1234-123456789abc", "hello")
    assert code == 1 and "not sent: this IM wasn't sent" in err and "sent (" not in out


def test_prim_create_leaves_the_deselecting_to_the_viewer(world_connected, capsys):
    w = world_connected
    code, out, _ = run(w, capsys, "prim-create", "--name", "block")
    assert code == 0 and out.strip() == "00000000-0000-0000-0000-000000000077"
    assert w.viewer.sent("IbashoBuild", "create") and not w.viewer.sent("IbashoObjects", "select")
