"""Tracer + `lime trace`/`lime cost` over a synthetic Run (ticket 25, spec §11)."""

import ast
import json
import logging
import os
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lime.cli import _new_events, main
from lime.tracer import (
    EVENT_TYPES,
    NO_RUN,
    PortsExhausted,
    Tracer,
    _INDEXES,
    _SCHEMA,
    connect,
    legacy_stage,
    runs_lacks_added_columns,
)

LEDGER_TABLES = {
    "runs",
    "phases",
    "events",
    "envelopes",
    "gate_results",
    "processes",
    "sessions",
    "summaries",
    "batch_children",
}


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # CLI resolves .lime/runs/ from cwd
    return tmp_path / ".lime" / "runs"


@pytest.fixture
def tracer(runs_dir):
    return Tracer(runs_dir)


def run_ended_at(tracer: Tracer) -> str | None:
    return tracer.con.execute("SELECT ended_at FROM runs WHERE run_id='R1'").fetchone()[0]


def synthetic_run(tracer: Tracer) -> str:
    """One feature Run: plan (pi) then build (claude_code), with turn costs."""
    tracer.start_run(
        "R1",
        workflow="feature",
        request="add trace",
        ticket="25",
        branch="run/R1",
        worktree="/tmp/wt/R1",
        port=4611,
        db_name="app_r1",
        profile="default",
    )
    plan = tracer.add_phase("R1", seq=1, name="plan", stage="Build", kind="agent", owner="planner")
    build = tracer.add_phase("R1", seq=2, name="build", stage="Build", kind="agent", owner="builder")
    tracer.set_phase_status(plan, "running")
    s_plan = tracer.start_session(
        "R1", plan, seq=1, role="planner", runtime="pi", model="x-ai/grok-4"
    )
    for turn_no, cost in enumerate([0.10, 0.15], start=1):
        tracer.event(
            "R1",
            "turn",
            phase_id=plan,
            name=s_plan,
            tokens=10_000 * turn_no,
            payload={
                "turn_no": turn_no,
                "input": 9_000,
                "output": 1_000,
                "cache_read": 0,
                "cache_write": 0,
                "cost": cost,
            },
        )
    tracer.set_phase_status(plan, "success")
    tracer.set_phase_status(build, "running")
    s_build = tracer.start_session(
        "R1", build, seq=2, role="builder", runtime="claude_code", model="claude-fable-5"
    )
    for turn_no in [1, 2, 3]:
        tracer.event(
            "R1",
            "turn",
            phase_id=build,
            name=s_build,
            tokens=20_000 * turn_no,
            payload={
                "turn_no": turn_no,
                "input": 18_000,
                "output": 2_000,
                "cache_read": 0,
                "cache_write": 0,
                "cost": 0.20,
            },
        )
    tracer.record_gate("R1", build, attempt=1, gate="tests", passed=True)
    return "R1"


# --- schema ---


def test_schema_has_exactly_the_ledger_tables(tracer, runs_dir):
    con = sqlite3.connect(runs_dir / "lime.db")
    names = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'"
        )
    }
    con.close()
    assert names == LEDGER_TABLES


def test_batch_children_table_shape(tracer):
    info = list(tracer.con.execute("PRAGMA table_info(batch_children)"))
    assert [r[1] for r in info] == [
        "batch_id", "position", "ticket", "state", "reason", "updated_at",
    ]
    pk = {r[1]: r[5] for r in info if r[5]}
    assert pk == {"batch_id": 1, "position": 2}


def test_db_is_wal(tracer, runs_dir):
    con = sqlite3.connect(runs_dir / "lime.db")
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()


def test_runs_has_the_lime_columns(tracer):
    cols = {r[1] for r in tracer.con.execute("PRAGMA table_info(runs)")}
    assert {"ticket", "branch", "worktree", "port", "db_name", "profile", "kind"} <= cols


def test_sessions_keyed_by_session_id_phase_continuation(tracer):
    cols = {r[1] for r in tracer.con.execute("PRAGMA table_info(sessions)")}
    assert {"session_id", "phase_id", "continuation", "role", "runtime", "model"} <= cols
    pk = [r[1] for r in tracer.con.execute("PRAGMA table_info(sessions)") if r[5]]
    assert pk == ["session_id"]


def test_event_types_are_the_twenty_one():
    assert len(EVENT_TYPES) == 21
    assert {"turn", "budget_warn", "continuation", "tool_call", "handoff", "crash_detected", "repeat_stop", "refusal", "tool_result"} <= EVENT_TYPES
    assert not RETIRED_EVENT_TYPES & EVENT_TYPES


# --- run row + folder ---


def test_start_run_writes_row_and_identity_card(tracer, runs_dir):
    synthetic_run(tracer)
    row = tracer.con.execute(
        "SELECT status, kind, ticket, port FROM runs WHERE run_id='R1'"
    ).fetchone()
    assert tuple(row) == ("running", "run", "25", 4611)
    card = json.loads((runs_dir / "R1" / "run.json").read_text())
    assert card["run_id"] == "R1"
    assert card["profile"] == "default"
    assert (runs_dir / "R1" / "context_handoff").is_dir()


def test_start_run_stores_batch_id(tracer):
    tracer.start_run("R1", workflow="feature", request="x", batch_id="b1")
    tracer.start_run("R2", workflow="feature", request="x")
    tracer.start_run("R3", workflow="feature", request="x", ports=[4100], batch_id="b2")
    rows = {
        r["run_id"]: r["batch_id"]
        for r in tracer.con.execute("SELECT run_id, batch_id FROM runs")
    }
    assert rows == {"R1": "b1", "R2": None, "R3": "b2"}


def test_update_card_merges_into_run_json(tracer, runs_dir):
    synthetic_run(tracer)
    tracer.update_card("R1", roster=[{"name": "builder", "runtime": "pi", "model": "x/y", "thinking": "high"}])
    card = tracer.card("R1")
    assert card["roster"] == [{"name": "builder", "runtime": "pi", "model": "x/y", "thinking": "high"}]
    assert card["run_id"] == "R1"  # original keys survive
    # file on disk re-reads identically
    assert json.loads((runs_dir / "R1" / "run.json").read_text()) == card


def test_card_is_empty_for_an_unknown_run(tracer):
    assert tracer.card("ghost") == {}


def test_run_folder_layout(tracer, runs_dir):
    synthetic_run(tracer)
    run = runs_dir / "R1"
    assert (run / "run.json").is_file()
    assert (run / "events.jsonl").is_file()
    assert (run / "01-planner" / "prompts").is_dir()
    assert (run / "02-builder" / "prompts").is_dir()
    assert (run / "02-builder" / "raw_output.jsonl").is_file()


def test_continuation_session_gets_dot_one_dir(tracer, runs_dir):
    synthetic_run(tracer)
    sid = tracer.start_session(
        "R1",
        "R1:02",
        seq=2,
        role="builder",
        runtime="claude_code",
        model="claude-fable-5",
        continuation=1,
    )
    assert sid.endswith(".1")
    assert (runs_dir / "R1" / "02-builder.1" / "prompts").is_dir()


# --- port allocation: BEGIN IMMEDIATE, pid-checked (spec §7, §8, ticket 32) ---


def _live_run(tracer: Tracer, run_id: str, port: int) -> None:
    """A `running` Run holding `port`, backed by this test process's own (live) pid."""
    tracer.start_run(run_id, workflow="feature", request="x", port=port)
    tracer.start_process(run_id, kind="run", name="lime", pid=os.getpid(), command="lime run x")


def test_start_run_with_ports_allocates_the_first_free_port(tracer):
    port = tracer.start_run("R1", workflow="feature", request="x", ports=[3100, 3101, 3102])
    assert port == 3100
    row = tracer.con.execute("SELECT port FROM runs WHERE run_id='R1'").fetchone()
    assert row["port"] == 3100


def test_start_run_with_ports_gives_two_live_runs_distinct_ports(tracer):
    p1 = tracer.start_run("R1", workflow="feature", request="x", ports=[3100, 3101, 3102])
    tracer.start_process("R1", kind="run", name="lime", pid=os.getpid(), command="lime run 1")
    p2 = tracer.start_run("R2", workflow="feature", request="y", ports=[3100, 3101, 3102])
    tracer.start_process("R2", kind="run", name="lime", pid=os.getpid(), command="lime run 2")
    assert {p1, p2} == {3100, 3101}


def test_start_run_with_ports_refuses_and_names_the_live_runs_when_none_free(tracer):
    _live_run(tracer, "R1", 3100)
    _live_run(tracer, "R2", 3101)
    with pytest.raises(PortsExhausted) as exc:
        tracer.start_run("R3", workflow="feature", request="z", ports=[3100, 3101])
    assert exc.value.held == {3100: "R1", 3101: "R2"}
    assert "R1" in str(exc.value) and "R2" in str(exc.value)
    # the refusal must not leave a half-written R3 row behind
    assert tracer.con.execute("SELECT 1 FROM runs WHERE run_id='R3'").fetchone() is None


def test_start_run_with_ports_never_double_allocates_before_the_pid_registers(tracer):
    """Regression: `_held_ports` must treat a just-reserved, not-yet-registered
    Run as held — otherwise two concurrent `lime run` processes could land on
    the same port in the gap between `start_run` committing and the caller's
    later `start_process` insert (session.ensure does these as two calls)."""
    p1 = tracer.start_run("R1", workflow="feature", request="x", ports=[3100, 3101])
    p2 = tracer.start_run("R2", workflow="feature", request="y", ports=[3100, 3101])
    assert {p1, p2} == {3100, 3101}


def test_start_run_with_ports_reclaims_a_dead_pids_port(tracer):
    tracer.start_run("R1", workflow="feature", request="x", port=3100)
    tracer.start_process("R1", kind="run", name="lime", pid=999_999_999, command="lime run 1")
    # R1's row still says `running`, but its pid is dead — the port is free again
    port = tracer.start_run("R2", workflow="feature", request="y", ports=[3100])
    assert port == 3100


def test_live_pid_is_none_without_a_registered_process(tracer):
    tracer.start_run("R1", workflow="feature", request="x", port=3100)
    assert tracer.live_pid("R1") is None


def test_live_pid_is_none_once_the_pid_is_dead(tracer):
    tracer.start_run("R1", workflow="feature", request="x", port=3100)
    tracer.start_process("R1", kind="run", name="lime", pid=999_999_999, command="lime run 1")
    assert tracer.live_pid("R1") is None


def test_live_pid_returns_a_registered_alive_pid(tracer):
    tracer.start_run("R1", workflow="feature", request="x", port=3100)
    tracer.start_process("R1", kind="run", name="lime", pid=os.getpid(), command="lime run 1")
    assert tracer.live_pid("R1") == os.getpid()


def test_port_holder_names_the_run_and_excludes_the_given_one(tracer):
    _live_run(tracer, "R1", 3100)
    assert tracer.port_holder(3100) == "R1"
    assert tracer.port_holder(3100, exclude_run_id="R1") is None
    assert tracer.port_holder(3101) is None


# --- status transitions, all on the one row ---


