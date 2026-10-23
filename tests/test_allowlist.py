"""The daemon's allowlist: what Ibasho sends passes, everything else is refused (the ways around the old denylist
among it), refusals say how to allow more, and what the person allows still meets the hard rules."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ibasho import config as config_mod, policy as policy_mod
from ibasho.cli import main
from ibasho.policy import ALLOWED, Caps, Ledger, Policy, ViewerState

from conftest import write_config
from test_daemon import raw_http

SRC = Path(policy_mod.__file__).parent
IN = ViewerState(logged_in=True, notifications={})


@pytest.fixture
def folders(tmp_path):
    shots, life = tmp_path / "data" / "shots", tmp_path / "life"
    shots.mkdir(parents=True)
    life.mkdir()
    return shots, life


def make(tmp_path, folders, allow=(), **caps) -> Policy:
    shots, life = folders
    return Policy(Caps(**caps), Ledger(tmp_path / "ledger.json"), shots=shots, readable=[life, shots], allow=allow)


def note(name, *buttons, message=""):
    return {"name": name, "message": message, "form": [{"type": "button", "name": b, "text": b} for b in buttons]}


# ---------- what Ibasho itself sends passes ----------

def requests_in_source():
    """(pump, op, args) of every viewer request in Ibasho's code with a literal pump and op: `api.cmd`, `.op`,
    `.post` (the commands, MCP tools, hearing, the probe). The daemon's own requests go straight to the viewer."""
    found = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("cmd", "op", "post") and len(node.args) >= 2):
                continue
            pump, op = node.args[:2]
            if isinstance(pump, ast.Constant) and isinstance(op, ast.Constant) and isinstance(pump.value, str):
                args = node.args[2] if len(node.args) > 2 and isinstance(node.args[2], ast.Dict) else None
                found.append((pump.value, op.value, args, f"{path.name}:{node.lineno}"))
    return found


def test_every_request_in_the_code_is_on_the_list():
    found = requests_in_source()
    assert len(found) > 60       # the scan sees the commands
    missing = [(p, o, where) for p, o, _, where in found if o not in ALLOWED.get(p, {})]
    assert not missing


def test_every_setting_in_the_code_passes(tmp_path, folders):
    pol = make(tmp_path, folders)
    seen = set()
    for pump, op, args, where in requests_in_source():
        if pump != "LLViewerControl" or args is None:
            continue
        d = {k.value: v.value for k, v in zip(args.keys, args.values)
             if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)}
        if "key" not in d:
            continue   # a key from a table (below)
        data = {"op": op, **d}
        if op == "set" and "value" not in data:
            data["value"] = "a device" if d["key"] == "VoiceOutputAudioDevice" else 1.0
        assert pol.check(pump, data, IN).ok, where
        seen.add(d["key"])
    assert {"CameraAngle", "RenderFarClip", "VoiceOutputAudioDevice", "AudioLevelMusic"} <= seen
    from ibasho.cli.see import MAP_SETTINGS   # `overhead --map`'s overlays, on and back
    for key, value in MAP_SETTINGS.items():
        for v in (value, not value):
            assert pol.check("LLViewerControl", {"op": "set", "group": "Global", "key": key, "value": v}, IN).ok
        assert pol.check("LLViewerControl", {"op": "get", "group": "Global", "key": key}, IN).ok


