"""Session signal handling: a killed run records why it died (ticket 57)."""

import json
import logging
import signal
import sqlite3
import subprocess
import threading
import time

import pytest

from lime import session
from lime.data_types import PhaseParams


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


@pytest.fixture
def project(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("v1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def cfg():
    return {
        "defaults": {"runtime": "pi", "model": "openrouter/x/y", "thinking": "medium"},
        "agents": [{"name": "builder", "prompt_engineering": {}}],
    }


@pytest.fixture(autouse=True)
def restore_signal_handlers():
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    yield
    for s, h in saved.items():
        signal.signal(s, h)


SIGNALS = pytest.mark.parametrize(
    "sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP], ids=lambda s: s.name
)


@SIGNALS
def test_killed_phase_records_the_signal_name_not_the_number(project, sig):
    """This one must fail before the fix — asserts the signal name is in the error."""
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    with pytest.raises(SystemExit):
        with run.phase(
            PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="First phase")
        ):
            signal.raise_signal(sig)
    assert run.phases[0].status == "fail"
    assert f"killed by {sig.name}" in run.phases[0].error
    row = run.tracer.con.execute(
        "SELECT error FROM phases WHERE run_id='R1'"
    ).fetchone()
    assert f"killed by {sig.name}" in row["error"]


@SIGNALS
def test_killed_phase_still_exits_with_128_plus_signum(project, sig):
    """Guard: the fix must not turn the exit status into a string/1."""
    run = session.ensure(cfg(), "R2", runs_dir=project / "runs")
    with pytest.raises(SystemExit) as caught:
        with run.phase(
            PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="First phase")
        ):
            signal.raise_signal(sig)
    assert caught.value.code == 128 + sig


@SIGNALS
def test_kill_closes_the_run_before_exiting(project, sig):
    """close_run(ok=False) happens inside the handler, not only via phase crash."""
    run = session.ensure(cfg(), "R3", runs_dir=project / "runs")
    handler = signal.getsignal(sig)
    with pytest.raises(SystemExit):
        handler(sig, None)  # no phase open
    assert run._run_closed
    row = run.tracer.con.execute(
        "SELECT status FROM runs WHERE run_id='R3'"
    ).fetchone()
    assert row["status"] == "fail"

@SIGNALS
def test_killed_phase_records_crash_detected_with_signal_and_code(project, sig):
    run = session.ensure(cfg(), "R4", runs_dir=project / "runs")
    with pytest.raises(SystemExit) as caught:
        with run.phase(
            PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="First phase")
        ):
            signal.raise_signal(sig)
    rows = run.tracer.con.execute(
        "SELECT type, payload_json FROM events WHERE run_id='R4' AND type IN ('error','crash_detected') ORDER BY event_id"
    ).fetchall()
    types = [r["type"] for r in rows]
    assert types == ["error", "crash_detected"]
    assert json.loads(rows[1]["payload_json"]) == {"signal": sig.name, "exit_code": 128 + sig}
    assert caught.value.code == 128 + sig
    assert f"killed by {sig.name}" in run.phases[0].error

def test_explicit_runs_dir_isolates_session_state(project, monkeypatch):
    """session.ensure(..., runs_dir=throwaway) creates its row/artifacts only
    under that explicit directory and does not touch the cwd project's .lime/runs."""
    throwaway = project / "throwaway_runs"
    run = session.ensure({
        "defaults": {"runtime": "pi", "model": "openrouter/x/y", "thinking": "medium"},
        "agents": [{"name": "builder", "prompt_engineering": {}}],
    }, "ISOLATED", runs_dir=throwaway)

    # The run's tracer is based on the explicit runs_dir
    assert run.tracer.runs_dir == throwaway
    # The project's .lime/runs/lime.db is untouched
    assert not (project / ".lime" / "runs" / "lime.db").exists()
    # The throwaway db has the row
    assert (throwaway / "lime.db").exists()
    row = run.tracer.con.execute("SELECT run_id FROM runs WHERE run_id='ISOLATED'").fetchone()
    assert row is not None
    assert row["run_id"] == "ISOLATED"


# --- pinned birth identity (ticket 144) ---


