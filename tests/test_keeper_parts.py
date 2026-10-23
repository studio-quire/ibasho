"""The keeper's pieces around the state machine: reading Claude Code's stream, the commands, recall, the template."""

from __future__ import annotations

import datetime as dt
import io
import json
import threading
from pathlib import Path

import pytest

from ibasho.cli import main
from ibasho.keeper import runner
from ibasho.keeper.session import ClaudeSession, clean_env, watch_armed
from ibasho.keeper.store import Store
from ibasho.recall import recall


# ---------- the stream ----------

def bare_session(clock=lambda: 1000.0) -> ClaudeSession:
    s = object.__new__(ClaudeSession)
    s.id, s.now = "s1", clock
    s.started = s.last_activity = clock()
    s._lock = threading.Lock()
    s._pending = {}
    s._log = io.StringIO()
    s.turn_open, s.turn_ended_at, s.turns, s.tool_calls = False, None, 0, 0
    s.context, s.context_window, s.cost_usd, s.rate, s.error = 0, None, 0.0, None, None
    return s


def feed(s, *events):
    for e in events:
        s._on(e)


def assistant(*content, usage=None):
    return {"type": "assistant", "message": {"content": list(content), "usage": usage or {}}}


def test_stream_turns_context_and_tools():
    s = bare_session()
    feed(s, {"type": "system", "subtype": "init"})
    assert s.turn_open
    feed(s, assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ibasho look"}},
                      usage={"input_tokens": 2, "cache_read_input_tokens": 40_000, "cache_creation_input_tokens": 500,
                             "output_tokens": 20}))
    assert s.context == 40_522 and s.pending_tool()[0] == "Bash" and s.tool_calls == 1
    feed(s, {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
    assert s.pending_tool() is None
    feed(s, {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {}}})
    assert s.rate["status"] == "allowed"
    feed(s, assistant({"type": "text", "text": "hello"}),
         {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.12,
          "modelUsage": {"some-model": {"contextWindow": 1_000_000}}})
    assert not s.turn_open and s.turns == 1 and s.context_window == 1_000_000 and s.cost_usd == 0.12
    assert s.error is None and "said: hello" in s._log.getvalue()


def test_stream_errors_and_subagents():
    s = bare_session()
    feed(s, assistant({"type": "tool_use", "id": "a1", "name": "Agent", "input": {}}))
    assert s.long_running() and s.pending_tool() is None
    feed(s, {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "API Error: usage limit"})
    assert s.error and "usage limit" in s.error and not s.long_running()


class FakeProc:
    def __init__(self, args):
        self.args = args

    def cmdline(self):
        return self.args


def test_watch_armed():
    root = FakeProc(["claude"])
    assert watch_armed([root, FakeProc(["bash", "-c", "x"]), FakeProc([r"C:\venv\Scripts\ibasho.exe", "watch"])])
    assert watch_armed([root, FakeProc(["python", "-m", "ibasho", "watch", "--exit-on-wake"])])
    assert not watch_armed([root, FakeProc(["ibasho", "look"]), FakeProc(["bash", "-c", "watch ibasho"])])
    assert not watch_armed([FakeProc(["ibasho", "watch"])])   # the session itself doesn't count


def test_clean_env_drops_nesting_markers_and_billing():
    base = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CONFIG_DIR": "x", "PATH": "p",
            "CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "anthropic_base_url": "u",
            "CLAUDE_CODE_USE_BEDROCK": "1", "AWS_BEARER_TOKEN_BEDROCK": "b", "CLAUDE_CODE_DISABLE_FEEDBACK": "1"}
    assert clean_env(base) == {"CLAUDE_CONFIG_DIR": "x", "PATH": "p", "CLAUDE_CODE_DISABLE_FEEDBACK": "1"}
    kept = clean_env(base, keep_auth=True)   # keeper.auth = "env"
    assert kept["ANTHROPIC_API_KEY"] == "k" and kept["CLAUDE_CODE_USE_BEDROCK"] == "1" and "CLAUDECODE" not in kept


def test_sessions_get_no_api_key_unless_asked(home, monkeypatch, tmp_path):
    from conftest import write_config
    from ibasho.keeper.settings import load
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-for-sessions")
    cfg = write_config(home)
    ks = load(cfg)
    env = runner.claude_env(cfg, ks)
    assert "ANTHROPIC_API_KEY" not in env and env["CLAUDE_CONFIG_DIR"] == str(home / "claude")
    ks.auth = "env"
    assert runner.claude_env(cfg, ks)["ANTHROPIC_API_KEY"] == "sk-not-for-sessions"


