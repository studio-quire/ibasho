"""Pure pieces: LEAP framing, config, policy, event log, geometry, probe parsing."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import time

import pytest

from ibasho import config as config_mod, geometry as geo, probe
from ibasho.config import Caps
from ibasho.eventlog import EventLog, involves
from ibasho.leap import parse, read_frame, frame, encode
from ibasho.policy import Ledger, Policy, RateLimiter, ViewerState

from conftest import write_config
from fake_viewer import viewer_frame


# ---------- LEAP ----------

def test_viewer_binary_frame_round_trips():
    raw = viewer_frame("p", {"op": "x", "n": 3, "f": 1.5, "l": [1, None, "é"]})

    async def go():
        r = asyncio.StreamReader()
        r.feed_data(raw + encode("q", {"a": True}))
        r.feed_eof()
        return parse(await read_frame(r)), parse(await read_frame(r))
    a, b = asyncio.run(go())
    assert a == {"pump": "p", "data": {"op": "x", "n": 3, "f": 1.5, "l": [1, None, "é"]}}
    assert b == {"pump": "q", "data": {"a": True}}


def test_frame_length_is_bytes():
    assert frame("é".encode()) == b"2:\xc3\xa9"


# ---------- config ----------

def test_template_needs_a_name(tmp_path):
    p = config_mod.write_template(tmp_path / "config.toml")
    with pytest.raises(config_mod.ConfigError, match="agent.name"):
        config_mod.load(p)


def test_template_loads_with_defaults(tmp_path):
    p = config_mod.write_template(tmp_path / "config.toml", name="Example Resident", viewer="C:/x/viewer.exe")
    c = config_mod.load(p)
    assert c.linden_grid and not c.scripted_agent_flag_confirmed
    assert c.caps.messages_per_day == 3000 and c.caps.maturity == "moderate"
    assert c.name_words() == ["Example"]
    assert c.grid_arg == "util.agni.lindenlab.com"
    assert c.data_dir == tmp_path / "data"


def test_message_cap_must_leave_a_margin(home):
    with pytest.raises(config_mod.ConfigError, match="5,000"):
        write_config(home, messages_per_day=5000)


def test_viewer_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))

    def cfg(**kw):
        return config_mod.Config(path=tmp_path / "config.toml", name="A", **kw)

    ibasho = cfg(viewer_path="C:/Ibasho/IbashoViewer.exe")
    assert ibasho.ibasho_viewer and ibasho.viewer_dirs() == [tmp_path / "Ibasho"]
    assert ibasho.saved_login_file() == tmp_path / "Ibasho" / "user_settings" / "bin_conf.dat"
    stock = cfg(viewer_path="C:/LL/SecondLifeViewer.exe")
    assert not stock.ibasho_viewer and stock.viewer_dirs() == [tmp_path / "SecondLife"]
    assert stock.saved_login_file() is None          # another viewer's folder could be anywhere
    own = cfg(viewer_path="C:/Other/Viewer.exe", viewer_user_dir=str(tmp_path / "Other"))
    assert own.saved_login_file() == tmp_path / "Other" / "user_settings" / "bin_conf.dat"


def test_other_grid_goes_into_the_viewers_grids_xml(tmp_path, monkeypatch):
    import llsd
    monkeypatch.setenv("APPDATA", str(tmp_path))
    p = config_mod.write_template(tmp_path / "config.toml", name="A", grid="http://127.0.0.1:9000/",
                                  viewer="C:/Ibasho/IbashoViewer.exe")
    c = config_mod.load(p)
    assert c.grid == "127.0.0.1-9000" and c.login_uri == "http://127.0.0.1:9000/" and not c.linden_grid
    grids = c.grids_file()
    grids.parent.mkdir(parents=True)
    grids.write_bytes(llsd.format_xml({"other": {"keyname": "other", "login_uri": ["http://x/"]}}))
    assert config_mod.viewer_grid_problem(c) is None
    got = llsd.parse_xml(grids.read_bytes())
    assert got["127.0.0.1-9000"]["login_uri"] == ["http://127.0.0.1:9000/"] and "other" in got   # others kept
    c.login_uri = ""
    assert config_mod.viewer_grid_problem(c) is None          # known now
    c.grid = "elsewhere"
    assert "main Second Life grid" in config_mod.viewer_grid_problem(c)   # unknown: the viewer would go there


def test_grid_name_without_colon(home):
    with pytest.raises(config_mod.ConfigError, match="no ':'"):
        write_config(home, grid="localhost:9000")


@pytest.mark.parametrize("grid,linden",[("secondlife", True), ("aditi", True), ("util.agni.lindenlab.com", True),
                                         ("localhost:9000", False), ("127.0.0.1:9400", False)])
def test_linden_grids(grid, linden):
    assert config_mod.is_linden_grid(grid) is linden


# ---------- policy ----------

def make_policy(tmp_path, **caps):
    c = Caps(**caps)
    clock = [0.0]
    lim = RateLimiter(c.messages_per_minute, clock=lambda: clock[0])
    return Policy(c, Ledger(tmp_path / "ledger.json"), lim), clock


IN = ViewerState(logged_in=True, notifications={})


def test_chat_counts_and_rate_limits(tmp_path):
    pol, clock = make_policy(tmp_path, messages_per_minute=6)
    say = {"op": "sendChat", "message": "hi", "channel": 0}
    for _ in range(5):                      # the burst
        v = pol.check("LLChatBar", say, IN)
        assert v.ok and v.messages == 1
        pol.sent(v)
    v = pol.check("LLChatBar", say, IN)
    assert not v.ok and v.status == 429 and 0 < v.retry_after <= 10
    clock[0] += 10                           # 6/min refills one per 10 s
    assert pol.check("LLChatBar", say, IN).ok
    assert pol.ledger.messages == 5
    assert pol.check("LLChatBar", {"op": "sendChat", "message": "  "}, IN).messages == 0


def test_daily_message_cap(tmp_path):
    pol, clock = make_policy(tmp_path, messages_per_day=3, messages_per_minute=600)
    im = {"op": "send", "to": "x", "message": "hello"}
    for _ in range(3):
        pol.sent(pol.check("IbashoIM", im, IN))
    v = pol.check("IbashoIM", im, IN)
    assert not v.ok and "daily message cap" in v.reason
    # shared across restarts: a new Ledger on the same file still knows
    assert Ledger(tmp_path / "ledger.json").messages == 3


@pytest.mark.parametrize("data,counted", [
    ({"message": "hi"}, True),
    ({"message": "hi", "channel": 0}, True),
    ({"message": "/0 hi"}, True),
    ({"message": "/me waves"}, True),
    ({"message": "lights on", "channel": 7}, False),
    ({"message": "/7 lights on"}, False),
    ({"message": "/42on"}, False),                  # the chat bar reads "/42on" as channel 42 too
    ({"message": "menu", "channel": -9}, False),    # sent as "/-9 menu"
    ({"message": "//again"}, True),                 # whichever channel was last: counted, to be safe
])
def test_chat_to_scripts_isnt_a_message(tmp_path, data, counted):
    # as the Ibasho viewer counts its IbashoMaxMessagesPerDay: local chat (channel 0) and IMs
    pol, _ = make_policy(tmp_path)
    v = pol.check("LLChatBar", dict(data, op="sendChat"), IN)
    assert v.ok and v.messages == (1 if counted else 0)


def test_the_viewers_own_switches_stay_off(tmp_path):
    pol, _ = make_policy(tmp_path, messages_per_day=3000)

    def setting(key, value):
        return pol.check("LLViewerControl", {"op": "set", "group": "Global", "key": key, "value": value}, IN).ok

    assert not setting("IbashoAllowLandBuy", True) and not setting("IbashoAllowCurrencyBuy", 1)
    assert setting("IbashoAllowLandBuy", False)
    assert not pol.check("LLViewerControl", {"op": "toggle", "group": "Global", "key": "IbashoAllowLandBuy"}, IN).ok
    assert setting("IbashoMaxMessagesPerDay", 2000)
    assert not setting("IbashoMaxMessagesPerDay", 0) and not setting("IbashoMaxMessagesPerDay", 4000)


def test_ledger_takes_the_viewers_higher_count(tmp_path):
    from ibasho.policy import slt_day
    led = Ledger(tmp_path / "ledger.json")
    led.add_messages(5)
    led.sync_messages(slt_day(), 3)
    assert led.messages == 5
    led.sync_messages("2000-01-01", 99)    # another day's count
    assert led.messages == 5
    led.sync_messages(slt_day(), 12)
    assert Ledger(tmp_path / "ledger.json").messages == 12


def test_payment_caps(tmp_path):
    pol, _ = make_policy(tmp_path, max_payment=100, max_session_spend=150, max_daily_spend=1000)
    assert not pol.check("IbashoAgent", {"op": "pay", "to": "a", "amount": 101}, IN).ok
    v = pol.check("IbashoAgent", {"op": "pay", "to": "a", "amount": 100}, IN)
    assert v.ok and v.spend == 100
    pol.charged(v.spend)
    v = pol.check("IbashoAgent", {"op": "pay", "to": "a", "amount": 60}, IN)
    assert not v.ok and "this login" in v.reason
    pol.ledger.new_session("next login")
    assert pol.check("IbashoObjects", {"op": "buy", "id": "o", "price": 60}, IN).ok
    assert not pol.check("IbashoAgent", {"op": "pay", "to": "a", "amount": -5}, IN).ok


def test_daily_spend_cap(tmp_path):
    pol, _ = make_policy(tmp_path, max_payment=100, max_session_spend=1000, max_daily_spend=150)
    pol.charged(100)
    pol.ledger.new_session("another login")
    v = pol.check("IbashoAgent", {"op": "pay", "to": "a", "amount": 60}, IN)
    assert not v.ok and "today" in v.reason


def test_uploads_get_a_max_cost(tmp_path):
    pol, _ = make_policy(tmp_path, max_upload_cost=50, max_payment=100)
    pol.readable = [tmp_path]
    png = str(tmp_path / "x.png")
    v = pol.check("IbashoAgent", {"op": "upload", "path": png}, IN)
    assert v.ok and v.data["max_cost"] == 50
    v = pol.check("IbashoAgent", {"op": "upload", "path": png, "max_cost": 10}, IN)
    assert v.data["max_cost"] == 10
    assert "max_cost" not in pol.check("IbashoAgent", {"op": "upload", "path": png, "quote": True}, IN).data


@pytest.mark.parametrize("pump,data", [
    ("UI", {"op": "call", "function": "BuyCurrency"}),
    ("UI", {"op": "call", "function": "Land.Buy"}),
    ("LLFloaterReg", {"op": "showInstance", "name": "buy_currency"}),
    ("LLURLDispatcher", {"op": "dispatch", "url": "secondlife:///app/buycurrencyhtml"}),
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "PreferredMaturity", "value": 42}),
    ("LLViewerControl", {"op": "set", "group": "Global", "key": "IbashoMaxPayment", "value": 100000}),
    ("LLViewerControl", {"op": "get", "group": "Global", "key": "UserLoginInfo"}),
])
def test_refused_outright(tmp_path, pump, data):
    pol, _ = make_policy(tmp_path)
    assert not pol.check(pump, data, IN).ok


def test_maturity_can_go_down(tmp_path):
    pol, _ = make_policy(tmp_path)
    assert pol.check("LLViewerControl", {"op": "set", "group": "Global", "key": "PreferredMaturity", "value": 13},
                     IN).ok


def test_nothing_but_quitting_before_login_or_behind_the_gate(tmp_path):
    pol, _ = make_policy(tmp_path)
    out = ViewerState(logged_in=False, notifications={})
    assert not pol.check("UI", {"op": "getValue", "path": "/x/password_edit"}, out).ok
    assert not pol.check("LLWindow", {"op": "keyDown", "char": "a"}, out).ok
    assert pol.check("LLViewerControl", {"op": "get", "key": "CurrentGrid"}, out).ok
    assert pol.check("LLAppViewer", {"op": "requestQuit"}, out).ok
    gated = ViewerState(logged_in=True, gate="not flagged", notifications={})
    assert not pol.check("LLAgent", {"op": "getPosition"}, gated).ok
    assert pol.check("LLAppViewer", {"op": "forceQuit"}, gated).ok


def note(name, message, *buttons):
    return {"name": name, "message": message,
            "form": [{"type": "button", "name": b, "text": b} for b in buttons]}


def test_notification_answers(tmp_path):
    pol, _ = make_policy(tmp_path, max_payment=100)
    notes = {
        "q1": note("ScriptQuestion", "'Vendor', an object owned by 'X', would like to: Take Linden dollars (L$) from you",
                   "Yes", "No"),
        "q2": note("ScriptQuestion", "'Dance ball' would like to: Animate your avatar", "Yes", "No"),
        "mfa": note("PromptMFAToken", "Enter your code", "Continue"),
        "pass": note("LandBuyPass", "Buy a pass for L$25 to enter 'Garden' for 1 hours?", "OK", "Cancel"),
        "dear": note("BuyOriginal", "Buy 'Statue' from X for L$250?", "Buy", "Cancel"),
        "cur": note("PromptGoToCurrencyPage", "Buy L$?", "OK", "Cancel"),
        "dlg": {"name": "ScriptDialog", "message": "Pick", "form": [{"type": "button", "name": "Red", "text": "Red"}]},
    }
    st = ViewerState(logged_in=True, notifications=notes)

    def answer(i, btn):
        return pol.check("LLNotifications", {"op": "respond", "uuid": i, "response": {btn: True}}, st)

    assert not answer("q1", "Yes").ok
    assert answer("q1", "No").ok
    assert answer("q2", "Yes").ok
    assert not answer("mfa", "Continue").ok
    v = answer("pass", "OK")
    assert v.ok and v.spend == 25
    assert not answer("dear", "Buy").ok
    assert not answer("cur", "OK").ok
    assert answer("cur", "Cancel").ok
    assert answer("dlg", "Red").ok


# ---------- event log ----------

def test_eventlog_keeps_seq_across_restarts(tmp_path):
    log = EventLog(tmp_path)
    for i in range(3):
        log.append("chat", {"message": str(i)})
    again = EventLog(tmp_path)
    assert again.seq == 3
    assert [e["data"]["message"] for e in again.since(1)] == ["1", "2"]
    assert again.append("chat", {})["seq"] == 4


def test_eventlog_retention(tmp_path):
    log = EventLog(tmp_path, retention_days=7)
    old = time.time() - 10 * 86400
    log.append("chat", {"message": "old"}, t=old)
    log.append("chat", {"message": "new"})
    assert len(log.files()) == 2
    assert log.prune() == 1
    assert [e["data"]["message"] for e in log.since(0)] == ["new"]
    assert [e["data"]["message"] for e in EventLog(tmp_path).since(0)] == ["new"]


def test_forget_one_person(tmp_path):
    log = EventLog(tmp_path)
    them, other = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002"
    log.append("chat", {"from": "Ann Example", "from_id": them, "message": "hello"})
    log.append("chat", {"from": "Bo Sample", "from_id": other, "message": "hi"})
    log.append("im", {"dir": "out", "to_id": them, "session_type": "p2p", "message": "hey Ann"})
    log.append("im", {"dir": "in", "from": "ann.example", "from_id": them, "session_type": "p2p", "message": "x"})
    log.append("im_transcript", {"conversation": "ann.example", "line": "[12:00] Ann: x"})
    n = log.forget(involves([them], ["ann.example", "Ann Example"]))
    assert n == 4
    left = EventLog(tmp_path).since(0)
    assert [e["data"].get("from") for e in left] == ["Bo Sample"]
    assert [e["data"].get("from") for e in log.since(0)] == ["Bo Sample"]


# ---------- geometry ----------

def test_look_at_and_viewer_quaternion_agree():
    pos, focus = (10.0, 10.0, 30.0), (10.0, 30.0, 30.0)     # looking north
    a = geo.Camera.look_at(pos, focus, 1.0, 800, 600)
    # the viewer's camera looking north: rotate +x (east) by +90 degrees about z
    q = [0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)]
    b = geo.Camera.from_quat(pos, q, 1.0, 800, 600)
    for p in [(15.0, 40.0, 33.0), (5.0, 20.0, 28.0)]:
        pa, pb = a.project(p), b.project(p)
        assert pa and pb and abs(pa[0] - pb[0]) < 1e-6 and abs(pa[1] - pb[1]) < 1e-6
    x, y, _ = a.project((15.0, 40.0, 30.0))
    assert x > 400 and abs(y - 300) < 1e-6     # east of the line of sight shows on the right


def test_overhead_is_north_up():
    pos, focus = geo.overhead_pose(100, 100, 20, 40, 1.0)
    cam = geo.Camera.look_at(pos, focus, 1.0, 1000, 1000)
    nx, ny, _ = cam.project((100, 130, 20))
    ex, ey, _ = cam.project((130, 100, 20))
    assert ny < 500 and abs(nx - 500) < 2      # north is up
    assert ex > 500 and abs(ey - 500) < 2      # east is right


def test_detour_goes_round_someone_in_the_way():
    side = geo.detour((0, 0, 20), (0, 10), [(0.3, 5, 20)])
    assert side and abs(side[0]) > 1.5 and abs(side[1] - 5) < 0.5
    assert geo.detour((0, 0, 20), (0, 10), [(0, 9.5, 20)]) is None    # at the goal: walking to them
    assert geo.detour((0, 0, 20), (0, 10), [(0.3, 5, 30)]) is None    # another floor


def test_labels_skip_things_behind_and_far():
    cam = geo.Camera.look_at((0, 0, 0), (0, 10, 0), 1.0, 640, 480)
    items = [((0, 5, 0), "near", (1, 1, 1)), ((0, -5, 0), "behind", (1, 1, 1)), ((0, 50, 0), "far", (1, 1, 1))]
    assert [l.text.split()[0] for l in geo.place_labels(cam, items, 30)] == ["near"]


# ---------- probe ----------

def test_probe_parsing_and_drops():
    p = probe.parse_attachment("IBASHO|probe|21.50|1.00:0.02,2.00:-0.10,3.00:-2.40,4.00:X")
    assert p.feet == 21.5 and probe.first_drop(p) == 3.0
    assert probe.first_drop(probe.parse_attachment("IBASHO|probe|20|1:0,2:?,3:4.5")) is None
    assert probe._ground({"terrain": 20.0, "hit": 23.0, "water": 20.0}) == 23.0
    assert probe._ground({"terrain": 15.0, "hit": None, "water": 20.0}) == 18.5
    assert probe._ground({"terrain": 12.0, "hit": 21.0, "water": 20.0}) == 21.0   # a dock over the water
    assert probe._ground({"terrain": 25.0, "hit": 23.0, "water": 20.0}) == 25.0   # a buried prim under a hill
