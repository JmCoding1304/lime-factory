"""`lime/batch.py`: the batch's own ledger mirror (ticket 254)."""

import ast
import contextlib
import itertools
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lime import batch, session, tracer
from lime.data_types import PhaseParams

_REPO_ROOT = Path(__file__).resolve().parent.parent


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


@pytest.fixture(autouse=True)
def restore_signal_handlers():
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    yield
    for s, h in saved.items():
        signal.signal(s, h)


@pytest.fixture
def project(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("v1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "41-toy.md").write_text("# 41 — Toy\n")
    (tickets / "42-toy.md").write_text("# 42 — Toy\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def cfg():
    return {
        "defaults": {"runtime": "pi", "model": "openrouter/x/y", "thinking": "medium"},
        "agents": [{"name": "builder", "prompt_engineering": {}}],
    }


def _db(project):
    return project / ".lime" / "runs" / "lime.db"


def _children(db):
    """`batch_children` as `(position, ticket, state, reason, updated_at)` by position."""
    con = tracer.connect(db, readonly=True)
    try:
        return [
            tuple(row)
            for row in con.execute(
                "SELECT position, ticket, state, reason, updated_at FROM batch_children"
                " ORDER BY position"
            )
        ]
    finally:
        con.close()


def _batch_row(db):
    """Every `processes` row of kind `batch`."""
    con = tracer.connect(db, readonly=True)
    try:
        return [
            dict(row)
            for row in con.execute("SELECT * FROM processes WHERE kind='batch'")
        ]
    finally:
        con.close()


def test_open_writes_the_batch_row_and_one_child_per_arg_in_order(project):
    mirror = batch.Batch.open(project, ["41", "nope", "41"])
    assert re.fullmatch(r"[0-9a-f]{8}", mirror.batch_id)

    db = _db(project)
    rows = _batch_row(db)
    assert len(rows) == 1
    assert rows[0]["run_id"] == mirror.batch_id
    assert rows[0]["pid"] == os.getpid()
    assert rows[0]["ended_at"] is None

    children = _children(db)
    assert [c[:4] for c in children] == [
        (0, "41-toy", "queued", None),
        (1, "nope", "queued", None),
        (2, "41-toy", "queued", None),
    ]
    assert all(c[4] for c in children)

    con = tracer.connect(db, readonly=True)
    try:
        assert tracer.liveness(con, mirror.batch_id, datetime.now(UTC), "batch") == "live"
    finally:
        con.close()

    mirror.close()
    assert _batch_row(db)[0]["ended_at"] is not None
    mirror.close()  # idempotent
    assert _batch_row(db)[0]["ended_at"] is not None


def test_open_is_one_transaction_and_a_failed_write_is_retried(project, caplog):
    tr = tracer.Tracer(project / ".lime" / "runs")
    tr.con.execute(
        "CREATE TRIGGER no_children BEFORE INSERT ON batch_children"
        " BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )
    tr.con.commit()
    tr.con.close()

    db = _db(project)
    with caplog.at_level(logging.WARNING):
        mirror = batch.Batch.open(project, ["41", "42"])
    assert "mirror write failed" in caplog.text
    assert _batch_row(db) == []
    assert _children(db) == []  # the opening transaction is atomic

    mirror.start(0)
    tr = tracer.Tracer(project / ".lime" / "runs")
    tr.con.execute("DROP TRIGGER no_children")
    tr.con.commit()
    tr.con.close()

    assert mirror.flush() is True
    assert len(_batch_row(db)) == 1
    assert [c[:4] for c in _children(db)] == [
        (0, "41-toy", "started", None),
        (1, "42-toy", "queued", None),
    ]
    mirror.close()


def test_transitions_and_reason_written_only_when_it_changes(project, monkeypatch):
    counter = itertools.count(1)
    monkeypatch.setattr(batch, "_now", lambda: f"stamp{next(counter)}")
    mirror = batch.Batch.open(project, ["41", "42", "41", "42"])
    db = _db(project)

    mirror.hold(1, "blocked 99")
    assert mirror.flush() is True
    stamp = _children(db)[1][4]
    assert stamp

    mirror.hold(1, "blocked 99")
    mirror.flush()
    assert _children(db)[1][4] == stamp  # unchanged reason never rewrites

    mirror.hold(1, "overlap X")
    mirror.flush()
    assert _children(db)[1][4] != stamp

    mirror.start(0)
    mirror.flush()
    assert _children(db)[0][:4] == (0, "41-toy", "started", None)
    mirror.hold(0, "slot")  # both are no-ops on a non-queued row
    mirror.drop(0, "x")
    mirror.flush()
    assert _children(db)[0][:4] == (0, "41-toy", "started", None)

    mirror.drop(2, batch.NO_TICKET_FILE)
    mirror.flush()
    assert _children(db)[2][:4] == (2, "41-toy", "dropped", "no ticket file")

    mirror.give_up(1)
    mirror.flush()
    assert _children(db)[1][:4] == (1, "42-toy", "dropped", "gave up: overlap X")

    mirror.wait_for_slot([3])
    mirror.flush()
    assert _children(db)[3][:4] == (3, "42-toy", "queued", "slot")
    mirror.hold(3, "blocked 7")
    mirror.flush()
    assert _children(db)[3][:4] == (3, "42-toy", "queued", "blocked 7")
    mirror.close()


def test_reason_text():
    assert (
        batch.blocked_reason([".lime/tickets/99-blocker.md", ".lime/tickets/252-x.md"])
        == "blocked 99, 252"
    )
    assert (
        batch.overlap_reason(["Run ab12cd34 (lime/feature/x)"])
        == "overlap Run ab12cd34 (lime/feature/x)"
    )


SCRIPT = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from lime import batch\n"
    "b = batch.Batch.open(Path(sys.argv[1]), ['41', '42'])\n"
    "print(b.batch_id, flush=True)\n"
    "while True:\n"
    "    time.sleep(0.05)\n"
)


@pytest.mark.parametrize(
    "sig",
    [signal.SIGTERM, signal.SIGINT, signal.SIGKILL, signal.SIGHUP],
    ids=lambda s: s.name,
)
def test_signal_closes_or_leaves_the_batch_row(project, sig):
    proc = subprocess.Popen(
        [sys.executable, "-c", SCRIPT, str(project)],
        cwd=_REPO_ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        batch_id = proc.stdout.readline().strip()
        assert re.fullmatch(r"[0-9a-f]{8}", batch_id)
        proc.send_signal(sig)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    con = tracer.connect(_db(project), readonly=True)
    try:
        row = con.execute(
            "SELECT ended_at FROM processes WHERE run_id=? AND kind='batch'",
            (batch_id,),
        ).fetchone()
        assert row is not None
        if sig in (signal.SIGTERM, signal.SIGINT):
            assert proc.returncode == 128 + sig
            assert row["ended_at"] is not None
        else:
            assert proc.returncode == -sig
            assert row["ended_at"] is None
            now = datetime.now(UTC) + timedelta(seconds=tracer.LIVENESS_S + 1)
            assert tracer.liveness(con, batch_id, now, "batch") == "dead"
    finally:
        con.close()


_WRITE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+batch_children\b",
    re.I,
)


def test_only_batch_module_writes_batch_children():
    writers = set()
    for path in sorted((_REPO_ROOT / "lime").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _WRITE.search(node.value)
            ):
                writers.add(str(path.relative_to(_REPO_ROOT)))
    assert writers == {"lime/batch.py"}


_RETRY_WRITE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE)\s+processes\b[^;]*\b(?:retried_at|retry_of)\b",
    re.I | re.S,
)


def test_only_tracer_writes_the_retry_columns():
    writers = set()
    for path in sorted((_REPO_ROOT / "lime").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _RETRY_WRITE.search(node.value)
            ):
                writers.add(str(path.relative_to(_REPO_ROOT)))
    assert writers == {"lime/tracer.py"}


def test_a_retry_batch_row_records_retry_of(project):
    db = _db(project)
    retry = batch.Batch.open(project, ["41"], retry_of="deadbeef")
    row = next(r for r in _batch_row(db) if r["run_id"] == retry.batch_id)
    assert row["retry_of"] == "deadbeef"
    assert row["retried_at"] is None

    plain = batch.Batch.open(project, ["42"])
    row = next(r for r in _batch_row(db) if r["run_id"] == plain.batch_id)
    assert row["retry_of"] is None

    retry.close()
    plain.close()


def test_processes_gains_the_retry_columns(project):
    runs_dir = project / ".lime" / "runs"
    runs_dir.mkdir(parents=True)
    con = sqlite3.connect(runs_dir / "lime.db")
    con.execute(
        "CREATE TABLE processes (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT,"
        " name TEXT, pid INTEGER, command TEXT, started_at TEXT, ended_at TEXT, heartbeat_at TEXT)"
    )
    con.execute(
        "INSERT INTO processes (run_id, kind, name, pid, command, started_at, ended_at, heartbeat_at)"
        " VALUES ('legacy', 'batch', 'old', 1, 'old', 't', NULL, 't')"
    )
    con.commit()
    con.close()

    tracer.Tracer(runs_dir).con.close()  # first open migrates
    tr = tracer.Tracer(runs_dir)  # second open is a no-op
    try:
        cols = {row[1] for row in tr.con.execute("PRAGMA table_info(processes)")}
        assert {"retried_at", "retry_of"} <= cols
        row = tr.con.execute(
            "SELECT retried_at, retry_of FROM processes WHERE run_id='legacy'"
        ).fetchone()
        assert row["retried_at"] is None
        assert row["retry_of"] is None
    finally:
        tr.con.close()


def test_claim_retry_goes_ahead_once_on_an_open_batch_row(project):
    db = _db(project)
    mirror = batch.Batch.open(project, ["41"])
    tr = tracer.Tracer(project / ".lime" / "runs")
    try:
        assert tr.claim_retry(mirror.batch_id) is True
        assert tr.claim_retry(mirror.batch_id) is False
        row = tr.con.execute(
            "SELECT retried_at FROM processes WHERE run_id=?", (mirror.batch_id,)
        ).fetchone()
        assert row["retried_at"] is not None
    finally:
        tr.con.close()
    mirror.close()


def test_claim_retry_refuses_a_closed_row_a_retry_row_and_no_row(project):
    tr = tracer.Tracer(project / ".lime" / "runs")
    try:
        closed = batch.Batch.open(project, ["41"])
        closed.close()
        assert tr.claim_retry(closed.batch_id) is False
        row = tr.con.execute(
            "SELECT retried_at FROM processes WHERE run_id=?", (closed.batch_id,)
        ).fetchone()
        assert row["retried_at"] is None

        retry = batch.Batch.open(project, ["42"], retry_of="deadbeef")
        assert tr.claim_retry(retry.batch_id) is False
        row = tr.con.execute(
            "SELECT retried_at FROM processes WHERE run_id=?", (retry.batch_id,)
        ).fetchone()
        assert row["retried_at"] is None
        retry.close()

        assert tr.claim_retry("00000000") is False
    finally:
        tr.con.close()


def test_two_claimers_racing_claim_exactly_once(project):
    mirror = batch.Batch.open(project, ["41"])
    runs_dir = project / ".lime" / "runs"
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def claimer():
        tr = tracer.Tracer(runs_dir)
        try:
            barrier.wait()
            outcome = tr.claim_retry(mirror.batch_id)
        finally:
            tr.con.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=claimer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]
    mirror.close()