def test_what_the_commands_send_passes(tmp_path, folders):
    shots, life = folders
    pol = make(tmp_path, folders)
    notes = {"tp": note("TeleportViaSLAPP", "OK_okcancelignore", "Cancel_okcancelignore"),
             "dlg": note("ScriptDialog", "Red", "Client_Side_Ignore"),
             "box": note("ScriptDialog", "!!llTextBox!!", "Client_Side_Ignore"),
             "fail": note("CouldNotTeleportReason", "OK")}
    st = ViewerState(logged_in=True, notifications=notes)
    for pump, data in [
        ("LLViewerWindow", {"op": "saveSnapshot", "filename": str(shots / "a.bmp"), "showui": False, "showhud": True,
                            "type": "COLOR"}),
        ("LLAgent", {"op": "requestTeleport", "regionname": "Sandbox Island", "x": 128.0, "y": 64.5, "z": 22.0,
                     "skip_confirmation": True}),
        ("LLAgent", {"op": "startAutoPilot", "target_global": [1.0, 2.0, 3.0], "stop_distance": 1.0,
                     "allow_flying": False}),
        ("LLAgent", {"op": "setCameraParams", "camera_pos": [1, 2, 3], "focus_pos": [1, 2, 0], "camera_locked": True,
                     "focus_locked": True}),
        ("UI", {"op": "getValue", "path": policy_mod.BALANCE_PATH}),
        ("LLNotifications", {"op": "respond", "uuid": "tp", "response": {"OK_okcancelignore": True}}),
        ("LLNotifications", {"op": "respond", "uuid": "dlg", "response": {"Red": True}}),
        ("LLNotifications", {"op": "respond", "uuid": "box", "response": {"!!llTextBox!!": "hello"}}),
        ("LLNotifications", {"op": "cancel", "uuid": "fail"}),
        ("LLGesture", {"op": "startGesture", "id": "g"}),
        ("LLInventory", {"op": "collectDescendantsIf", "folder_id": "f", "name": ""}),
        ("_command", {"op": "getAPI", "api": "IbashoAgent"}),
        ("IbashoAgent", {"op": "upload", "path": str(life / "sign.png")}),
        ("IbashoAgent", {"op": "attach", "item_id": "i", "point": "Right Hand"}),
    ]:
        assert pol.check(pump, data, st).ok, (pump, data)


# ---------- what the audits found, refused ----------

UPLOADS = ("upload_image", "upload_sound", "upload_anim_bvh", "upload_anim_anim", "upload_script")


@pytest.mark.parametrize("pump,data", [
    # uploading any file on this machine through the upload windows
    *[("LLFloaterReg", {"op": "showInstance", "name": n, "key": "C:/Users/someone/secret.txt"}) for n in UPLOADS],
    *[("LLFloaterReg", {"op": "showInstance", "name": n, "key": {"filename": "C:/x/secret.txt", "dest": "x"}})
      for n in UPLOADS],
    ("LLFloaterReg", {"op": "clickButton", "name": "upload_image", "button": "ok_btn"}),
    ("LLFloaterReg", {"op": "showInstance", "name": "bulk_upload",
                      "key": {"files": ["C:/x/a.png"], "local_dir": "C:/x"}}),
    # settings changed past the protected list
    ("UI", {"op": "call", "function": "Advanced.ShowDebugSettings", "parameter": "all"}),
    ("LLWindow", {"op": "pasteText", "text": "ExternalEditor"}),
    ("LLWindow", {"op": "keyDown", "keycode": 13}),
    ("UI", {"op": "call", "function": "ToggleControl", "parameter": "RenderGlow"}),
    ("LLFloaterReg", {"op": "showInstance", "name": "preferences"}),
    # pages opened in the built-in browser
    ("LLNotifications", {"op": "requestAdd", "name": "LoadWebPage", "payload": {"url": "https://example.com/"}}),
    ("LLNotifications", {"op": "requestAdd", "name": "GotoURL", "payload": {"url": "https://example.com/"}}),
    ("UI", {"op": "call", "function": "Advanced.WebBrowserTest", "parameter": "https://example.com/"}),
    ("UI", {"op": "call", "function": "Advanced.WebContentTest", "parameter": "https://example.com/"}),
    # the cache folder that's deleted at the next launch, and a snapshot type that stops the viewer
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "DiskCacheDirName", "value": ".."}),
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "CacheLocation", "value": "C:/"}),
    ("LLViewerWindow", {"op": "saveSnapshot", "filename": "SHOTS/a.bmp", "type": "NOPE"}),
    # the browser's file access
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "BrowserFileAccessFromFileUrls", "value": True}),
    ("LLViewerControl", {"op": "toggle", "group": "Global", "key": "BrowserFileAccessFromFileUrls"}),
    # and some neighbours
    ("LLViewerControl", {"op": "set_notation", "group": "Global", "key": "LeapPlaybackEventsCommand",
                         "value": "['calc']"}),
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "EnableVoiceChat", "value": True}),
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "RenderFarClip", "value": "C:/x"}),
    ("LLViewerControl", {"op": "set", "group": "Warnings", "key": "RenderFarClip", "value": 64}),
    ("LLViewerControl", {"op": "get", "group": "Global", "key": "SnapshotBaseDir"}),
    ("LLViewerControl", {"op": "groups"}),
    ("LLViewerWindow", {"op": "requestReshape", "w": 1, "h": 1}),
    ("LLViewerWindow", {"op": "saveSnapshot", "filename": "SHOTS/a.bmp", "width": 100000}),
    ("LLAgent", {"op": "requestTeleport", "regionname": "x/../../app/openfloater/web", "x": 1, "y": 1, "z": 1}),
    ("UI", {"op": "getValue", "path": "/main_view/login/password_edit"}),
    ("UI", {"op": "setSelectedByValue", "path": "/x", "value": 1}),
    ("LLCommandDispatcher", {"op": "dispatch", "cmd": "openfloater", "params": ["preferences"]}),
    ("LLURLDispatcher", {"op": "dispatch", "url": "secondlife:///app/openfloater/preferences"}),
    ("LLPipeline", {"op": "disableAllRenderTypes"}),
    ("GroupChat", {"op": "sendGroupIM", "message": "hi", "group_id": "g"}),
    ("IbashoAgent", {"op": "spectator", "on": False}),
    ("IbashoAgent", {"op": "streamUI", "on": True}),
    ("IbashoAgent", {"op": "somethingNew"}),
    ("_command", {"op": "listen", "source": "LLWindow", "listener": "x"}),
    ("_command", {"op": "newpump", "name": "x"}),
    ("SomeoneElsesAPI", {"op": "anything"}),
])
def test_refused(tmp_path, folders, pump, data):
    shots, _ = folders
    if data.get("filename") == "SHOTS/a.bmp":
        data = dict(data, filename=str(shots / "a.bmp"))
    v = make(tmp_path, folders).check(pump, data, IN)
    assert not v.ok and v.status == 403 and v.rule, (pump, data)


