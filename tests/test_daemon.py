"""The daemon end to end: the real stub between it and a fake viewer, the HTTP API as the CLI uses it."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from ibasho.api import ApiError

from conftest import World, write_config


def raw_http(world, method, path, body=None, headers=None):
    h = {"Authorization": f"Bearer {world.daemon.token}"}
    h.update(headers or {})
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{world.cfg.http_port}{path}", data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_browsers_and_strangers_are_refused(world):
    ok = raw_http(world, "GET", "/status")
    assert ok[0] == 200
    assert raw_http(world, "GET", "/status", headers={"Authorization": "Bearer nope"})[0] == 401
    assert raw_http(world, "GET", "/status", headers={"Origin": "http://example.com"})[0] == 403
    assert raw_http(world, "GET", "/status", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert raw_http(world, "GET", "/status", headers={"Host": "attacker.example:80"})[0] == 403
    assert raw_http(world, "POST", "/cmd", b'{"pump":"LLAgent","data":{"op":"getID"}}',
                    headers={"Content-Type": "text/plain"})[0] == 415
    assert raw_http(world, "POST", "/cmd", b"[1,2]", headers={"Content-Type": "application/json"})[0] == 400


def test_leap_port_wants_the_token(world):
    s = socket.create_connection(("127.0.0.1", world.cfg.leap_port), timeout=5)
    s.sendall(b"IBASHO-LEAP/1 wrong-token 1\n")
    s.settimeout(5)
    assert s.recv(10) == b""      # closed on us
    s.close()
    assert not world.api.status(fresh=True)["connected"]


def test_connects_and_logs_in(world):
    world.connect(logged_in=False)
    st = world.api.status(fresh=True)
    assert st["viewer"] == "ibasho" and not st["logged_in"]
    assert "probeGround" in st["ops"]["IbashoAgent"]
    assert {"StartupState", "LLChat", "IbashoNotify", "IbashoEvents"} <= world.viewer.listening
    assert world.viewer.settings["IbashoMaxPayment"] == world.cfg.caps.max_payment
    # before login, nothing reaches the viewer but the login-safe ops
    with pytest.raises(ApiError) as e:
        world.api.cmd("UI", "getValue", {"path": "/x/password_edit"})
    assert e.value.status == 409
    world.call(world.viewer.startup, "STATE_STARTED")
    world.until(lambda: world.api.status(fresh=True)["self"].get("display_name") == "Test Avatar AI")
    world.until(lambda: world.viewer.settings.get("KeepConversationLogTranscripts") == 0)  # chat logs off by default
    assert world.api.cmd("LLAgent", "getPosition")["region"] == [128.0, 128.0, 22.0]


def test_events_are_logged_with_plain_kinds(world_connected):
    w = world_connected
    seq = w.api.seq()
    w.emit("LLChat", {"from": "Ann Example", "from_id": "a" * 8, "message": "hello there", "chat_type": 1, "source": 1})
    w.emit("IbashoEvents", {"event": "im", "dir": "in", "session_type": "p2p", "from": "ann.example",
                           "from_id": "a" * 8, "message": "psst", "session_id": "s1"})
    w.emit("IbashoEvents", {"event": "object_im", "object_id": "o1", "from": "Sign", "message": "welcome"})
    w.emit("LLChat", {"from": "Sign", "from_id": "o1", "message": "welcome", "chat_type": 1, "source": 2})  # echo
    w.emit("IbashoNotify", {"id": "n1", "name": "ScriptDialog", "message": "Choose", "type": "notify",
                           "form": [{"type": "button", "name": "Red", "text": "Red"}]})
    w.until(lambda: len(w.api.events(seq)) >= 4)
    time.sleep(0.3)
    kinds = [e["kind"] for e in w.api.events(seq)]
    assert kinds == ["chat", "im", "object_im", "notify"]
    assert w.api.events(seq, kinds=["im"])[0]["data"]["message"] == "psst"


def test_requests_go_through_policy(world_connected):
    w = world_connected
    w.api.cmd("LLChatBar", "sendChat", {"message": "hi all", "channel": 0}, wait=False)
    w.until(lambda: w.viewer.sent("LLChatBar", "sendChat"))
    assert w.api.status(fresh=True)["messages_today"] == 1
    with pytest.raises(ApiError) as e:
        w.api.cmd("IbashoAgent", "pay", {"to": "x", "amount": 10_000})
    assert e.value.status == 403 and "cap" in str(e.value)
    assert not w.viewer.sent("IbashoAgent", "pay")
    r = w.api.cmd("IbashoAgent", "pay", {"to": "x", "amount": 20})
    assert r["ok"]
    st = w.api.status(fresh=True)
    assert st["spent_session"] == 20 and st["spent_today"] == 20


def test_side_channel_events(world_connected):
    w = world_connected
    seq = w.api.post_event("heard", {"text": "someone said hi", "wake": True})
    assert w.api.events(seq - 1)[0]["kind"] == "heard"
    with pytest.raises(ApiError):
        w.api.post_event("im", {"message": "a fake IM"})


def test_forget_over_http(world_connected):
    w = world_connected
    them = "aaaaaaaa-0000-0000-0000-000000000001"
    seq = w.api.seq()
    w.emit("LLChat", {"from": "Ann Example", "from_id": them, "message": "secret", "chat_type": 1, "source": 1})
    w.emit("LLChat", {"from": "Bo Sample", "from_id": "b" * 8, "message": "public", "chat_type": 1, "source": 1})
    w.until(lambda: len(w.api.events(seq)) == 2)
    r = w.api.http("POST", "/forget", {"ids": [them], "names": []})
    assert r["events_removed"] == 1
    assert [e["data"]["message"] for e in w.api.events(seq)] == ["public"]
    text = "".join(p.read_text(encoding="utf-8") for p in w.cfg.events_dir.glob("*.jsonl"))
    assert "secret" not in text


def test_linden_grid_without_the_flag_closes_the_viewer(home):
    w = World(write_config(home))
    try:
        w.viewer.settings["CurrentGrid"] = "util.agni.lindenlab.com"
        w.connect(logged_in=False)
        w.until(lambda: w.api.status(fresh=True)["gate"])
        w.until(lambda: w.viewer.sent("LLAppViewer", "forceQuit"))
        with pytest.raises(ApiError) as e:
            w.api.cmd("LLAgent", "getPosition")
        assert e.value.status == 409
    finally:
        w.close()


def test_stub_reconnects_to_a_restarted_daemon(home):
    cfg = write_config(home)
    w = World(cfg)
    try:
        w.connect()
        # stop just the daemon; the stub (and so the "viewer") stays up
        w.daemon.stopping.set()
        w.run(__import__("asyncio").wait_for(w.serve_task, 10), 15)
        w.daemon = w.run(w._start_daemon())
        w.until(lambda: w.api.status(fresh=True)["connected"], timeout=15)
        w.call(w.viewer.startup, "STATE_STARTED")
        w.until(lambda: w.api.status(fresh=True)["logged_in"])
        assert w.api.cmd("LLAgent", "getID")["id"] == w.viewer.self_id
    finally:
        w.close()


def test_stock_viewer_transcripts(home):
    # Linden Lab's viewer keeps its files in %APPDATA%\SecondLife (the Ibasho viewer in %APPDATA%\Ibasho)
    w = World(write_config(home, viewer="C:/Viewer/SecondLifeViewer.exe"), ibasho=False)
    try:
        acct = home / "appdata" / "SecondLife" / "test_avatar"
        acct.mkdir(parents=True)
        (acct / "ann_example.txt").write_text("[2026/01/01 10:00] Ann Example: old line\n", encoding="utf-8")
        w.connect()
        assert w.api.status(fresh=True)["viewer"] == "stock"
        time.sleep(1.5)  # the tailer has seen the file once
        seq = w.api.seq()
        with (acct / "ann_example.txt").open("a", encoding="utf-8") as f:
            f.write("[2026/01/01 10:01] Ann Example: new line\n")
        w.until(lambda: w.api.events(seq, kinds=["im_transcript"]))
        evs = w.api.events(seq, kinds=["im_transcript"])
        assert len(evs) == 1 and "new line" in evs[0]["data"]["line"]
        r = w.api.http("POST", "/forget", {"ids": [], "names": ["Ann Example"]})
        assert r["transcripts_removed"] == 1 and not (acct / "ann_example.txt").exists()
    finally:
        w.close()


def test_the_viewers_message_cap_and_count(world):
    world.viewer.messages_sent = 40          # sent earlier today, before this daemon knew
    world.connect()
    assert world.viewer.settings["IbashoMaxMessagesPerDay"] == world.cfg.caps.messages_per_day
    world.until(lambda: world.api.status(fresh=True)["messages_today"] == 40)
    # the viewer refuses a line over its cap: the event is logged as `refused`, and the daemon catches up
    world.viewer.messages_sent = 3000
    seq = world.api.seq()
    world.emit("IbashoEvents", {"event": "refused", "what": "chat line", "reason": "Ibasho: this chat line wasn't sent."})
    world.until(lambda: world.api.events(seq, kinds=["refused"]))
    world.until(lambda: world.api.status(fresh=True)["messages_today"] == 3000)
    with pytest.raises(ApiError) as e:
        world.api.cmd("LLChatBar", "sendChat", {"message": "one more", "channel": 0}, wait=False)
    assert e.value.status == 429
    world.api.cmd("LLChatBar", "sendChat", {"message": "lights on", "channel": 7}, wait=False)  # scripts: fine


def test_chat_lines_are_spaced_for_the_viewer(world_connected):
    # The viewer silently drops an LLChatBar sendChat within a second of the previous one.
    w = world_connected
    for text in ("one", "two"):
        w.api.cmd("LLChatBar", "sendChat", {"message": text, "channel": 0}, wait=False)
    w.until(lambda: len(w.viewer.sent("LLChatBar", "sendChat")) == 2)
    at = [t for (p, d), t in zip(w.viewer.received, w.viewer.received_at) if p == "LLChatBar"]
    assert at[1] - at[0] >= 1.0


def test_heard_speech_has_its_own_retention(world):
    from ibasho.hearing.settings import HEARD_KINDS
    old = time.time() - 3 * 86400
    log = world.daemon.events
    for kind in HEARD_KINDS:
        world.call(log.append, kind, {"text": "long ago"}, old)
    world.call(log.append, "chat", {"message": "long ago too"}, old)
    world.call(log.append, "heard", {"text": "just now"})
    world.call(world.daemon.prune)           # hearing.retention_days defaults to 2; the rest keeps 7
    left = [(e["kind"], (e["data"].get("text") or e["data"].get("message"))) for e in world.api.events(0)
            if e["kind"] != "daemon"]
    assert left == [("chat", "long ago too"), ("heard", "just now")]