def test_new_run_receives_and_stores_the_pin(project):
    run = session.ensure(cfg(), "P1", runs_dir=project / "runs", base_sha="a" * 40)
    assert run.base_sha == "a" * 40
    row = run.tracer.con.execute("SELECT base_sha FROM runs WHERE run_id='P1'").fetchone()
    assert row["base_sha"] == "a" * 40


def test_reattach_after_more_commits_still_reads_the_original_pin(project):
    first = session.ensure(cfg(), "P2", runs_dir=project / "runs", base_sha="a" * 40)
    (project / "app.py").write_text("v2\n")
    git(project, "add", "-A")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "later")

    rejoined = session.ensure(cfg(), "P2", runs_dir=project / "runs")
    assert rejoined.base_sha == "a" * 40  # stored row, never current HEAD
    assert first.base_sha == "a" * 40


def test_join_cannot_overwrite_the_pin(project):
    session.ensure(cfg(), "P3", runs_dir=project / "runs", base_sha="a" * 40)
    rejoined = session.ensure(cfg(), "P3", runs_dir=project / "runs", base_sha="b" * 40)
    assert rejoined.base_sha == "a" * 40
    row = rejoined.tracer.con.execute("SELECT base_sha FROM runs WHERE run_id='P3'").fetchone()
    assert row["base_sha"] == "a" * 40


def test_join_cannot_backfill_a_legacy_null(project):
    session.ensure(cfg(), "P4", runs_dir=project / "runs")  # legacy NULL
    rejoined = session.ensure(cfg(), "P4", runs_dir=project / "runs", base_sha="b" * 40)
    assert rejoined.base_sha is None
    row = rejoined.tracer.con.execute("SELECT base_sha FROM runs WHERE run_id='P4'").fetchone()
    assert row["base_sha"] is None


# --- processes.heartbeat_at + the beat thread (ticket 252, step 1) ---


def _poll(cond, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _heartbeat(run, process_id: int):
    return run.tracer.con.execute(
        "SELECT heartbeat_at FROM processes WHERE id=?", (process_id,)
    ).fetchone()[0]


@pytest.mark.parametrize("kind", ["run", "research", "prototype"])
def test_ensure_beats_its_run_row_until_the_run_closes(project, monkeypatch, kind):
    import lime.tracer as lt

    monkeypatch.setattr(lt, "HEARTBEAT_S", 0.01)
    before = set(threading.enumerate())
    run = session.ensure(cfg(), "H1", kind=kind, runs_dir=project / "runs")
    row = run.tracer.con.execute(
        "SELECT id FROM processes WHERE run_id='H1' AND kind='run'"
    ).fetchone()
    process_id = row["id"]
    assert _poll(lambda: _heartbeat(run, process_id) is not None)

    name = f"lime-heartbeat-{process_id}"
    new_threads = set(threading.enumerate()) - before
    thread = next(t for t in new_threads if t.name == name)
    assert thread.is_alive()

    run.close_run(ok=True)
    thread.join(2)
    assert not thread.is_alive()


def test_a_failed_beat_never_stops_the_run(project, monkeypatch, caplog):
    import lime.tracer as lt

    monkeypatch.setattr(lt, "HEARTBEAT_S", 0.01)
    real = lt._beat
    calls = {"n": 0}

    def flaky(con, pid):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real(con, pid)

    monkeypatch.setattr(lt, "_beat", flaky)
    run = session.ensure(cfg(), "H1", runs_dir=project / "runs")
    with caplog.at_level(logging.WARNING, logger="lime.tracer"):
        with run.phase(PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="d")):
            pass

    row = run.tracer.con.execute(
        "SELECT id FROM processes WHERE run_id='H1' AND kind='run'"
    ).fetchone()
    process_id = row["id"]
    assert _poll(lambda: _heartbeat(run, process_id) is not None)

    assert run.finish(accepted=True) == 0
    assert run.tracer.con.execute(
        "SELECT status FROM runs WHERE run_id='H1'"
    ).fetchone()[0] == "success"
    assert any(r.levelno == logging.WARNING and "heartbeat" in r.getMessage() for r in caplog.records)