def test_a_child_run_never_touches_its_batch_rows(project):
    db = _db(project)
    mirror = batch.Batch.open(project, ["41", "42"])
    mirror.start(0)
    mirror.hold(1, "blocked 99")
    assert mirror.flush() is True
    children_before = _children(db)

    ticket = project / ".lime" / "tickets" / "41-toy.md"
    run = session.ensure(
        cfg(), "C1", runs_dir=project / ".lime" / "runs",
        ticket=str(ticket), batch_id=mirror.batch_id,
    )
    with run.phase(
        PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="d")
    ):
        pass
    run.close_run(ok=False)
    run.tracer.set_run_status("C1", "discarded")

    assert _children(db) == children_before  # updated_at included
    rows = _batch_row(db)
    assert len(rows) == 1
    assert rows[0]["ended_at"] is None  # the batch row is still open

    con = tracer.connect(db, readonly=True)
    try:
        row = con.execute("SELECT batch_id FROM runs WHERE run_id='C1'").fetchone()
        assert row["batch_id"] == mirror.batch_id
    finally:
        con.close()
    mirror.close()


# --- the watchdog and the retry launch (ticket 255) ---


def _log_text(project, bid):
    path = batch.watch_log(project / ".lime" / "runs", bid)
    return path.read_text() if path.exists() else ""


