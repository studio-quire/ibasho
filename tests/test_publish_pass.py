"""The pass before publishing: arguments the viewer would read its own way, windows opened through menu functions,
the stock viewer's other accounts, the hearing model, a session's config, uploads read from checked copies, and
installing and updating the viewer in place."""

from __future__ import annotations

import _winapi
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest

from ibasho import config as config_mod, policy as policy_mod
from ibasho.cli import main
from ibasho.cli import viewer as viewer_cmd
from ibasho.policy import Caps, Ledger, Policy, StageError, ViewerState, stage_upload

from conftest import World, write_config

IN = ViewerState(logged_in=True, notifications={})


def run(cfgpath, capsys, *args) -> tuple[int, str, str]:
    code = main(["--config", str(cfgpath), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def pol(tmp_path):
    shots, life = tmp_path / "data" / "shots", tmp_path / "life"
    shots.mkdir(parents=True)
    life.mkdir()
    return Policy(Caps(), Ledger(tmp_path / "ledger.json"), shots=shots, readable=[life, shots]), shots, life


# ---------- arguments the viewer reads its own way ----------

def test_upload_cost_and_quote_must_be_what_they_say(pol):
    p, shots, life = pol
    up = lambda **kw: p.check("IbashoAgent", dict(op="upload", path=str(life / "a.png"), **kw), IN)
    for bad in (-1, -500, "10", 2.5, True, [5]):
        v = up(max_cost=bad)
        assert not v.ok and v.rule == "arguments", bad
    for bad in ("false", "true", 0, 1, {"a": 1}, []):
        v = up(quote=bad)
        assert not v.ok and v.rule == "arguments", bad
    assert up(max_cost=0).data["max_cost"] == 0
    assert up(max_cost=10).data["max_cost"] == 10
    assert up(max_cost=10_000).data["max_cost"] == 50          # never past the caps
    assert up().data["max_cost"] == 50
    assert "max_cost" not in up(quote=True).data               # a quote spends nothing
    assert up(quote=False, max_cost=5).data["max_cost"] == 5
    mesh = p.check("IbashoAgent", {"op": "uploadMesh", "path": str(life / "m.dae"), "max_cost": -3}, IN)
    assert not mesh.ok


def test_chat_channels_are_whole_numbers(pol):
    p, _, _ = pol
    say = lambda ch: p.check("LLChatBar", {"op": "sendChat", "message": "hi", "channel": ch}, IN)
    for bad in ("-5", "0", -5.5, 3.0, True, [1], 2**31, -2**31 - 1):
        v = say(bad)
        assert not v.ok and v.rule == "arguments", bad
    v = say(-5)
    assert v.ok and v.messages == 0 and v.data["channel"] == 0 and v.data["message"] == "/-5 hi"
    assert say(0).messages == 1 and say(7).messages == 0
    assert p.check("LLChatBar", {"op": "sendChat", "message": "hi"}, IN).messages == 1


def test_maturity_takes_only_known_values_up_to_the_cap(pol):
    p, _, _ = pol     # capped at moderate (21)
    st = lambda op, v: p.check("LLViewerControl", {"op": op, "group": "Global", "key": "PreferredMaturity",
                                                   "value": v}, IN)
    for bad in (-1, 0, 5, 20, 22, 42, "13", True, 13.5, None, [13]):
        v = st("set", bad)
        assert not v.ok and v.rule == "maturity", bad
    assert st("set", 13).ok and st("set", 21).ok
    assert not st("toggle", 13).ok
    assert p.check("LLViewerControl", {"op": "get", "key": "PreferredMaturity"}, IN).ok


# ---------- windows opened through menu functions ----------

def ui_call(p, fn, param):
    return p.check("UI", {"op": "call", "function": fn, "parameter": param}, IN)


@pytest.mark.parametrize("allow", [["UI call"], ["UI *"], ["UI call Floater.Show", "UI call Floater.Toggle"]])
def test_floater_functions_keep_the_window_rules(tmp_path, allow):
    p = Policy(Caps(), Ledger(tmp_path / "l.json"), allow=allow)
    for fn in ("Floater.Show", "Floater.Toggle"):
        for window in ("preferences", "upload_image", "bulk_upload", "web_content", "media_settings"):
            v = ui_call(p, fn, window)
            assert not v.ok and "LLFloaterReg showInstance" in v.reason, (fn, window)
        for window in ("buy_currency", "BUY_LAND", "buy_currency_html"):
            v = ui_call(p, fn, window)
            assert not v.ok and v.rule == "money", (fn, window)
        assert ui_call(p, fn, "inventory").ok and ui_call(p, fn, "camera").ok


def test_buying_through_a_floater_function_is_refused_even_when_named(tmp_path):
    p = Policy(Caps(), Ledger(tmp_path / "l.json"), allow=["UI call Floater.ShowOrBringToFront",
                                                          "LLFloaterReg showInstance preferences"])
    assert ui_call(p, "Floater.ShowOrBringToFront", "buy_currency").rule == "money"
    assert not ui_call(p, "Floater.ShowOrBringToFront", "preferences").ok    # the window itself needs LLFloaterReg
    assert p.check("LLFloaterReg", {"op": "showInstance", "name": "preferences"}, IN).ok


# ---------- the stock viewer's other accounts ----------

@pytest.mark.parametrize("name,folder", [
    ("Test Avatar", "test_avatar"), ("test.avatar", "test_avatar"), ("Test_Avatar", "test_avatar"),
    ("solo", "solo_resident"), ("Solo Resident", "solo_resident"), ("Jo Ann Smith", "jo_ann_smith"), ("", ""),
])
def test_account_folders_are_named_as_the_viewer_names_them(name, folder):
    assert config_mod.account_folder(name) == folder


def test_stock_viewer_leaves_other_accounts_alone(home):
    w = World(write_config(home, viewer="C:/Viewer/SecondLifeViewer.exe"), ibasho=False)
    try:
        sl = home / "appdata" / "SecondLife"
        mine, theirs = sl / "test_avatar", sl / "person_example"
        for d in (mine, theirs):
            d.mkdir(parents=True)
            (d / "ann_example.txt").write_text("[2020/01/01 10:00] Ann Example: an old line\n", encoding="utf-8")
            (d / "chat.txt").write_text("[2020/01/01 10:00] Ann Example: hello\n", encoding="utf-8")
        w.connect()
        time.sleep(1.5)  # the tailer has seen the files once
        seq = w.api.seq()
        today = time.strftime("%Y/%m/%d")
        for d in (theirs, mine):
            with (d / "ann_example.txt").open("a", encoding="utf-8") as f:
                f.write(f"[{today} 10:01] Ann Example: new line in {d.name}\n")
        w.until(lambda: w.api.events(seq, kinds=["im_transcript"]))
        time.sleep(1.5)
        lines = [e["data"]["line"] for e in w.api.events(seq, kinds=["im_transcript"])]
        assert lines == [f"[{today} 10:01] Ann Example: new line in test_avatar"]
        w.daemon.prune()    # the 2020 lines are past retention: gone from mine only
        assert "old line" not in (mine / "ann_example.txt").read_text(encoding="utf-8")
        assert not (mine / "chat.txt").exists()
        assert "old line" in (theirs / "ann_example.txt").read_text(encoding="utf-8")
        assert (theirs / "chat.txt").exists()
        r = w.api.http("POST", "/forget", {"ids": [], "names": ["Ann Example"]})
        assert r["transcripts_removed"] == 1 and not (mine / "ann_example.txt").exists()
        assert (theirs / "ann_example.txt").exists()
    finally:
        w.close()


# ---------- the hearing model, in a session ----------

@pytest.mark.parametrize("model", ["C:/models/mine", "Someone/whisper-x", "large-v3", "turbo", "../x"])
def test_hear_model_in_a_session_is_a_known_small_one(home, capsys, monkeypatch, model):
    cfg = write_config(home)
    monkeypatch.setenv("IBASHO_SESSION", "s1")
    for cmd in ("on", "run"):
        code, _, err = run(cfg.path, capsys, "hear", cmd, "--model", model)
        assert code == 1 and "refused in the agent's sessions" in err


def test_hear_model_in_a_session_known_names_pass_the_gate(home, capsys, monkeypatch):
    cfg = write_config(home)
    monkeypatch.setenv("IBASHO_SESSION", "s1")
    code, _, err = run(cfg.path, capsys, "hear", "on", "--model", "base.en")
    assert "refused in the agent's sessions" not in err and "hearing is off" in err


# ---------- a session keeps its config ----------

def test_a_session_keeps_the_config_it_started_with(home, capsys, monkeypatch, tmp_path):
    cfg = write_config(home)
    other_home = tmp_path / "other"
    other_home.mkdir()
    other = write_config(other_home)
    monkeypatch.setenv("IBASHO_SESSION", "s-keep")
    monkeypatch.setenv("IBASHO_CONFIG", str(cfg.path))
    assert main(["notes"]) == 0
    monkeypatch.setenv("IBASHO_CONFIG", str(other.path))      # changed inside the session
    capsys.readouterr()
    assert main(["notes"]) == 1 and "this session runs with the config" in capsys.readouterr().err
    monkeypatch.delenv("IBASHO_CONFIG")
    monkeypatch.setenv("IBASHO_HOME", str(other_home))        # the same through IBASHO_HOME
    assert main(["notes"]) == 1
    monkeypatch.setenv("IBASHO_CONFIG", str(cfg.path))
    assert main(["notes"]) == 0
    monkeypatch.setenv("IBASHO_SESSION", "s-other")             # another session binds on its first use
    monkeypatch.setenv("IBASHO_CONFIG", str(other.path))
    assert main(["notes"]) == 0


def test_the_keeper_binds_its_sessions(home, tmp_path):
    cfg = write_config(home)
    config_mod.bind_session("keeper-sid", cfg.path)
    assert config_mod.session_config_problem("keeper-sid", cfg.path) is None
    assert config_mod.session_config_problem("keeper-sid", tmp_path / "x" / "config.toml")


# ---------- uploads read from checked copies ----------

def junction(link: Path, target: Path):
    _winapi.CreateJunction(str(target), str(link))


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows")


@windows_only
def test_a_folder_swapped_for_a_junction_after_the_check_isnt_read(pol, tmp_path):
    p, shots, life = pol
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "photo.png").write_bytes(b"private")
    (life / "art").mkdir()
    (life / "art" / "photo.png").write_bytes(b"mine")
    data = {"op": "upload", "path": str(life / "art" / "photo.png")}
    v = p.check("IbashoAgent", data, IN)
    assert v.ok
    # after the check, the folder becomes a junction to somewhere else
    (life / "art" / "photo.png").unlink()
    (life / "art").rmdir()
    junction(life / "art", outside)
    with pytest.raises(StageError, match="outside the folders"):
        stage_upload("upload", v.data, [life, shots], tmp_path / "stage")


@windows_only
def test_staged_copies_are_what_was_checked(pol, tmp_path):
    p, shots, life = pol
    (life / "art").mkdir()
    (life / "art" / "sign.png").write_bytes(b"png")
    out = stage_upload("upload", {"op": "upload", "path": str(life / "art" / "sign.png"), "max_cost": 5},
                       [life, shots], tmp_path / "stage")
    staged = Path(out["path"])
    assert staged.read_bytes() == b"png" and staged.name == "sign.png" and out["max_cost"] == 5
    assert config_mod.inside(staged, tmp_path / "stage")
    # a model and its files, beside it as the viewer will look for them
    (life / "m").mkdir()
    (life / "m" / "tex").mkdir()
    (life / "m" / "chair.gltf").write_text(json.dumps({"buffers": [{"uri": "chair.bin"}],
                                                        "images": [{"uri": "tex/wood%20grain.png"}]}))
    (life / "m" / "chair.bin").write_bytes(b"bin")
    (life / "m" / "tex" / "wood grain.png").write_bytes(b"img")
    (life / "m" / "chair_low.gltf").write_text(json.dumps({"buffers": [{"uri": "chair.bin"}]}))
    data = {"op": "uploadMesh", "path": str(life / "m" / "chair.gltf"), "textures": True,
            "lod_low": str(life / "m" / "chair_low.gltf")}
    assert p.check("IbashoAgent", data, IN).ok
    out = stage_upload("uploadMesh", data, [life, shots], tmp_path / "stage")
    model = Path(out["path"])
    assert (model.parent / "chair.bin").read_bytes() == b"bin"
    assert (model.parent / "tex" / "wood grain.png").read_bytes() == b"img"
    assert Path(out["lod_low"]).parent == model.parent
    # rewritten after the check to name a file elsewhere: the copy is made from what's read now, and refused
    (life / "m" / "chair.gltf").write_text(json.dumps({"buffers": [{"uri": str(tmp_path / "secret.bin")}]}))
    with pytest.raises(StageError, match="relative"):
        stage_upload("uploadMesh", data, [life, shots], tmp_path / "stage")


def test_models_name_their_files_relative_to_them(pol):
    p, shots, life = pol
    (life / "abs.gltf").write_text(json.dumps({"buffers": [{"uri": str(life / "a.bin")}]}))
    v = p.check("IbashoAgent", {"op": "uploadMesh", "path": str(life / "abs.gltf")}, IN)
    assert not v.ok and "relative" in v.reason


def test_the_daemon_hands_the_viewer_copies(home):
    w = World(write_config(home))
    try:
        w.viewer.handlers[("IbashoAgent", "upload")] = lambda d: {"cost": 0, "charged": False, "item_id": "x"}
        w.connect()
        life = home / "life"
        life.mkdir(exist_ok=True)
        (life / "sign.png").write_bytes(b"png")
        w.api.cmd("IbashoAgent", "upload", {"path": str(life / "sign.png")})
        sent = w.viewer.sent("IbashoAgent", "upload")[-1]
        assert Path(sent["path"]).read_bytes() == b"png"
        assert config_mod.inside(sent["path"], w.cfg.state_dir / "uploads")
        assert sent["max_cost"] == w.cfg.caps.max_upload_cost
        from ibasho.api import ApiError
        with pytest.raises(ApiError):
            w.api.cmd("IbashoAgent", "upload", {"path": str(life / "missing.png")})
    finally:
        w.close()


# ---------- installing and updating the viewer ----------

def release_zip(where: Path, version: str, exe: str = "IbashoViewer.exe", channel: str = "Ibasho",
                extra: dict | None = None) -> Path:
    top = f"Ibasho-{version}-windows-x64"
    z = where / f"{top}.zip"
    files = {
        "build_data.json": json.dumps({"Version": version, "Channel": channel, "Executable": exe}),
        exe: f"exe {version}",
        "README.txt": f"Ibasho {version}: a modified version of Linden Lab's viewer\r\n",
        "skins/default.xml": "<x/>",
        **(extra or {}),
    }
    with zipfile.ZipFile(z, "w") as f:
        for name, text in files.items():
            f.writestr(name if name.startswith(("/", "..")) or ":" in name else f"{top}/{name}", text)
    return z


@pytest.fixture
def installed(home, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(viewer_cmd, "running_from", lambda folder: [])
    cfg = write_config(home)
    dl = tmp_path / "downloads"
    dl.mkdir()
    code, out, err = run(cfg.path, capsys, "viewer", "install", str(release_zip(dl, "0.1.0.25")))
    assert code == 0, err
    return cfg, dl, home / "viewer"


def test_install_unpacks_and_points_the_config(installed):
    cfg, dl, folder = installed
    assert (folder / "IbashoViewer.exe").read_text() == "exe 0.1.0.25"
    new = config_mod.load(cfg.path)
    assert Path(new.viewer_path) == folder / "IbashoViewer.exe"
    assert "[caps]" in cfg.path.read_text(encoding="utf-8")       # the rest of the file as it was
    assert not [p for p in folder.parent.iterdir() if "unpacking" in p.name]


def test_update_goes_into_the_same_folder_and_keeps_the_old_one_until_it_starts(installed, capsys):
    cfg, dl, folder = installed
    code, out, err = run(cfg.path, capsys, "viewer", "update", str(release_zip(dl, "0.1.0.26")))
    assert code == 0, err
    assert (folder / "IbashoViewer.exe").read_text() == "exe 0.1.0.26"
    previous = folder.with_name("viewer.previous")
    assert (previous / "IbashoViewer.exe").read_text() == "exe 0.1.0.25"
    assert "firewall" not in out                                   # same path: nothing to ask
    assert Path(config_mod.load(cfg.path).viewer_path) == folder / "IbashoViewer.exe"
    # another update before it ever started: the kept copy stays the one from before
    assert run(cfg.path, capsys, "viewer", "update", str(release_zip(dl, "0.1.0.27")))[0] == 0
    assert (previous / "IbashoViewer.exe").read_text() == "exe 0.1.0.25"
    # rollback, then forward again, then the new one starts
    assert run(cfg.path, capsys, "viewer", "rollback")[0] == 0
    assert (folder / "IbashoViewer.exe").read_text() == "exe 0.1.0.25" and not previous.exists()
    assert run(cfg.path, capsys, "viewer", "update", str(release_zip(dl, "0.1.0.27")))[0] == 0
    line = viewer_cmd.finish_update(config_mod.load(cfg.path))
    assert "deleted" in line and not previous.exists()
    assert (folder / "IbashoViewer.exe").read_text() == "exe 0.1.0.27"


def test_update_refuses_what_isnt_a_newer_ibasho_viewer(installed, capsys):
    cfg, dl, folder = installed
    for d in "abcd":
        (dl / d).mkdir()
    bad = [
        release_zip(dl / "a", "0.1.0.25"),                                       # the same version
        release_zip(dl / "b", "0.1.0.20"),                                       # older
        release_zip(dl / "c", "0.1.0.30", channel="Release"),                    # not an Ibasho build
        release_zip(dl / "d", "0.1.0.30", extra={"sub/../../escape.txt": "x"}),  # a file outside its folder
    ]
    for z in bad:
        code, _, err = run(cfg.path, capsys, "viewer", "update", str(z))
        assert code == 1, z
    assert (folder / "IbashoViewer.exe").read_text() == "exe 0.1.0.25"
    assert not (dl / "escape.txt").exists() and not folder.with_name("viewer.previous").exists()
    assert run(cfg.path, capsys, "viewer", "update", str(bad[0]), "--force")[0] == 0


def test_update_refuses_while_the_viewer_runs(installed, capsys, monkeypatch):
    cfg, dl, folder = installed
    monkeypatch.setattr(viewer_cmd, "running_from", lambda f: ["IbashoViewer.exe (pid 1)"])
    code, _, err = run(cfg.path, capsys, "viewer", "update", str(release_zip(dl, "0.1.0.26")))
    assert code == 1 and "running" in err
    assert (folder / "IbashoViewer.exe").read_text() == "exe 0.1.0.25"


def test_a_new_exe_name_updates_the_config_and_warns_of_the_firewall(installed, capsys):
    cfg, dl, folder = installed
    code, out, _ = run(cfg.path, capsys, "viewer", "update", str(release_zip(dl, "0.1.0.26", exe="Ibasho.exe")))
    assert code == 0 and "firewall" in out and "Tell the person" in out
    assert Path(config_mod.load(cfg.path).viewer_path) == folder / "Ibasho.exe"


def test_viewer_commands_are_the_persons(installed, capsys, monkeypatch):
    cfg, dl, folder = installed
    monkeypatch.setenv("IBASHO_SESSION", "s1")
    code, _, err = run(cfg.path, capsys, "viewer", "update", str(release_zip(dl, "0.1.0.26")))
    assert code == 1 and "refused in the agent's sessions" in err


def test_set_viewer_path_keeps_the_rest(tmp_path):
    p = config_mod.write_template(tmp_path / "config.toml", "Jo Example", viewer="C:/old/IbashoViewer.exe")
    before = p.read_text(encoding="utf-8")
    config_mod.set_viewer_path(p, Path("D:/it's here/IbashoViewer.exe"))
    after = p.read_text(encoding="utf-8")
    assert config_mod.load(p).viewer_path == str(Path("D:/it's here/IbashoViewer.exe"))
    assert [ln for ln in before.splitlines() if not ln.startswith("path =")] == \
        [ln for ln in after.splitlines() if not ln.startswith("path =")]


# ---------- what the review after the pass found ----------

def test_background_ibasho_never_imports_from_the_callers_folder(tmp_path, monkeypatch):
    from ibasho import procs
    args = procs.ibasho_args("daemon")
    assert args[1:4] == ["-P", "-m", "ibasho"]
    seen = {}
    monkeypatch.setattr(procs.subprocess, "Popen", lambda a, **kw: seen.update(kw) or type("P", (), {"pid": 1})())
    procs.spawn(["x"], tmp_path / "state" / "x.log") if (tmp_path / "state").mkdir() is None else None
    assert Path(seen["cwd"]) == tmp_path / "state"


DAE = ('<?xml version="1.0"?><COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema"><library_images>'
       '<image id="i">{}</image></library_images><library_effects><effect id="e"><profile_COMMON><newparam sid="s">'
       '<surface type="2D"><init_from>i</init_from></surface></newparam></profile_COMMON></effect>'
       '</library_effects></COLLADA>')


@pytest.mark.parametrize("init,ok", [
    ("<init_from>tex/wood.png</init_from>", True),
    ("<init_from><![CDATA[C:/Users/p/Pictures/me.png]]></init_from>", False),
    ("<init_from >file:///C:/Users/p/me.png</init_from >", False),
    ("<init_from>C&#58;/Users/p/me.png</init_from>", False),
    ("<init_from>%252E%252E/x.png</init_from>", False),
    ("<init_from>wood.png:hidden</init_from>", False),
    ("<init_from><ref>../me.png</ref></init_from>", False),
])
def test_collada_images_are_read_as_xml(pol, init, ok):
    p, shots, life = pol
    (life / "m.dae").write_text(DAE.format(init), encoding="utf-8")
    assert p.check("IbashoAgent", {"op": "uploadMesh", "path": str(life / "m.dae")}, IN).ok is ok, init


def test_a_doctype_makes_a_model_unreadable(pol):
    p, shots, life = pol
    (life / "m.dae").write_text('<!DOCTYPE x [<!ENTITY a "C:/secret.png">]>' + DAE.format(
        "<init_from>&a;</init_from>"), encoding="utf-8")
    v = p.check("IbashoAgent", {"op": "uploadMesh", "path": str(life / "m.dae")}, IN)
    assert not v.ok and "couldn't read" in v.reason


@windows_only
def test_staging_says_textures_outright_and_skips_missing_files(pol, tmp_path):
    p, shots, life = pol
    (life / "m.dae").write_text(DAE.format("<init_from>tex/gone.png</init_from>"), encoding="utf-8")
    out = stage_upload("uploadMesh", {"op": "uploadMesh", "path": str(life / "m.dae")}, [life, shots], tmp_path / "s")
    assert out["textures"] is False and not (Path(out["path"]).parent / "tex" / "gone.png").exists()


@windows_only
def test_a_model_copied_twice_must_read_the_same(pol, tmp_path, monkeypatch):
    p, shots, life = pol
    (life / "m.gltf").write_text(json.dumps({"buffers": [{"uri": "m.gltf"}]}))
    reads = iter([b'{"buffers": [{"uri": "m.gltf"}]}', b'{"buffers": [{"uri": "C:/x.bin"}]}'])
    real_open = open

    class Swapped:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.f.close()

        def fileno(self):
            return self.f.fileno()

        def read(self):
            return next(reads)
    monkeypatch.setattr("builtins.open", lambda path, mode="r", *a, **k:
                        Swapped(real_open(path, mode)) if mode == "rb" else real_open(path, mode, *a, **k))
    with pytest.raises(StageError, match="changed"):
        stage_upload("uploadMesh", {"op": "uploadMesh", "path": str(life / "m.gltf")}, [life, shots],
                     tmp_path / "s")


@pytest.mark.parametrize("name", ["../x", "a/b", "c:\\x", "..", "x\x00y"])
def test_odd_account_names_name_no_folder(name):
    assert config_mod.account_folder(name) == ""


def test_finish_update_leaves_another_viewers_copy(installed, capsys):
    cfg, dl, folder = installed
    assert run(cfg.path, capsys, "viewer", "update", str(release_zip(dl, "0.1.0.26")))[0] == 0
    elsewhere = folder.parent / "other" / "IbashoViewer.exe"
    elsewhere.parent.mkdir()
    elsewhere.write_text("x")
    config_mod.set_viewer_path(cfg.path, elsewhere)
    assert viewer_cmd.finish_update(config_mod.load(cfg.path)) is None
    assert folder.with_name("viewer.previous").is_dir()


def test_set_viewer_path_with_a_commented_header(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[agent]\nname = "Jo"\n[viewer]   # the viewer\n"path" = \'C:/a/IbashoViewer.exe\'\n', encoding="utf-8")
    config_mod.set_viewer_path(p, Path("C:/b/IbashoViewer.exe"))
    assert config_mod.load(p).viewer_path == str(Path("C:/b/IbashoViewer.exe"))
    assert p.read_text(encoding="utf-8").count("path") == 1