def test_notifications_that_open_pages_are_only_dismissed(tmp_path, folders):
    pol = make(tmp_path, folders)
    notes = {"web": note("LoadWebPage", "Gotopage", "Cancel", message="Load web page http://x.example/ ?"),
             "go": note("GotoURL", "Later", "GoNow..."),
             "new": note("SomeFutureNotification", "Do it", "Cancel"),
             "ok": note("RegionRestartMinutes", "OK"),
             "mfa": note("PromptMFAToken", "Continue")}
    st = ViewerState(logged_in=True, notifications=notes)
    answer = lambda i, resp: pol.check("LLNotifications", {"op": "respond", "uuid": i, "response": resp}, st)
    v = answer("web", {"Gotopage": True})
    assert not v.ok and v.rule == "notifications" and "Cancel" in v.reason
    assert 'allow = ["LLNotifications respond LoadWebPage"]' in v.reason
    assert answer("web", {"Cancel": True}).ok and answer("web", {}).ok
    assert not answer("go", {"GoNow...": True}).ok and answer("go", {"Later": True}).ok
    assert not answer("new", {"Do it": True}).ok
    assert not answer("new", {"Cancel": True, "Do it": True}).ok
    assert answer("ok", {"OK": True}).ok           # a lone OK only acknowledges
    # buttons from the viewer's templates carry the template's name
    notes.update(tpl=note("SomeConfirmation", "OK_okcancelbuttons", "Cancel_okcancelbuttons"),
                 info=note("FailedToGetBenefits", "OK_okbutton"), help=note("Something", "OK_okhelpbuttons",
                                                                         "Help_okhelpbuttons"))
    assert answer("tpl", {"Cancel_okcancelbuttons": True}).ok and not answer("tpl", {"OK_okcancelbuttons": True}).ok
    assert answer("info", {"OK_okbutton": True}).ok and not answer("help", {"Help_okhelpbuttons": True}).ok
    assert not answer("help", {"OK_okhelpbuttons": True}).ok
    v = answer("mfa", {"Continue": True})
    assert not v.ok and "only the person" in v.reason
    assert pol.check("LLNotifications", {"op": "cancel", "uuid": "web"}, st).ok


