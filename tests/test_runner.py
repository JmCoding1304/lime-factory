"""The Run object and session lifecycle: the ONE phase primitive (spec §2, §11)."""

import ast
import json
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from lime import agents, runner, session, tracer
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


def test_ensure_creates_a_run_row_and_context_handoff_dir(project):
    run = session.ensure(cfg(), "R1", workflow="feature", request="do X", runs_dir=project / "runs")
    assert run.run_id == "R1"
    assert run.context_handoff_dir.is_dir()
    assert run.repo_root == project.resolve()
    assert run.tracer.run_exists("R1")


def test_ensure_is_idempotent_and_continues_the_phase_sequence(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    with run.phase(PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="First phase")):
        pass
    rejoined = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    assert rejoined._seq == 1  # continues, does not restart at 0


def test_ensure_exposes_the_allocated_port_fresh_and_on_rejoin(project):
    run = session.ensure(
        cfg(), "R1", workflow="feature", request="x", runs_dir=project / "runs",
        ports=[3100, 3101],
    )
    assert run.port == 3100

    rejoined = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    assert rejoined.port == 3100  # looked back up from the row, not re-allocated


def test_phase_defaults_to_fail_and_flips_to_success_on_clean_exit(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    with run.phase(PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="First phase")) as ph:
        ph.log(note="working")
    assert run.phases[0].status == "success"