def test_run_transitions_happy_path_stays_one_row(tracer):
    synthetic_run(tracer)
    tracer.set_run_status("R1", "success")
    tracer.set_run_status("R1", "deployed")
    rows = tracer.con.execute("SELECT status FROM runs").fetchall()
    assert [tuple(r) for r in rows] == [("deployed",)]


@pytest.mark.parametrize("bad", ["success", "budget_exhausted"])
def test_run_rejects_a_sideways_jump_out_of_fail(tracer, bad):
    synthetic_run(tracer)
    tracer.set_run_status("R1", "fail")
    with pytest.raises(ValueError):
        tracer.set_run_status("R1", bad)


@pytest.mark.parametrize("stalled", ["fail", "budget_exhausted"])
def test_resume_reopens_a_stalled_run(tracer, monkeypatch, stalled):
    synthetic_run(tracer)
    tracer.set_run_status("R1", stalled)
    crashed_at = run_ended_at(tracer)
    assert crashed_at
    tracer.set_run_status("R1", "running")  # lime run --resume
    assert run_ended_at(tracer) is None  # open again: the dashboard chip ticks
    # the second finish stamps its own time, not the crash's
    later = "2030-01-01T00:00:00+00:00"
    monkeypatch.setattr("lime.tracer._now", lambda: later)
    tracer.set_run_status("R1", "success")
    assert run_ended_at(tracer) == later
    assert run_ended_at(tracer) > crashed_at


@pytest.mark.parametrize("start", ["running", "success", "fail", "budget_exhausted"])
def test_any_live_run_can_be_discarded(tracer, start):
    synthetic_run(tracer)
    if start != "running":
        tracer.set_run_status("R1", start)
    before = run_ended_at(tracer)
    tracer.set_run_status("R1", "discarded")
    status, ended = tracer.con.execute("SELECT status, ended_at FROM runs").fetchone()
    assert status == "discarded"
    assert ended  # a discarded run is finished, whatever it was before
    if before:
        assert ended == before  # a verdict on a finished run keeps its finish time


def test_deployed_keeps_the_success_timestamp(tracer, monkeypatch):
    synthetic_run(tracer)
    tracer.set_run_status("R1", "success")
    finished = run_ended_at(tracer)
    monkeypatch.setattr("lime.tracer._now", lambda: "2030-01-01T00:00:00+00:00")
    tracer.set_run_status("R1", "deployed")
    assert run_ended_at(tracer) == finished


def test_terminal_run_status_sets_ended_at(tracer):
    synthetic_run(tracer)
    tracer.set_run_status("R1", "success")
    assert tracer.con.execute("SELECT ended_at FROM runs").fetchone()[0]


@pytest.mark.parametrize("start", ["running", "success", "fail", "budget_exhausted"])
def test_no_live_status_reaches_needs_rescope(tracer, start):
    synthetic_run(tracer)
    if start != "running":
        tracer.set_run_status("R1", start)
    with pytest.raises(ValueError):
        tracer.set_run_status("R1", "needs_rescope")
    status = tracer.con.execute(
        "SELECT status FROM runs WHERE run_id='R1'"
    ).fetchone()["status"]
    assert status == start


def _plant_legacy_needs_rescope(
    tracer: Tracer, run_id: str = "R1", ended_at: str = "2025-01-02T03:04:05+00:00"
) -> None:
    """A `needs_rescope` row no code path sets any more, planted directly so a Run
    preserved before ticket 193 still loads and can be discarded (ticket 194)."""
    tracer.con.execute(
        "UPDATE runs SET status='needs_rescope', ended_at=? WHERE run_id=?",
        (ended_at, run_id),
    )
    tracer.con.commit()
    handoff = tracer.event(
        run_id,
        "handoff",
        name="needs_rescope",
        payload={
            "path": str(tracer.run_dir(run_id) / "context_handoff" / "needs_rescope.json")
        },
    )
    tracer.event(
        run_id,
        "log",
        parent_id=handoff,
        name="rescope_recovery_decision",
        payload={"decision": "approved"},
    )
    tracer.event(
        run_id,
        "log",
        name="rescope_recovery_started",
        payload={"decision_event_id": handoff},
    )


def test_legacy_needs_rescope_row_loads_and_only_discards(tracer):
    synthetic_run(tracer)
    ended_at = "2025-01-02T03:04:05+00:00"
    _plant_legacy_needs_rescope(tracer, ended_at=ended_at)

    tracer.resume_blocked("R1")  # an old row must not blow up the resume pre-flight
    for bad in ("running", "success"):
        with pytest.raises(ValueError):
            tracer.set_run_status("R1", bad)

    tracer.set_run_status("R1", "discarded")
    row = tracer.con.execute(
        "SELECT status, ended_at FROM runs WHERE run_id='R1'"
    ).fetchone()
    assert row["status"] == "discarded"
    assert row["ended_at"] == ended_at


def test_set_run_status_unknown_status_is_a_value_error(tracer):
    synthetic_run(tracer)
    tracer.con.execute("UPDATE runs SET status='bogus' WHERE run_id='R1'")
    tracer.con.commit()
    with pytest.raises(ValueError):
        tracer.set_run_status("R1", "running")


def test_rescope_survives_only_as_the_legacy_status_string():
    import ast

    import lime

    def names_of(node):
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, ast.Attribute):
            return [node.attr]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return [node.name]
        if isinstance(node, (ast.arg, ast.keyword)):
            return [node.arg]
        if isinstance(node, ast.alias):
            return [node.name, node.asname]
        return []

    offenders = []
    for path in Path(lime.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text())
        legacy_file = path.name in {"tracer.py", "cli.py"}
        for node in ast.walk(tree):
            for name in names_of(node):
                if name and "rescope" in name.lower():
                    offenders.append((str(path), node.lineno, name))
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "rescope" in node.value.lower()
                and not (node.value == "needs_rescope" and legacy_file)
            ):
                offenders.append((str(path), node.lineno, node.value))
    assert offenders == []


def test_phase_transitions_and_retry(tracer):
    synthetic_run(tracer)
    pid = tracer.add_phase("R1", seq=3, name="verify", stage="Build", kind="code", owner="spine")
    with pytest.raises(ValueError):  # success is earned: queued can't jump there
        tracer.set_phase_status(pid, "success")
    tracer.set_phase_status(pid, "running")
    tracer.set_phase_status(pid, "fail", error="tests red")
    tracer.set_phase_status(pid, "running")  # retry
    status, attempt, error = tracer.con.execute(
        "SELECT status, attempt, error FROM phases WHERE phase_id=?", (pid,)
    ).fetchone()
    assert (status, attempt) == ("running", 2)
    tracer.set_phase_status(pid, "success")


def test_totals_accumulate_on_the_run_row(tracer):
    synthetic_run(tracer)
    tracer.add_totals("R1", tokens=20_000, cost=0.25)
    tracer.add_totals("R1", tokens=60_000, cost=0.60)
    tokens, cost = tracer.con.execute(
        "SELECT total_tokens, total_cost FROM runs WHERE run_id='R1'"
    ).fetchone()
    assert (tokens, cost) == (80_000, pytest.approx(0.85))


# --- events ---


def test_event_rejects_unknown_type(tracer):
    synthetic_run(tracer)
    with pytest.raises(ValueError):
        tracer.event("R1", "telemetry")


def test_event_lands_in_db_and_jsonl(tracer, runs_dir):
    synthetic_run(tracer)
    tracer.event("R1", "budget_warn", payload={"context_tokens": 151_000, "limit": 150_000})
    n = tracer.con.execute(
        "SELECT count(*) FROM events WHERE type='budget_warn'"
    ).fetchone()[0]
    assert n == 1
    lines = (runs_dir / "R1" / "events.jsonl").read_text().splitlines()
    last = json.loads(lines[-1])
    assert last["type"] == "budget_warn"
    assert last["payload"]["limit"] == 150_000
    assert len(lines) == 6  # 5 turns + this one