def test_settings(tmp_path, folders):
    pol = make(tmp_path, folders)
    s = lambda op, key, **kw: pol.check("LLViewerControl", {"op": op, "group": "Global", "key": key, **kw}, IN)
    assert s("set", "RenderShadowDetail", value=2).ok and s("set", "AudioLevelMaster", value=0.5).ok
    assert s("set", "SkySunDefaultPosition", value=[1.0, 0.0, 0.1]).ok and s("toggle", "UseChatBubbles").ok
    assert s("set", "RenderGlow").ok                       # back to its default
    assert not s("set", "RenderDebugPipeline", value=True).ok
    assert not s("set", "RenderGlow", value={"x": 1}).ok and not s("set", "RenderGlow", value="1").ok
    assert s("get", "CurrentGrid").ok and s("get", "IbashoMaxPayment").ok and not s("set", "CurrentGrid", value=1).ok
    assert s("get", "YieldTime").ok and not s("set", "YieldTime", value=-1).ok   # the machine's share is the person's
    assert not s("set", "IbashoMaxPayment", value=500).ok and s("set", "IbashoMaxPayment", value=10).ok
    v = s("set", "renderfarclip", value=64)
    assert not v.ok and "case-sensitive" in v.reason
    v = s("set", "audiolevelmusic", value=0)
    assert not v.ok and "case-sensitive: AudioLevelMusic" in v.reason
    v = s("set", "SomeUnknownSetting", value=1)
    assert not v.ok and 'allow = ["LLViewerControl set SomeUnknownSetting"]' in v.reason


# ---------- refusals say how to allow more ----------

def test_refusals_name_the_rule_and_the_way_to_allow_it(tmp_path, folders):
    v = make(tmp_path, folders).check("LLWindow", {"op": "keyDown", "keycode": 13}, IN)
    assert v.rule == "allowlist" and "keys and clicks" in v.reason
    assert '[policy] allow = ["LLWindow keyDown"]' in v.reason and "docs/safety.md" in v.reason
    v = make(tmp_path, folders).check("LLAgent", {"op": "flyToTheMoon"}, IN)
    assert "getPosition" in v.reason     # what LLAgent does pass
    v = make(tmp_path, folders).check("UI", {"op": "call", "function": "Advanced.ShowDebugSettings"}, IN)
    assert 'allow = ["UI call Advanced.ShowDebugSettings"]' in v.reason


# ---------- what the person allows ----------

def test_the_person_can_allow_more(tmp_path, folders):
    pol = make(tmp_path, folders, allow=["LLWindow keyDown", "UI call", "LLViewerControl set SnapshotFormat",
                                         "LLFloaterReg showInstance about", "LLNotifications respond GotoURL",
                                         "_command listen LLAutopilotSomething", "rpc script.list",
                                         "rpc command.execute viewer.other"])
    ok = lambda pump, data, st=IN: pol.check(pump, data, st).ok
    assert ok("LLWindow", {"op": "keyDown", "keycode": 13})
    assert not ok("LLWindow", {"op": "pasteText", "text": "x"})
    assert ok("UI", {"op": "call", "function": "World.EnvSettings", "parameter": "midday"})
    assert ok("LLViewerControl", {"op": "set", "group": "Global", "key": "SnapshotFormat", "value": 2})
    assert ok("LLFloaterReg", {"op": "showInstance", "name": "about"})
    assert not ok("LLFloaterReg", {"op": "showInstance", "name": "upload_image", "key": "C:/x.png"})
    st = ViewerState(logged_in=True, notifications={"go": note("GotoURL", "Later", "GoNow...")})
    assert ok("LLNotifications", {"op": "respond", "uuid": "go", "response": {"GoNow...": True}}, st)
    assert pol.check_listen("LLAutopilotSomething", IN).ok and not pol.check_listen("LLWindow", IN).ok
    assert pol.check_rpc("script.list", None, IN).ok and not pol.check_rpc("script.subscribe", {}, IN).ok
    assert pol.check_rpc("command.execute", {"command": "viewer.other"}, IN).ok


def test_a_wildcard_still_needs_the_dangerous_kinds_named(tmp_path, folders):
    pol = make(tmp_path, folders, allow=["UI call", "LLViewerControl set", "LLFloaterReg"])
    no = lambda pump, data: not pol.check(pump, data, IN).ok
    assert no("UI", {"op": "call", "function": "Advanced.ShowDebugSettings"})
    assert no("UI", {"op": "call", "function": "File.UploadImage"})
    assert no("UI", {"op": "call", "function": "ToggleControl", "parameter": "BrowserFileAccessFromFileUrls"})
    assert no("LLViewerControl", {"op": "set", "group": "Global", "key": "ExternalEditor", "value": "calc"})
    assert no("LLFloaterReg", {"op": "showInstance", "name": "about", "key": {"filename": "x"}})
    assert no("LLFloaterReg", {"op": "showInstance", "name": "bulk_upload"})
    assert pol.check("LLViewerControl", {"op": "set", "group": "Global", "key": "SnapshotFormat", "value": 1}, IN).ok
    assert pol.check("UI", {"op": "call", "function": "World.EnvSettings"}, IN).ok