def test_phase_row_stores_the_params_stage(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    with run.phase(PhaseParams(name="p1", kind="code", owner="git", stage="Ship", description="First phase")):
        pass
    row = run.tracer.con.execute(
        "SELECT stage FROM phases WHERE run_id='R1'"
    ).fetchone()
    assert row["stage"] == "Ship"


def test_phase_flips_to_fail_and_reraises_on_exception(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    with pytest.raises(RuntimeError, match="boom"):
        with run.phase(PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="First phase")):
            raise RuntimeError("boom")
    assert run.phases[0].status == "fail"
    assert "boom" in run.phases[0].error


def test_a_failed_phase_closes_the_run_as_fail(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    with pytest.raises(RuntimeError):
        with run.phase(PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="First phase")):
            raise RuntimeError("boom")
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "fail"


def test_a_non_fatal_phase_failure_reraises_without_closing_the_run(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    with pytest.raises(RuntimeError, match="boom"):
        with run.phase(PhaseParams(
            name="p1", stage="Build", kind="code", owner="git", description="First phase", fatal=False,
        )):
            raise RuntimeError("boom")

    assert run.phases[0].status == "fail"
    phase_row = run.tracer.con.execute(
        "SELECT status FROM phases WHERE name='p1'"
    ).fetchone()
    assert phase_row["status"] == "fail"
    error = run.tracer.con.execute(
        "SELECT payload_json FROM events WHERE type='error' AND name='p1'"
    ).fetchone()
    assert "boom" in error["payload_json"]
    run_row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert run_row["status"] == "running"  # a non-fatal failure does not close the Run

    # A later, normal phase can still run and the Run can still finish success.
    with run.phase(PhaseParams(name="p2", stage="Build", kind="code", owner="git", description="Second phase")):
        pass
    assert run.finish(accepted=True) == 0
    run_row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert run_row["status"] == "success"


def test_a_signal_kill_in_a_non_fatal_phase_still_closes_the_run(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    with pytest.raises(runner.KilledBySignal):
        with run.phase(PhaseParams(
            name="p1", stage="Build", kind="code", owner="git", description="First phase", fatal=False,
        )):
            raise runner.KilledBySignal(signal.SIGTERM)
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "fail"  # a signal kill is not an Exception — it closes the Run


def test_close_run_call_sites_are_known():
    """Any new Run-closing writer must honour `PhaseParams.fatal` (ticket 197): a
    non-fatal Phase's Exception fails only that Phase, so a close_run added for it
    would sink the Run. This pins the five sites that exist today."""
    lime_dir = Path(runner.__file__).parent
    sites = set()
    for path in lime_dir.rglob("*.py"):
        for owner, attr in _call_owners(path.read_text()):
            if attr == "close_run":
                sites.add((str(path.relative_to(lime_dir)), owner))
    assert sites == {
        ("runner.py", "phase"),
        ("runner.py", "finish"),
        ("agents.py", "execute"),
        ("session.py", "handler"),
        ("workflows/feature.py", "run"),
    }


def test_finish_requires_every_phase_to_succeed_and_the_run_to_be_accepted(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    with run.phase(PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="First phase")):
        pass
    assert run.finish(accepted=False, reason="tests still red") == 1
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "fail"


def test_finish_succeeds_when_every_phase_passed_and_accepted(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    with run.phase(PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="First phase")):
        pass
    assert run.finish(accepted=True) == 0
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "success"


def test_close_run_is_idempotent(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    with run.phase(PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="First phase")):
        pass
    run.close_run(ok=True)
    run.close_run(ok=True)  # a second call must not raise (already-terminal transition)
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "success"


def test_agent_map_persists_across_sessions(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    run.save_agent_map("builder", {"session_id": "s1", "model": "m"})
    rejoined = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    assert rejoined.agent_map["builder"]["session_id"] == "s1"


def test_close_agent_session_preserves_metadata_and_persists_closed_entry(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    builder_entry = {
        "session_id": "s-builder", "model": "openrouter/x/y", "runtime": "pi",
        "session_dir": "/tmp/builder", "resume": True,
    }
    scout_entry = {
        "session_id": "s-scout", "model": "openrouter/x/y", "runtime": "pi",
        "session_dir": "/tmp/scout",
    }
    run.save_agent_map("builder", builder_entry)
    run.save_agent_map("scout", scout_entry)

    # Close builder
    run.close_agent_session("builder")

    entry = run.agent_map["builder"]
    assert entry["closed"] is True
    # All original metadata preserved
    assert entry["session_id"] == "s-builder"
    assert entry["model"] == "openrouter/x/y"
    assert entry["runtime"] == "pi"
    assert entry["session_dir"] == "/tmp/builder"

    # Scout unchanged, no closed key
    assert run.agent_map["scout"]["session_id"] == "s-scout"
    assert run.agent_map["scout"].get("closed") is None

    # Disk matches memory
    on_disk = json.loads((run.run_dir / "agent_map.json").read_text())
    assert on_disk["builder"]["closed"] is True
    assert on_disk["scout"]["session_id"] == "s-scout"

    # session.ensure reloads closed: True
    rejoined = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    assert rejoined.agent_map["builder"]["closed"] is True


def test_close_agent_session_is_idempotent_and_missing_role_is_a_noop(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    run.save_agent_map("builder", {"session_id": "s1", "model": "m"})

    # First close
    run.close_agent_session("builder")
    assert run.agent_map["builder"]["closed"] is True
    sid = run.agent_map["builder"]["session_id"]

    # Second close is harmless
    run.close_agent_session("builder")
    assert run.agent_map["builder"]["closed"] is True
    assert run.agent_map["builder"]["session_id"] == sid  # metadata unchanged

    # Missing role is a no-op
    run.close_agent_session("nonexistent")
    assert "nonexistent" not in run.agent_map


def test_save_agent_map_replaces_closed_entry_with_a_live_session(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    run.save_agent_map("builder", {
        "session_id": "s-closed", "model": "m", "closed": True,
    })

    # Save a new live entry (no closed flag)
    run.save_agent_map("builder", {
        "session_id": "s-live", "model": "m",
    })

    # The old closed flag is gone; the new entry has no closed
    assert run.agent_map["builder"]["session_id"] == "s-live"
    assert run.agent_map["builder"].get("closed") is None

    # Disk is consistent
    on_disk = json.loads((run.run_dir / "agent_map.json").read_text())
    assert on_disk["builder"]["session_id"] == "s-live"
    assert on_disk["builder"].get("closed") is None


def test_agent_resolves_from_the_merged_roster(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    assert run.agent("builder").name == "builder"
    with pytest.raises(RuntimeError, match="not in the roster"):
        run.agent("ghost")


def test_add_usage_rolls_into_run_totals(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    run.add_usage(tokens=100, cost=0.05)
    run.add_usage(tokens=50, cost=0.0)  # a zero-dollar increment still adds tokens
    run.add_usage(tokens=10, cost=1.25)
    assert run.tokens == 160
    assert run.cost == pytest.approx(1.30)
    row = run.tracer.con.execute(
        "SELECT total_tokens, total_cost FROM runs WHERE run_id='R1'"
    ).fetchone()
    assert row["total_tokens"] == 160
    assert row["total_cost"] == pytest.approx(1.30)


def test_cc_cost_reported_map_lifecycle(project):
    """`cost_reported` survives save, disk reload and repeated close; a new-entry
    replacement never inherits the old counter or the closed bit."""
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    run.save_agent_map("builder", {
        "session_id": "s1", "model": "m", "runtime": "claude_code",
        "session_dir": "/tmp/s1", "cost_reported": 1.25,
    })
    assert run.agent_map["builder"]["cost_reported"] == 1.25

    rejoined = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    assert rejoined.agent_map["builder"]["cost_reported"] == 1.25  # survives disk reload

    run.close_agent_session("builder")
    run.close_agent_session("builder")  # repeated close is harmless
    entry = run.agent_map["builder"]
    assert entry["cost_reported"] == 1.25
    assert entry["closed"] is True
    on_disk = json.loads((run.run_dir / "agent_map.json").read_text())
    assert on_disk["builder"]["cost_reported"] == 1.25

    # a replacement entry is exactly what was written — no old counter, no closed bit
    run.save_agent_map("builder", {
        "session_id": "s2", "model": "m", "runtime": "claude_code",
        "session_dir": "/tmp/s2", "cost_reported": 0.0,
    })
    assert run.agent_map["builder"] == {
        "session_id": "s2", "model": "m", "runtime": "claude_code",
        "session_dir": "/tmp/s2", "cost_reported": 0.0,
    }
    assert "closed" not in run.agent_map["builder"]


def _call_owners(source):
    """(enclosing function name, called attribute) for every `x.attr(...)` in `source`."""
    tree = ast.parse(source)
    calls = []

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            child_owner = child.name if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) else owner
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                calls.append((child_owner, child.func.attr))
            visit(child, child_owner)

    visit(tree, None)
    return calls


def _assignment_owners(source, attr):
    """Owners of `x.attr[...] =` (subscript) and `x.attr =` (attribute) stores."""
    tree = ast.parse(source)
    stores = {"subscript": [], "attribute": []}

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            co = child.name if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) else owner
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if (isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Attribute)
                            and target.value.attr == attr):
                        stores["subscript"].append(co)
                    elif isinstance(target, ast.Attribute) and target.attr == attr:
                        stores["attribute"].append(co)
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Attribute) and child.target.attr == attr:
                stores["attribute"].append(co)
            visit(child, co)

    visit(tree, None)
    return stores


def test_usage_and_map_writer_boundaries():
    """Source guard: entry replacement stays inside Run.save_agent_map (the constructor
    only loads); close_agent_session spreads the old entry; execute books usage only
    through Run.add_usage, which rolls once into the additive Tracer.add_totals SQL."""
    runner_src = Path(runner.__file__).read_text()
    stores = _assignment_owners(runner_src, "agent_map")
    assert stores["subscript"] == ["save_agent_map"]  # the ONE replacement site
    assert stores["attribute"] == ["__init__"]  # constructor load only

    tree = ast.parse(runner_src)
    save_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "save_agent_map")
    [save_assign] = [
        n for n in ast.walk(save_fn)
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Subscript)
    ]
    assert "self.agent_map[agent] = entry" in (ast.get_source_segment(runner_src, save_assign) or "")

    close_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "close_agent_session")
    [close_call] = [
        c for c in ast.walk(close_fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "save_agent_map"
    ]
    merged = close_call.args[1]
    assert isinstance(merged, ast.Dict) and any(k is None for k in merged.keys)  # {**entry, ...}

    agents_src = Path(agents.__file__).read_text()
    assert {o for o, a in _call_owners(agents_src) if a == "add_usage"} == {"_send_once"}

    add_usage = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "add_usage")
    [totals] = [
        c for c in ast.walk(add_usage)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "add_totals"
    ]

    tracer_src = Path(tracer.__file__).read_text()
    totals_fn = next(
        n for n in ast.walk(ast.parse(tracer_src))
        if isinstance(n, ast.FunctionDef) and n.name == "add_totals"
    )
    sql = "\n".join(
        n.value for n in ast.walk(totals_fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    assert "total_tokens = total_tokens + ?" in sql
    assert "total_cost = total_cost + ?" in sql


def test_roster_rows_returns_the_expected_fields(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    rows = run.roster_rows(["builder"])
    assert len(rows) == 1
    assert rows[0] == {"name": "builder", "runtime": "pi", "model": "openrouter/x/y", "thinking": "medium"}


def test_roster_rows_skips_roles_not_in_the_roster(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    rows = run.roster_rows(["builder", "ghost"])
    assert len(rows) == 1
    assert rows[0]["name"] == "builder"


def test_announce_roster_prints_and_stores_the_rows(project, capsys):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    run.announce_roster(["builder"])
    card = json.loads((project / "runs" / "R1" / "run.json").read_text())
    assert card["roster"] == [{"name": "builder", "runtime": "pi", "model": "openrouter/x/y", "thinking": "medium"}]
    out = capsys.readouterr().out
    assert "pi \u00b7 openrouter/x/y \u00b7 medium" in out
    assert "new" not in out
    assert "was" not in out


def test_announce_roster_marks_what_changed_since_the_card(project, capsys):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    run.tracer.update_card("R1", roster=[{"name": "builder", "runtime": "pi", "model": "openrouter/old/model", "thinking": "high"}])
    run.announce_roster(["builder"])
    out = capsys.readouterr().out
    assert "was pi \u00b7 openrouter/old/model \u00b7 high" in out
    # the card now holds the current row
    card = json.loads((project / "runs" / "R1" / "run.json").read_text())
    assert card["roster"][0]["model"] == "openrouter/x/y"


def test_a_phase_starts_no_thread(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    before = set(threading.enumerate())
    with run.phase(PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="First phase")):
        assert set(threading.enumerate()) - before == set()
    assert not hasattr(runner, "StallWatch")


def test_an_ordinary_exception_is_not_a_crash(project):
    run = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    with pytest.raises(RuntimeError):
        with run.phase(PhaseParams(name="p1", stage="Build", kind="code", owner="git", description="First phase")):
            raise RuntimeError("boom")
    count = run.tracer.con.execute(
        "SELECT count(*) FROM events WHERE type='crash_detected'"
    ).fetchone()[0]
    assert count == 0