def test_tool_result_event_is_written_and_masked(tracer):
    synthetic_run(tracer)
    tracer.event(
        "R1", "tool_result", name="bash",
        payload={"tool_call_id": "c1", "tool": "bash", "ok": True,
                 "duration_ms": 12, "out_bytes": 40,
                 "tail": "token sk-ant-api03-" + "A1b2C3d4" * 6},
    )
    row = tracer.con.execute(
        "SELECT payload_json FROM events WHERE type='tool_result'"
        " ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    stored = json.loads(row["payload_json"])
    assert "[masked]" in stored["tail"]
    assert "sk-ant-api03-" not in stored["tail"]


def test_event_masks_every_payload_string(tracer, runs_dir, monkeypatch):
    synthetic_run(tracer)
    monkeypatch.setenv("LIME_TEST_API_KEY", "envsecret-249-zq")
    key = "sk-or-v1-" + "0123456789abcdef" * 4
    payload = {
        "args": {"command": f"curl -H 'Authorization: Bearer {key}'"},
        "envelope": {
            "summary": f"used {key}",
            "artifacts": [["deep", "envsecret-249-zq"]],
        },
        "n": 3,
    }
    tracer.event("R1", "tool_call", name="bash", payload=payload)

    row = tracer.con.execute(
        "SELECT payload_json FROM events WHERE type='tool_call'"
        " ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    stored = json.loads(row["payload_json"])
    line = json.loads((runs_dir / "R1" / "events.jsonl").read_text().splitlines()[-1])
    for parsed in (stored, line["payload"]):
        blob = json.dumps(parsed)
        assert key not in blob
        assert "envsecret-249-zq" not in blob
        assert "[masked]" in blob
        assert parsed["args"]["command"] == "curl -H 'Authorization: Bearer [masked]'"
        assert parsed["envelope"]["summary"] == "used [masked]"
        assert parsed["envelope"]["artifacts"] == [["deep", "[masked]"]]
        assert parsed["n"] == 3
    # the caller's payload is not mutated
    assert key in payload["args"]["command"]
    assert key in payload["envelope"]["summary"]


def test_refusal_payload_is_masked(tracer):
    key = "sk-or-v1-" + "0123456789abcdef" * 4
    event_id = tracer.refusal(None, "r", detail=f"token {key}")
    row = tracer.con.execute(
        "SELECT payload_json FROM events WHERE event_id=?", (event_id,)
    ).fetchone()
    assert key not in row["payload_json"]
    assert "[masked]" in row["payload_json"]


def test_only_tracer_event_writes_trace_events():
    root = Path(__file__).resolve().parents[1]
    found = set()
    total = 0
    for path in (root / "lime").rglob("*.py"):
        rel = str(path.relative_to(root))
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "INSERT INTO events" in node.value or node.value == "events.jsonl":
                    total += 1
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if not (
                        isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                    ):
                        continue
                    if "INSERT INTO events" in sub.value:
                        found.add((rel, node.name, "insert"))
                    elif sub.value == "events.jsonl":
                        found.add((rel, node.name, "jsonl"))
    assert found == {
        ("lime/tracer.py", "event", "insert"),
        ("lime/tracer.py", "event", "jsonl"),
        ("lime/tracer.py", "start_run", "jsonl"),
    }
    assert total == 3


# --- lime trace ---


def test_trace_summary_is_at_most_20_lines(tracer, capsys):
    synthetic_run(tracer)
    assert main(["trace", "R1", "--summary"]) == 0
    out = capsys.readouterr().out
    lines = out.rstrip("\n").splitlines()
    assert len(lines) <= 20
    assert "R1" in out and "feature" in out and "running" in out
    assert "plan" in out and "build" in out


def test_trace_summary_defaults_to_latest_run(tracer, capsys):
    synthetic_run(tracer)
    assert main(["trace", "--summary"]) == 0
    assert "R1" in capsys.readouterr().out


def test_trace_default_shows_phases_gates_events(tracer, capsys):
    synthetic_run(tracer)
    assert main(["trace", "R1"]) == 0
    out = capsys.readouterr().out
    assert "planner" in out and "builder" in out
    assert "tests" in out  # the gate row
    assert "turn" in out  # recent events


def test_trace_default_shows_tokens_and_cost_per_phase(tracer, capsys):
    synthetic_run(tracer)  # plan (pi): 2 turns, 10_000+20_000 tokens, $0.10+$0.15
    assert main(["trace", "R1"]) == 0
    lines = capsys.readouterr().out.splitlines()
    header = next(line for line in lines if line.strip().startswith("seq"))
    assert "tokens" in header and "cost" in header
    plan_row = next(line for line in lines if " plan " in f" {line} ")
    assert "30000" in plan_row
    assert "$0.2500" in plan_row


def test_follow_cursor_returns_only_events_past_it(tracer):
    synthetic_run(tracer)
    all_events = _new_events(tracer.con, "R1", 0)
    assert len(all_events) == 5
    cursor = all_events[2]["event_id"]
    assert [e["event_id"] for e in _new_events(tracer.con, "R1", cursor)] == [
        e["event_id"] for e in all_events[3:]
    ]


def test_trace_unknown_run_errors(tracer, capsys):
    synthetic_run(tracer)
    assert main(["trace", "nope"]) == 1
    assert "nope" in capsys.readouterr().err


RETIRED_EVENT_TYPES = {"handoff_truncated", "phase_stalled"}  # ticket 191: no longer written. Old rows still carry them and load into the trace, but ticket 215 dropped the dead members from the dashboard union.


def test_dashboard_event_type_union_mirrors_event_types():
    types_ts = Path(__file__).resolve().parent.parent / "dashboard" / "shared" / "types.ts"
    text = types_ts.read_text()
    start = text.index("export type EventType =")
    end = text.index(";", start)
    block = text[start:end]
    types_in_union = set(re.findall(r'"(\w+)"', block))
    assert types_in_union == EVENT_TYPES


def test_retired_event_types_are_refused_at_write(tracer):
    synthetic_run(tracer)
    for name in RETIRED_EVENT_TYPES:
        with pytest.raises(ValueError):
            tracer.event("R1", name, name="x")


def test_old_retired_events_still_load_and_show_in_trace(tracer, runs_dir, capsys):
    synthetic_run(tracer)
    for name in RETIRED_EVENT_TYPES:
        tracer.con.execute(
            "INSERT INTO events (run_id, phase_id, type, name, payload_json, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("R1", None, name, "builder", "{}", "2026-10-10T12:00:00+00:00"),
        )
    tracer.con.commit()
    Tracer(runs_dir)  # the load must not raise on the retired names
    assert main(["trace", "R1"]) == 0
    out = capsys.readouterr().out
    assert "handoff_truncated" in out and "phase_stalled" in out


def test_trace_without_db_errors(runs_dir, capsys):
    assert main(["trace"]) == 1
    assert "no trace database" in capsys.readouterr().err


# --- lime cost ---


def test_cost_matches_hand_summed_fixture(tracer, capsys):
    synthetic_run(tracer)
    assert main(["cost", "R1"]) == 0
    out = capsys.readouterr().out
    # by provider: x-ai = 0.10+0.15, claude_code = 3×0.20 (subscription, notional)
    assert "x-ai" in out and "$0.25" in out
    assert "claude_code" in out and "$0.60" in out
    assert "notional" in out
    # then by role
    assert "planner" in out and "builder" in out


def test_cost_default_window_is_seven_days(tracer, capsys):
    synthetic_run(tracer)
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    tracer.con.execute(
        "UPDATE events SET started_at=? WHERE type='turn' AND phase_id='R1:01'", (old,)
    )
    tracer.con.commit()
    assert main(["cost"]) == 0
    out = capsys.readouterr().out
    assert "$0.60" in out  # builder turns still counted
    assert "$0.25" not in out  # planner turns aged out
    assert main(["cost", "R1"]) == 0  # a named run sums its whole life
    assert "$0.25" in capsys.readouterr().out


# --- replay_map: what `lime run --resume` skips instead of re-running (spec §9) ---


def test_replay_map_keeps_the_latest_success_per_name_and_skips_failures(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    first = tracer.add_phase("R1", seq=1, name="plan", stage="Build", kind="agent", owner="planner")
    tracer.set_phase_status(first, "running")
    tracer.set_phase_status(first, "success")
    tracer.record_envelope(
        "R1", first, agent="planner", output_type="PlanOutput",
        payload_json='{"status": "success", "summary": "old"}', valid=True, attempt=1,
    )
    failed = tracer.add_phase("R1", seq=2, name="push_pr", stage="Build", kind="code", owner="git")
    tracer.set_phase_status(failed, "running")
    tracer.set_phase_status(failed, "fail", error="rejected")
    again = tracer.add_phase("R1", seq=3, name="plan", stage="Build", kind="agent", owner="planner")
    tracer.set_phase_status(again, "running")
    tracer.set_phase_status(again, "success")
    tracer.record_envelope(
        "R1", again, agent="planner", output_type="PlanOutput",
        payload_json='{"status": "success", "summary": "newer"}', valid=True, attempt=1,
    )

    replay = tracer.replay_map("R1")
    assert set(replay) == {"plan"}  # the failed push_pr must re-run, not replay
    assert replay["plan"]["phase_id"] == again
    assert "newer" in replay["plan"]["payload_json"]


def test_replay_map_serves_the_latest_valid_envelope(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    phase = tracer.add_phase("R1", seq=1, name="build_1", stage="Build", kind="agent", owner="builder")
    tracer.set_phase_status(phase, "running")
    tracer.set_phase_status(phase, "success")
    tracer.record_envelope(
        "R1", phase, agent="builder", output_type="BuildOutput",
        payload_json='{"status": "success"}', valid=True, attempt=1,
    )
    tracer.record_envelope(
        "R1", phase, agent="builder", output_type="BuildOutput",
        payload_json='{"raw": "garbage"}', valid=False, attempt=2,
    )

    replay = tracer.replay_map("R1")
    assert json.loads(replay["build_1"]["payload_json"]) == {"status": "success"}


def test_replay_map_leaves_envelope_fields_none_for_a_success_without_one(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    phase = tracer.add_phase("R1", seq=1, name="commit_code", stage="Build", kind="code", owner="git")
    tracer.set_phase_status(phase, "running")
    tracer.set_phase_status(phase, "success")

    replay = tracer.replay_map("R1")
    assert replay["commit_code"] == {
        "phase_id": phase, "output_type": None, "payload_json": None, "seq": 1,
    }


def test_invalidate_from_an_explicit_seq_spares_a_running_row(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    _success_phase_in(tracer, "R1", "verify_1", 1)
    _success_phase_in(tracer, "R1", "review", 2)
    running = tracer.add_phase("R1", seq=3, name="verify_1", stage="Build", kind="code", owner="git")
    tracer.set_phase_status(running, "running")

    assert tracer.invalidate_from("R1", "verify_1", reason="x", seq=1) == [
        "verify_1", "review",
    ]
    assert tracer.con.execute(
        "SELECT COUNT(*) FROM events WHERE phase_id=? AND name='phase_invalidated'",
        (running,),
    ).fetchone()[0] == 0


def test_rereview_after_a_resumed_rerun_still_drops_the_review(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    _success_phase_in(tracer, "R1", "verify_1", 1)
    _success_phase_in(tracer, "R1", "build_2", 2)
    _success_phase_in(tracer, "R1", "verify_2", 3)
    _success_phase_in(tracer, "R1", "review", 4)
    _success_phase_in(tracer, "R1", "verify_2", 5)  # the resume's passing re-run

    assert tracer.rereview_target("R1") == ("verify_2", 3)
    assert tracer.rereview("R1", engineer="t") == ["verify_2", "review", "verify_2"]
    replay = tracer.replay_map("R1")
    assert "verify_1" in replay and "build_2" in replay
    assert "review" not in replay and "verify_2" not in replay


def test_latest_verify_name_is_the_highest_seq_verify_of_any_status(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    assert tracer.latest_verify_name("R1") is None
    _success_phase_in(tracer, "R1", "verify_1", 1)
    _success_phase_in(tracer, "R1", "review", 2)
    assert tracer.latest_verify_name("R1") == "verify_1"  # a later non-verify row is skipped
    running = tracer.add_phase("R1", seq=3, name="verify_2_retry3", stage="Build", kind="code", owner="git")
    tracer.set_phase_status(running, "running")
    assert tracer.latest_verify_name("R1") == "verify_2_retry3"
    _success_phase_in(tracer, "R1", "verify_2", 4)
    assert tracer.latest_verify_name("R1") == "verify_2"
    tracer.invalidate_from("R1", "verify_2", reason="x")
    assert tracer.latest_verify_name("R1") == "verify_2"  # invalidated rows count


def test_invalidate_from_drops_the_named_phase_and_everything_after_it(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    eng = tracer.add_phase("R1", seq=1, name="engineer", stage="Build", kind="agent", owner="engineer")
    tracer.set_phase_status(eng, "running")
    tracer.set_phase_status(eng, "success")
    plan = tracer.add_phase("R1", seq=2, name="plan", stage="Build", kind="agent", owner="planner")
    tracer.set_phase_status(plan, "running")
    tracer.set_phase_status(plan, "success")
    tracer.record_envelope("R1", plan, agent="planner", output_type="PlanOutput",
                           payload_json='{"status": "ok"}', valid=True, attempt=1)
    b1 = tracer.add_phase("R1", seq=3, name="build_1", stage="Build", kind="agent", owner="builder")
    tracer.set_phase_status(b1, "running")
    tracer.set_phase_status(b1, "success")
    tracer.record_envelope("R1", b1, agent="builder", output_type="BuildOutput",
                           payload_json='{"status": "ok"}', valid=True, attempt=1)
    v1 = tracer.add_phase("R1", seq=4, name="verify_1", stage="Build", kind="code", owner="spine")
    tracer.set_phase_status(v1, "running")
    tracer.set_phase_status(v1, "success")

    dropped = tracer.invalidate_from("R1", "plan", reason="boom")
    assert dropped == ["plan", "build_1", "verify_1"]
    assert set(tracer.replay_map("R1")) == {"engineer"}


def test_invalidate_from_is_a_noop_when_the_named_phase_never_succeeded(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    eng = tracer.add_phase("R1", seq=1, name="engineer", stage="Build", kind="agent", owner="engineer")
    tracer.set_phase_status(eng, "running")
    tracer.set_phase_status(eng, "success")

    assert tracer.invalidate_from("R1", "plan", reason="x") == []
    assert set(tracer.replay_map("R1")) == {"engineer"}


def test_resume_blocked_names_the_run_level_reason_when_every_phase_succeeded(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    plan = tracer.add_phase("R1", seq=1, name="plan", stage="Build", kind="agent", owner="planner")
    tracer.set_phase_status(plan, "running")
    tracer.set_phase_status(plan, "success")
    review = tracer.add_phase("R1", seq=2, name="review", stage="Build", kind="agent", owner="reviewer")
    tracer.set_phase_status(review, "running")
    tracer.set_phase_status(review, "success")
    tracer.event("R1", "error", name="not_accepted", payload={"reason": "step 1 still failed verify"})

    blocked = tracer.resume_blocked("R1")
    assert "step 1" in blocked

    tracer.invalidate_from("R1", "plan", reason="boom")
    assert tracer.resume_blocked("R1") is None


def test_resume_blocked_is_none_when_a_phase_failed(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    plan = tracer.add_phase("R1", seq=1, name="plan", stage="Build", kind="agent", owner="planner")
    tracer.set_phase_status(plan, "running")
    tracer.set_phase_status(plan, "success")
    push = tracer.add_phase("R1", seq=2, name="push_pr", stage="Build", kind="code", owner="git")
    tracer.set_phase_status(push, "running")
    tracer.set_phase_status(push, "fail", error="rejected")

    assert tracer.resume_blocked("R1") is None


# --- session_index independence from continuation ---


@pytest.mark.parametrize("sequence", [[0, 1, 1], [0, 0, 1]])
def test_session_index_is_independent_of_continuation(tracer, runs_dir, sequence):
    """Explicit session_index produces unique dirs/row IDs regardless of continuation values."""
    tracer.start_run("R1", workflow="feature", request="x")
    phase = tracer.add_phase("R1", seq=1, name="build", stage="Build", kind="agent", owner="builder")
    tracer.set_phase_status(phase, "running")

    session_ids = []
    for continuation_val, session_idx in zip(sequence, [0, 1, 2]):
        sid = tracer.start_session(
            "R1", phase, seq=1, role="builder", runtime="pi", model="x",
            continuation=continuation_val, session_index=session_idx,
        )
        session_ids.append(sid)
        # Verify row IDs are unique
        row_ids = [
            r[0] for r in tracer.con.execute(
                "SELECT session_id FROM sessions WHERE run_id='R1'"
            ).fetchall()
        ]
        assert len(row_ids) == len(set(row_ids))

    # Three unique session IDs
    assert len(set(session_ids)) == 3

    # Three unique directories
    dirs = [
        runs_dir / "R1" / (f"01-builder" + (f".{i}" if i else ""))
        for i in range(3)
    ]
    for d in dirs:
        assert d.is_dir(), f"{d} should exist"
    assert len({str(d) for d in dirs}) == 3

    # DB stores the continuation values as given, not the session_index
    continuation_vals = sorted(
        r[0] for r in tracer.con.execute(
            "SELECT continuation FROM sessions WHERE run_id='R1'"
        ).fetchall()
    )
    assert continuation_vals == sorted(sequence)

    # Sentinels preserved after each later creation
    for i in range(3):
        d = runs_dir / "R1" / (f"01-builder" + (f".{i}" if i else ""))
        assert (d / "prompts").is_dir()
        assert (d / "raw_output.jsonl").is_file()


def test_legacy_caller_without_session_index(tracer, runs_dir):
    """Old callers without explicit session_index retain legacy behavior:
    directory name derived from continuation."""
    tracer.start_run("R1", workflow="feature", request="x")
    phase = tracer.add_phase("R1", seq=1, name="build", stage="Build", kind="agent", owner="builder")
    tracer.set_phase_status(phase, "running")

    # continuation=0 -> 01-builder
    sid = tracer.start_session(
        "R1", phase, seq=1, role="builder", runtime="pi", model="x",
        continuation=0,
    )
    assert sid.endswith("01-builder")
    assert (runs_dir / "R1" / "01-builder" / "prompts").is_dir()

    # continuation=1 -> 01-builder.1
    sid2 = tracer.start_session(
        "R1", phase, seq=1, role="builder", runtime="pi", model="x",
        continuation=1,
    )
    assert sid2.endswith("01-builder.1")
    assert (runs_dir / "R1" / "01-builder.1" / "prompts").is_dir()


def test_explicit_session_index_zero_overrides_continuation_one(tracer, runs_dir):
    """session_index=0 with continuation=1 produces dir 01-builder (ordinal 0),
    not 01-builder.1."""
    tracer.start_run("R1", workflow="feature", request="x")
    phase = tracer.add_phase("R1", seq=1, name="build", stage="Build", kind="agent", owner="builder")
    tracer.set_phase_status(phase, "running")

    sid = tracer.start_session(
        "R1", phase, seq=1, role="builder", runtime="pi", model="x",
        continuation=1, session_index=0,
    )
    assert sid.endswith("01-builder")
    assert not sid.endswith("01-builder.1")
    assert (runs_dir / "R1" / "01-builder" / "prompts").is_dir()
    assert not (runs_dir / "R1" / "01-builder.1" / "prompts").is_dir()


# --- ticket 132: stale-verify invalidation and replay-eligible envelope selection ---


def _success_phase(tracer, name, seq, output_type=None, payload=None):
    phase = tracer.add_phase("R1", seq=seq, name=name, stage="Build", kind="code", owner="git")
    tracer.set_phase_status(phase, "running")
    tracer.set_phase_status(phase, "success")
    if output_type is not None:
        tracer.record_envelope(
            "R1", phase, agent="git", output_type=output_type,
            payload_json=json.dumps(payload), valid=True, attempt=1,
        )
    return phase


def test_stale_verify_invalidation_removes_dependent_replay(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    _success_phase(tracer, "build_1", 1)
    _success_phase(tracer, "verify_1", 2, "VerifyOutput", {"status": "success", "passed": True})
    _success_phase(tracer, "review", 3, "ReviewOutput", {"status": "success", "approved": True})
    _success_phase(tracer, "commit_code", 4)

    assert set(tracer.replay_map("R1")) == {"build_1", "verify_1", "review", "commit_code"}
    dropped = tracer.invalidate_from("R1", "verify_1", reason="stale verify")
    assert dropped == ["verify_1", "review", "commit_code"]

    # Persistent: a reconstructed Run (fresh Tracer) still sees the invalidation.
    replay = Tracer(tracer.runs_dir).replay_map("R1")
    assert set(replay) == {"build_1"}


def test_latest_verify_ignores_invalidated_envelopes(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    stale = _success_phase(
        tracer, "verify_1", 1, "VerifyOutput",
        {"status": "success", "passed": True, "summary": "old"},
    )
    assert "old" in tracer.latest_envelope("R1", "VerifyOutput")

    tracer.invalidate_from("R1", "verify_1", reason="stale")
    assert tracer.latest_envelope("R1", "VerifyOutput") is None
    # the invalidated phase row is untouched, only its replay eligibility changed
    assert tracer.con.execute(
        "SELECT status FROM phases WHERE phase_id=?", (stale,)
    ).fetchone()["status"] == "success"

    # a fresh verify supersedes it and is served
    _success_phase(
        tracer, "verify_1", 2, "VerifyOutput",
        {"status": "success", "passed": True, "summary": "new"},
    )
    assert "new" in tracer.latest_envelope("R1", "VerifyOutput")


def test_plan_provenance_is_associated_with_the_correct_successful_phase(tracer):
    from lime.data_types import PlanProvenance

    tracer.start_run("R1", workflow="feature", request="x")
    plan = tracer.add_phase("R1", seq=1, name="plan", stage="Build", kind="agent", owner="planner")
    tracer.set_phase_status(plan, "running")
    tracer.set_phase_status(plan, "success")
    prov = PlanProvenance(ticket_sha256="a" * 64, prompt="ask", plan_sha256="b" * 64)
    tracer.event("R1", "handoff", phase_id=plan, name="plan_provenance", payload=prov.model_dump())

    other = tracer.add_phase("R1", seq=2, name="other", stage="Build", kind="agent", owner="planner")
    tracer.set_phase_status(other, "running")
    tracer.set_phase_status(other, "success")

    row = tracer.con.execute(
        "SELECT payload_json FROM events WHERE run_id='R1' AND phase_id=? AND name='plan_provenance'",
        (plan,),
    ).fetchone()
    assert row is not None
    assert json.loads(row["payload_json"])["ticket_sha256"] == "a" * 64
    assert tracer.con.execute(
        "SELECT 1 FROM events WHERE phase_id=? AND name='plan_provenance'", (other,)
    ).fetchone() is None


# --- pinned birth identity: runs.base_sha (ticket 144) ---


def test_runs_has_the_base_sha_column(tracer):
    cols = {r[1] for r in tracer.con.execute("PRAGMA table_info(runs)")}
    assert "base_sha" in cols


def test_base_sha_round_trips_row_and_card(tracer, runs_dir):
    tracer.start_run("P1", workflow="feature", request="x", base_sha="a" * 40)
    row = tracer.con.execute("SELECT base_sha FROM runs WHERE run_id='P1'").fetchone()
    assert row["base_sha"] == "a" * 40
    assert json.loads((runs_dir / "P1" / "run.json").read_text())["base_sha"] == "a" * 40


def test_base_sha_round_trips_through_the_ports_path(tracer, runs_dir):
    tracer.start_run("P2", workflow="feature", request="x", ports=[4700], base_sha="b" * 40)
    row = tracer.con.execute("SELECT port, base_sha FROM runs WHERE run_id='P2'").fetchone()
    assert row["port"] == 4700
    assert row["base_sha"] == "b" * 40
    assert json.loads((runs_dir / "P2" / "run.json").read_text())["base_sha"] == "b" * 40


def test_omitted_base_sha_stays_null(tracer, runs_dir):
    tracer.start_run("P3", workflow="feature", request="x")
    row = tracer.con.execute("SELECT base_sha FROM runs WHERE run_id='P3'").fetchone()
    assert row["base_sha"] is None
    assert json.loads((runs_dir / "P3" / "run.json").read_text())["base_sha"] is None


# --- ledger indexes in Tracer._migrate (ticket 215) ---


def _index_names(db: Path) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_%'"
            )
        }
    finally:
        con.close()


def test_migrate_creates_four_to_seven_ledger_indexes(runs_dir):
    assert 4 <= len(_INDEXES) <= 7
    Tracer(runs_dir)
    assert _index_names(runs_dir / "lime.db") == set(_INDEXES)


def test_reopening_an_indexed_ledger_is_a_no_op(runs_dir):
    first = Tracer(runs_dir)
    names = _index_names(runs_dir / "lime.db")
    first.start_run("R1", workflow="feature", request="x")
    reopened = Tracer(runs_dir)  # reopening must not raise and must not change anything
    assert _index_names(runs_dir / "lime.db") == names
    row = reopened.con.execute("SELECT run_id FROM runs WHERE run_id='R1'").fetchone()
    assert row["run_id"] == "R1"


def test_raw_sql_ledger_without_indexes_gains_them_on_writer_open(runs_dir):
    runs_dir.mkdir(parents=True)
    db = runs_dir / "lime.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO events (run_id, type, started_at) VALUES ('R1', 'turn', '2020-01-01T00:00:00Z')"
    )
    con.commit()
    con.close()

    assert _index_names(db) == set()  # a plain SQLite build has no ix_ indexes

    connect(db, readonly=True).close()
    assert _index_names(db) == set()  # a read-only open never writes an index

    Tracer(runs_dir)
    assert _index_names(db) == set(_INDEXES)
    con = sqlite3.connect(db)
    try:
        row = con.execute("SELECT run_id FROM events").fetchone()
    finally:
        con.close()
    assert row[0] == "R1"  # the events row survives the migration


def test_legacy_ledger_without_base_sha_gains_indexes(runs_dir):
    runs_dir.mkdir(parents=True)
    _legacy_ledger(runs_dir / "lime.db")
    Tracer(runs_dir)
    db = runs_dir / "lime.db"
    con = sqlite3.connect(db)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    finally:
        con.close()
    assert "base_sha" in cols
    assert _index_names(db) == set(_INDEXES)


def test_last_event_subselect_uses_the_run_index(runs_dir):
    tracer = Tracer(runs_dir)
    plan = " ".join(
        r[3]
        for r in tracer.con.execute(
            "EXPLAIN QUERY PLAN SELECT max(started_at) FROM events WHERE run_id = ?", ("R1",)
        )
    )
    assert "ix_events_run_started" in plan


_OLD_RUNS_SCHEMA = """
CREATE TABLE runs (
    run_id       TEXT PRIMARY KEY,
    workflow     TEXT NOT NULL,
    request      TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    engineer     TEXT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost   REAL NOT NULL DEFAULT 0,
    archived     INTEGER NOT NULL DEFAULT 0,
    ticket       TEXT,
    branch       TEXT,
    worktree     TEXT,
    port         INTEGER,
    db_name      TEXT,
    profile      TEXT,
    kind         TEXT NOT NULL DEFAULT 'run'
);
"""


def test_existing_schema_gains_nullable_base_sha_without_backfill(runs_dir):
    runs_dir.mkdir(parents=True)
    con = sqlite3.connect(runs_dir / "lime.db")
    con.executescript(_OLD_RUNS_SCHEMA)
    con.execute(
        "INSERT INTO runs (run_id, workflow, request, engineer, started_at, ticket,"
        " branch, worktree, port, db_name, profile, kind)"
        " VALUES ('OLD', 'feature', 'x', 'e', '2020-01-01T00:00:00Z', '25', 'b', 'w',"
        " 4611, 'db', 'default', 'run')"
    )
    con.commit()
    con.close()

    Tracer(runs_dir)  # first open migrates
    Tracer(runs_dir)  # reopening is safe/idempotent

    con = sqlite3.connect(runs_dir / "lime.db")
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    assert "base_sha" in cols
    row = con.execute("SELECT * FROM runs WHERE run_id='OLD'").fetchone()
    assert row["base_sha"] is None  # never backfilled
    assert (row["ticket"], row["branch"], row["port"], row["db_name"]) == (
        "25", "b", 4611, "db",
    )  # existing data preserved
    con.close()


def _legacy_ledger(db: Path) -> None:
    con = sqlite3.connect(db)
    con.executescript(_OLD_RUNS_SCHEMA)
    con.execute(
        "INSERT INTO runs (run_id, workflow, request, engineer, started_at, ticket,"
        " branch, worktree, port, db_name, profile, kind)"
        " VALUES ('OLD', 'feature', 'x', 'e', '2020-01-01T00:00:00Z', '25', 'b', 'w',"
        " 4611, 'db', 'default', 'run')"
    )
    con.commit()
    con.close()


def test_readonly_connect_reads_a_pre_144_ledger_without_writing_it(tmp_path):
    db = tmp_path / "lime.db"
    _legacy_ledger(db)

    for _ in range(2):
        con = connect(db, readonly=True)
        rows = [tuple(r) for r in con.execute("SELECT run_id, base_sha FROM runs").fetchall()]
        assert rows == [("OLD", None)]  # the TEMP view reads the absent column as NULL
        row = con.execute("SELECT * FROM runs WHERE run_id='OLD'").fetchone()
        assert row["base_sha"] is None
        assert row["ticket"] == "25"
        con.close()

    check = sqlite3.connect(db)
    cols = {r[1] for r in check.execute("PRAGMA table_info(runs)")}
    assert "base_sha" not in cols  # the ledger file was never altered
    check.close()


def test_readonly_connect_leaves_a_current_ledger_unshadowed(runs_dir):
    Tracer(runs_dir).start_run("R1", workflow="feature", request="x", base_sha="a" * 40)
    con = connect(runs_dir / "lime.db", readonly=True)
    row = con.execute("SELECT base_sha FROM runs WHERE run_id='R1'").fetchone()
    assert row["base_sha"] == "a" * 40  # read straight from main.runs
    assert con.execute(
        "SELECT name FROM sqlite_temp_master WHERE name='runs'"
    ).fetchone() is None  # no shadowing view
    con.close()


def test_readonly_connect_on_a_db_without_runs_table(tmp_path):
    db = tmp_path / "lime.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE other(x)")
    con.commit()
    con.close()

    con = connect(db, readonly=True)  # does not raise
    assert con.execute(
        "SELECT name FROM sqlite_temp_master WHERE name='runs'"
    ).fetchone() is None
    con.close()


def test_only_tracer_opens_a_sqlite_connection():
    root = Path(__file__).resolve().parents[1]
    openers = set()
    for path in (root / "lime").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sqlite3"
            ):
                openers.add(str(path.relative_to(root)))
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "sqlite3"
                and any(a.name == "connect" for a in node.names)
            ):
                openers.add(str(path.relative_to(root)))
    assert openers == {"lime/tracer.py"}  # every opener must go through tracer.connect


def _rereview_seed(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    ids = {}
    names = ["scout", "plan", "build_1", "verify_1", "build_2", "verify_2", "layout",
             "review", "revise_1", "verify_2_retry3", "review_retry1"]
    for seq, name in enumerate(names, start=1):
        ids[name] = _success_phase(tracer, name, seq)
    return ids


def test_rereview_target_is_the_last_live_verify_before_the_first_live_review(tracer):
    _rereview_seed(tracer)
    assert tracer.rereview_target("R1") == ("verify_2", 6)

    # an earlier invalidated verify_2 plus a later live one: the live one wins
    tracer.start_run("R2", workflow="feature", request="x")
    old = tracer.add_phase("R2", seq=1, name="verify_2", stage="Build", kind="code", owner="git")
    tracer.set_phase_status(old, "running")
    tracer.set_phase_status(old, "success")
    tracer.event("R2", "log", phase_id=old, name="phase_invalidated", payload={})
    new = tracer.add_phase("R2", seq=2, name="verify_2", stage="Build", kind="code", owner="git")
    tracer.set_phase_status(new, "running")
    tracer.set_phase_status(new, "success")
    rev = tracer.add_phase("R2", seq=3, name="review", stage="Build", kind="agent", owner="reviewer")
    tracer.set_phase_status(rev, "running")
    tracer.set_phase_status(rev, "success")
    assert tracer.rereview_target("R2") == ("verify_2", 2)


def test_rereview_target_is_none_without_a_live_review(tracer):
    tracer.start_run("R1", workflow="feature", request="x")
    _success_phase(tracer, "verify_1", 1)
    assert tracer.rereview_target("R1") is None  # no review row

    failed = tracer.add_phase("R1", seq=2, name="review", stage="Build", kind="agent", owner="reviewer")
    tracer.set_phase_status(failed, "running")
    tracer.set_phase_status(failed, "fail")
    assert tracer.rereview_target("R1") is None  # only a failed review

    tracer.start_run("R2", workflow="feature", request="x")
    for seq, name in enumerate(["verify_1", "review"], start=1):
        pid = tracer.add_phase("R2", seq=seq, name=name, stage="Build", kind="code", owner="git")
        tracer.set_phase_status(pid, "running")
        tracer.set_phase_status(pid, "success")
    assert tracer.rereview_target("R2") == ("verify_1", 1)
    tracer.invalidate_from("R2", "review", reason="x")
    assert tracer.rereview_target("R2") is None  # review invalidated
    assert tracer.rereview("R2", engineer="t") == []


def test_rereview_records_the_decision_then_invalidates_the_suffix(tracer):
    _rereview_seed(tracer)

    dropped = tracer.rereview("R1", engineer="t")
    assert dropped == ["verify_2", "layout", "review", "revise_1", "verify_2_retry3", "review_retry1"]

    row = tracer.con.execute(
        "SELECT payload_json FROM events WHERE run_id='R1' AND name='rereview_decision'"
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["verify"] == "verify_2" and payload["seq"] == 6 and payload["engineer"] == "t"

    assert tracer.rereview("R1", engineer="t") == []  # no live review left
    assert tracer.resume_blocked("R1") is None


def _success_phase_in(tracer, run_id, name, seq):
    pid = tracer.add_phase(run_id, seq=seq, name=name, stage="Build", kind="code", owner="git")
    tracer.set_phase_status(pid, "running")
    tracer.set_phase_status(pid, "success")
    return pid


def test_rereview_after_a_lost_invalidation_is_idempotent(tracer):
    _rereview_seed(tracer)
    tracer.event(
        "R1", "log", name="rereview_decision",
        payload={"reason": "engineer requested --rereview", "engineer": "t",
                 "verify": "verify_2", "seq": 6},
    )  # crash: the invalidation never landed
    assert tracer.rereview_target("R1") == ("verify_2", 6)

    dropped = tracer.rereview("R1", engineer="t")
    assert dropped[0] == "verify_2" and "review" in dropped
    decisions = tracer.con.execute(
        "SELECT COUNT(*) FROM events WHERE name='rereview_decision'"
    ).fetchone()[0]
    assert decisions == 2


def test_invalidation_and_rereview_events_have_one_writer():
    import ast

    import lime

    def refs(node, names, consts):
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and n.id in names:
                return True
            if isinstance(n, ast.Attribute) and n.attr in names:
                return True
            if isinstance(n, ast.Constant) and n.value in consts:
                return True
        return False

    offenders = []
    for path in Path(lime.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text())
        owners: dict[int, str] = {}

        def walk(node, stack):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    walk(child, stack + [child.name])
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    walk(child, stack + [child.name])
                else:
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "event"
                    ):
                        args = ast.Module(
                            body=[ast.Expr(a) for a in [*child.args, *[k.value for k in child.keywords]]],
                            type_ignores=[],
                        )
                        where = ".".join(stack)
                        if refs(args, {"INVALIDATED"}, {"phase_invalidated"}) and where != "Tracer.invalidate_from":
                            offenders.append((str(path), child.lineno, where))
                        if refs(args, {"REREVIEW_DECISION"}, {"rereview_decision"}) and where != "Tracer.rereview":
                            offenders.append((str(path), child.lineno, where))
                    walk(child, stack)

        walk(tree, [])
    assert offenders == []


# --- ticket 216: PR number/URL on the Run row + the refusal event ---


def test_runs_has_the_pr_columns(tracer):
    cols = {r[1] for r in tracer.con.execute("PRAGMA table_info(runs)")}
    assert {"pr_number", "pr_url"} <= cols


def test_set_pr_survives_status_totals_and_reopen(tracer, runs_dir):
    tracer.start_run("P1", workflow="feature", request="x")
    url = "https://github.com/o/r/pull/7"
    tracer.set_pr("P1", 7, url)
    tracer.set_run_status("P1", "success")
    tracer.add_totals("P1", tokens=5, cost=0.1)
    tracer.con.close()

    tracer = Tracer(runs_dir)  # reopened writer keeps the pair
    row = tracer.con.execute("SELECT pr_number, pr_url FROM runs WHERE run_id='P1'").fetchone()
    assert (row["pr_number"], row["pr_url"]) == (7, url)

    tracer.set_pr("P1", 7, url)  # idempotent: a re-run rewrites the same values
    row = tracer.con.execute("SELECT pr_number, pr_url FROM runs WHERE run_id='P1'").fetchone()
    assert (row["pr_number"], row["pr_url"]) == (7, url)

    tracer.start_run("P2", workflow="feature", request="y")  # never set → NULL pair
    row = tracer.con.execute("SELECT pr_number, pr_url FROM runs WHERE run_id='P2'").fetchone()
    assert (row["pr_number"], row["pr_url"]) == (None, None)


def test_only_set_pr_writes_the_pr_columns():
    import lime

    runs_re = re.compile(r"\b(UPDATE|INSERT INTO)\s+runs\b")
    prs_re = re.compile(r"\bpr_(number|url)\b")
    hits = set()
    for path in Path(lime.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            consts = [
                n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            if any(runs_re.search(c) for c in consts) and any(prs_re.search(c) for c in consts):
                hits.add((path.name, node.name))
    assert hits == {("tracer.py", "set_pr")}


def test_post_144_ledger_gains_pr_columns_on_writer_open_only(tmp_path):
    db = tmp_path / "lime.db"
    _legacy_ledger(db)
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE runs ADD COLUMN base_sha TEXT")
    con.commit()
    con.close()

    con = connect(db, readonly=True)
    row = con.execute("SELECT pr_number, pr_url FROM runs WHERE run_id='OLD'").fetchone()
    assert tuple(row) == (None, None)  # the TEMP view reads the absent columns as NULL
    assert runs_lacks_added_columns(con) is False  # base_sha is present → not legacy
    con.close()

    check = sqlite3.connect(db)
    cols = {r[1] for r in check.execute("PRAGMA table_info(runs)")}
    check.close()
    assert "pr_number" not in cols and "pr_url" not in cols  # the reader never wrote it

    Tracer(tmp_path)  # writer open migrates the pair in
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    assert {"pr_number", "pr_url"} <= cols
    row = con.execute("SELECT pr_number, pr_url FROM runs WHERE run_id='OLD'").fetchone()
    assert (row["pr_number"], row["pr_url"]) == (None, None)  # never backfilled
    con.close()


def test_pre_254_ledger_gains_batch_id_on_writer_open_only(tmp_path):
    db = tmp_path / "lime.db"
    _legacy_ledger(db)
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE runs ADD COLUMN base_sha TEXT")
    con.execute("ALTER TABLE runs ADD COLUMN pr_number INTEGER")
    con.execute("ALTER TABLE runs ADD COLUMN pr_url TEXT")
    con.commit()
    con.close()

    con = connect(db, readonly=True)
    row = con.execute("SELECT batch_id FROM runs WHERE run_id='OLD'").fetchone()
    assert row["batch_id"] is None  # the TEMP view reads the absent column as NULL
    con.close()

    check = sqlite3.connect(db)
    cols = {r[1] for r in check.execute("PRAGMA table_info(runs)")}
    check.close()
    assert "batch_id" not in cols  # the reader never wrote it

    Tracer(tmp_path)  # writer open migrates it in
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    assert "batch_id" in cols
    row = con.execute("SELECT batch_id FROM runs WHERE run_id='OLD'").fetchone()
    assert row["batch_id"] is None  # never backfilled
    con.close()


def test_refusal_before_any_run_lands_in_the_ledger_only(tracer, runs_dir):
    tracer.refusal(
        "41-toy", "overlap_refused",
        holders={"Run deadbeef (lime/feature/70-x)": ["app.py"]},
    )
    rows = tracer.con.execute("SELECT run_id, type, name, payload_json FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0]["run_id"] == NO_RUN
    assert rows[0]["type"] == "refusal"
    assert rows[0]["name"] == "overlap_refused"
    assert json.loads(rows[0]["payload_json"]) == {
        "ticket": "41-toy",
        "holders": {"Run deadbeef (lime/feature/70-x)": ["app.py"]},
    }
    assert tracer.con.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert not (runs_dir / NO_RUN).exists()  # no Run folder for a NO_RUN row


def test_refusal_on_a_run_appends_its_events_jsonl(tracer, runs_dir):
    synthetic_run(tracer)
    tracer.refusal("41-toy", "ci_red", run_id="R1")
    lines = (runs_dir / "R1" / "events.jsonl").read_text().splitlines()
    last = json.loads(lines[-1])
    assert last["type"] == "refusal"
    assert last["name"] == "ci_red"
    assert last["payload"] == {"ticket": "41-toy"}


# --- summaries (ticket 235) ---


def test_legacy_ledger_gains_summaries_on_writer_open_only(tmp_path):
    db = tmp_path / "lime.db"
    _legacy_ledger(db)  # only a `runs` table

    con = connect(db, readonly=True)
    assert con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='summaries'"
    ).fetchone() is None
    con.close()

    check = sqlite3.connect(db)
    names = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check.close()
    assert "summaries" not in names  # the reader never wrote it

    Tracer(tmp_path)  # the writer open creates it
    check = sqlite3.connect(db)
    names = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check.close()
    assert "summaries" in names


def test_claim_summary_skips_ok_same_fingerprint_and_reclaims_changed_or_failed(tracer):
    assert tracer.claim_summary("k", "fp1") is True
    assert tracer.finish_summary("k", "fp1", summary="do it", error=None) is True
    assert tracer.claim_summary("k", "fp1") is False  # ok, same fingerprint

    assert tracer.claim_summary("k", "fp2") is True  # changed fingerprint reclaims
    row = tracer.con.execute(
        "SELECT fingerprint, state, summary FROM summaries WHERE item_key='k'"
    ).fetchone()
    assert (row["fingerprint"], row["state"], row["summary"]) == ("fp2", "working", None)

    assert tracer.finish_summary("k", "fp2", summary=None, error="boom") is True
    row = tracer.con.execute(
        "SELECT state, summary, error FROM summaries WHERE item_key='k'"
    ).fetchone()
    assert (row["state"], row["summary"], row["error"]) == ("failed", None, "boom")
    assert tracer.claim_summary("k", "fp2") is True  # a failed row retries


def test_claim_summary_skips_a_live_owner_and_reclaims_a_dead_one(tmp_path, monkeypatch):
    import lime.tracer as tracer_module

    a = Tracer(tmp_path)
    b = Tracer(tmp_path)
    assert a.claim_summary("k", "fp", pid=os.getppid()) is True  # a live foreign pid
    assert b.claim_summary("k", "fp", pid=os.getpid()) is False

    monkeypatch.setattr(tracer_module, "pid_alive", lambda pid: False)
    assert b.claim_summary("k", "fp", pid=os.getpid()) is True  # the dead owner is reclaimed


def test_finish_summary_drops_a_stale_completion(tracer, monkeypatch):
    import lime.tracer as tracer_module

    p1, p2 = 111111, 222222
    assert tracer.claim_summary("k", "fp", pid=p1) is True
    monkeypatch.setattr(tracer_module, "pid_alive", lambda pid: False)  # p1 died
    assert tracer.claim_summary("k", "fp", pid=p2) is True  # reclaimed

    assert tracer.finish_summary("k", "fp", summary="late", error=None, pid=p1) is False
    row = tracer.con.execute(
        "SELECT state, owner_pid FROM summaries WHERE item_key='k'"
    ).fetchone()
    assert (row["state"], row["owner_pid"]) == ("working", p2)


def test_summary_session_has_no_run_row_and_no_folder(tracer, runs_dir, capsys):
    sid = tracer.start_summary_session(
        "summary:k", role="summarizer", runtime="claude_code", model="claude-haiku-5-5"
    )
    assert sid.startswith(f"{NO_RUN}:")
    row = tracer.con.execute(
        "SELECT run_id, role FROM sessions WHERE session_id=?", (sid,)
    ).fetchone()
    assert row["run_id"] == NO_RUN
    tracer.event(NO_RUN, "turn", name=sid, payload={"cost": 0.01})

    assert tracer.con.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert not (runs_dir / NO_RUN).exists()  # no Run folder
    assert main(["cost"]) == 0
    assert "summarizer" in capsys.readouterr().out


def test_only_the_tracer_writes_the_summaries_table():
    import lime

    summaries_re = re.compile(
        r"\b(INSERT INTO|INSERT OR REPLACE INTO|REPLACE INTO|UPDATE|DELETE FROM)\s+summaries\b"
    )
    hits = set()
    for path in Path(lime.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            consts = [
                n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            if any(summaries_re.search(c) for c in consts):
                hits.add((path.name, node.name))
    assert hits == {("tracer.py", "claim_summary"), ("tracer.py", "finish_summary")}


def test_only_tracer_inserts_sessions():
    import lime

    sessions_re = re.compile(r"\bINSERT INTO sessions\b")
    hits = set()
    for path in Path(lime.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            consts = [
                n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            if any(sessions_re.search(c) for c in consts):
                hits.add((path.name, node.name))
    assert hits == {("tracer.py", "start_session"), ("tracer.py", "start_summary_session")}


# --- events.seq: per-Run cursor + migration fill (ticket 251) ---

_OLD_EVENTS_SCHEMA = """
CREATE TABLE events (
    event_id     INTEGER PRIMARY KEY,
    run_id       TEXT NOT NULL,
    phase_id     TEXT,
    parent_id    INTEGER,
    type         TEXT NOT NULL,
    name         TEXT,
    payload_json TEXT,
    tokens       INTEGER,
    started_at   TEXT NOT NULL,
    ended_at     TEXT
);
"""


def _seqs(tracer: Tracer, run_id: str) -> list[int]:
    return [
        r[0]
        for r in tracer.con.execute(
            "SELECT seq FROM events WHERE run_id=? ORDER BY event_id", (run_id,)
        )
    ]


def test_event_seq_counts_from_one_per_run(tracer, runs_dir):
    tracer.start_run("R1", workflow="feature", request="x")
    tracer.start_run("R2", workflow="feature", request="x")
    tracer.event("R1", "log")
    tracer.event("R2", "log")
    tracer.event("R1", "log")
    tracer.refusal(None, "x")

    assert _seqs(tracer, "R1") == [1, 2]
    assert _seqs(tracer, "R2") == [1]
    assert _seqs(tracer, NO_RUN) == [1]

    lines = [
        json.loads(line)
        for line in (runs_dir / "R1" / "events.jsonl").read_text().splitlines()
    ]
    assert [line["seq"] for line in lines] == [1, 2]


def test_two_writers_on_one_ledger_never_share_a_seq(tracer, runs_dir):
    tracer.start_run("R1", workflow="feature", request="x")
    other = Tracer(runs_dir)
    for i in range(6):
        (tracer if i % 2 == 0 else other).event("R1", "log")
    assert _seqs(tracer, "R1") == [1, 2, 3, 4, 5, 6]


def test_events_seq_is_unique_per_run(tracer, runs_dir):
    tracer.start_run("R1", workflow="feature", request="x")
    names = {
        r[0]
        for r in tracer.con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ux_events_run_seq'"
        )
    }
    assert names == {"ux_events_run_seq"}

    tracer.event("R1", "log")
    with pytest.raises(sqlite3.IntegrityError):
        tracer.con.execute(
            "INSERT INTO events (run_id, type, started_at, seq) VALUES ('R1','log','t',1)"
        )
    tracer.con.rollback()


def _old_events_ledger(db: Path, rows: list[str]) -> None:
    con = sqlite3.connect(db)
    con.executescript(_OLD_EVENTS_SCHEMA)
    for run_id in rows:
        con.execute(
            "INSERT INTO events (run_id, type, started_at) VALUES (?, 'log', '2020-01-01T00:00:00Z')",
            (run_id,),
        )
    con.commit()
    con.close()


def test_migration_fills_seq_in_event_id_order_and_a_rerun_changes_nothing(runs_dir):
    runs_dir.mkdir(parents=True)
    _old_events_ledger(runs_dir / "lime.db", ["R1", "R2", "R1", "R2", "R1"])

    tracer = Tracer(runs_dir)
    assert _seqs(tracer, "R1") == [1, 2, 3]
    assert _seqs(tracer, "R2") == [1, 2]

    dump = [
        tuple(r)
        for r in tracer.con.execute(
            "SELECT event_id, run_id, seq FROM events ORDER BY event_id"
        )
    ]
    Tracer(runs_dir)  # a second migration run changes nothing
    again = [
        tuple(r)
        for r in tracer.con.execute(
            "SELECT event_id, run_id, seq FROM events ORDER BY event_id"
        )
    ]
    assert again == dump

    (runs_dir / "R1").mkdir(parents=True, exist_ok=True)
    (runs_dir / "R1" / "events.jsonl").touch()
    tracer.event("R1", "log")
    assert _seqs(tracer, "R1") == [1, 2, 3, 4]


def test_a_null_seq_row_is_numbered_after_its_runs_highest(runs_dir):
    runs_dir.mkdir(parents=True)
    _old_events_ledger(runs_dir / "lime.db", ["R1", "R1"])
    tracer = Tracer(runs_dir)
    assert _seqs(tracer, "R1") == [1, 2]

    con = sqlite3.connect(runs_dir / "lime.db")
    con.execute("INSERT INTO events (run_id, type, started_at) VALUES ('R1','log','t')")
    con.commit()
    con.close()

    reopened = Tracer(runs_dir)
    rows = [
        tuple(r)
        for r in reopened.con.execute(
            "SELECT event_id, seq FROM events WHERE run_id='R1' ORDER BY event_id"
        )
    ]
    assert [seq for _, seq in rows] == [1, 2, 3]  # the NULL row lands after 2
    assert rows[0][1] == 1 and rows[1][1] == 2  # earlier rows are unchanged


def test_add_column_tolerates_a_column_another_writer_added(tracer):
    tracer._add_column("events", "seq", "INTEGER")  # already present → no raise
    with pytest.raises(sqlite3.OperationalError):
        tracer._add_column("no_such_table", "x", "TEXT")


def test_only_the_tracer_inserts_ledger_rows():
    import lime

    ledger_re = re.compile(r"INSERT\s+INTO\s+(events|phases|sessions)\b", re.I)
    hits = set()
    root = Path(lime.__file__).parent
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if ledger_re.search(node.value):
                    hits.add(str(path.relative_to(root.parent)))
    assert hits == {"lime/tracer.py"}


# --- sessions.budget_warn/budget_stop (ticket 251, step 2) ---

_OLD_SESSIONS_SCHEMA = """
CREATE TABLE sessions (
    session_id     TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    phase_id       TEXT NOT NULL,
    role           TEXT NOT NULL,
    continuation   INTEGER NOT NULL DEFAULT 0,
    runtime        TEXT NOT NULL,
    model          TEXT NOT NULL,
    color          TEXT,
    context_tokens INTEGER,
    context_window INTEGER,
    created_at     TEXT NOT NULL,
    last_used_at   TEXT
);
"""


def _budget(tracer: Tracer, session_id: str) -> tuple[int | None, int | None]:
    row = tracer.con.execute(
        "SELECT budget_warn, budget_stop FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    return (row["budget_warn"], row["budget_stop"])


def test_start_session_stores_the_budget_it_is_given(tracer):
    sid = tracer.start_session(
        "R1", "ph", seq=1, role="engineer", runtime="pi", model="m",
        budget_warn=90_000, budget_stop=160_000,
    )
    assert _budget(tracer, sid) == (90_000, 160_000)

    plain = tracer.start_session("R1", "ph", seq=2, role="engineer", runtime="pi", model="m")
    assert _budget(tracer, plain) == (None, None)

    summary = tracer.start_summary_session(
        "summary:k", role="summarizer", runtime="claude_code", model="m"
    )
    assert _budget(tracer, summary) == (None, None)


def test_legacy_sessions_table_gains_budget_columns_without_backfill(runs_dir):
    runs_dir.mkdir(parents=True)
    db = runs_dir / "lime.db"
    con = sqlite3.connect(db)
    con.executescript(_OLD_SESSIONS_SCHEMA)
    con.execute(
        "INSERT INTO sessions (session_id, run_id, phase_id, role, runtime, model, created_at)"
        " VALUES ('R1:1-engineer','R1','ph','engineer','pi','m','2020-01-01T00:00:00Z')"
    )
    con.commit()
    con.close()

    first = Tracer(runs_dir)
    second = Tracer(runs_dir)  # a second writer open changes nothing

    cols = {row[1] for row in second.con.execute("PRAGMA table_info(sessions)")}
    assert {"budget_warn", "budget_stop"} <= cols
    assert _budget(second, "R1:1-engineer") == (None, None)
    assert first.con.execute(
        "SELECT budget_warn, budget_stop FROM sessions WHERE session_id='R1:1-engineer'"
    ).fetchone()[:] == (None, None)


# --- phases.stage (ticket 251, step 3) ---

_OLD_PHASES_SCHEMA = """
CREATE TABLE phases (
    phase_id    TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    owner       TEXT,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    attempt     INTEGER NOT NULL DEFAULT 1,
    retries     INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    started_at  TEXT,
    ended_at    TEXT
);
"""


def _stage(tracer: Tracer, phase_id: str) -> str:
    return tracer.con.execute(
        "SELECT stage FROM phases WHERE phase_id=?", (phase_id,)
    ).fetchone()[0]


def test_add_phase_stores_the_stage(tracer):
    phase_id = tracer.add_phase(
        "R1", seq=1, name="review", kind="agent", stage="Review", owner="reviewer"
    )
    assert _stage(tracer, phase_id) == "Review"


def test_add_phase_refuses_an_unknown_stage_without_a_row(tracer):
    with pytest.raises(ValueError, match="unknown stage"):
        tracer.add_phase(
            "R1", seq=1, name="review", kind="agent", stage="Other", owner="reviewer"
        )
    assert tracer.con.execute("SELECT count(*) FROM phases").fetchone()[0] == 0


def test_legacy_phases_table_gains_a_stage_column(runs_dir):
    runs_dir.mkdir(parents=True)
    con = sqlite3.connect(runs_dir / "lime.db")
    con.executescript(_OLD_PHASES_SCHEMA)
    con.execute(
        "INSERT INTO phases (phase_id, run_id, seq, name, kind, owner)"
        " VALUES ('R1:01','R1',1,'plan','agent','planner')"
    )
    con.commit()
    con.close()

    tracer = Tracer(runs_dir)
    cols = {row[1] for row in tracer.con.execute("PRAGMA table_info(phases)")}
    assert "stage" in cols

    phase_id = tracer.add_phase(
        "R1", seq=2, name="push_pr", kind="code", stage="Ship", owner="git"
    )
    assert _stage(tracer, phase_id) == "Ship"


# --- phases.stage migration fill (ticket 251, step 4) ---


@pytest.mark.parametrize(
    "name, expected",
    [
        ("engineer", "Prepare"),
        ("setup", "Prepare"),
        ("scout", "Prepare"),
        ("plan", "Prepare"),
        ("research", "Prepare"),
        ("build_1", "Build"),
        ("build_3_retry2", "Build"),
        ("verify_1", "Build"),
        ("verify_2_retry3", "Build"),
        ("design", "Build"),
        ("design_continue", "Build"),
        ("review", "Review"),
        ("review_retry1", "Review"),
        ("revise_1", "Review"),
        ("revise_2", "Review"),
        ("commit_code", "Review"),
        ("changes", "Review"),
        ("design_lock", "Review"),
        ("document", "Ship"),
        ("commit_docs", "Ship"),
        ("push_pr", "Ship"),
        ("deploy", "Ship"),
        ("layout", "Other"),
        ("layout_retry1", "Other"),
        ("build", "Other"),
        ("verify", "Other"),
        ("request", "Other"),
        ("", "Other"),
    ],
)
def test_legacy_stage_follows_the_owner_table(name, expected):
    assert legacy_stage(name) == expected


def test_migration_fills_stage_on_old_phases_rows_and_a_rerun_changes_nothing(runs_dir):
    runs_dir.mkdir(parents=True)
    con = sqlite3.connect(runs_dir / "lime.db")
    con.executescript(_OLD_PHASES_SCHEMA)
    for seq, name in enumerate(
        ["plan", "build_2_retry1", "review_retry1", "push_pr", "layout", "request"],
        start=1,
    ):
        con.execute(
            "INSERT INTO phases (phase_id, run_id, seq, name, kind, owner)"
            " VALUES (?, 'R1', ?, ?, 'agent', 'x')",
            (f"R1:{seq:02d}", seq, name),
        )
    con.commit()
    con.close()

    tracer = Tracer(runs_dir)
    stages = [
        r[0]
        for r in tracer.con.execute(
            "SELECT stage FROM phases WHERE run_id='R1' ORDER BY seq"
        )
    ]
    assert stages == ["Prepare", "Build", "Review", "Ship", "Other", "Other"]

    dump = [
        tuple(r)
        for r in tracer.con.execute("SELECT phase_id, stage FROM phases ORDER BY phase_id")
    ]
    Tracer(runs_dir)  # a second migration run changes nothing
    again = [
        tuple(r)
        for r in tracer.con.execute("SELECT phase_id, stage FROM phases ORDER BY phase_id")
    ]
    assert again == dump


def test_the_stage_fill_never_overwrites_a_declared_stage(runs_dir, tracer):
    tracer.add_phase("R1", seq=1, name="plan", kind="agent", stage="Ship", owner="x")
    tracer.con.execute(
        "INSERT INTO phases (phase_id, run_id, seq, name, kind, owner)"
        " VALUES ('R1:02','R1',2,'plan','agent','x')"
    )
    tracer.con.commit()

    reopened = Tracer(runs_dir)
    rows = [
        (r[0], r[1])
        for r in reopened.con.execute("SELECT phase_id, stage FROM phases ORDER BY seq")
    ]
    assert rows == [("R1:01", "Ship"), ("R1:02", "Prepare")]


# --- processes.heartbeat_at + the beat thread (ticket 252, step 1) ---

_OLD_PROCESSES_SCHEMA = """
CREATE TABLE processes (
    id         INTEGER PRIMARY KEY,
    run_id     TEXT NOT NULL,
    kind       TEXT,
    name       TEXT,
    pid        INTEGER,
    command    TEXT,
    started_at TEXT,
    ended_at   TEXT
);
"""


def _poll(cond, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _heartbeat(tracer_obj: Tracer, process_id: int):
    return tracer_obj.con.execute(
        "SELECT heartbeat_at FROM processes WHERE id=?", (process_id,)
    ).fetchone()[0]


def test_legacy_processes_table_gains_heartbeat_at(runs_dir):
    runs_dir.mkdir(parents=True)
    db = runs_dir / "lime.db"
    con = sqlite3.connect(db)
    con.executescript(_OLD_PROCESSES_SCHEMA)
    con.execute(
        "INSERT INTO processes (run_id, kind, name, pid, command, started_at)"
        " VALUES ('R1','run','lime',1,'lime run', '2020-01-01T00:00:00Z')"
    )
    con.commit()
    con.close()

    first = Tracer(runs_dir)
    second = Tracer(runs_dir)  # a second writer open changes nothing

    cols = {row[1] for row in second.con.execute("PRAGMA table_info(processes)")}
    assert "heartbeat_at" in cols
    assert first.con.execute(
        "SELECT heartbeat_at FROM processes WHERE run_id='R1'"
    ).fetchone()[0] is None
    assert second.con.execute(
        "SELECT heartbeat_at FROM processes WHERE run_id='R1'"
    ).fetchone()[0] is None


@pytest.mark.parametrize("closer", ["end_process", "set_run_status"])
def test_heartbeat_stamps_its_row_and_stops_once_it_is_closed(runs_dir, closer):
    import lime.tracer as lt

    tracer = Tracer(runs_dir)
    tracer.start_run("R1", workflow="feature", request="x")
    process_id = tracer.start_process(
        "R1", kind="run", name="lime", pid=os.getpid(), command="lime run x"
    )
    thread = lt.start_heartbeat(runs_dir / "lime.db", process_id, interval=0.01)

    assert _poll(lambda: _heartbeat(tracer, process_id) is not None)

    if closer == "end_process":
        tracer.end_process("R1", os.getpid())
    else:
        tracer.set_run_status("R1", "success")
    thread.join(2)
    assert not thread.is_alive()

    stamp = _heartbeat(tracer, process_id)
    time.sleep(0.05)
    assert _heartbeat(tracer, process_id) == stamp


def test_heartbeat_beats_only_its_own_row(runs_dir):
    import lime.tracer as lt

    tracer = Tracer(runs_dir)
    tracer.start_run("R1", workflow="feature", request="x")
    tracer.start_run("R2", workflow="feature", request="x")
    first = tracer.start_process("R1", kind="run", name="lime", pid=111, command="c")
    second = tracer.start_process("R2", kind="run", name="lime", pid=222, command="c")

    thread = lt.start_heartbeat(runs_dir / "lime.db", first, interval=0.01)
    try:
        assert _poll(lambda: _heartbeat(tracer, first) is not None)
        assert _heartbeat(tracer, second) is None
    finally:
        tracer.end_process("R1", 111)
        thread.join(2)
    assert not thread.is_alive()


def test_start_process_without_commit_leaves_the_transaction_to_the_caller(runs_dir):
    tracer = Tracer(runs_dir)
    tracer.start_run("R1", workflow="feature", request="x")
    tracer.start_process("R1", kind="run", name="lime", pid=111, command="c", commit=False)
    tracer.con.rollback()
    assert tracer.con.execute("SELECT count(*) FROM processes").fetchone()[0] == 0
    tracer.start_process("R1", kind="run", name="lime", pid=222, command="c", commit=False)
    tracer.con.commit()
    assert tracer.con.execute("SELECT count(*) FROM processes").fetchone()[0] == 1


def test_a_failed_beat_is_logged_and_retried(runs_dir, monkeypatch, caplog):
    import lime.tracer as lt

    tracer = Tracer(runs_dir)
    tracer.start_run("R1", workflow="feature", request="x")
    process_id = tracer.start_process(
        "R1", kind="run", name="lime", pid=os.getpid(), command="c"
    )
    real = lt._beat
    calls = {"n": 0}

    def flaky(con, pid):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real(con, pid)

    monkeypatch.setattr(lt, "_beat", flaky)
    with caplog.at_level(logging.WARNING, logger="lime.tracer"):
        thread = lt.start_heartbeat(runs_dir / "lime.db", process_id, interval=0.01)
        try:
            assert _poll(lambda: _heartbeat(tracer, process_id) is not None)
        finally:
            tracer.end_process("R1", os.getpid())
            thread.join(2)
    assert calls["n"] > 2
    assert any(r.levelno == logging.WARNING and "heartbeat" in r.getMessage() for r in caplog.records)


def test_a_failed_heartbeat_connection_is_logged_and_retried(runs_dir, monkeypatch, caplog):
    import lime.tracer as lt

    tracer = Tracer(runs_dir)
    tracer.start_run("R1", workflow="feature", request="x")
    process_id = tracer.start_process(
        "R1", kind="run", name="lime", pid=os.getpid(), command="c"
    )
    real = lt.connect
    calls = {"n": 0}

    def flaky(path, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(path, **kw)

    monkeypatch.setattr(lt, "connect", flaky)
    with caplog.at_level(logging.WARNING, logger="lime.tracer"):
        thread = lt.start_heartbeat(runs_dir / "lime.db", process_id, interval=0.01)
        try:
            assert _poll(lambda: _heartbeat(tracer, process_id) is not None)
        finally:
            tracer.end_process("R1", os.getpid())
            thread.join(2)
    assert calls["n"] > 1
    assert not thread.is_alive()
    assert any(r.levelno == logging.WARNING and "heartbeat" in r.getMessage() for r in caplog.records)


def test_heartbeat_ends_when_its_ledger_is_gone(runs_dir, monkeypatch):
    import lime.tracer as lt

    runs_dir.mkdir(parents=True)
    Tracer(runs_dir)  # create the ledger the thread will connect to
    db = runs_dir / "lime.db"

    def gone(con, pid):
        db.unlink(missing_ok=True)
        raise sqlite3.OperationalError("ledger gone")

    monkeypatch.setattr(lt, "_beat", gone)
    thread = lt.start_heartbeat(db, 1, interval=0.01)
    thread.join(2)
    assert not thread.is_alive()


def test_only_the_heartbeat_writes_heartbeat_at():
    import lime

    root = Path(lime.__file__).parent
    hits = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and "SET heartbeat_at" in node.value
                ):
                    hits.append((str(path.relative_to(root.parent)), func.name))
    assert hits == [("lime/tracer.py", "_beat")]


# --- liveness(): the one rule, shared with lime status (ticket 252, step 2) ---

_LIVENESS_FIXTURE = Path(__file__).parent / "fixtures" / "liveness.json"


def _liveness_cases():
    return json.loads(_LIVENESS_FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _liveness_cases(), ids=lambda c: c["name"])
def test_liveness_fixture_case(tracer, monkeypatch, case):
    from lime.tracer import liveness

    run = case["run"]
    tracer.start_run(run["run_id"], workflow="feature", request="x")
    tracer.con.execute(
        "UPDATE runs SET started_at=? WHERE run_id=?", (run["started_at"], run["run_id"])
    )
    for proc in case["processes"]:
        tracer.con.execute(
            "INSERT INTO processes (run_id, kind, name, pid, command, started_at,"
            " ended_at, heartbeat_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                run["run_id"], proc["kind"], proc["kind"], proc["pid"], "c",
                proc["started_at"], proc["ended_at"], proc["heartbeat_at"],
            ),
        )
    tracer.con.commit()

    monkeypatch.setattr("lime.tracer.pid_alive", lambda pid: pid in case["alive_pids"])
    now = datetime.fromisoformat(case["now"])
    assert liveness(tracer.con, run["run_id"], now, case["kind"]) == case["verdict"]


def test_liveness_fixture_covers_the_ticket_cases():
    cases = _liveness_cases()
    names = {c["name"] for c in cases}
    assert names >= {
        "fresh beat",
        "stale beat",
        "beat at the 45 s edge",
        "null beat, pid alive",
        "null beat, pid dead",
        "no row, young run",
        "no row, old run",
        "resumed, stopped old row and fresh new row",
        "resumed, stopped old row only",
        "closed row with a fresh beat",
        "deploy row beating",
        "run kind ignores a beating deploy row",
        "batch row beating",
        "batch closed by SIGTERM or Ctrl-C",
        "batch killed by SIGKILL or SIGHUP, stale beat",
        "batch stale beat beats a reused live pid",
        "run kind ignores a beating batch row",
    }
    assert {c["verdict"] for c in cases} >= {"live", "starting", "dead"}


def test_liveness_reads_a_legacy_ledger_read_only(runs_dir, monkeypatch):
    from lime.tracer import liveness

    tracer = Tracer(runs_dir)
    tracer.start_run("R1", workflow="feature", request="x")
    tracer.start_process("R1", kind="run", name="lime", pid=os.getpid(), command="c")
    db = runs_dir / "lime.db"
    raw = sqlite3.connect(db)
    raw.execute("ALTER TABLE processes DROP COLUMN heartbeat_at")
    raw.commit()
    raw.close()

    con = connect(db, readonly=True)
    now = datetime.now(UTC)
    assert liveness(con, "R1", now) == "live"
    monkeypatch.setattr("lime.tracer.pid_alive", lambda pid: False)
    assert liveness(con, "R1", now) == "dead"
    con.close()


def _call_names(func: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def test_launch_guards_keep_the_pid_check():
    root = Path(__file__).resolve().parents[1]
    for rel, func_name in (
        ("lime/tracer.py", "_held_ports"),
        ("lime/tracer.py", "live_pid"),
        ("lime/overlap.py", "_run_is_live"),
    ):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        funcs = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == func_name
        ]
        assert funcs, (rel, func_name)
        names: set[str] = set()
        for func in funcs:
            names |= _call_names(func)
        assert "pid_alive" in names, (rel, func_name)
        assert "liveness" not in names, (rel, func_name)