class _Exited:
    """A stand-in for a launched retry that has already exited."""

    def __init__(self, code):
        self.code = code

    def wait(self):
        return self.code


def _wait_for(path, needle, timeout=60):
    """Poll a file until `needle` shows up, every 0.1 s. True when it did."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and needle in path.read_text():
            return True
        time.sleep(0.1)
    return False


def _retried_at(project, bid):
    con = tracer.connect(_db(project), readonly=True)
    try:
        row = con.execute(
            "SELECT retried_at FROM processes WHERE run_id=? AND kind='batch'", (bid,)
        ).fetchone()
        return row["retried_at"] if row is not None else None
    finally:
        con.close()


def test_watchdog_runs_in_its_own_session_and_exits_when_the_batch_closes(project):
    mirror = batch.Batch.open(project, ["41"])
    proc = mirror.start_watchdog()
    try:
        assert proc is not None
        assert os.getsid(proc.pid) == proc.pid != os.getsid(0)
        assert proc.poll() is None
        mirror.close()
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    log = _log_text(project, mirror.batch_id)
    assert "not retried — batch row closed" in log
    assert _retried_at(project, mirror.batch_id) is None


def test_a_retry_batch_starts_no_watchdog_and_is_never_claimed(project, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("a retry must not spawn anything")

    mirror = batch.Batch.open(project, ["41"], retry_of="deadbeef")
    monkeypatch.setattr(batch.subprocess, "Popen", boom)
    monkeypatch.setattr(batch, "_spawn_retry", boom)
    try:
        assert mirror.start_watchdog(deploy=True) is None
        # the row stays open, but a retry's own row is never claimed
        assert batch.retry_dead(project, mirror.batch_id, deploy=True) is False
        assert _retried_at(project, mirror.batch_id) is None
        assert "a retry is never retried" in _log_text(project, mirror.batch_id)
    finally:
        mirror.close()


@pytest.mark.parametrize(
    "deploy, profile, tail",
    [(True, "quality", ["--deploy", "--profile", "quality"]), (False, None, [])],
)
def test_retry_dead_launches_the_queued_and_started_children(
    project, monkeypatch, deploy, profile, tail
):
    mirror = batch.Batch.open(project, ["41", "nope", "42", "nope2"])
    mirror.start(0)
    mirror.drop(1, batch.NO_TICKET_FILE)
    assert mirror.flush() is True
    calls = []

    def rec(cmd, cwd, log):
        calls.append((cmd, cwd))
        return _Exited(0)

    monkeypatch.setattr(batch, "_spawn_retry", rec)
    try:
        assert batch.retry_dead(
            project, mirror.batch_id, deploy=deploy, profile=profile
        ) is True
        expected = [
            sys.executable, "-m", "lime", "run", "41-toy", "42-toy", "nope2",
            "--retry-of", mirror.batch_id, *tail,
        ]
        assert calls == [(expected, project)]
        assert _retried_at(project, mirror.batch_id) is not None
        assert batch.retry_dead(project, mirror.batch_id) is False
        assert len(calls) == 1
        log = _log_text(project, mirror.batch_id)
        assert log.count("launching retry") == 1
        assert "retry exited 0" in log
        assert "already claimed" in log
    finally:
        mirror.close()


def test_claimed_with_nothing_queued_launches_nothing(project, monkeypatch):
    mirror = batch.Batch.open(project, ["41", "nope"])
    mirror.drop(0, batch.NO_TICKET_FILE)
    mirror.drop(1, batch.NO_TICKET_FILE)
    assert mirror.flush() is True

    def boom(*a, **kw):
        raise AssertionError("nothing left to retry must launch nothing")

    monkeypatch.setattr(batch, "_spawn_retry", boom)
    try:
        assert batch.retry_dead(project, mirror.batch_id) is True
        assert _retried_at(project, mirror.batch_id) is not None
        assert "nothing left to retry" in _log_text(project, mirror.batch_id)
    finally:
        mirror.close()


def test_two_watchdogs_racing_launch_exactly_one_retry(project, monkeypatch):
    mirror = batch.Batch.open(project, ["41"])
    calls = []

    def rec(cmd, cwd, log):
        calls.append(cmd)
        return _Exited(0)

    monkeypatch.setattr(batch, "_spawn_retry", rec)
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def watchdog():
        barrier.wait()
        outcome = batch.retry_dead(project, mirror.batch_id)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=watchdog) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert sorted(results) == [False, True]
        assert len(calls) == 1
    finally:
        mirror.close()


@pytest.mark.parametrize("mode", ["spawn", "exit"], ids=["spawn-error", "nonzero-exit"])
def test_a_retry_that_fails_to_launch_stays_claimed_and_is_logged(
    project, monkeypatch, mode
):
    ticket = "42" if mode == "exit" else "41"
    mirror = batch.Batch.open(project, [ticket])

    def boom(cmd, cwd, log):
        raise OSError("spawn boom")

    def never(*a, **kw):
        raise AssertionError("nothing tries again")

    if mode == "spawn":
        monkeypatch.setattr(batch, "_spawn_retry", boom)
    try:
        assert batch.retry_dead(project, mirror.batch_id) is True
        assert _retried_at(project, mirror.batch_id) is not None
        monkeypatch.setattr(batch, "_spawn_retry", never)
        assert batch.retry_dead(project, mirror.batch_id) is False
        log = _log_text(project, mirror.batch_id)
        if mode == "spawn":
            assert "retry failed to launch: spawn boom" in log
        else:
            assert "Where parses zero paths" in log
            assert "retry exited 2" in log
    finally:
        mirror.close()


_SIGNAL_SCRIPT = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from lime import batch\n"
    "b = batch.Batch.open(Path(sys.argv[1]), ['gone', 'nope'])\n"
    "b.start(0)\n"
    "b.flush()\n"
    "w = b.start_watchdog()\n"
    "print(b.batch_id, w.pid, flush=True)\n"
    "while True:\n"
    "    time.sleep(0.05)\n"
)


@pytest.mark.parametrize(
    "sig",
    [signal.SIGKILL, signal.SIGHUP, signal.SIGTERM, signal.SIGINT],
    ids=lambda s: s.name,
)
def test_signal_retries_only_a_batch_that_died_with_its_row_open(project, sig):
    retried = sig in (signal.SIGKILL, signal.SIGHUP)
    proc = subprocess.Popen(
        [sys.executable, "-c", _SIGNAL_SCRIPT, str(project)],
        cwd=_REPO_ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    watchdog_pid = None
    found = False
    try:
        batch_id, watchdog_pid = proc.stdout.readline().split()
        proc.send_signal(sig)
        proc.wait(timeout=20)
        log = batch.watch_log(project / ".lime" / "runs", batch_id)
        if retried:
            found = _wait_for(log, "retry exited")
            assert found, log.read_text() if log.exists() else "no watchdog log"
            dead = [r for r in _batch_row(_db(project)) if r["run_id"] == batch_id]
            assert len(dead) == 1
            assert dead[0]["retried_at"] is not None
            assert dead[0]["ended_at"] is None
            con = tracer.connect(_db(project), readonly=True)
            try:
                retries = [dict(r) for r in con.execute(
                    "SELECT * FROM processes WHERE kind='batch' AND retry_of=?", (batch_id,)
                )]
                assert len(retries) == 1
                assert retries[0]["ended_at"] is not None
                children = [
                    tuple(r) for r in con.execute(
                        "SELECT position, ticket, state, reason FROM batch_children"
                        " WHERE batch_id=? ORDER BY position",
                        (retries[0]["run_id"],),
                    )
                ]
            finally:
                con.close()
            assert children == [
                (0, "gone", "dropped", "no ticket file"),
                (1, "nope", "dropped", "no ticket file"),
            ]
            assert log.read_text().count("launching retry") == 1
            assert batch.retry_dead(project, batch_id) is False
        else:
            found = _wait_for(log, "not retried — batch row closed")
            assert found, log.read_text() if log.exists() else "no watchdog log"
            assert _retried_at(project, batch_id) is None
            con = tracer.connect(_db(project), readonly=True)
            try:
                count = con.execute(
                    "SELECT COUNT(*) FROM processes WHERE kind='batch' AND retry_of=?",
                    (batch_id,),
                ).fetchone()[0]
            finally:
                con.close()
            assert count == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if watchdog_pid is not None and not found:
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(watchdog_pid), signal.SIGKILL)


_RETRY_SCRIPT = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from lime import batch\n"
    "b = batch.Batch.open(Path(sys.argv[1]), ['41'], retry_of=sys.argv[2])\n"
    "b.start(0)\n"
    "b.flush()\n"
    "print(b.batch_id, b.start_watchdog(), flush=True)\n"
    "while True:\n"
    "    time.sleep(0.05)\n"
)


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM], ids=lambda s: s.name)
def test_a_retry_that_dies_leaves_no_subsequent_retry(project, sig):
    # A retry kills itself: SIGKILL leaves its row open (the state that retries an
    # original batch), SIGTERM closes it first — either way nothing retries a retry.
    original = batch.Batch.open(project, ["41"])
    original.close()
    proc = subprocess.Popen(
        [sys.executable, "-c", _RETRY_SCRIPT, str(project), original.batch_id],
        cwd=_REPO_ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        retry_id, watchdog = proc.stdout.readline().split()
        # a retry starts no watchdog, so nothing is waiting to retry it
        assert watchdog == "None"
        proc.send_signal(sig)
        proc.wait(timeout=20)

        assert batch.retry_dead(project, retry_id) is False
        reason = "batch row closed" if sig == signal.SIGTERM else "a retry is never retried"
        assert reason in _log_text(project, retry_id)
        rows = _batch_row(_db(project))
        retry_row = next(r for r in rows if r["run_id"] == retry_id)
        assert retry_row["retry_of"] == original.batch_id
        # this retry, and no batch pointing at it as its dead parent
        assert [r for r in rows if r["retry_of"] == original.batch_id] == [retry_row]
        assert [r for r in rows if r["retry_of"] == retry_id] == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        original.close()


# --- ticket 256: reading a started child's Run and its fate ---

def _runs_dir(project):
    return (project / ".lime" / "runs").resolve()


def _dead_pid():
    """A pid that has certainly exited: a `true` subprocess reaped by `wait`."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _stale_beat(db, run_id):
    """The raw stale beat a Mac-sleep run leaves behind."""
    con = sqlite3.connect(db)
    try:
        con.execute(
            "UPDATE processes SET heartbeat_at='2020-01-01T00:00:00+00:00' WHERE run_id=?",
            (run_id,),
        )
        con.commit()
    finally:
        con.close()


