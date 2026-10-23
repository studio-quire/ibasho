"""Holes found in review, kept shut."""

from __future__ import annotations

import time

from ibasho import config as config_mod
from ibasho.eventlog import EventLog, involves
from ibasho.policy import ViewerState

from conftest import write_config
from test_units import IN, make_policy, note

THEM = "aaaaaaaa-0000-0000-0000-000000000001"


def test_negative_channels_stay_off_public_chat(tmp_path):
    pol, _ = make_policy(tmp_path)
    v = pol.check("LLChatBar", {"op": "sendChat", "message": "open", "channel": -42}, IN)
    assert v.ok and v.data["channel"] == 0 and v.data["message"] == "/-42 open"
    v = pol.check("LLChatBar", {"op": "sendChat", "message": "hi"}, IN)
    assert v.data == {"op": "sendChat", "message": "hi"}


def test_notification_answers_fail_closed(tmp_path):
    pol, _ = make_policy(tmp_path, max_payment=100)
    st = ViewerState(logged_in=True, notifications={"dear": note("BuyCopy", "Buy 'Lamp' for L$500?", "Buy", "No")})
    assert not pol.check("LLNotifications", {"op": "respond", "uuid": "unseen", "response": {"OK": True}}, st).ok
    assert not pol.check("LLNotifications", {"op": "respond", "uuid": "dear", "response": {"Buy": 1}}, st).ok
    assert not pol.check("LLNotifications", {"op": "ignore", "name": "BuyCopy"}, st).ok
    assert not pol.check("LLNotifications", {"op": "forward", "channel": "Visible", "pump": "x"}, st).ok
    assert pol.check("LLNotifications", {"op": "cancel", "uuid": "unseen"}, st).ok


def test_login_pumps_and_own_listeners(tmp_path):
    pol, _ = make_policy(tmp_path)
    out = ViewerState(logged_in=False, notifications={})
    assert not pol.check("_command", {"op": "listen", "source": "LLXMLRPCTransaction", "listener": "x"}, out).ok
    assert not pol.check("_command", {"op": "listen", "source": "LLXMLRPCTransaction", "listener": "x"}, IN).ok
    assert pol.check("_command", {"op": "getAPIs"}, out).ok
    assert not pol.check("_command", {"op": "stoplistening", "source": "LLChat", "listener": "ibasho_LLChat"}, IN).ok
    assert not pol.check("LLViewerControl", {"op": "vars", "group": "Global"}, IN).ok
    assert not pol.check("LLViewerControl", {"op": "set", "group": "PerAccount", "key": "LogNearbyChat",
                                             "value": True}, IN).ok
    assert not pol.check("LLURLDispatcher", {"op": "dispatch", "url": "secondlife:///app/chat/5/hello"}, IN).ok


def test_pending_spend_counts(tmp_path):
    pol, _ = make_policy(tmp_path, max_payment=100, max_session_spend=150)
    v = pol.check("IbashoAgent", {"op": "pay", "to": "a", "amount": 100}, IN)
    pol.reserve(v.spend)
    assert not pol.check("IbashoAgent", {"op": "pay", "to": "b", "amount": 100}, IN).ok
    pol.release(v.spend)
    assert pol.check("IbashoAgent", {"op": "pay", "to": "b", "amount": 100}, IN).ok


def test_dialog_answers_are_not_messages(tmp_path):
    # an answer is chat to a script on its channel, which the viewer's daily cap doesn't count either
    pol, _ = make_policy(tmp_path)
    st = ViewerState(logged_in=True, notifications={"g": note("ScriptDialogGroup", "Pick", "Red")})
    v = pol.check("LLNotifications", {"op": "respond", "uuid": "g", "response": {"Red": True}}, st)
    assert v.ok and v.messages == 0


def test_forget_reaches_into_notifications(tmp_path):
    log = EventLog(tmp_path)
    log.append("notify", {"name": "IMToast", "payload": {"from_id": THEM, "message": "a private thing"},
                          "substitutions": {"FROM": "Ann Example"}})
    log.append("notify", {"name": "OfferFriendship", "substitutions": {"NAME": "Ann Example", "MESSAGE": "hi"}})
    log.append("notify", {"name": "ScriptDialog", "substitutions": {"NAME": "Some Object"}})
    assert log.forget(involves([THEM], ["Ann Example"])) == 2
    assert [e["data"]["name"] for e in log.since(0)] == ["ScriptDialog"]


def test_seq_never_goes_back(tmp_path):
    log = EventLog(tmp_path, retention_days=1)
    log.append("chat", {}, t=time.time() - 5 * 86400)
    log.prune()
    assert EventLog(tmp_path, retention_days=1).append("chat", {})["seq"] == 2


def test_retention_keeps_exactly_n_days(tmp_path):
    log = EventLog(tmp_path, retention_days=2)
    now = time.time()
    for days in (0, 1, 2):
        log.append("chat", {"d": days}, t=now - days * 86400)
    log.prune(now)
    assert sorted(e["data"]["d"] for e in log.since(0)) == [0, 1]


def test_flag_checks_are_per_grid(home):
    cfg = write_config(home, grid="secondlife")
    config_mod.save_flag_check(cfg, "127.0.0.1:9400", {"flagged": False})
    assert config_mod.flag_check(cfg) == {}
    config_mod.save_flag_check(cfg, "util.agni.lindenlab.com", {"flagged": True})
    assert config_mod.flag_check(cfg)["flagged"] is True
