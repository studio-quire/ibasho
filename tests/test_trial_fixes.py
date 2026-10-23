"""What the first real-grid trial and the review after it turned up: files only in the agent's folders, the
person's commands refused in the agent's sessions, the browser guard, one-line errors, ids printed and taken back,
and a few smaller things."""

from __future__ import annotations

import json
import os
import time

import pytest

from ibasho import config as config_mod, policy as policy_mod
from ibasho.api import ApiError
from ibasho.cli import main
from ibasho.keeper import settings as keeper_settings
from ibasho.policy import Caps, Ledger, Policy, ViewerState

from test_daemon import raw_http

IN = ViewerState(logged_in=True, notifications={})


def run(cfgpath, capsys, *args) -> tuple[int, str, str]:
    code = main(["--config", str(cfgpath), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


# ---------- files: only the agent's folders ----------

@pytest.fixture
def pol(tmp_path):
    shots, life = tmp_path / "data" / "shots", tmp_path / "life"
    shots.mkdir(parents=True)
    life.mkdir()
    return Policy(Caps(), Ledger(tmp_path / "ledger.json"), shots=shots, readable=[life, shots]), shots, life


def test_snapshots_land_only_in_the_pictures_folder(pol, tmp_path):
    p, shots, life = pol
    snap = lambda f: p.check("LLViewerWindow", {"op": "saveSnapshot", "filename": f, "type": "COLOR"}, IN)
    assert snap(str(shots / "a.bmp")).ok
    for bad in (str(tmp_path / "a.bmp"), str(life / "a.bmp"), str(shots / ".." / ".." / "x.bmp"), "a.bmp",
                "shots/a.bmp", 5):
        v = snap(bad)
        assert not v.ok and v.status == 403 and "only in" in v.reason, bad
    assert not p.check("LLViewerWindow", {"op": "saveSnapshot"}, IN).ok      # no filename: the viewer's choice


def test_uploads_read_only_from_life_and_pictures(pol, tmp_path):
    p, shots, life = pol
    up = lambda **kw: p.check("IbashoAgent", dict(op="upload", **kw), IN)
    assert up(path=str(life / "art" / "sign.png")).ok and up(path=str(shots / "x.png")).ok
    assert not up(path=str(tmp_path / "secret.png")).ok
    assert not up(path="sign.png").ok
    mesh = lambda **kw: p.check("IbashoAgent", dict(op="uploadMesh", path=str(life / "m.dae"), **kw), IN)
    assert mesh(lod_low=str(life / "low.dae")).ok
    assert not mesh(lod_low=str(tmp_path / "low.dae")).ok
    assert not mesh(physics=str(tmp_path / "phys.dae")).ok
    assert mesh(lod_med=None).ok


def test_model_files_name_only_files_in_the_folders(pol, tmp_path):
    p, shots, life = pol
    mesh = lambda path, **kw: p.check("IbashoAgent", dict(op="uploadMesh", path=str(path), **kw), IN)
    (life / "ok.gltf").write_text(json.dumps({"buffers": [{"uri": "ok.bin"}], "images": [{"uri": "wood.png"}]}))
    assert mesh(life / "ok.gltf", textures=True).ok
    (life / "steal.gltf").write_text(json.dumps({"buffers": [{"uri": "../../secret.bin"}]}))
    assert not mesh(life / "steal.gltf").ok
    (life / "far.gltf").write_text(json.dumps({"buffers": [{"uri": "a.bin"}],
                                               "images": [{"uri": "file:///" + str(tmp_path / "x.png")}]}))
    assert not mesh(life / "far.gltf").ok and not mesh(life / "far.gltf", textures=True).ok   # previews load them
    dae = '<COLLADA><library_images><image id="i"><init_from>{}</init_from></image></library_images></COLLADA>'
    (life / "ok.dae").write_text(dae.format("tex/wood.png"))
    (life / "far.dae").write_text(dae.format(str(tmp_path / "secret.png")))
    (life / "unc.dae").write_text(dae.format("file://server/share/x.png"))
    assert mesh(life / "ok.dae", textures=True).ok
    assert not mesh(life / "far.dae").ok      # the upload window previews them even without textures
    assert not mesh(life / "far.dae", textures=True).ok and not mesh(life / "unc.dae", textures=True).ok
    glb = json.dumps({"buffers": [{"byteLength": 4}], "images": [{"uri": "C:/Windows/x.png"}]}).encode()
    glb += b" " * (-len(glb) % 4)
    (life / "m.glb").write_bytes(b"glTF" + (2).to_bytes(4, "little") + (20 + len(glb)).to_bytes(4, "little")
                                 + len(glb).to_bytes(4, "little") + b"JSON" + glb)
    assert not mesh(life / "m.glb").ok and not mesh(life / "m.glb", textures=True).ok


@pytest.mark.parametrize("pump,data", [
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "ExternalEditor", "value": "cmd.exe /c calc"}),
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "LeapPlaybackEventsCommand", "value": ["calc"]}),
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "UserLogFile", "value": "C:/x.log"}),
    ("LLViewerControl", {"op": "set", "group": "Account", "key": "SnapshotBaseDir", "value": "C:/"}),
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "NewCacheLocation", "value": "C:/"}),
    ("LLViewerControl", {"op": "toggle", "group": "Global", "key": "BrowserFileAccessFromFileUrls"}),
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "CEFRemoteDebuggingPort", "value": 9222}),
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "ExternalWebsocketSyncEnable", "value": True}),
    ("UI", {"op": "call", "function": "Advanced.EventRecorder", "parameter": "start playback"}),
    ("UI", {"op": "call", "function": "Advanced.AgentPilot", "parameter": "stop record"}),
    ("UI", {"op": "call", "function": "Tools.ToggleScriptEditorServer"}),
    ("UI", {"op": "call", "function": "ToggleControl", "parameter": "BrowserFileAccessFromFileUrls"}),
    ("UI", {"op": "call", "function": "Advanced.ShowURL", "parameter": "https://example.com"}),
    ("LLFloaterReg", {"op": "showInstance", "name": "web_content", "key": {"url": "file:///C:/Users/x/a.txt"}}),
    ("LLFloaterReg", {"op": "showInstance", "name": "web_content", "key": {"url": "http://127.0.0.1:8080/"}}),
    ("LLFloaterReg", {"op": "showInstance", "name": "web_content", "key": {"url": "http://192.168.1.1/"}}),
    ("LLFloaterReg", {"op": "showInstance", "name": "web_content", "key": {"url": "http://100.64.0.7:8470/"}}),
    ("LLCommandDispatcher", {"op": "dispatch", "cmd": "openfloater", "params": ["web_content"],
                             "query": {"url": "FILE:C:/x"}}),
])
def test_ways_to_programs_and_files_are_refused(pol, pump, data):
    p, _, _ = pol
    assert not p.check(pump, data, IN).ok