def test_the_hard_rules_hold_for_what_the_person_allows(tmp_path, folders):
    shots, _ = folders
    pol = make(tmp_path, folders, allow=[
        "LLFloaterReg showInstance buy_currency", "LLFloaterReg showInstance web_content",
        "LLViewerControl set IbashoMaxPayment", "LLViewerControl get UserLoginInfo", "_command listen LLLogin",
        "LLNotifications respond PromptMFAToken", "LLViewerControl set PreferredMaturity"])
    no = lambda pump, data, st=IN: not pol.check(pump, data, st).ok
    assert no("LLFloaterReg", {"op": "showInstance", "name": "buy_currency"})
    assert no("LLFloaterReg", {"op": "showInstance", "name": "web_content", "key": {"url": "file:///C:/x"}})
    assert no("LLViewerControl", {"op": "set", "group": "Global", "key": "IbashoMaxPayment", "value": 99999})
    assert no("LLViewerControl", {"op": "set", "group": "Global", "key": "PreferredMaturity", "value": 42})
    assert no("LLViewerControl", {"op": "get", "group": "Global", "key": "UserLoginInfo"})
    assert not pol.check_listen("LLLogin", IN).ok
    st = ViewerState(logged_in=True, notifications={"m": note("PromptMFAToken", "Continue")})
    assert no("LLNotifications", {"op": "respond", "uuid": "m", "response": {"Continue": True}}, st)
    assert no("LLViewerWindow", {"op": "saveSnapshot", "filename": str(tmp_path / "elsewhere.bmp")})


def test_config_takes_the_persons_entries(home):
    cfg = write_config(home)
    base = cfg.path.read_text(encoding="utf-8")
    cfg.path.write_text(base + '\n[policy]\nallow = ["LLWindow  keyDown", "LLViewerControl set RenderGlow"]\n',
                        encoding="utf-8")
    assert config_mod.load(cfg.path).policy_allow == ["LLWindow keyDown", "LLViewerControl set RenderGlow"]
    for bad in ('allow = ["* *"]', 'allow = ["LLWindow * x"]', 'allow = "LLWindow"', 'alow = []'):
        cfg.path.write_text(base + f"\n[policy]\n{bad}\n", encoding="utf-8")
        with pytest.raises(config_mod.ConfigError):
            config_mod.load(cfg.path)


def test_the_template_has_an_empty_allow(tmp_path):
    path = config_mod.write_template(tmp_path / "config.toml", name="Test Avatar")
    assert config_mod.load(path).policy_allow == []


# ---------- through the daemon ----------

def test_apis_marks_what_isnt_passed_on(world_connected, capsys):
    w = world_connected
    code = main(["--config", str(w.cfg.path), "apis"])
    out = capsys.readouterr().out
    assert code == 0 and "LLAgent: " in out and "UI: " in out   # UI passes getValue
    assert "LLWindow [not passed on]: " in out


def test_the_api_says_which_rule(world_connected):
    post = lambda path, body: raw_http(world_connected, "POST", path, body, {"Content-Type": "application/json"})
    code, body = post("/cmd", {"pump": "LLWindow", "data": {"op": "keyDown"}})
    assert code == 403 and body["refused"] and body["rule"] == "allowlist"
    assert "LLWindow keyDown" in body["error"]
    assert not world_connected.viewer.sent("LLWindow")
    code, body = post("/listen", {"pump": "LLWindow"})
    assert code == 403 and body["rule"] == "allowlist"


def test_the_daemon_turns_file_pages_off(world_connected):
    assert world_connected.viewer.settings.get("BrowserFileAccessFromFileUrls") is False


def test_raw_is_refused_with_the_way_to_allow_it(world_connected, capsys):
    code = main(["--config", str(world_connected.cfg.path), "raw", "LLFloaterReg", "showInstance",
                 '{"name": "upload_image", "key": "C:/secret.txt"}'])
    err = capsys.readouterr().err
    assert code == 1 and '[policy] allow = ["LLFloaterReg showInstance upload_image"]' in err
    assert not world_connected.viewer.sent("LLFloaterReg")