def test_child_run_finds_the_batchs_own_run_by_ticket(project):
    runs_dir = _runs_dir(project)
    db = runs_dir / "lime.db"
    tr = tracer.Tracer(runs_dir)
    tr.start_run("R1", workflow="wf", request="r", kind="run",
                 ticket=".lime/tickets/41-toy.md", batch_id="deadbeef")
    tr.start_run("R2", workflow="wf", request="r", kind="run",
                 ticket=".lime/tickets/41-toy.md", batch_id="deadbeef")
    tr.start_run("Rof", workflow="wf", request="r", kind="run",
                 ticket=".lime/tickets/41-toy.md", batch_id="other")
    tr.start_run("Rnull", workflow="wf", request="r", kind="run",
                 ticket=".lime/tickets/41-toy.md", batch_id=None)
    tr.con.close()

    # make R2 unambiguously the newest by started_at
    con = sqlite3.connect(db)
    con.execute("UPDATE runs SET started_at='2020-01-01T00:00:00+00:00' WHERE run_id='R1'")
    con.execute("UPDATE runs SET started_at='2021-01-01T00:00:00+00:00' WHERE run_id='R2'")
    con.commit()
    con.close()

    con = tracer.connect(db, readonly=True)
    try:
        assert batch.child_run(con, "deadbeef", "41")["run_id"] == "R2"  # newest wins, prefix match
        assert batch.child_run(con, "deadbeef", "41-toy")["run_id"] == "R2"  # stem match
        assert batch.child_run(con, "deadbeef", "42") is None  # no match
        assert batch.child_run(con, "deadbeef", "041")["run_id"] == "R2"  # a number ignores zero padding
        assert batch.child_run(con, "deadbeef", "4") is None
    finally:
        con.close()