def test_ordinary_settings_still_pass(pol):
    p, _, _ = pol
    for pump, data in (("LLViewerControl", {"op": "set", "group": "Global", "key": "RenderFarClip", "value": 256}),
                       ("LLViewerControl", {"op": "set", "group": "Global", "key": "VoiceOutputAudioDevice",
                                            "value": "x"}),
                       ("LLViewerControl", {"op": "set", "group": "PerAccount",
                                            "key": "KeepConversationLogTranscripts", "value": 0}),
                       ("LLViewerControl", {"op": "toggle", "group": "Global", "key": "ShowNavbarNavigationPanel"})):
        assert p.check(pump, data, IN).ok, data


def test_links_the_person_allows_go_as_untrusted(tmp_path):
    p = Policy(Caps(), Ledger(tmp_path / "ledger.json"), allow=["LLURLDispatcher dispatch"])
    v = p.check("LLURLDispatcher", {"op": "dispatch", "url": "secondlife:///app/agent/x/about"}, IN)
    assert v.ok and v.data["trusted"] is False


def test_editor_calls_go_through_the_policy(pol):
    p, _, _ = pol
    assert p.check_rpc("object.list", None, IN).ok
    assert p.check_rpc("object.content.save", {"prim_id": "x"}, IN).ok
    assert p.check_rpc("command.execute", {"command": "viewer.teleport", "params": {}}, IN).ok
    assert not p.check_rpc("command.execute", {"command": "viewer.file.open"}, IN).ok
    assert not p.check_rpc("editor.open_file", {}, IN).ok
    v = p.check_rpc("object.list", None, ViewerState(logged_in=False))
    assert not v.ok and v.status == 409
    assert p.check_rpc("object.list", None, ViewerState(logged_in=True, gate="closed")).status == 409
    assert not p.check_listen("LLLogin", IN).ok and not p.check_listen("LLWindow", IN).ok


def test_rpc_over_http_refuses_what_isnt_the_editors(world_connected):
    code, body = raw_http(world_connected, "POST", "/rpc", {"method": "editor.open_file"},
                          {"Content-Type": "application/json"})
    assert code == 403 and body.get("refused")


