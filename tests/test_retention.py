"""What ages out, what's capped, and what stays out of pictures."""

from __future__ import annotations

import datetime as dt
import os
import sys
import time

import psutil

from ibasho import procs
from ibasho.cli import main
from ibasho.keeper import runner
from ibasho.keeper.store import Store

from conftest import write_config


def later() -> float:
    return time.time() + 60   # a cutoff that makes everything written so far old


def test_notes_age_out_once_delivered_or_read(tmp_path):
    store = Store(tmp_path / "keeper")
    delivered = store.add_note("dinner's at seven", "person")
    store.mark_notes([delivered], "delivered", how="turn")
    store.add_note("still waiting", "person")
    seen = store.add_note("thanks", "agent")
    store.mark_notes([seen], "read")
    store.add_note("unread reply", "agent")

    assert store.prune_notes(time.time() - 3600) == 0   # nothing old yet
    assert store.prune_notes(later()) == 2
    left = {n["text"]: n for n in store.notes()}
    assert set(left) == {"still waiting", "unread reply"}
    assert [n["text"] for n in store.undelivered("person")] == ["still waiting"]
    assert [n["text"] for n in store.unread("agent")] == ["unread reply"]
    assert store.prune_notes(later()) == 0
    store.mark_notes([delivered], "read")                # an op for a note that's gone goes at the next prune
    store.add_note("after the prune", "person")          # appends still land
    assert "after the prune" in {n["text"] for n in store.notes()}
    store.prune_notes(later())
    assert delivered not in (store.path("notes.jsonl").read_text(encoding="utf-8"))


def test_keeper_log_keeps_only_its_recent_lines(tmp_path):
    log = tmp_path / "keeper.log"
    old = dt.datetime.now() - dt.timedelta(days=10)
    new = dt.datetime.now() - dt.timedelta(hours=1)
    log.write_text(f"10-01 08:00:00 a line in an older format\n"
                   f"{old:{runner.LOG_TIME}} note from Test Avatar: an old note\n"
                   f"{new:{runner.LOG_TIME}} session started\n"
                   f"a line of its own\n"
                   f"{new:{runner.LOG_TIME}} note from Test Avatar: a new note\n", encoding="utf-8")
    runner.prune_keeper_log(log, time.time() - 7 * 86400)
    text = log.read_text(encoding="utf-8")
    assert "old note" not in text and "older format" not in text
    assert text.startswith(f"{new:{runner.LOG_TIME}} session started\na line of its own\n") and "a new note" in text
    runner.prune_keeper_log(log, later())
    assert log.read_text(encoding="utf-8") == ""


def test_prune_logs_covers_the_keepers_records(tmp_path):
    kdir = tmp_path / "keeper"
    store = Store(kdir)
    n = store.add_note("hello", "person")
    store.mark_notes([n], "delivered", how="turn")
    (kdir / "sessions").mkdir()
    session = kdir / "sessions" / "s.log"
    session.write_text("words", encoding="utf-8")
    os.utime(session, (time.time() - 9 * 86400,) * 2)
    log = runner.make_logger(kdir, echo=False)
    log("something")
    runner.prune_logs(kdir, 7, store)
    assert not session.exists() and store.notes() and "something" in (kdir / "keeper.log").read_text("utf-8")
    runner.prune_logs(kdir, -1, store)   # a cutoff in the future: everything that's done goes
    assert not store.notes() and (kdir / "keeper.log").read_text("utf-8") == ""


def test_spawned_output_starts_afresh_past_its_cap(tmp_path, monkeypatch):
    out = tmp_path / "x.out"
    out.write_bytes(b"x" * 100)
    monkeypatch.setattr(procs, "OUT_MAX", 50)
    pid = procs.spawn([sys.executable, "-c", "print('fresh')"], out)
    try:
        psutil.Process(pid).wait(20)
    except psutil.NoSuchProcess:
        pass
    assert out.read_bytes().strip() == b"fresh"


def test_shot_leaves_the_ui_out_unless_asked(home, monkeypatch, capsys):
    from ibasho.cli import see
    cfg = write_config(home)
    asked = []
    monkeypatch.setattr(see, "take_shot", lambda ctx, noui=False, max_w=1280: asked.append(noui) or "x.png")
    assert main(["--config", str(cfg.path), "shot"]) == 0
    assert main(["--config", str(cfg.path), "shot", "--ui"]) == 0
    assert asked == [True, False]