_CHILD_FATE_CASES = {
    "running-live-pid": "live",
    "running-no-process": "live",
    "running-dead-pid": "killed",
    "running-stale-live-pid": "live",
    "running-stale-dead-pid": "killed",
    "success": "passed",
    "deployed": "deployed",
    "fail-crash-after-phase": "killed",
    "fail-crash-then-phase": "failed",
    "fail-crash-no-signal": "failed",
    "fail-no-crash": "failed",
    "budget-exhausted": "failed",
    "discarded": "failed",
}


@pytest.mark.parametrize("case", list(_CHILD_FATE_CASES), ids=list(_CHILD_FATE_CASES))
def test_child_fate_reads_each_started_state(project, case):
    expected = _CHILD_FATE_CASES[case]
    runs_dir = _runs_dir(project)
    db = runs_dir / "lime.db"
    tr = tracer.Tracer(runs_dir)
    tr.start_run("R", workflow="wf", request="r", kind="run",
                 ticket=".lime/tickets/41-toy.md", batch_id="deadbeef")

    if case == "running-live-pid":
        tr.start_process("R", kind="run", name="lime", pid=os.getpid(), command="x")
    elif case == "running-dead-pid":
        tr.start_process("R", kind="run", name="lime", pid=_dead_pid(), command="x")
    elif case == "running-stale-live-pid":
        tr.start_process("R", kind="run", name="lime", pid=os.getpid(), command="x")
        _stale_beat(db, "R")
    elif case == "running-stale-dead-pid":
        tr.start_process("R", kind="run", name="lime", pid=_dead_pid(), command="x")
        _stale_beat(db, "R")
    elif case == "success":
        tr.set_run_status("R", "success")
    elif case == "deployed":
        tr.set_run_status("R", "success")
        tr.set_run_status("R", "deployed")
    elif case == "fail-crash-after-phase":
        tr.event("R", "phase_start")
        tr.event("R", "crash_detected", payload={"signal": "SIGHUP", "exit_code": 129})
        tr.set_run_status("R", "fail")
    elif case == "fail-crash-then-phase":
        tr.event("R", "phase_start")
        tr.event("R", "crash_detected", payload={"signal": "SIGHUP"})
        tr.event("R", "phase_start")
        tr.set_run_status("R", "fail")
    elif case == "fail-crash-no-signal":
        tr.event("R", "phase_start")
        tr.event("R", "crash_detected", payload={"signal": None})
        tr.set_run_status("R", "fail")
    elif case == "fail-no-crash":
        tr.set_run_status("R", "fail")
    elif case == "budget-exhausted":
        tr.set_run_status("R", "budget_exhausted")
    elif case == "discarded":
        tr.set_run_status("R", "discarded")
    tr.con.close()

    con = tracer.connect(db, readonly=True)
    try:
        row = con.execute("SELECT * FROM runs WHERE run_id='R'").fetchone()
        assert batch.child_fate(con, row, datetime.now(UTC)) == expected
    finally:
        con.close()