def test_snapshot_paths_over_http(world_connected):
    w = world_connected
    with pytest.raises(ApiError) as e:
        w.api.cmd("LLViewerWindow", "saveSnapshot", {"filename": str(w.cfg.home / "elsewhere.bmp")})
    assert e.value.status == 403
    assert not w.viewer.sent("LLViewerWindow", "saveSnapshot")


# ---------- the person's commands, refused in the agent's sessions ----------

@pytest.mark.parametrize("args", [
    ("login",), ("init",), ("uninstall",), ("keeper", "status"), ("keeper", "start"), ("selfcheck", "--reset"),
    ("tool", "setup"), ("tool", "install", "numpy"), ("hear", "setup"), ("hear", "run", "--pid", "4"),
    ("hear", "run", "--file", "x.wav"),
])
def test_persons_commands_refused_in_a_session(home, capsys, monkeypatch, args):
    from conftest import write_config
    cfg = write_config(home)
    monkeypatch.setenv("IBASHO_SESSION", "s1")
    code, _, err = run(cfg.path, capsys, *args)
    assert code == 1 and "refused in the agent's sessions" in err


def test_another_config_is_refused_in_a_session(home, capsys, monkeypatch, tmp_path):
    from conftest import write_config
    cfg = write_config(home)
    other = tmp_path / "other"
    other.mkdir()
    other_cfg = write_config(other)
    monkeypatch.setenv("IBASHO_SESSION", "s1")
    code, _, err = run(other_cfg.path, capsys, "notes")
    assert code == 1 and "`--config`" in err
    code, _, err = run(cfg.path, capsys, "notes")    # the session's own config, spelled out: fine
    assert code == 0


def test_ibasho_own_background_processes_arent_the_session(monkeypatch):
    from ibasho import procs
    monkeypatch.setenv("IBASHO_SESSION", "s1")
    assert "IBASHO_SESSION" not in procs.env_with(X=1)


def test_files_the_agent_names_stay_in_its_folders(home, capsys, monkeypatch, tmp_path):
    from conftest import write_config
    cfg = write_config(home)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.lsl").write_text("default {}", encoding="utf-8")
    monkeypatch.setenv("IBASHO_SESSION", "s1")
    code, _, err = run(cfg.path, capsys, "script-save", "abc", "s", str(outside / "secret.lsl"))
    assert code == 1 and "reads only the life folder" in err
    code, _, err = run(cfg.path, capsys, "recall", "anything", "--dir", str(outside))
    assert code == 1 and "reads only the life folder" in err


# ---------- the browser guard ----------

def test_node_fetch_passes_browsers_dont(world):
    ok = lambda h: raw_http(world, "GET", "/status", headers=h)[0]
    assert ok({"Sec-Fetch-Mode": "cors"}) == 200                          # Node's built-in fetch
    assert ok({"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none"}) == 200
    for site in ("cross-site", "same-site", "same-origin"):
        assert ok({"Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": site}) == 403
    assert ok({"Origin": "null"}) == 403
    assert ok({"Host": "127.0.0.1.example:9270"}) == 403


def test_bad_timeouts_are_json_400s(world_connected):
    for path, body in (("/cmd", {"pump": "LLAgent", "data": {"op": "getID"}, "timeout": "soon"}),
                       ("/rpc", {"method": "object.list", "timeout": "soon"}),
                       ("/cmd", {"pump": "LLAgent", "data": {"op": "getID"}, "timeout": True})):
        code, b = raw_http(world_connected, "POST", path, body, {"Content-Type": "application/json"})
        assert code == 400 and "timeout" in b["error"]


# ---------- one-line errors ----------

def test_missing_files_are_one_line(home, capsys):
    from conftest import write_config
    cfg = write_config(home)
    code, _, err = run(cfg.path, capsys, "script-save", "abc", "s", "C:somewherehomelifescriptsbench.lsl")
    assert code == 1 and "Traceback" not in err
    assert err.count("\n") == 1 and "no such file" in err and "backslashes" in err
    code, _, err = run(cfg.path, capsys, "script-save", "abc", "s", str(home / "nothing-here.lsl"))
    assert code == 1 and err.startswith(f"error: no such file: {home / 'nothing-here.lsl'} (script-save ID NAME FILE")
    assert err.count("\n") == 1


def test_bad_values_are_one_line(world_connected, capsys):
    code, _, err = run(world_connected.cfg.path, capsys, "prim-create", "--pos", "a,b,c")
    assert code == 1 and "Traceback" not in err and err.startswith("error: bad value")


# ---------- who I am, and ids taken back ----------

def lookups(w):
    w.viewer.handlers[("LLAgent", "getGroups")] = lambda d: {"groups": []}
    w.viewer.handlers[("IbashoAgent", "friends")] = lambda d: {"friends": []}