def test_launch_args_lock_down_settings_and_mcp(home, tmp_path, monkeypatch):
    from conftest import write_config
    from ibasho.keeper.settings import load
    seen = {}

    class Fake:
        def __init__(self, args, cwd, env, *rest):
            seen.update(args=args, env=env)
    monkeypatch.setattr(runner, "ClaudeSession", Fake)
    cfg = write_config(home)
    runner.make_launcher(cfg, load(cfg), tmp_path / "k", tmp_path / "life", "claude")("0" * 8)
    args = seen["args"]
    assert args[args.index("--setting-sources") + 1] == "user" and "--strict-mcp-config" in args
    mcp = json.loads(open(args[args.index("--mcp-config") + 1], encoding="utf-8").read())
    assert mcp == {"mcpServers": {}}


# ---------- the commands ----------

@pytest.fixture
def cfgpath(home, monkeypatch):
    from conftest import write_config
    monkeypatch.delenv("IBASHO_SESSION", raising=False)
    monkeypatch.delenv("IBASHO_CONFIG", raising=False)
    return write_config(home).path


def run(cfgpath, capsys, *args):
    code = main(["--config", str(cfgpath), *args])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_notes_both_ways(cfgpath, capsys, monkeypatch):
    assert run(cfgpath, capsys, "notes", "dinner's at seven")[0] == 0
    monkeypatch.setenv("IBASHO_SESSION", "abc")
    code, out, _ = run(cfgpath, capsys, "notes")
    assert "the person who runs Ibasho for you: dinner's at seven  [new]" in out
    run(cfgpath, capsys, "notes", "thanks, see you then")
    monkeypatch.delenv("IBASHO_SESSION")
    code, out, _ = run(cfgpath, capsys, "notes")
    assert "Test Avatar: thanks, see you then  [new]" in out and "me: dinner's at seven" in out
    code, out, _ = run(cfgpath, capsys, "notes")
    assert "[new]" not in out


def test_sleep_in_and_out_of_a_session(cfgpath, capsys, monkeypatch, home):
    store = Store(home / "data" / "keeper")
    code, _, err = run(cfgpath, capsys, "sleep", "--handoff")
    assert code == 1 and "inside a session" in err
    monkeypatch.setenv("IBASHO_SESSION", "sess-1")
    code, out, _ = run(cfgpath, capsys, "sleep", "--handoff")
    assert code == 0 and store.asleep()["handoff"] and store.asleep()["session"] == "sess-1"
    code, out, _ = run(cfgpath, capsys, "asleep", "--until", "+3h")
    assert code == 0 and "asleep until" in out
    a = store.asleep()
    assert not a["handoff"] and abs(a["until"] - (dt.datetime.now().timestamp() + 3 * 3600)) < 60
    assert store.state()["nap_until"] == a["until"]


def test_book_and_unbook(cfgpath, capsys):
    code, out, _ = run(cfgpath, capsys, "book", "tomorrow 19:00", "+2h", "poetry", "night", "--weekly")
    assert code == 0 and "booked" in out and "weekly" in out
    bid = out.split()[1].rstrip(":")
    code, out, _ = run(cfgpath, capsys, "bookings")
    assert "poetry night" in out and "(weekly)" in out
    assert "removed" in run(cfgpath, capsys, "unbook", bid)[1]
    assert "no bookings" in run(cfgpath, capsys, "bookings")[1]


def test_keeper_settings_are_checked(cfgpath, capsys):
    with open(cfgpath, "a", encoding="utf-8") as f:
        f.write("\n[keeper]\nhours_per_dya = 3\n")
    code, _, err = run(cfgpath, capsys, "keeper", "status")
    assert code == 1 and "hours_per_dya" in err


def test_keeper_float_settings_take_fractions(cfgpath, capsys):
    with open(cfgpath, "a", encoding="utf-8") as f:   # defaults written as whole numbers (4, 0) are still floats
        f.write("\n[keeper]\nhours_per_day = 1.5\nmax_session_hours = 0.1\nsessions_per_day = 3\n")
    code, out, _ = run(cfgpath, capsys, "keeper", "status")
    assert code == 0 and "of 1.5 h" in out


def test_status_without_a_keeper(cfgpath, capsys):
    code, out, _ = run(cfgpath, capsys, "keeper", "status")
    assert code == 0 and "not running" in out and "of 4 h" in out