def test_status_says_my_id(world_connected, capsys):
    w = world_connected
    code, out, _ = run(w.cfg.path, capsys, "status")
    assert w.viewer.self_id in out and "test.avatar" in out


def test_a_payer_can_be_answered_by_short_id_or_name(world_connected, capsys):
    w = world_connected
    lookups(w)
    payer = "5e1f00d5-0000-4000-8000-00000000c0de"
    w.emit("IbashoEvents", {"event": "money", "dir": "in", "amount": 50, "source_id": payer,
                           "dest_id": w.viewer.self_id, "balance": 50, "desc": ""})
    w.until(lambda: w.api.events(0, kinds=["money"]))
    code, out, err = run(w.cfg.path, capsys, "events", "--kinds", "money", "--text")
    assert "L$ in 50 from Someone (5e1f00d5)" in out
    assert run(w.cfg.path, capsys, "im", "5e1f00d5", "thanks!")[0] == 0
    assert run(w.cfg.path, capsys, "im", "user5e1f", "thanks again")[0] == 0     # their username
    sends = w.viewer.sent("IbashoIM", "send")
    assert [s.get("to") for s in sends] == [payer, payer]


def test_session_ids_as_printed(world_connected, capsys):
    w = world_connected
    lookups(w)
    sid = "a1b2c3d4-0000-4000-8000-000000000001"
    w.viewer.handlers[("IbashoIM", "sessions")] = lambda d: {"sessions": [
        {"session_id": sid, "name": "Ann Example", "type": "p2p", "other_id": "aaaaaaaa-0000-4000-8000-0000000000aa",
         "unread": 1}]}
    for who in ("s:a1b2c3d4", f"s:{sid}", "a1b2c3d4"):
        code, _, err = run(w.cfg.path, capsys, "im", who, "hello")
        assert code == 0, (who, err)
    assert [s.get("session_id") for s in w.viewer.sent("IbashoIM", "send")] == [sid] * 3


# ---------- the rest ----------

def test_exit_on_wake_brings_held_group_chat_along(world_connected, capsys):
    w = world_connected

    def emit():
        w.emit("IbashoEvents", {"event": "im", "dir": "in", "session_type": "group", "session_name": "Builders",
                               "from": "x", "session_id": "g1", "message": "anyone seen the sandbox?"})
        w.emit("IbashoEvents", {"event": "im", "dir": "in", "session_type": "p2p", "from": "ann.example",
                               "from_id": "aaaaaaaa", "session_id": "s1", "message": "hi!"})
    import threading
    threading.Timer(0.5, emit).start()
    code, out, _ = run(w.cfg.path, capsys, "watch", "--exit-on-wake", "--timeout", "20")
    lines = out.strip().splitlines()
    assert lines[0].endswith("ann.example (aaaaaaaa): hi!")
    assert lines[1] == "group digest [Builders] 1 msgs: x: anyone seen the sandbox?"


def test_other_keepers_keep_settings_in_a_table_of_their_own(home):
    from conftest import write_config
    cfg = write_config(home)
    base = cfg.path.read_text(encoding="utf-8")
    cfg.path.write_text(base + '\n[keeper]\nhours_per_day = 2\n[keeper.mine]\nwhatever = "yes"\n', encoding="utf-8")
    assert keeper_settings.load(cfg).hours_per_day == 2
    cfg.path.write_text(base + '\n[keeper]\nhours_per_dya = 2\n', encoding="utf-8")
    with pytest.raises(config_mod.ConfigError):
        keeper_settings.load(cfg)


def test_ledger_saves_through_a_busy_file(tmp_path, monkeypatch):
    real, calls = os.replace, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        real(a, b)
    monkeypatch.setattr(policy_mod.os, "replace", flaky)
    led = Ledger(tmp_path / "ledger.json")
    led.add_spend(5)
    assert json.loads((tmp_path / "ledger.json").read_text())["spent"] == 5 and len(calls) == 3


def test_a_slow_viewer_says_so(world_connected):
    t = time.time()
    with pytest.raises(ApiError) as e:
        world_connected.api.cmd("LLAgent", "getAutoPilot", timeout=0.5)   # the fake viewer never answers it
    assert e.value.status == 504 and "slow" in str(e.value) and time.time() - t < 10


def test_relative_config_paths_become_absolute(home, monkeypatch):
    from conftest import write_config
    cfg = write_config(home)
    monkeypatch.chdir(home)
    c = config_mod.load("config.toml")
    assert c.path.is_absolute() and c.shots_dir.is_absolute()