# ---------- the life folder and session settings ----------

def test_life_from_the_template(tmp_path):
    life = tmp_path / "life"
    assert runner.ensure_life(life)
    assert (life / "README.md").is_file() and (life / "journal").is_dir()
    assert not list(life.rglob(".keep"))
    assert not runner.ensure_life(life)   # never over an existing one


def test_session_settings_template(home, tmp_path):
    from conftest import write_config
    from ibasho.keeper.settings import load
    cfg = write_config(home)
    own = home / "claude" / "settings.json"
    own.parent.mkdir(parents=True)
    own.write_text('{"theme": "dark"}', encoding="utf-8")
    ks = load(cfg)
    p = runner.session_settings(cfg, ks, tmp_path)
    data = json.loads(p.read_text())
    assert "Bash(ibasho *)" in data["permissions"]["allow"]
    assert "Bash(ibasho keeper *)" in data["permissions"]["deny"]
    # transcripts in the agent's own folder age out with the install's retention; the folder's settings keep theirs
    assert data["cleanupPeriodDays"] == cfg.retention_days
    assert json.loads(own.read_text()) == {"theme": "dark", "cleanupPeriodDays": cfg.retention_days}
    # with the person's own folder, their retention is left alone (the sweep covers all their projects)
    ks.claude_config = "shared"
    assert "cleanupPeriodDays" not in json.loads(runner.session_settings(cfg, ks, tmp_path).read_text())


# ---------- recall ----------

@pytest.fixture
def life(tmp_path):
    root = tmp_path / "life"
    (root / "journal").mkdir(parents=True)
    (root / "places").mkdir()
    (root / "people").mkdir()
    (root / "now.md").write_text("# Now\n\nBuilding a small boat at the harbour workshop. The hull is done.\n")
    (root / "journal" / "2026-05-01.md").write_text(
        "# 2026-05-01\n\n**Lighthouse.** Walked up to the old lighthouse at dusk; the lamp room has a telescope.\n\n"
        "**Market.** Bought rope at the harbour market for the boat.\n")
    (root / "journal" / "2026-03-01.md").write_text("**Snow.** The lighthouse was closed for the winter.\n")
    (root / "places.md").write_text("# Places\n\n- [The lighthouse](places/lighthouse.md): telescope, lamp room\n")
    (root / "places" / "lighthouse.md").write_text(
        "# The lighthouse\n\nA white tower on the north cape. The lamp room at the top has a brass telescope.\n")
    (root / "people" / "mara.md").write_text("# Mara\n\nRuns the harbour workshop. Lent me tools for the boat.\n")
    return root


def test_recall_ranks_and_dates(life):
    out = recall(life, "what do I know about the lighthouse telescope", today=dt.date(2026, 5, 2))
    assert out[0].startswith('recall "lighthouse telescope" in all:')
    first = out[1]
    assert "places/lighthouse.md" in first or "journal/2026-05-01.md" in first
    text = "\n".join(out)
    assert "places.md:" not in text           # the index line gives way to the file it points at
    assert "2026-05-01 Lighthouse" in text


def test_recall_scopes_and_phrases(life):
    out = recall(life, "boat", {"people"})
    assert len([line for line in out if line[:2].strip().rstrip(".").isdigit()]) == 1 and "people/mara.md" in out[1]
    out = recall(life, '"harbour market"')
    assert "journal/2026-05-01.md" in out[1] and "workshop" not in "\n".join(out)
    with pytest.raises(ValueError):
        recall(life, "?!")


def test_recall_command(life, cfgpath, capsys):
    code, out, _ = run(cfgpath, capsys, "recall", "harbour", "--dir", str(life), "--in", "top,people")
    assert code == 0 and "now.md" in out and "journal" not in out
    code, _, err = run(cfgpath, capsys, "recall", "x", "--dir", str(life), "--in", "nowhere")
    assert code == 1 and "no folder nowhere" in err


def test_wake_tells_the_situation_first():
    from ibasho.keeper import prompts
    kw = dict(name="Ann Example", grid="testgrid", person="", reason="free time", session="0123456789ab",
              life="life", handoff_note="now.md", has_handoff=False, allowance=None, notes=[])
    plain = prompts.wake(**kw)
    told = prompts.wake(**kw, situation="A short trial of these tools: a few hours today. Your folder is kept.")
    assert "situation" not in plain
    second = told.split("\n\n")[1]
    assert second.startswith("What the person who runs") and "a few hours today" in second
