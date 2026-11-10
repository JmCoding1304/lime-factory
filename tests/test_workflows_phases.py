"""The step library: engineer/setup/plan/build/verify/commit/push_pr (spec §7, ticket 28)."""

import ast
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

import lime
from lime import agents, gates, git_helper, session, tracker
from lime.data_types import AgentResult, BuildStep, ChangesOutput, CostBreakdown, PhaseParams, UsageBreakdown, ReviewStep
from lime.tracker import Ticket
from lime.workflows import phases


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


def git_ls(cwd, ref):
    """`git ls-remote origin <ref>` output for a test repo (empty string = absent)."""
    return subprocess.run(
        ["git", "-C", str(cwd), "ls-remote", "origin", ref],
        capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def project(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("v1\n")
    (tmp_path / ".gitignore").write_text("runs/\n")  # trace dir is gitignored, like .lime/runs/ (spec §2)
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def trunk_project(tmp_path, monkeypatch):
    """A repo on the non-protected `trunk` — the ADR tests never touch main/master."""
    git(tmp_path, "init", "-q", "-b", "trunk")
    (tmp_path / "app.py").write_text("v1\n")
    (tmp_path / ".gitignore").write_text("runs/\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def attach_trunk_origin(project):
    """Wire a temporary bare origin with `trunk` as its default branch and push to it."""
    origin = project.parent / "origin.git"
    git(project.parent, "init", "-q", "--bare", "-b", "trunk", str(origin))
    git(project, "remote", "add", "origin", str(origin))
    git(project, "push", "-q", "-u", "origin", "trunk")
    git(project, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    return origin


def cfg(commands=None):
    return {
        "defaults": {"runtime": "pi", "model": "openrouter/x/y", "thinking": "medium"},
        "agents": [
            {"name": "scout", "prompt_engineering": {}, "tools": ["read", "write"], "writes": []},
            {"name": "planner", "prompt_engineering": {}, "tools": ["read", "write"], "writes": []},
            {"name": "builder", "prompt_engineering": {}, "tools": ["read", "edit", "write"]},
            {"name": "reviewer", "prompt_engineering": {}, "tools": ["read", "write"], "writes": []},
            {"name": "documenter", "prompt_engineering": {}, "tools": ["read", "edit", "write"],
             "writes": ["AGENTS.md"]},
        ],
        "commands": commands or {},
    }


def make_run(project, commands=None):
    return session.ensure(cfg(commands), "R1", runs_dir=project / "runs")


def usage() -> UsageBreakdown:
    return UsageBreakdown(input=100, output=50, total=150, cost=CostBreakdown(total=0.01))


def stub_adapter(reply_json: dict, write: dict[str, str] | None = None):
    """Replays one success AgentResult; optionally writes files before reporting."""
    calls = []

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        calls.append(req)
        for rel_path, content in (write or {}).items():
            p = Path(rel_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return AgentResult(
            status="ok", text="", session_id=req.session_id,
            usage=usage(), cost=0.01, context_tokens=150, context_window=100_000,
            envelope=reply_json,
        )

    adapter.calls = calls
    return adapter


TICKET = Ticket(
    path=".lime/tickets/41-toy.md", number=41, slug="41-toy", title="Toy Ticket",
    workflow="feature", ask="Do the small thing.", done_means="It works.",
)

# A valid one-step plan.md: the mandatory `failure:`/`writers:` lines the plan gate requires.
VALID_PLAN = "## Repo facts\nnone\n\n## Step 1\nDo it.\nfailure: pure\nwriters: none\n"


# --- engineer ---


def test_engineer_records_the_prompt_as_a_log_event(project):
    run = make_run(project)
    phases.engineer(run, "do X")
    assert run.phases[0].status == "success"
    events = run.tracer.con.execute("SELECT * FROM events WHERE type='log'").fetchall()
    assert any("do X" in (e["payload_json"] or "") for e in events)


# --- setup ---


def test_setup_skips_when_commands_setup_is_unset(project):
    run = make_run(project, {})
    phases.setup(run)
    assert run.phases[0].status == "success"


def test_setup_runs_the_configured_command(project):
    run = make_run(project, {"setup": "touch setup-ran.txt"})
    phases.setup(run)
    assert run.phases[0].status == "success"
    assert (project / "setup-ran.txt").exists()


def test_setup_raises_and_fails_the_run_on_a_red_command(project):
    run = make_run(project, {"setup": "echo boom >&2; exit 1"})
    with pytest.raises(RuntimeError, match="commands.setup exited"):
        phases.setup(run)
    assert run.phases[0].status == "fail"
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "fail"


# --- scout ---


def test_scout_returns_the_scout_envelope_and_writes_the_brief(project, monkeypatch):
    run = make_run(project)
    brief = run.context_handoff_dir / "scout_findings.md"
    adapter = stub_adapter(
        {"status": "success", "summary": "found it", "findings": [{"file": "app.py", "note": "greet lives here"}], "artifacts": [str(brief)]},
        write={str(brief): "# brief\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    envelope = phases.scout(run, "where does greet() live?")
    assert envelope.findings[0].file == "app.py"
    assert brief.exists()
    assert run.phases[0].params.name == "scout"
    assert run.phases[0].params.owner == "scout"
    assert run.phases[0].params.description == phases.PLANNER_BRIEF_DESCRIPTION
    assert "where does greet() live?" in adapter.calls[0].prompt


def test_scout_description_is_overridable_for_the_prototype(project, monkeypatch):
    run = make_run(project)
    brief = run.context_handoff_dir / "scout_findings.md"
    adapter = stub_adapter(
        {"status": "success", "summary": "found it", "findings": [{"file": "app.py", "note": "greet lives here"}], "artifacts": [str(brief)]},
        write={str(brief): "# brief\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    phases.scout(run, "brief the designer", description="Brief the designer on routes.")
    assert run.phases[0].params.description == "Brief the designer on routes."
    assert run.phases[0].params.name == "scout"


def test_scout_failure_still_raises_and_closes_the_run_by_default(project, monkeypatch):
    """`lime scout` and the prototype keep `fatal=True`: a failure raises and closes the Run,
    and no fallback brief is written (ticket 197)."""
    run = make_run(project)
    adapter = stub_adapter({"status": "fail", "summary": "nope", "findings": [], "artifacts": []})
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    with pytest.raises(RuntimeError, match="reported status='fail'"):
        phases.scout(run, "x")

    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "fail"
    assert not (run.context_handoff_dir / "scout_findings.md").exists()


def test_a_non_fatal_scout_failure_writes_the_unfinished_brief(project, monkeypatch):
    """`fatal=False` catches the scout's failure: the Phase row is `fail`, the Run stays
    open and `scout_findings.md` carries the no-brief notice and reason (ticket 197)."""
    run = make_run(project)
    adapter = stub_adapter({"status": "fail", "summary": "nope", "findings": [], "artifacts": []})
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert phases.scout(run, "x", fatal=False) is None

    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "running"
    text = (run.context_handoff_dir / "scout_findings.md").read_text()
    assert "Scout did not finish" in text
    assert "nope" in text
    assert run.phases[0].status == "fail"


def test_only_the_chain_scout_is_non_fatal_and_only_phases_writes_the_brief():
    """Source guard (ticket 197): the Chain's scout is the only non-fatal `phases.scout`
    call, and the only code writer of `scout_findings.md` is `_brief_unfinished_scout`."""
    lime_root = Path(lime.__file__).parent
    scout_calls = []  # (relpath, owner, has_fatal, value_node)
    brief_writers = []  # (relpath, owner)

    for path in sorted(lime_root.rglob("*.py")):
        rel = str(path.relative_to(lime_root))
        source = path.read_text()
        tree = ast.parse(source)
        doc_ids = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)) and body:
                first = body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    doc_ids.add(id(first.value))

        def visit(node, owner):
            for child in ast.iter_child_nodes(node):
                co = child.name if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) else owner
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "scout"
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "phases"
                ):
                    fatal_kw = next((k for k in child.keywords if k.arg == "fatal"), None)
                    scout_calls.append((rel, co, fatal_kw is not None, fatal_kw.value if fatal_kw else None))
                if (
                    isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and "scout_findings.md" in child.value
                    and id(child) not in doc_ids
                ):
                    brief_writers.append((rel, co))
                visit(child, co)

        visit(tree, None)

    # (a) only the Chain's scout is non-fatal, and only with the constant `False`
    fatal_calls = [c for c in scout_calls if c[2]]
    assert [(rel, owner) for rel, owner, _, _ in fatal_calls] == [("workflows/feature.py", "_run_chain")]
    assert all(isinstance(value, ast.Constant) and value.value is False for _, _, _, value in fatal_calls)
    # `lime scout` and the prototype call `phases.scout` with no `fatal` keyword
    assert [c for c in scout_calls if c[0] == "cli.py" and not c[2]]

    # (b) the only non-docstring writer of scout_findings.md is the phases fallback
    assert brief_writers == [("workflows/phases.py", "_brief_unfinished_scout")]


# --- plan ---


def test_plan_returns_the_planner_envelope(project, monkeypatch):
    run = make_run(project)
    plan_path = run.context_handoff_dir / "plan.md"
    adapter = stub_adapter(
        {"status": "success", "summary": "planned", "artifacts": [str(plan_path)]},
        write={str(plan_path): VALID_PLAN},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    envelope = phases.plan(run, "do X")
    assert envelope.status == "success"
    assert plan_path.exists()
    assert run.phases[0].params.name == "plan"
    rows = run.tracer.con.execute(
        "SELECT gate, passed FROM gate_results WHERE phase_id=? ORDER BY id",
        (run.phases[0].phase_id,),
    ).fetchall()
    plan_gates = {r["gate"]: bool(r["passed"]) for r in rows}
    assert plan_gates["artifacts_exist"] is True
    assert plan_gates["plan_has_failure_lines"] is True


def test_plan_failure_lines_gate_corrects_before_acceptance(project, monkeypatch):
    """First planner write misses a line; the gate fails it, a correction lands a valid
    plan, and only then is the envelope accepted and provenance recorded."""
    run = make_run(project)
    plan_path = run.context_handoff_dir / "plan.md"
    invalid = "## Repo facts\nnone\n\n## Step 1\nDo it.\nfailure: pure\n"  # no writers:
    calls = []
    replies = iter([
        ({"status": "success", "summary": "planned", "artifacts": [str(plan_path)]}, invalid),
        ({"status": "success", "summary": "planned", "artifacts": [str(plan_path)]}, VALID_PLAN),
    ])

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        calls.append(req)
        reply, content = next(replies)
        plan_path.write_text(content)
        return AgentResult(
            status="ok", text="", session_id=req.session_id, usage=usage(),
            cost=0.01, context_tokens=150, context_window=100_000, envelope=reply,
        )

    adapter.calls = calls
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    envelope = phases.plan(run, "do X")
    assert envelope.status == "success"
    assert len(calls) == 2
    assert "writers" in calls[1].prompt  # the correction names the missing line
    rows = run.tracer.con.execute(
        "SELECT attempt, gate, passed FROM gate_results WHERE phase_id=? ORDER BY id",
        (run.phases[0].phase_id,),
    ).fetchall()
    plan_rows = [r for r in rows if r["gate"] == "plan_has_failure_lines"]
    assert [bool(r["passed"]) for r in plan_rows] == [False, True]
    assert plan_path.read_text() == VALID_PLAN
    # provenance is recorded from the corrected plan's bytes, and only after acceptance
    import hashlib

    prov = run.tracer.con.execute(
        "SELECT payload_json FROM events WHERE run_id='R1' AND name='plan_provenance'"
    ).fetchone()
    assert prov is not None
    assert json.loads(prov["payload_json"])["plan_sha256"] == hashlib.sha256(
        VALID_PLAN.encode()
    ).hexdigest()


def test_plan_failure_lines_gate_exhausts_on_an_invalid_plan(project, monkeypatch):
    """Every correction still misses both lines: the phase fails and the chain never
    advances to build."""
    run = make_run(project)
    plan_path = run.context_handoff_dir / "plan.md"
    invalid = "## Repo facts\nnone\n\n## Step 1\nDo it.\n"  # no failure:/writers:
    calls = []

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        calls.append(req)
        plan_path.write_text(invalid)
        return AgentResult(
            status="ok", text="", session_id=req.session_id, usage=usage(),
            cost=0.01, context_tokens=150, context_window=100_000,
            envelope={"status": "success", "summary": "planned", "artifacts": [str(plan_path)]},
        )

    adapter.calls = calls
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    with pytest.raises(agents.GateFailure, match="step 1 failure"):
        phases.plan(run, "do X")

    assert run.phases[0].status == "fail"
    assert [p.params.name for p in run.phases] == ["plan"]  # never advanced


# --- build ---


def test_build_returns_the_builder_envelope_and_names_the_phase_by_step(project, monkeypatch):
    run = make_run(project)
    adapter = stub_adapter(
        {
            "status": "success", "summary": "built", "changed_files": ["out.txt"],
            "artifacts": [], "commit_message": "Add out.txt", "notes_for_next_agent": "",
        },
        write={"out.txt": "hi\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    envelope = phases.build(run, BuildStep(step_no=1, prompt="implement step 1"))
    assert envelope.changed_files == ["out.txt"]
    assert run.phases[0].params.name == "build_1"


def test_build_retry_names_the_phase_with_the_attempt_number(project, monkeypatch):
    run = make_run(project)
    adapter = stub_adapter(
        {"status": "success", "summary": "built", "changed_files": [], "artifacts": [],
         "commit_message": "", "notes_for_next_agent": ""},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    phases.build(run, BuildStep(step_no=2, prompt="fix it", attempt=2))
    assert run.phases[0].params.name == "build_2_retry1"


# --- verify ---


def test_verify_passes_when_the_configured_commands_pass(project):
    run = make_run(project, {"test": "exit 0"})
    envelope = phases.verify(run, 1)
    assert envelope.passed
    assert run.phases[0].status == "success"  # the code phase itself always succeeds


def test_verify_reports_failure_without_raising(project):
    run = make_run(project, {"test": "echo boom && exit 1"})
    envelope = phases.verify(run, 1)
    assert not envelope.passed
    assert run.phases[0].status == "success"  # running the checks is the phase's job
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "running"  # a red check does not fail the Run by itself


def test_verify_names_the_phase_with_the_attempt_number(project):
    run = make_run(project, {"test": "exit 0"})
    phases.verify(run, 1, attempt=2)
    assert run.phases[0].params.name == "verify_1_retry1"


# --- review ---


def test_review_returns_the_reviewer_envelope(project, monkeypatch):
    run = make_run(project)
    adapter = stub_adapter(
        {
            "status": "success", "summary": "reviewed", "approved": True, "findings": [],
            "blocking": [], "trust_violations": [], "artifacts": [], "notes_for_next_agent": "",
        }
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    envelope = phases.review(run, ReviewStep(prompt="do X", checklist="It works.", base="main"))
    assert envelope.approved
    assert run.phases[0].params.name == "review"
    assert run.phases[0].params.owner == "reviewer"


def test_review_retry_names_the_phase_with_the_attempt_number(project, monkeypatch):
    run = make_run(project)
    adapter = stub_adapter(
        {
            "status": "success", "summary": "reviewed", "approved": True, "findings": [],
            "blocking": [], "trust_violations": [], "artifacts": [], "notes_for_next_agent": "",
        }
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    phases.review(run, ReviewStep(prompt="do X", checklist="It works.", base="main", attempt=2))
    assert run.phases[0].params.name == "review_retry1"


def test_review_prompt_carries_checklist_diff_changed_files_and_tests(project, monkeypatch):
    import json

    run = make_run(project, {"test": "exit 0"})
    phases.verify(run, 1)
    (project / "app.py").write_text("v2\n")
    (project / "new.py").write_text("print(1)\n")
    adapter = stub_adapter(
        {
            "status": "success", "summary": "reviewed", "approved": True, "findings": [],
            "blocking": [], "trust_violations": [], "artifacts": [], "notes_for_next_agent": "",
        }
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    phases.review(run, ReviewStep(prompt="do X", checklist="It works.", base="main"))
    p = adapter.calls[0].prompt
    assert "## Checklist\nIt works." in p
    assert "## Diff" in p
    assert "-v1" in p and "+v2" in p
    assert "- app.py" in p
    assert "- new.py (new)" in p
    assert "### app.py" not in p
    assert "## Tests\npassed: true\nnone" in p
    assert "Everything above is already in front of you" not in p
    assert "One grep for related code" not in p
    assert run.phases[1].params.name == "review"  # phase 0 is verify


def test_review_sends_no_turn_cap(project, monkeypatch):
    """Ticket 72: a reviewer that read the plan, inspected the diff and wrote review.md
    spent 5 tool turns; its closing text turn was the 6th and tripped the cap with an
    approved review already on disk. The token budget is the only ceiling now."""
    run = make_run(project)
    adapter = stub_adapter(
        {
            "status": "success", "summary": "reviewed", "approved": True, "findings": [],
            "blocking": [], "trust_violations": [], "artifacts": [], "notes_for_next_agent": "",
        }
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    phases.review(run, ReviewStep(prompt="do X", checklist="It works.", base="main"))
    assert adapter.calls[0].max_turns is None
    assert not hasattr(phases, "REVIEW_MAX_TURNS")
    assert not hasattr(phases, "REVIEW_DIFF_LIMIT")


def test_review_diff_is_hunks_with_context_and_a_large_file_never_rides_whole(project, monkeypatch):
    """A 3000-row file produces a hunk with 30 lines of context, not the whole file."""
    run = make_run(project, {"test": "exit 0"})
    # Write a large file
    lines = "".join(f"row {i:05d} " + "x" * 32 + "\n" for i in range(3000))
    (project / "app.py").write_text(lines)
    git(project, "add", "-A")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "big file")
    # Change one row
    lines = "".join(
        "row 01500 changed\n" if i == 1500 else f"row {i:05d} " + "x" * 32 + "\n"
        for i in range(3000)
    )
    (project / "app.py").write_text(lines)
    phases.verify(run, 1)
    adapter = stub_adapter(
        {
            "status": "success", "summary": "reviewed", "approved": True, "findings": [],
            "blocking": [], "trust_violations": [], "artifacts": [], "notes_for_next_agent": "",
        }
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    phases.review(run, ReviewStep(prompt="do X", checklist="It works.", base="main"))
    p = adapter.calls[0].prompt
    assert "@@" in p
    assert "+row 01500 changed" in p
    assert "row 01470" in p
    assert "row 01530" in p
    assert "row 01000" not in p
    assert p.count("row 01500 changed") == 1
    assert "### app.py" not in p
    assert "- app.py" in p
    assert len(p.encode()) < 60 * 1024


def test_review_prompt_omits_the_largest_files_hunks_first_when_over_budget(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    phases.verify(run, 1)
    (project / "app.py").write_text("v2\n")
    (project / "big.py").write_text("".join("b" * 99 + "\n" for _ in range(200)))
    (project / "mid.py").write_text("".join("m" * 99 + "\n" for _ in range(80)))
    monkeypatch.setattr(phases, "REVIEW_PROMPT_TOKENS", 6_000)
    adapter = stub_adapter(
        {
            "status": "success", "summary": "reviewed", "approved": True, "findings": [],
            "blocking": [], "trust_violations": [], "artifacts": [], "notes_for_next_agent": "",
        }
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    phases.review(run, ReviewStep(prompt="do X", checklist="It works.", base="main"))
    p = adapter.calls[0].prompt
    base = git_helper.rev("HEAD")
    assert "b" * 99 not in p
    assert "+" + "m" * 99 in p
    assert "+v2" in p
    assert "- big.py: +200 -0" in p
    assert f"git diff -U30 {base} -- big.py" in p
    assert "largest first" in p
    assert "- big.py (new)" in p
    assert "- mid.py (new)" in p
    assert "### app.py" not in p
    assert "### big.py" not in p
    assert "### mid.py" not in p


def test_fit_hunks_drops_the_largest_first_until_the_rest_fit():
    hunks = [("small", "s" * 10_000), ("big", "b" * 30_000), ("mid", "m" * 20_000)]
    assert phases._fit_hunks.__wrapped__ if hasattr(phases._fit_hunks, "__wrapped__") else phases._fit_hunks
    inline, omitted = phases._fit_hunks(hunks, 17_500)
    assert [p for p, _ in inline] == ["small", "mid"]
    assert [p for p, _ in omitted] == ["big"]
    inline, omitted = phases._fit_hunks(hunks, 7_500)
    assert [p for p, _ in inline] == ["small"]
    assert [p for p, _ in omitted] == ["big", "mid"]
    inline, omitted = phases._fit_hunks(hunks, 0)
    assert inline == []
    assert [p for p, _ in omitted] == ["big", "mid", "small"]
    inline, omitted = phases._fit_hunks(hunks, 100_000)
    assert [p for p, _ in inline] == ["small", "big", "mid"]
    assert omitted == []


def test_estimate_tokens_is_half_the_bytes_rounded_up():
    assert phases._estimate_tokens("ab" * 100) == 100
    assert phases._estimate_tokens("é") == 1  # 2 bytes -> ceil(2/2) = 1
    assert phases._estimate_tokens("") == 0


def test_review_prompt_reports_a_red_verify(project, monkeypatch):
    run = make_run(project, {"test": "echo boom && exit 1"})
    envelope = phases.verify(run, 1)
    adapter = stub_adapter(
        {
            "status": "success", "summary": "reviewed", "approved": True, "findings": [],
            "blocking": [], "trust_violations": [], "artifacts": [], "notes_for_next_agent": "",
        }
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    phases.review(run, ReviewStep(prompt="do X", checklist="It works.", base="main"))
    p = adapter.calls[0].prompt
    assert "passed: false" in p
    assert envelope.failures[0] in p
    assert "`echo boom && exit 1`: exit 1, " in p


_OK_REVIEW = {
    "status": "success", "summary": "reviewed", "approved": True, "findings": [],
    "blocking": [], "trust_violations": [], "artifacts": [], "notes_for_next_agent": "",
}


def _review_prompt(run, monkeypatch):
    adapter = stub_adapter(_OK_REVIEW)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    phases.review(run, ReviewStep(prompt="do X", checklist="It works.", base="main"))
    return adapter.calls[0].prompt


def test_verify_envelope_records_every_quality_command(project):
    run = make_run(project, {"test": "exit 0", "lint": "exit 0"})
    envelope = phases.verify(run, 1)
    phases.verify(run, 2)  # so verify_1 is an earlier verify, not the latest
    row = run.tracer.con.execute(
        "SELECT payload_json FROM envelopes WHERE output_type='VerifyOutput'"
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert [c["name"] for c in payload["checks"]] == ["test", "lint"]
    for c in payload["checks"]:
        assert c["command"] == "exit 0"
        assert c["returncode"] == 0 and c["passed"] is True
        assert c["duration_seconds"] >= 0
    assert "output_tail" not in row["payload_json"]
    assert "output_artifact" not in row["payload_json"]

    run2 = make_run(project, {"test": "exit 0", "lint": "exit 0"})
    run2.replay = run2.tracer.replay_map("R1")
    replayed = phases.verify(run2, 1)
    assert replayed.checks == envelope.checks
    assert [c.name for c in replayed.checks] == ["test", "lint"]
    assert run2.phases == []


def test_review_tests_section_lists_spine_recorded_commands_with_head(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    envelope = phases.verify(run, 1)
    assert envelope.head_sha == git_helper.rev("HEAD")
    p = _review_prompt(run, monkeypatch)
    assert "## Tests\npassed: true\nnone\nRecorded by the Spine" in p
    assert f"verified HEAD {git_helper.rev('HEAD')[:12]}" in p
    assert "- test `exit 0`: exit 0, " in p


def test_review_tests_section_without_a_head_sha_says_head_unavailable(project, monkeypatch):
    run = make_run(project)
    pid = run.tracer.add_phase("R1", seq=90, name="verify_1", stage="Build", kind="code", owner="git")
    run.tracer.set_phase_status(pid, "running")
    run.tracer.set_phase_status(pid, "success")
    run.tracer.record_envelope(
        "R1", pid, agent="git", output_type="VerifyOutput",
        payload_json=json.dumps({
            "status": "success", "passed": True,
            "checks": [{
                "name": "test", "command": "exit 0", "returncode": 0,
                "passed": True, "duration_seconds": 0.1,
            }],
        }),
        valid=True, attempt=1,
    )
    assert "verified HEAD unavailable" in _review_prompt(run, monkeypatch)


def test_review_tests_section_renders_a_legacy_verify_as_today(project, monkeypatch):
    run = make_run(project)
    pid = run.tracer.add_phase("R1", seq=90, name="verify_1", stage="Build", kind="code", owner="git")
    run.tracer.set_phase_status(pid, "running")
    run.tracer.set_phase_status(pid, "success")
    run.tracer.record_envelope(
        "R1", pid, agent="git", output_type="VerifyOutput",
        payload_json=json.dumps({"status": "success", "passed": True}), valid=True, attempt=1,
    )
    p = _review_prompt(run, monkeypatch)
    assert "## Tests\npassed: true\nnone\n\n" in p
    assert "Recorded by the Spine (the factory ran" not in p  # the prompt text itself names the phrase


def test_interrupted_verify_records_no_partial_checks(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})

    def boom(*a, **kw):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(phases.diff_accounting, "evaluate", boom)
    with pytest.raises(RuntimeError, match="interrupted"):
        phases.verify(run, 1)
    n = run.tracer.con.execute(
        "SELECT COUNT(*) FROM envelopes WHERE output_type='VerifyOutput'"
    ).fetchone()[0]
    assert n == 0


def test_review_without_a_verify_row_says_so(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    adapter = stub_adapter(
        {
            "status": "success", "summary": "reviewed", "approved": True, "findings": [],
            "blocking": [], "trust_violations": [], "artifacts": [], "notes_for_next_agent": "",
        }
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    phases.review(run, ReviewStep(prompt="do X", checklist="It works.", base="main"))
    p = adapter.calls[0].prompt
    assert "passed: false\nno verify recorded" in p


def test_a_replayed_review_makes_no_git_calls(project, monkeypatch):
    import json

    run = make_run(project)
    run.replay = {
        "review": {
            "phase_id": "R1:06", "output_type": "ReviewOutput",
            "payload_json": json.dumps({"status": "success", "approved": True}),
        }
    }
    monkeypatch.setattr(git_helper, "diff_hunks", lambda base, **kw: (_ for _ in ()).throw(AssertionError("replayed review must not diff")))

    result = phases.review(run, ReviewStep(prompt="do X", checklist="c", base="main"))
    assert result.approved
    assert run.phases == []


# --- revise ---


@pytest.mark.parametrize("attempt", [1, 2])
def test_revise_hands_the_builder_the_reviewers_blocking_items(project, monkeypatch, attempt):
    from lime.data_types import ReviewOutput, ReviewFinding

    sentinel = "SENTINEL_GIT_FACTS"
    called_with = []

    def fake_git_facts(repo_root, continuation):
        called_with.append((repo_root, continuation))
        return sentinel

    monkeypatch.setattr(phases.budget, "git_facts", fake_git_facts)

    run = make_run(project)
    adapter = stub_adapter(
        {"status": "success", "summary": "fixed", "changed_files": [], "artifacts": [],
         "commit_message": "", "notes_for_next_agent": ""},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    review_output = ReviewOutput(
        status="success", approved=False,
        blocking=["missing greet()", "wrong file"],
        trust_violations=["hard-coded secret"],
        findings=[
            ReviewFinding(requirement="it works", met=False, evidence="fails"),
        ],
    )

    phases.revise(run, review_output, attempt=attempt)
    assert run.phases[0].params.name == f"revise_{attempt}"
    assert run.phases[0].params.owner == "builder"
    prompt = adapter.calls[0].prompt

    # All review items present before the fresh-session line/facts
    assert "missing greet()" in prompt
    assert "wrong file" in prompt
    assert "hard-coded secret" in prompt
    assert "it works — fails" in prompt
    assert prompt.index("missing greet()") < prompt.index("This is a fresh session")
    assert prompt.index("wrong file") < prompt.index("This is a fresh session")
    assert prompt.index("hard-coded secret") < prompt.index("This is a fresh session")
    assert prompt.index("it works — fails") < prompt.index("This is a fresh session")

    # Fresh-session line and git sentinel present
    assert "This is a fresh session" in prompt
    assert sentinel in prompt

    # git_facts called with (run.repo_root, 0) exactly once
    assert called_with == [(run.repo_root, 0)]


@pytest.mark.parametrize("root", ["lime/skill/lime/", ".claude/skills/lime/"], ids=["lime", "claude"])
def test_revise_routes_missing_skill_requirements_to_builder(project, monkeypatch, root):
    """Ticket 132: a missing required skill change goes back to the builder, never the documenter."""
    from lime.data_types import ReviewFinding, ReviewOutput

    run = make_run(project)
    adapter = stub_adapter(
        {"status": "success", "summary": "fixed", "changed_files": [], "artifacts": [],
         "commit_message": "", "notes_for_next_agent": ""},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    path = root + "SKILL.md"
    change = "add the required rule"
    review_output = ReviewOutput(
        status="success", approved=False,
        blocking=[f"missing required skill update: {path} — {change}"],
        findings=[ReviewFinding(requirement=f"update {path}", met=False, evidence=f"{path}: rule absent")],
    )

    phases.revise(run, review_output, attempt=1)

    assert run.phases[0].params.name == "revise_1"
    assert run.phases[0].params.owner == "builder"
    assert all(p.params.owner != "documenter" for p in run.phases)
    request = adapter.calls[0]
    # the missing path and change survive the handoff
    assert path in request.prompt
    assert change in request.prompt
    assert f"update {path}" in request.prompt
    # the effective builder system prompt owns skill markdown
    for skill_root in ("lime/skill/lime/", ".claude/skills/lime/"):
        assert skill_root in request.system_prompt
    assert "are yours, not the documenter's" in request.system_prompt


# --- commit ---


def test_commit_stages_and_commits_the_working_tree(project):
    run = make_run(project)
    (project / "changed.txt").write_text("new\n")
    sha = phases.commit(run, "Add changed.txt")
    assert sha
    log = subprocess.run(
        ["git", "-C", str(project), "log", "-1", "--pretty=%B"], capture_output=True, text=True
    ).stdout
    assert "Add changed.txt" in log
    assert f"Lime-Run: {run.run_id}" in log
    assert "Lime-Role: builder" in log


def test_commit_docs_names_the_role_and_deletes_the_ticket(project):
    run = make_run(project)
    ticket_path = project / "ticket.md"
    ticket_path.write_text("close me\n")
    (project / "AGENTS.md").write_text("docs\n")
    sha = phases.commit_docs(run, "Close ticket", ticket_path)
    assert sha
    assert not ticket_path.exists()
    log = subprocess.run(
        ["git", "-C", str(project), "log", "-1", "--pretty=%B"], capture_output=True, text=True
    ).stdout
    assert "Lime-Role: documenter" in log
    assert run.phases[0].params.name == "commit_docs"


def test_commit_docs_deletes_a_consumed_prototype_route_dir(project):
    """Front-end wiring (spec §7): the docs commit deletes the route dir once
    the feature chain has consumed it, when the authoritative store is available
    and no other open ticket names it."""
    run = make_run(project)
    ticket_path = project / "ticket.md"
    ticket_path.write_text("close me\n")
    route_dir = project / "app" / "proto-toy"
    route_dir.mkdir(parents=True)
    (route_dir / "page.tsx").write_text("proto\n")
    (route_dir / "NOTES.md").write_text("fixed: layout\n")
    tickets_dir = project / ".lime" / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(project), "add", "-A"], capture_output=True)
    subprocess.run(
        ["git", "-C", str(project), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "add prototype route"],
        capture_output=True,
    )

    sha = phases.commit_docs(run, "Close ticket", ticket_path, route_dir, tickets_dir=tickets_dir)

    assert sha
    assert not route_dir.exists()
    log = subprocess.run(
        ["git", "-C", str(project), "log", "-1", "--pretty=%B"], capture_output=True, text=True
    ).stdout
    assert "Lime-Role: documenter" in log


def test_commit_docs_ignores_a_route_dir_that_does_not_exist(project):
    """No prototype consumed (feature/bug-fix/greenfield tickets) — nothing to delete."""
    run = make_run(project)
    ticket_path = project / "ticket.md"
    ticket_path.write_text("close me\n")
    (project / "AGENTS.md").write_text("docs\n")

    sha = phases.commit_docs(run, "Close ticket", ticket_path, project / "app" / "proto-none")

    assert sha
    assert not ticket_path.exists()


def test_commit_docs_alone_still_commits_when_only_the_ticket_deletion_changed(project):
    """Delete-on-close: nothing else needs to change for the docs commit to exist."""
    run = make_run(project)
    ticket_path = project / "ticket.md"
    ticket_path.write_text("close me\n")
    subprocess.run(["git", "-C", str(project), "add", "-A"], capture_output=True)
    subprocess.run(
        ["git", "-C", str(project), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "add ticket"],
        capture_output=True,
    )
    sha = phases.commit_docs(run, "lime(R1): close ticket 41", ticket_path)
    assert sha
    assert not ticket_path.exists()


# --- commit_docs: file prototypes are kept, directory routes deleted (ticket 168) ---


def _log_payloads(run, name):
    rows = run.tracer.con.execute(
        "SELECT payload_json FROM events WHERE run_id=? AND type='log' AND name=? ORDER BY event_id",
        (run.run_id, name),
    ).fetchall()
    return [json.loads(r["payload_json"] or "{}") for r in rows]


def _git_rev(cwd, ref="HEAD"):
    return subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", ref], capture_output=True, text=True, check=True
    ).stdout.strip()


def _git_show(cwd, spec):
    return subprocess.run(
        ["git", "-C", str(cwd), "show", spec], capture_output=True, text=True
    )


@pytest.mark.parametrize("as_str", [False, True], ids=["path", "str"])
def test_commit_docs_keeps_file_prototype(trunk_project, as_str):
    """A `Prototype:` that names a file is kept at close; only a route directory is
    deleted. Both a Path and a string `route_dir` are accepted."""
    run = make_run(trunk_project)
    ticket_path = trunk_project / ".lime" / "tickets" / "41-front.md"
    ticket_path.parent.mkdir(parents=True)
    ticket_path.write_text("close me\n")
    proto = trunk_project / "prototypes" / "tokens.css"
    proto.parent.mkdir(parents=True)
    proto.write_text(":root { --x: 1 }\n")
    (trunk_project / "AGENTS.md").write_text("docs v1\n")
    git(trunk_project, "add", "-A")
    git(trunk_project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed")
    (trunk_project / "AGENTS.md").write_text("docs v2\n")  # pending docs change

    route_dir = str(proto) if as_str else proto
    sha = phases.commit_docs(run, "Close ticket", ticket_path, route_dir)

    assert sha
    assert not ticket_path.exists()
    assert _git_show(trunk_project, "HEAD:.lime/tickets/41-front.md").returncode != 0
    assert proto.read_bytes() == b":root { --x: 1 }\n"
    assert _git_show(trunk_project, "HEAD:prototypes/tokens.css").stdout == ":root { --x: 1 }\n"
    assert _git_show(trunk_project, "HEAD:AGENTS.md").stdout == "docs v2\n"
    log = subprocess.run(
        ["git", "-C", str(trunk_project), "log", "-1", "--pretty=%B"], capture_output=True, text=True
    ).stdout
    assert "Lime-Role: documenter" in log
    payloads = _log_payloads(run, "commit_docs")
    assert any(str(proto) in (p.get("kept") or []) for p in payloads)
    assert all(str(proto) not in (p.get("deleted") or []) for p in payloads)


def test_commit_docs_recovers_after_file_prototype_crash(trunk_project):
    """The interrupted state the old crash left behind — ticket already unlinked,
    docs modified — commits cleanly on restart without recreating the ticket."""
    run = make_run(trunk_project)
    ticket_path = trunk_project / ".lime" / "tickets" / "41-front.md"
    ticket_path.parent.mkdir(parents=True)
    ticket_path.write_text("close me\n")
    proto = trunk_project / "prototypes" / "tokens.css"
    proto.parent.mkdir(parents=True)
    proto.write_text(":root {}\n")
    (trunk_project / "AGENTS.md").write_text("docs v1\n")
    git(trunk_project, "add", "-A")
    git(trunk_project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed")
    # Reproduce the exact interrupted state: the old code unlinked the ticket and
    # then crashed on the file prototype, leaving docs + deletion pending.
    ticket_path.unlink()
    (trunk_project / "AGENTS.md").write_text("docs v2\n")

    sha = phases.commit_docs(run, "Close ticket", ticket_path, proto)

    assert sha
    assert proto.read_bytes() == b":root {}\n"
    assert _git_show(trunk_project, "HEAD:AGENTS.md").stdout == "docs v2\n"
    assert _git_show(trunk_project, "HEAD:.lime/tickets/41-front.md").returncode != 0
    status = subprocess.run(
        ["git", "-C", str(trunk_project), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert status == ""


def test_commit_docs_successful_replay_leaves_files_and_git_alone(trunk_project, monkeypatch):
    """Duplicate delivery: a replayed commit_docs returns before any deletion or commit."""
    run = make_run(trunk_project)
    run.replay = {"commit_docs": {"phase_id": "R1:11", "output_type": None, "payload_json": None}}
    ticket_path = trunk_project / "ticket.md"
    ticket_path.write_text("still here\n")
    proto = trunk_project / "prototypes" / "tokens.css"
    proto.parent.mkdir(parents=True)
    proto.write_text("css\n")
    head_before = _git_rev(trunk_project)

    def explode(*a, **kw):
        raise AssertionError("a replayed commit_docs must not commit")

    monkeypatch.setattr(git_helper, "commit_all", explode)

    result = phases.commit_docs(run, "msg", ticket_path, proto)
    assert result == ""
    assert run.phases == []
    assert ticket_path.exists()
    assert proto.read_bytes() == b"css\n"
    assert _git_rev(trunk_project) == head_before


def test_commit_docs_without_prototype_keeps_unrelated_assets(trunk_project):
    """`route_dir=None` deletes nothing else on disk — unrelated assets survive close."""
    run = make_run(trunk_project)
    ticket_path = trunk_project / "ticket.md"
    ticket_path.write_text("close me\n")
    (trunk_project / "AGENTS.md").write_text("docs\n")
    other_file = trunk_project / "prototypes" / "other.css"
    other_file.parent.mkdir(parents=True)
    other_file.write_text("x\n")
    other_dir = trunk_project / "prototypes" / "other"
    other_dir.mkdir(parents=True)
    (other_dir / "NOTES.md").write_text("n\n")

    sha = phases.commit_docs(run, "Close ticket", ticket_path, None)

    assert sha
    assert not ticket_path.exists()
    assert other_file.exists()
    assert other_dir.exists()


# --- changes ---


def test_changes_captures_the_diff_since_base_as_an_envelope(project):
    run = make_run(project)
    (project / "app.py").write_text("v2\n")
    subprocess.run(["git", "-C", str(project), "add", "-A"], capture_output=True)
    subprocess.run(
        ["git", "-C", str(project), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "change app.py"],
        capture_output=True,
    )

    envelope = phases.changes(run, "HEAD~1")
    assert envelope.status == "success"
    assert "app.py" in envelope.changed_files
    assert run.phases[0].params.name == "changes"
    assert run.phases[0].params.kind == "code"


# --- document ---

_DOC_REPLY = {"status": "success", "summary": "nothing to change", "changed_docs": [], "adrs": [],
             "commit_message": "", "artifacts": [], "notes_for_next_agent": ""}


def _record_review(run, phase_id, payload):
    run.tracer.record_envelope(
        run.run_id, phase_id, agent="reviewer", output_type="ReviewOutput",
        payload_json=json.dumps(payload), valid=True, attempt=1,
    )


def test_document_returns_the_documenter_envelope(project, monkeypatch):
    run = make_run(project)
    _record_review(run, "R1:99", {"approved": True, "summary": "ok", "findings": []})
    adapter = stub_adapter(
        {"status": "success", "summary": "nothing to change", "changed_docs": [], "adrs": [],
         "commit_message": "", "artifacts": [], "notes_for_next_agent": ""},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    changes_envelope = ChangesOutput(status="success", changed_files=["app.py"], diff_path="x")

    envelope = phases.document(run, "do X", changes_envelope)
    assert envelope.status == "success"
    assert envelope.changed_docs == []  # legitimate empty-success is preserved
    assert run.phases[0].params.name == "document"
    assert run.phases[0].params.owner == "documenter"

    request = adapter.calls[0]
    # the real composed request receives the ChangesOutput (changed paths and diff path)
    assert "\"changed_files\"" in request.prompt
    assert "app.py" in request.prompt
    assert "\"diff_path\"" in request.prompt and "x" in request.prompt
    # the caller's prompt is preserved and the factory's reservation lines are appended
    assert "do X" in request.prompt
    assert "ADR number: 0001 (reserved — use it or write no ADR)" in request.prompt
    assert f"Review: {run.context_handoff_dir / 'review.md'}" in request.prompt
    assert (run.context_handoff_dir / "review.md").exists()
    # and the shipped scope/out-of-scope instructions, not only the template files
    assert "Write only `AGENTS.md`, `CONTEXT.md`, and `docs/adr/**`" in request.system_prompt
    assert "stays untouched" in request.system_prompt
    assert "`notes_for_next_agent`" in request.system_prompt
    assert "handoff-document-1.md" in request.system_prompt
    # shipped system prompt (real composed text, not a mock): the requested ADR rules
    system = request.system_prompt
    assert (
        "Never add a rule sentence to AGENTS.md, CLAUDE.md or any doc; fix only a"
        " command, path or name the diff made false"
    ) in system
    assert "`docs/adr/<ADR number>-<slug>.md`" in system
    assert "number from the prompt, never self-chosen" in system
    assert "`Status: proposed`" in system
    assert "the owner accepts it in the slice's owner QA `Type: task` ticket" in system
    assert (
        "Read the review at `Review:` before writing; a behaviour the reviewer"
        " contested is not doctrine"
    ) in system
    assert "next free number" not in system
    assert "hard rule" not in system


def test_document_writes_review_md_from_the_recorded_review(project, monkeypatch):
    run = make_run(project)
    _record_review(run, "R1:99", {
        "approved": True,
        "summary": "all good",
        "findings": [
            {"requirement": "does the thing", "met": True, "evidence": "phases.py:860"},
            {"requirement": "covers the edge", "met": False, "evidence": "missing"},
        ],
        "blocking": ["add the test"],
        "trust_violations": ["x.py:1 — wrote .env"],
        "notes_for_next_agent": "check docs",
    })
    captured: dict[str, str] = {}

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        captured["review"] = (run.context_handoff_dir / "review.md").read_text()
        captured["prompt"] = req.prompt
        return stub_adapter(_DOC_REPLY)(req)

    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    changes_envelope = ChangesOutput(status="success", changed_files=["app.py"], diff_path="x")

    phases.document(run, "do X", changes_envelope)

    review = captured["review"]
    assert "approved: true" in review
    assert "all good" in review
    assert "- [x] does the thing — met — phases.py:860" in review
    assert "- [ ] covers the edge — unmet — missing" in review
    assert "add the test" in review
    assert "x.py:1 — wrote .env" in review
    assert "check docs" in review
    assert f"Review: {run.context_handoff_dir / 'review.md'}" in captured["prompt"]


def test_document_review_md_holds_the_latest_review_and_replaces_a_stale_file(project, monkeypatch):
    run = make_run(project)
    (run.context_handoff_dir / "review.md").write_text("stale")
    _record_review(run, "R1:97", {
        "approved": False, "summary": "first pass", "findings": [{"requirement": "x", "met": True}],
        "blocking": ["fix the gate"],
    })
    _record_review(run, "R1:98", {
        "approved": True, "summary": "second pass", "findings": [], "blocking": [],
    })
    monkeypatch.setitem(agents.ADAPTERS, "pi", stub_adapter(_DOC_REPLY))

    phases.document(run, "do X", ChangesOutput(status="success", changed_files=[], diff_path="x"))

    review = (run.context_handoff_dir / "review.md").read_text()
    assert "second pass" in review
    assert "approved: true" in review
    assert "stale" not in review
    assert "first pass" not in review
    assert "fix the gate" not in review


def test_document_without_a_recorded_review_writes_no_file_and_no_line(project, monkeypatch):
    run = make_run(project)
    adapter = stub_adapter(_DOC_REPLY)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    envelope = phases.document(
        run, "do X", ChangesOutput(status="success", changed_files=[], diff_path="x")
    )

    assert not (run.context_handoff_dir / "review.md").exists()
    assert "Review: " not in adapter.calls[0].prompt
    assert "review.md" not in adapter.calls[0].prompt
    assert envelope.status == "success"
    assert "ADR number: 0001 (reserved — use it or write no ADR)" in adapter.calls[0].prompt


def test_document_prompt_matches_reserved_origin_ref(trunk_project, monkeypatch):
    from lime import numbering

    origin = attach_trunk_origin(trunk_project)
    run = make_run(trunk_project)
    reply = {"status": "success", "summary": "nothing to change", "changed_docs": [], "adrs": [],
             "commit_message": "", "artifacts": [], "notes_for_next_agent": ""}
    captured: dict[str, str] = {}

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        match = re.search(r"ADR number: (\d{4}) ", req.prompt)
        assert match, req.prompt
        captured["prompt"] = req.prompt
        ref = f"refs/lime/adr/{match.group(1)}"
        assert git_ls(trunk_project, ref), "the reserved ref must exist before the agent runs"
        assert numbering._holder(trunk_project, ref) == run.run_id
        return stub_adapter(reply)(req)

    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    changes_envelope = ChangesOutput(status="success", changed_files=["app.py"], diff_path="x")

    envelope = phases.document(run, "do X", changes_envelope)
    assert envelope.status == "success"
    assert "do X" in captured["prompt"]  # supplied prompt preserved
    assert "ADR number: 0001 (reserved — use it or write no ADR)" in captured["prompt"]
    assert "Review: " not in captured["prompt"]  # no ReviewOutput recorded → no Review: line (ticket 232)
    # the ledger ref persists even though no ADR file was written
    assert git_ls(trunk_project, "refs/lime/adr/0001")
    assert not (trunk_project / "docs" / "adr").exists()


def test_document_reservation_failure_prevents_agent_call(project, monkeypatch):
    from lime import numbering

    run = make_run(project)

    def boom(root, run_id):
        raise numbering.NumberingError("origin unreachable")

    monkeypatch.setattr(numbering, "reserve_adr", boom)
    called: list = []

    def exploding_adapter(req, on_event=None, on_spawn=None, on_exit=None):
        called.append(req)
        raise AssertionError("reservation failure must prevent the agent call")

    monkeypatch.setitem(agents.ADAPTERS, "pi", exploding_adapter)

    with pytest.raises(numbering.NumberingError, match="origin unreachable"):
        phases.document(run, "do X", ChangesOutput(status="success", changed_files=[], diff_path="x"))
    assert not called
    assert run.phases[0].status == "fail"


def test_document_replay_does_not_reserve(project, monkeypatch):
    from lime import numbering

    run = make_run(project)
    run.replay = {"document": {
        "phase_id": "R1:09", "output_type": "DocumentOutput",
        "payload_json": json.dumps({"status": "success", "summary": "replayed"}),
    }}

    def boom(root, run_id):
        raise AssertionError("a replayed document must not reserve")

    monkeypatch.setattr(numbering, "reserve_adr", boom)

    envelope = phases.document(run, "do X", ChangesOutput(status="success"))
    assert envelope.summary == "replayed"
    assert run.phases == []  # no new phase row — the original success row stands


# --- push_pr ---


def _stub_push(monkeypatch):
    """Monkeypatch the git push/PR surface; returns the list of pushed branches."""
    pushed = []
    monkeypatch.setattr(git_helper, "push", lambda branch: pushed.append(branch))
    monkeypatch.setattr(git_helper, "open_pr", lambda title, body, base: body)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")
    return pushed


@pytest.mark.parametrize("added,cap,pct", [(120, 100, "+20%"), (40, 100, "-60%"), (100, 100, "+0%")])
def test_push_pr_body_shows_size_against_the_budget(project, monkeypatch, added, cap, pct):
    run = make_run(project)
    run.tracer.event(
        run.run_id, "log", name="verify_1", payload={"total_added": added, "max_added_lines": cap}
    )
    _stub_push(monkeypatch)
    body = phases.push_pr(run, TICKET, "main")
    assert f"### Diff\nSize: {added} added lines / budget {cap} ({pct})\n```" in body


def test_push_pr_body_size_is_the_newest_verify_measurement(project, monkeypatch):
    run = make_run(project)
    run.tracer.event(run.run_id, "log", name="verify_1", payload={"total_added": 30, "max_added_lines": 100})
    run.tracer.event(
        run.run_id, "log", name="verify_2_retry1", payload={"total_added": 150, "max_added_lines": 100}
    )
    run.tracer.event(
        run.run_id, "log", name="diff_early_check", payload={"total_added": 999, "max_added_lines": 100}
    )
    _stub_push(monkeypatch)
    body = phases.push_pr(run, TICKET, "main")
    assert "Size: 150 added lines / budget 100 (+50%)" in body
    assert "999" not in body


def test_push_pr_body_has_no_size_line_when_unmeasured(project, monkeypatch):
    run = make_run(project)
    pushed = _stub_push(monkeypatch)
    body = phases.push_pr(run, TICKET, "main")
    assert len(pushed) == 1
    assert "Size:" not in body
    assert "### Diff\n```" in body


def test_push_pr_body_skips_an_unreadable_size_payload(project, monkeypatch):
    run = make_run(project)
    run.tracer.event(
        run.run_id, "log", name="verify_1", payload={"total_added": None, "max_added_lines": 100}
    )
    _stub_push(monkeypatch)
    body = phases.push_pr(run, TICKET, "main")
    assert "Size:" not in body
    assert phases.last_size(run.tracer.con, run.run_id) is None


def test_push_pr_body_size_has_no_percentage_at_a_zero_budget(project, monkeypatch):
    run = make_run(project)
    run.tracer.event(run.run_id, "log", name="verify_1", payload={"total_added": 5, "max_added_lines": 0})
    _stub_push(monkeypatch)
    body = phases.push_pr(run, TICKET, "main")
    assert "Size: 5 added lines / budget 0\n```" in body


def test_push_pr_body_size_comes_from_a_real_verify(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    phases.verify(run, 1)
    p = _verify_log_payload(run)
    _stub_push(monkeypatch)
    body = phases.push_pr(run, TICKET, "main")
    assert f"Size: {p['total_added']} added lines / budget {p['max_added_lines']} (" in body


def test_total_added_has_one_writer_the_verify_log():
    hits = []
    for path in Path(lime.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    kws = [k.arg for k in inner.keywords]
                    if "total_added" in kws:
                        hits.append((path.name, node.name, kws))
                elif isinstance(inner, ast.Dict):
                    keys = [k.value for k in inner.keys if isinstance(k, ast.Constant)]
                    if "total_added" in keys:
                        hits.append((path.name, node.name, keys))
    assert [(name, fn) for name, fn, _ in hits] == [("phases.py", "verify")]
    assert "max_added_lines" in hits[0][2]


def test_push_pr_pushes_then_opens_a_pr_with_a_phase_table_body(project, monkeypatch):
    run = make_run(project)
    with run.phase(PhaseParams(
        name="build_1", stage="Build", kind="agent", owner="builder", description="Implement Step 1."
    )):
        pass

    calls = {}

    def fake_push(branch):
        calls["branch"] = branch

    def fake_open_pr(title, body, base):
        calls["title"] = title
        calls["body"] = body
        calls["base"] = base
        return "https://github.com/x/y/pull/1"

    monkeypatch.setattr(git_helper, "push", fake_push)
    monkeypatch.setattr(git_helper, "open_pr", fake_open_pr)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")

    url = phases.push_pr(run, TICKET, "main")
    assert url == "https://github.com/x/y/pull/1"
    row = run.tracer.con.execute(
        "SELECT pr_number, pr_url FROM runs WHERE run_id=?", (run.run_id,)
    ).fetchone()
    assert (row["pr_number"], row["pr_url"]) == (1, "https://github.com/x/y/pull/1")
    assert calls["branch"] == "lime/feature/41-toy"
    assert calls["base"] == "main"
    assert "41-toy: Toy Ticket" == calls["title"]
    assert "build_1" in calls["body"]
    assert f"lime trace {run.run_id}" in calls["body"]
    assert "### Last test result" in calls["body"]
    assert "(no test command ran)" in calls["body"]
    assert "### Review" in calls["body"]
    assert "(no review recorded)" in calls["body"]
    assert "### Diff" in calls["body"]


def test_resumed_push_pr_keeps_one_pr_number_and_url(project, monkeypatch):
    run = make_run(project)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")

    real = subprocess.run
    state = {"create_rc": 0}

    def fake_run(cmd, **kw):
        if cmd[:3] == ["gh", "pr", "create"]:
            if state["create_rc"] == 0:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="https://github.com/x/y/pull/7\n", stderr=""
                )
            return subprocess.CompletedProcess(
                cmd, 1, stdout="",
                stderr='a pull request for branch "b" into branch "main" already exists',
            )
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout='{"state":"OPEN","url":"https://github.com/x/y/pull/7"}',
                stderr="",
            )
        return real(cmd, **kw)

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    url = phases.push_pr(run, TICKET, "main")
    assert url == "https://github.com/x/y/pull/7"

    resumed = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    state["create_rc"] = 1
    url2 = phases.push_pr(resumed, TICKET, "main")
    assert url2 == "https://github.com/x/y/pull/7"  # the branch's OPEN PR, not an error
    row = resumed.tracer.con.execute(
        "SELECT pr_number, pr_url FROM runs WHERE run_id='R1'"
    ).fetchone()
    assert (row["pr_number"], row["pr_url"]) == (7, "https://github.com/x/y/pull/7")
    assert resumed.tracer.con.execute(
        "SELECT count(*) FROM runs WHERE run_id='R1'"
    ).fetchone()[0] == 1


def test_push_pr_delivers_when_the_pr_write_fails(project, monkeypatch, capsys):
    run = make_run(project)
    _stub_push(monkeypatch)
    monkeypatch.setattr(git_helper, "open_pr", lambda title, body, base: "https://github.com/x/y/pull/1")

    def boom(*a, **kw):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(run.tracer, "set_pr", boom)
    url = phases.push_pr(run, TICKET, "main")
    assert url == "https://github.com/x/y/pull/1"
    row = run.tracer.con.execute(
        "SELECT status FROM phases WHERE name='push_pr' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "success"
    captured = capsys.readouterr()
    assert "readonly" not in captured.out + captured.err


def test_push_pr_body_carries_the_last_verify_and_review_verdict(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    phases.verify(run, 1)  # runs the real `commands.test` command: passes
    run.tracer.record_envelope(
        run.run_id, run.phases[-1].phase_id, agent="reviewer", output_type="ReviewOutput",
        payload_json=json.dumps({"approved": True, "summary": "all good", "findings": []}),
        valid=True, attempt=1,
    )

    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda title, body, base: body)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")

    body = phases.push_pr(run, TICKET, "main")
    assert "test: passed" in body
    assert "approved — all good" in body


def test_push_pr_body_sources_phases_from_the_trace_not_memory(project, monkeypatch):
    """A resumed Run's in-memory phase list starts empty — the PR body must still
    carry the first pass's phases, so it reads the trace db (spec §9 resume)."""
    first = make_run(project)
    with first.phase(PhaseParams(
        name="build_1", stage="Build", kind="agent", owner="builder", description="Implement Step 1."
    )):
        pass

    resumed = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda title, body, base: body)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")

    body = phases.push_pr(resumed, TICKET, "main")
    assert "build_1" in body


def test_push_pr_body_lists_the_plans_assumed_steps(project, monkeypatch):
    run = make_run(project)
    plan = (
        "## Repo facts\nnone\n\n"
        "## Step 1\nDo it.\nfailure: pure\nwriters: none\n\n"
        "## Step 2 — x\nDo it.\nfailure: assumed — retry once\nwriters: none\n\n"
        "## Step 3\nDo it.\nfailure: assumed — keep the old row\nwriters: none\n"
    )
    (run.context_handoff_dir / "plan.md").write_text(plan)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda title, body, base: body)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")

    body = phases.push_pr(run, TICKET, "main")
    assert "## Assumptions\nStep 2: retry once\nStep 3: keep the old row\n\n" in body
    assert "Step 1:" not in body
    assert body.index("## Done means") < body.index("## Assumptions") < body.index("### Phases")


def test_push_pr_body_has_no_assumptions_section_without_an_assumed_step(project, monkeypatch):
    run = make_run(project)
    (run.context_handoff_dir / "plan.md").write_text(VALID_PLAN)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda title, body, base: body)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")

    body = phases.push_pr(run, TICKET, "main")
    assert "## Assumptions" not in body
    assert "## Done means\nIt works.\n\n### Phases" in body


@pytest.mark.parametrize("mode", ["missing", "bad-utf8", "directory"])
def test_push_pr_body_unchanged_when_plan_md_is_missing_or_unreadable(project, monkeypatch, mode):
    run = make_run(project)
    plan_path = run.context_handoff_dir / "plan.md"
    if mode == "bad-utf8":
        plan_path.write_bytes(b"## Step 1\nfailure: assumed \xff\xfe\n")
    elif mode == "directory":
        plan_path.mkdir()
    pushed = []
    monkeypatch.setattr(git_helper, "push", lambda branch: pushed.append(branch))
    monkeypatch.setattr(git_helper, "open_pr", lambda title, body, base: body)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")

    body = phases.push_pr(run, TICKET, "main")
    assert "## Assumptions" not in body
    assert "## Done means\nIt works.\n\n### Phases" in body
    assert len(pushed) == 1


def test_pr_body_parses_the_plan_through_gates(project, monkeypatch):
    run = make_run(project)
    (run.context_handoff_dir / "plan.md").write_text(VALID_PLAN)
    monkeypatch.setattr(gates, "plan_assumptions", lambda text: [(7, "sentinel outcome")])
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda title, body, base: body)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")

    body = phases.push_pr(run, TICKET, "main")
    assert "## Assumptions\nStep 7: sentinel outcome\n" in body


# --- resume replay: successful phases are not re-run (spec §9) ---


def test_verify_records_its_envelope_for_replay(project):
    run = make_run(project, {"test": "exit 0"})
    phases.verify(run, 1)
    row = run.tracer.con.execute(
        "SELECT agent, valid, payload_json FROM envelopes WHERE output_type='VerifyOutput'"
    ).fetchone()
    assert row["agent"] == "git"
    assert row["valid"] == 1
    assert json.loads(row["payload_json"])["passed"] is True


def test_changes_records_its_envelope_for_replay(project):
    run = make_run(project)
    (project / "app.py").write_text("v2\n")
    git(project, "add", "-A")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "change")

    phases.changes(run, "HEAD~1")
    row = run.tracer.con.execute(
        "SELECT agent, payload_json FROM envelopes WHERE output_type='ChangesOutput'"
    ).fetchone()
    assert row["agent"] == "git"
    assert "app.py" in json.loads(row["payload_json"])["changed_files"]


def test_a_replayed_agent_phase_returns_the_recorded_envelope_without_running(project, monkeypatch):
    import hashlib

    from lime import limits
    from lime.data_types import PlanProvenance

    run = make_run(project)
    plan_path = run.context_handoff_dir / "plan.md"
    plan_path.write_text(VALID_PLAN)
    expected = limits.append_planner("do X", run.agent("builder"), run.cfg)
    prov = PlanProvenance(
        ticket_sha256="", prompt=expected,
        plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
    )
    run.tracer.event(
        run.run_id, "handoff", phase_id="R1:03", name="plan_provenance",
        payload=prov.model_dump(),
    )
    run.replay = {"plan": {"phase_id": "R1:03", "output_type": "PlanOutput",
                           "payload_json": json.dumps({"status": "success", "summary": "planned"})}}

    def exploding_adapter(req, on_event=None, on_spawn=None, on_exit=None):
        raise AssertionError("a replayed phase must not spawn an agent")

    monkeypatch.setitem(agents.ADAPTERS, "pi", exploding_adapter)

    envelope = phases.plan(run, "do X")
    assert envelope.status == "success"
    assert envelope.summary == "planned"
    assert run.phases == []  # no new phase row — the original success row stands


def test_a_replayed_code_phase_is_skipped_without_a_new_phase_row(project):
    run = make_run(project, {"setup": "exit 1"})  # would fail the Run if it actually ran
    run.replay = {"setup": {"phase_id": "R1:02", "output_type": None, "payload_json": None}}
    phases.setup(run)
    assert run.phases == []


def test_a_replayed_commit_leaves_git_alone(project):
    run = make_run(project)
    run.replay = {"commit_code": {"phase_id": "R1:07", "output_type": None, "payload_json": None}}
    (project / "app.py").write_text("dirty\n")

    phases.commit(run, "msg")
    assert run.phases == []
    status = subprocess.run(
        ["git", "-C", str(project), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert "app.py" in status  # nothing staged or committed


def test_a_replayed_success_without_an_envelope_reruns_for_real(project):
    """Older trace dbs stored no envelope for verify — fall through to a real run."""
    run = make_run(project, {"test": "exit 0"})
    run.replay = {"verify_1": {"phase_id": "R1:05", "output_type": None, "payload_json": None}}
    envelope = phases.verify(run, 1)
    assert envelope.passed
    assert run.phases[0].params.name == "verify_1"


def test_a_replayed_scout_returns_the_recorded_envelope_without_running(project, monkeypatch):
    import json

    run = make_run(project)
    run.replay = {"scout": {"phase_id": "R1:03", "output_type": "ScoutOutput",
                           "payload_json": json.dumps({"status": "success", "summary": "scouted", "findings": []})}}

    def exploding_adapter(req, on_event=None, on_spawn=None, on_exit=None):
        raise AssertionError("a replayed phase must not spawn an agent")

    monkeypatch.setitem(agents.ADAPTERS, "pi", exploding_adapter)

    envelope = phases.scout(run, "x")
    assert envelope.summary == "scouted"
    assert run.phases == []


# --- review: no layout (ticket 192) ---


def test_review_sends_no_attachments_and_no_layout_section(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    phases.verify(run, 1)
    adapter = stub_adapter(
        {
            "status": "success", "summary": "reviewed", "approved": True, "findings": [],
            "blocking": [], "trust_violations": [],
            "artifacts": [], "notes_for_next_agent": "",
        }
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    phases.review(run, ReviewStep(prompt="do X", checklist="It works.", base="main"))
    p = adapter.calls[0].prompt
    # the rendered context carries no `## Layout` section (the reviewer's prose still
    # names `## Layout` until ticket 192's Step 3 drops it)
    assert "## Layout\n" not in p
    assert adapter.calls[0].attachments == []
    rows = run.tracer.con.execute(
        "SELECT gate FROM gate_results WHERE phase_id=?", (run.phases[0].phase_id,)
    ).fetchall()
    gate_names = [r["gate"] for r in rows]
    assert "layout_required" not in gate_names


# --- _last_review ---


def test_push_pr_body_ignores_a_pre_192_layout_verdict(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    run.tracer.record_envelope(
        run.run_id, "R1:99", agent="reviewer", output_type="ReviewOutput",
        payload_json=json.dumps({"approved": False, "layout": "fail", "summary": "overlap detected", "findings": []}),
        valid=True, attempt=1,
    )
    body = phases._last_review(run)
    assert "layout" not in body
    assert "not approved" in body
    assert "overlap detected" in body


def test_push_pr_body_shows_the_verdict_and_summary(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    run.tracer.record_envelope(
        run.run_id, "R1:99", agent="reviewer", output_type="ReviewOutput",
        payload_json=json.dumps({"approved": True, "summary": "all good", "findings": []}),
        valid=True, attempt=1,
    )
    body = phases._last_review(run)
    assert "layout:" not in body
    assert "approved" in body


# --- diff gate verification (ticket 122) ---


def _write_config_yaml(project: Path, raw: dict):
    """Write a temporary .lime/config.yaml and set it as the config to load."""
    (project / ".lime").mkdir(parents=True, exist_ok=True)
    import yaml
    (project / ".lime" / "config.yaml").write_text(yaml.safe_dump(raw))


def test_verify_logs_untracked_measurement_output_over_the_default_budget(project, monkeypatch):
    """Reproduce the 3800-line leak: no commit block -> defaults, untracked output is
    measured and logged against the default budget but never fails the run (ticket 193)."""
    run = make_run(project, {"test": "exit 0"})
    # Build config with no commit block (defaults apply)
    raw_cfg = {
        "defaults": {"runtime": "pi", "model": "openrouter/x/y", "thinking": "medium"},
        "agents": [{"name": "builder", "prompt_engineering": {}, "tools": ["read", "edit", "write"]}],
        "commands": {"test": "exit 0"},
    }
    run.cfg = raw_cfg
    # Create untracked measurement output file
    (project / "logs").mkdir(exist_ok=True)
    (project / "logs" / "measurement.json").write_text("".join(f'{{"line": {i}}}\n' for i in range(3800)))
    (project / "app.py").write_text("v2\n")
    # Stage the measurement output too (as if builder staged it)
    subprocess.run(["git", "-C", str(project), "add", "-A"], capture_output=True, check=True)

    envelope = phases.verify(run, 1, base="HEAD")
    assert envelope.passed
    assert envelope.failures == []
    assert envelope.notes_for_next_agent == ""
    payload = _verify_log_payload(run)
    assert payload["total_added"] == 3801
    assert payload["max_added_lines"] == 1500
    assert payload["top_contributors"][0] == {"path": "logs/measurement.json", "added_lines": 3800}
    # Verify persisted envelope: a pass, with no overflow field at all
    row = run.tracer.con.execute(
        "SELECT payload_json FROM envelopes WHERE run_id=? AND output_type='VerifyOutput'",
        (run.run_id,),
    ).fetchone()
    stored = json.loads(row["payload_json"])
    assert stored["passed"] is True
    assert "diff_cap_overflow" not in stored


@pytest.mark.parametrize("added,cap", [
    (1500, 1500), (1501, 1500), (5, 10), (11, 10), (0, 0), (1, 0), (2217, 2000), (2994, 2000),
])
def test_verify_logs_the_counted_size_and_never_fails_for_it(project, added, cap):
    """Size is measured and logged (`total_added` against `max_added_lines`) on both
    sides of the budget and never fails verify (ticket 193)."""
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg({"test": "exit 0"}, {"max_added_lines": cap, "uncounted": ["big-gen.txt"]})
    (project / "app.py").write_text("".join(f"line {i}\n" for i in range(added)))
    (project / "big-gen.txt").write_text("".join(f"g{i}\n" for i in range(5000)))
    subprocess.run(["git", "-C", str(project), "add", "app.py", "big-gen.txt"],
                   capture_output=True, check=True)

    envelope = phases.verify(run, 1, base="HEAD")
    assert envelope.passed, f"{added} <= {cap} must pass"
    assert envelope.failures == []
    payload = _verify_log_payload(run)
    assert payload["total_added"] == added
    assert payload["max_added_lines"] == cap
    assert payload["uncounted_files"] == [{"path": "big-gen.txt", "added_lines": 5000}]


def test_verify_logs_effective_accounting_against_a_named_base(project):
    """A non-default named base, committed branch work, dirty and untracked changes:
    the log carries the effective policy and contributor ranking; size never fails."""
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg(
        {"test": "exit 0"},
        {"max_added_lines": 50, "exclude": ["ignored.txt"], "uncounted": ["exempt/"]},
    )
    git(project, "branch", "release")
    (project / "committed.py").write_text("".join(f"c{i}\n" for i in range(30)))
    git(project, "add", "committed.py")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "branch work")
    (project / "app.py").write_text("".join(f"d{i}\n" for i in range(20)))
    (project / "new_untracked.py").write_text("".join(f"u{i}\n" for i in range(15)))
    (project / "exempt").mkdir()
    (project / "exempt" / "big.txt").write_text("".join(f"e{i}\n" for i in range(500)))
    (project / "ignored.txt").write_text("z\n" * 5)
    subprocess.run(
        ["git", "-C", str(project), "add", "app.py", "exempt/big.txt", "ignored.txt"],
        capture_output=True, check=True,
    )

    envelope = phases.verify(run, 1, base="release")
    assert envelope.passed
    assert envelope.head_sha == git_helper.rev("HEAD")
    payload = _verify_log_payload(run)
    assert payload["base"] == "release"
    assert payload["merge_base"] == git_helper.short_sha(git_helper.merge_base("release", "HEAD"))
    assert payload["total_added"] == 65  # committed.py(30) + app.py(20) + new_untracked.py(15)
    assert payload["max_added_lines"] == 50
    assert payload["uncounted_files"] == [{"path": "exempt/big.txt", "added_lines": 500}]
    assert payload["uncounted_patterns"] == ["exempt/"]
    top_paths = [c["path"] for c in payload["top_contributors"]]
    assert top_paths[0] == "committed.py"
    assert "exempt/big.txt" not in top_paths
    assert "ignored.txt" not in top_paths
    assert payload["omitted_contributors"] == 0


def test_verify_over_budget_fails_only_for_quality_and_binary(project):
    """Over the budget the only failures are quality and binary — never the size."""
    run = make_run(project, {"test": "exit 1"})
    run.cfg = _simple_cfg({"test": "exit 1"}, {"max_added_lines": 10})
    (project / "app.py").write_text("".join(f"line {i}\n" for i in range(50)))
    (project / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")
    subprocess.run(["git", "-C", str(project), "add", "-A"], capture_output=True, check=True)

    envelope = phases.verify(run, 1, base="HEAD")
    assert not envelope.passed
    failures_text = "\n".join(envelope.failures)
    assert "exit 1" in failures_text
    assert "image.png" in failures_text and "binary" in failures_text.lower()
    assert "exceeds" not in failures_text
    assert "needs_rescope" not in failures_text
    assert _verify_log_payload(run)["total_added"] == 50


def test_verify_binary_only_and_inspection_error_are_ordinary_failures(project, monkeypatch):
    """A failed accounting inspection must never manufacture a size failure from
    initialized counters; a binary-only failure is an ordinary failure too."""
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg({"test": "exit 0"})
    (project / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")
    subprocess.run(["git", "-C", str(project), "add", "image.png"], capture_output=True, check=True)
    envelope = phases.verify(run, 1, base="HEAD")
    assert not envelope.passed

    run2 = make_run(project, {"test": "exit 0"})
    run2.cfg = _simple_cfg({"test": "exit 0"})

    def boom(ref, other="HEAD"):
        raise RuntimeError("boom")

    monkeypatch.setattr(git_helper, "merge_base", boom)
    envelope2 = phases.verify(run2, 1, base="HEAD")
    assert not envelope2.passed
    assert any("inspection error" in f for f in envelope2.failures)


def test_legacy_verify_envelope_with_overflow_and_warnings_still_loads():
    """A pre-193 recorded envelope still carrying `diff_cap_overflow`/`warnings`
    loads and replays unchanged: extra keys are ignored on load."""
    from lime.data_types import VerifyOutput

    payload = {
        "status": "fail",
        "passed": False,
        "failures": ["3000 > 1500"],
        "diff_cap_overflow": {
            "requested_base": "main",
            "head_sha": "a" * 40,
            "merge_base_sha": "b" * 40,
            "max_added_lines": 1500,
            "counted_total": 3000,
            "uncounted_total": 0,
            "eligible_total": 3000,
            "top_contributors": [{"path": "x.py", "added_lines": 3000}],
        },
        "warnings": ["Diff-cap advisory: 3000 of 1500 added lines"],
    }
    envelope = VerifyOutput.model_validate_json(json.dumps(payload))
    assert envelope.passed is False
    assert envelope.failures == ["3000 > 1500"]
    dumped = envelope.model_dump()
    assert "diff_cap_overflow" not in dumped
    assert "warnings" not in dumped


def test_verify_logs_top_added_files_in_order(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg({"test": "exit 0"}, {"max_added_lines": 5})
    for i in range(1, 15):
        (project / f"file_{i:02d}.txt").write_text("".join(f"row {j}\n" for j in range(i * 10)))
    subprocess.run(["git", "-C", str(project), "add", "-A"], capture_output=True, check=True)

    envelope = phases.verify(run, 1, base="HEAD")
    assert envelope.passed
    payload = _verify_log_payload(run)
    top = payload["top_contributors"]
    assert top[:2] == [{"path": "file_14.txt", "added_lines": 140},
                       {"path": "file_13.txt", "added_lines": 130}]
    assert len(top) == 10
    assert payload["omitted_contributors"] == 4


def test_verify_rejects_new_binary_even_below_line_budget(project, monkeypatch):
    for binary_type in ["untracked", "staged"]:
        run = make_run(project, {"test": "exit 0"})
        run.cfg = {
            "defaults": {"runtime": "pi", "model": "openrouter/x/y", "thinking": "medium"},
            "agents": [{"name": "builder", "prompt_engineering": {}, "tools": ["read", "edit", "write"]}],
            "commands": {"test": "exit 0"},
        }
        (project / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")
        if binary_type == "staged":
            subprocess.run(["git", "-C", str(project), "add", "image.png"], capture_output=True, check=True)
        else:
            # untracked - just leave it
            pass
        envelope = phases.verify(run, 1, base="HEAD")
        assert not envelope.passed, f"expected fail for {binary_type} binary"
        failures_text = "\n".join(envelope.failures)
        assert "image.png" in failures_text
        assert "binary" in failures_text.lower()
        # required binaries are escalated to the engineer; uncounted cannot waive the rule
        assert any("commit.uncounted" in f and "engineer" in f for f in envelope.failures), binary_type
        assert not any("delete generated artefacts" in f.lower() for f in envelope.failures), binary_type


def _simple_cfg(commands, commit_block=None):
    base = {
        "defaults": {"runtime": "pi", "model": "openrouter/x/y", "thinking": "medium"},
        "agents": [{"name": "builder", "prompt_engineering": {}, "tools": ["read", "edit", "write"]}],
        "commands": commands,
    }
    if commit_block is not None:
        base["commit"] = commit_block
    return base


def _verify_log_payload(run, name="verify_1"):
    """The structured verify `log` payload (the one carrying total_added)."""
    rows = run.tracer.con.execute(
        "SELECT payload_json FROM events WHERE run_id=? AND type='log' AND name=? ORDER BY event_id",
        (run.run_id, name),
    ).fetchall()
    found = None
    for row in rows:
        payload = json.loads(row["payload_json"])
        if "total_added" in payload:
            found = payload
    if found is None:
        raise AssertionError(f"no structured verify log payload for {name}")
    return found


def _head_tree(project):
    return subprocess.run(
        ["git", "-C", str(project), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, text=True,
    ).stdout.splitlines()


def test_verify_ignores_excluded_and_gitignored_artefacts(project, monkeypatch):
    (project / ".gitignore").write_text("runs/\n*.log\n")
    subprocess.run(["git", "-C", str(project), "add", "-A"], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(project), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "add gitignore"],
        capture_output=True, check=True,
    )

    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg({"test": "exit 0"})  # no commit block -> default exclusions
    # Root handoff file (should be excluded)
    (project / "context_handoff").mkdir(exist_ok=True)
    (project / "context_handoff" / "data.md").write_text("y\n" * 100)
    # Nested handoff (should NOT be excluded)
    (project / "src" / "context_handoff").mkdir(parents=True, exist_ok=True)
    (project / "src" / "context_handoff" / "notes.md").write_text("z\n" * 50)
    # Gitignored file
    (project / "debug.log").write_text("trace\n" * 100)
    # Genuine change
    (project / "app.py").write_text("v2\n")
    subprocess.run(["git", "-C", str(project), "add", "app.py"], capture_output=True, check=True)

    envelope = phases.verify(run, 1, base="HEAD")
    assert envelope.passed, "excluded/gitignored files should not count"
    # Verify with custom exclusions
    run2 = make_run(project, {"test": "exit 0"})
    run2.cfg = _simple_cfg({"test": "exit 0"}, {"exclude": ["context_handoff/", "src/context_handoff/"]})
    (project / "code.py").write_text("print(42)\n")
    subprocess.run(["git", "-C", str(project), "add", "code.py"], capture_output=True, check=True)
    envelope2 = phases.verify(run2, 1, base="HEAD")
    assert envelope2.passed, "custom exclusions should work"


def test_verify_keeps_quality_failures_and_checks_outputs_created_by_commands(project, monkeypatch):
    """Passing command creates oversized output; failing test plus binary produces both failures."""
    run = make_run(project, {"test": "echo bug > output.txt && exit 1"})
    run.cfg = _simple_cfg({"test": "echo bug > output.txt && exit 1"})
    # Create a binary
    (project / "gen.bin").write_bytes(b"\x00\x01\x02")
    subprocess.run(["git", "-C", str(project), "add", "gen.bin"], capture_output=True, check=True)

    envelope = phases.verify(run, 1, base="HEAD")
    assert not envelope.passed
    failures_text = "\n".join(envelope.failures)
    assert "exit 1" in failures_text or "output.txt" in failures_text
    # Both quality and binary failures present
    assert any("binary" in f.lower() for f in envelope.failures), "binary failure should be present"
    # Check that output.txt created by the command is included in diff stats
    if (project / "output.txt").exists():
        assert any("output.txt" in f for f in envelope.failures), "command output in gate check"


def test_verify_uses_requested_merge_base_and_clean_base_has_zero_diff(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg({"test": "exit 0"})
    # Clean tree against HEAD should have zero diff
    envelope = phases.verify(run, 1, base="HEAD")
    assert envelope.passed


def test_verify_counts_committed_branch_additions_against_the_requested_base(project, monkeypatch):
    """verify resolves via merge_base(base, HEAD): committed branch changes between the
    requested base and HEAD are counted, not just the uncommitted working tree — and
    the 2000-line total is logged, never a failure (ticket 193)."""
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg({"test": "exit 0"})

    # A base ref (a branch) that HEAD has already advanced past
    git(project, "branch", "base-branch")
    # Commit branch additions on top of base-branch
    (project / "feature.py").write_text("".join(f"line {i}\n" for i in range(2000)))
    git(project, "add", "feature.py")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "branch work")

    # Verify against base-branch: committed 2000 added lines over the default budget
    envelope = phases.verify(run, 1, base="base-branch")
    assert envelope.passed
    payload = _verify_log_payload(run)
    assert payload["total_added"] == 2000
    assert payload["top_contributors"][0] == {"path": "feature.py", "added_lines": 2000}
    # A clean tree against base-branch still sees the same committed additions
    envelope2 = phases.verify(run, 1, base="base-branch")
    assert envelope2.passed
    assert _verify_log_payload(run)["total_added"] == 2000
    # And verify against HEAD (post-branch-work) is clean
    envelope3 = phases.verify(run, 1, base="HEAD")
    assert envelope3.passed
    assert _verify_log_payload(run)["total_added"] == 0


# --- uncounted accounting exemptions (ticket 126) ---


def test_verify_uncounted_2000_line_lockfile_passes_and_is_committed(project):
    """A required 2000-line lockfile is exempt from the 1500 cap and still ships."""
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg({"test": "exit 0"}, {"max_added_lines": 1500, "uncounted": ["pnpm-lock.yaml"]})
    (project / "pnpm-lock.yaml").write_text("".join(f"pkg-{i}\n" for i in range(2000)))
    subprocess.run(["git", "-C", str(project), "add", "pnpm-lock.yaml"], capture_output=True, check=True)

    envelope = phases.verify(run, 1, base="HEAD")
    assert envelope.passed
    payload = _verify_log_payload(run)
    assert payload["total_added"] == 0
    assert payload["uncounted_files"] == [{"path": "pnpm-lock.yaml", "added_lines": 2000}]

    sha = phases.commit(run, "ship the lockfile")
    assert sha
    head_lines = subprocess.run(
        ["git", "-C", str(project), "show", "HEAD:pnpm-lock.yaml"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    assert len(head_lines) == 2000


def test_verify_zero_cap_all_uncounted_passes(project):
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg({"test": "exit 0"}, {"max_added_lines": 0, "uncounted": ["vendor/"]})
    (project / "vendor").mkdir()
    (project / "vendor" / "lib.txt").write_text("".join(f"v{i}\n" for i in range(1000)))
    subprocess.run(["git", "-C", str(project), "add", "vendor/lib.txt"], capture_output=True, check=True)

    envelope = phases.verify(run, 1, base="HEAD")
    assert envelope.passed
    payload = _verify_log_payload(run)
    assert payload["total_added"] == 0
    assert payload["max_added_lines"] == 0


def test_verify_ranks_only_counted_files_and_logs_exemptions(project, monkeypatch):
    """Exempt paths appear once in uncounted_files and never in the ranking; the
    ranking/omitted count cover counted entries only. Checked under and over the budget."""
    # passing gate: tiny counted change, large exempt file
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg({"test": "exit 0"}, {"uncounted": ["exempt/"]})
    (project / ".gitignore").write_text("runs/\n*.log\n")
    subprocess.run(["git", "-C", str(project), "add", ".gitignore"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "gitignore"], capture_output=True, check=True)
    (project / "exempt").mkdir()
    (project / "exempt" / "big.txt").write_text("".join(f"e{i}\n" for i in range(400)))
    (project / "exempt" / "empty.txt").write_text("")
    (project / "exempt" / "unchanged.txt").write_text("tracked\n")
    subprocess.run(["git", "-C", str(project), "add", "exempt/unchanged.txt"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "tracked exempt"], capture_output=True, check=True)
    (project / "context_handoff").mkdir()
    (project / "context_handoff" / "plan.md").write_text("plan\n" * 50)
    (project / "debug.log").write_text("ignored\n" * 50)
    (project / "app.py").write_text("v2\n")
    subprocess.run(["git", "-C", str(project), "add", "app.py"], capture_output=True, check=True)

    notes = []
    monkeypatch.setattr(run.console, "note", notes.append)
    envelope = phases.verify(run, 1, base="HEAD")
    assert envelope.passed
    payload = _verify_log_payload(run)
    assert payload["uncounted_files"] == [
        {"path": "exempt/big.txt", "added_lines": 400},
        {"path": "exempt/empty.txt", "added_lines": 0},
    ]
    # excluded, ignored-untracked, and unchanged matches are absent
    paths = [entry["path"] for entry in payload["uncounted_files"]]
    assert "context_handoff/plan.md" not in paths
    assert "debug.log" not in paths
    assert "exempt/unchanged.txt" not in paths
    assert paths.count("exempt/big.txt") == 1
    # console evidence carries the exemption list
    assert any("uncounted_files" in note and "exempt/big.txt" in note for note in notes)

    # over-budget gate: >10 counted offenders (with ties) plus a larger exempt file
    run2 = make_run(project, {"test": "exit 0"})
    run2.cfg = _simple_cfg({"test": "exit 0"}, {"max_added_lines": 5, "uncounted": ["exempt/"]})
    lines = [40, 30, 30, 20, 20, 20, 10, 10, 10, 10, 5, 5, 5]
    for i, n in enumerate(lines, start=1):
        (project / f"c{i:02d}.txt").write_text("".join(f"row {j}\n" for j in range(n)))
    (project / "exempt" / "big.txt").write_text("".join(f"e{i}\n" for i in range(900)))
    subprocess.run(["git", "-C", str(project), "add", "-A"], capture_output=True, check=True)

    envelope2 = phases.verify(run2, 1, base="HEAD")
    assert envelope2.passed
    payload2 = _verify_log_payload(run2)
    ranking = payload2["top_contributors"]
    # exempt file never ranked, even though it is the largest by added lines
    assert "exempt/big.txt" not in [c["path"] for c in ranking]
    assert ranking[:3] == [
        {"path": "c01.txt", "added_lines": 40},
        {"path": "c02.txt", "added_lines": 30},
        {"path": "c03.txt", "added_lines": 30},
    ]
    assert {"path": "c07.txt", "added_lines": 10} in ranking
    assert payload2["omitted_contributors"] == 4
    assert payload2["total_added"] == sum(lines) + 1  # 13 counted files + the earlier app.py change
    assert {"path": "exempt/big.txt", "added_lines": 900} in payload2["uncounted_files"]


@pytest.mark.parametrize("kind", ["untracked_new", "staged_new", "modified_tracked", "deleted"])
def test_verify_uncounted_binaries_still_obey_binary_gate(project, kind):
    """An uncounted binary is not an allowed binary; a deletion still passes."""
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg({"test": "exit 0"}, {"uncounted": ["bin.dat"]})
    if kind in ("modified_tracked", "deleted"):
        (project / "bin.dat").write_bytes(b"\x00\x01")
        subprocess.run(["git", "-C", str(project), "add", "bin.dat"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(project), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "add bin"], capture_output=True, check=True)
        if kind == "modified_tracked":
            (project / "bin.dat").write_bytes(b"\x00\x02")
            subprocess.run(["git", "-C", str(project), "add", "bin.dat"], capture_output=True, check=True)
        else:
            subprocess.run(["git", "-C", str(project), "rm", "bin.dat"], capture_output=True, check=True)
    else:
        (project / "bin.dat").write_bytes(b"\x00\x01\x02")
        if kind == "staged_new":
            subprocess.run(["git", "-C", str(project), "add", "bin.dat"], capture_output=True, check=True)

    envelope = phases.verify(run, 1, base="HEAD")
    payload = _verify_log_payload(run)
    # exemption evidence records the eligible binary with its original zero additions
    assert payload["uncounted_files"] == [{"path": "bin.dat", "added_lines": 0}]
    if kind == "deleted":
        assert envelope.passed
    else:
        assert not envelope.passed
        assert any("binary" in f.lower() and "bin.dat" in f for f in envelope.failures)
        # required binaries are escalated; uncounted cannot waive the binary rule
        assert any("commit.uncounted" in f and "engineer" in f for f in envelope.failures)


def test_verify_uncounted_match_failure_fails_closed(project, monkeypatch):
    """A matcher failure flows into the inspection-error path — never fail open."""
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg({"test": "exit 0"}, {"uncounted": ["exempt/"]})
    (project / "exempt.txt").write_text("x\n")

    def boom(base, patterns):
        raise RuntimeError("matcher down")

    monkeypatch.setattr(git_helper, "uncounted_paths", boom)
    envelope = phases.verify(run, 1, base="HEAD")
    assert not envelope.passed
    assert envelope.status == "fail"
    assert any("inspection error" in f and "matcher down" in f for f in envelope.failures)
    payload = _verify_log_payload(run)
    assert payload["gate_passed"] is False
    assert payload["uncounted_files"] == []


def test_verify_uncounted_preserves_quality_checks(project):
    """A quality command's exempt output is classified afterward; a failing quality
    command still fails overall even when the line gate passes."""
    run = make_run(project, {"test": "printf 'gen\\n' > gen-out.txt"})
    run.cfg = _simple_cfg({"test": "printf 'gen\\n' > gen-out.txt"}, {"uncounted": ["gen-out.txt"]})
    envelope = phases.verify(run, 1, base="HEAD")
    assert envelope.passed
    payload = _verify_log_payload(run)
    assert payload["uncounted_files"] == [{"path": "gen-out.txt", "added_lines": 1}]

    run2 = make_run(project, {"test": "exit 1"})
    run2.cfg = _simple_cfg({"test": "exit 1"}, {"uncounted": ["gen-out.txt"]})
    envelope2 = phases.verify(run2, 1, base="HEAD")
    assert not envelope2.passed
    assert any("exit 1" in f or "test" in f for f in envelope2.failures)
    assert _verify_log_payload(run2)["quality_passed"] is False


def test_repeated_verify_with_uncounted_is_read_only(project, monkeypatch):
    """Distinct attempts both execute; neither call touches Git state, and each
    collects diff_stats exactly once with only exclude patterns."""
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg({"test": "exit 0"}, {"uncounted": ["exempt/"]})
    (project / "exempt").mkdir()
    (project / "exempt" / "big.txt").write_text("".join(f"e{i}\n" for i in range(100)))
    (project / "app.py").write_text("v2\n")
    subprocess.run(["git", "-C", str(project), "add", "app.py"], capture_output=True, check=True)

    real_stats = git_helper.diff_stats
    calls = []

    def spy(base, exclude_patterns=None):
        calls.append((base, list(exclude_patterns or [])))
        return real_stats(base, exclude_patterns)

    monkeypatch.setattr(git_helper, "diff_stats", spy)

    def snapshot():
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

        def out(*args):
            return subprocess.run(["git", "-C", str(project), *args],
                                  capture_output=True, text=True, env=env).stdout

        return {
            "head": out("rev-parse", "HEAD"),
            "index": (project / ".git" / "index").read_bytes(),
            "status": out("status", "--porcelain"),
            "files": {str(p.relative_to(project)): p.read_bytes()
                      for p in sorted(project.rglob("*"))
                      if p.is_file() and ".git" not in p.parts and "runs" not in p.parts},
        }

    before = snapshot()
    e1 = phases.verify(run, 1, attempt=1, base="HEAD")
    e2 = phases.verify(run, 1, attempt=2, base="HEAD")
    assert e1.passed and e2.passed
    assert snapshot() == before
    assert len(calls) == 2
    for _, exclude in calls:
        assert exclude == ["context_handoff/"]


def test_commit_logs_each_excluded_path_and_docs_does_not_readd_them(project):
    """Distinct overlapping exclude/uncounted patterns: both HEAD trees ship the
    eligible uncounted files, exclude unwanted additions, preserve working files, and
    retain ticket deletion/exclusion logging behavior."""
    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg(
        {"test": "exit 0"}, {"exclude": ["gen/private/"], "uncounted": ["gen/"]}
    )

    (project / ".gitignore").write_text("runs/\n*.log\n")
    subprocess.run(["git", "-C", str(project), "add", ".gitignore"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "gitignore"], capture_output=True, check=True)

    # pre-staged excluded file, eligible uncounted content, an ignored untracked match
    (project / "gen" / "private").mkdir(parents=True)
    (project / "gen" / "private" / "secret.txt").write_text("secret\n")
    (project / "gen" / "public.txt").write_text("public\n")
    (project / "debug.log").write_text("ignored\n")
    # committed content
    (project / "src").mkdir(exist_ok=True)
    (project / "src" / "main.py").write_text("print('hello')\n")
    ticket_path = project / "ticket.md"
    ticket_path.write_text("close me\n")
    subprocess.run(["git", "-C", str(project), "add", "gen/private/secret.txt"], capture_output=True, check=True)

    sha = phases.commit(run, "Code commit")
    assert sha
    tree = _head_tree(project)
    assert "src/main.py" in tree
    assert "ticket.md" in tree
    assert "gen/public.txt" in tree  # eligible uncounted content ships
    assert "gen/private/secret.txt" not in tree  # exclude keeps it out of staging
    assert "debug.log" not in tree  # ignored, never force-added
    # working files preserved
    assert (project / "gen" / "private" / "secret.txt").read_text() == "secret\n"
    assert (project / "debug.log").read_text() == "ignored\n"

    # eligible uncounted content created separately before the docs commit too
    (project / "gen" / "more.txt").write_text("more\n")
    (project / "gen" / "private" / "docs.txt").write_text("docs secret\n")
    subprocess.run(["git", "-C", str(project), "add", "gen/private/docs.txt"], capture_output=True, check=True)

    sha2 = phases.commit_docs(run, "Close ticket", ticket_path)
    assert sha2
    tree2 = _head_tree(project)
    assert "gen/public.txt" in tree2
    assert "gen/more.txt" in tree2  # ships in the docs commit
    assert "gen/private/docs.txt" not in tree2  # exclude wins on overlap
    assert "ticket.md" not in tree2  # ticket deletion retained
    assert (project / "gen" / "private" / "docs.txt").read_text() == "docs secret\n"

    # Check that excluded paths were logged
    log_events = run.tracer.con.execute(
        "SELECT payload_json FROM events WHERE run_id=? AND type='log' AND name='commit_code'",
        (run.run_id,),
    ).fetchall()
    found_excluded_log = False
    for row in log_events:
        payload = json.loads(row["payload_json"])
        if "excluded" in payload:
            assert "gen/private/secret.txt" in payload["excluded"]
            found_excluded_log = True
            break
    assert found_excluded_log, "commit_code should log excluded paths"


# --- shared accounting, advisory and verification provenance (ticket 132) ---


def _spy_quality(monkeypatch):
    from lime import quality as qmod

    calls = []
    real = qmod.run_quality

    def spy(run_):
        calls.append(1)
        return real(run_)

    monkeypatch.setattr(qmod, "run_quality", spy)
    return calls


def test_verify_logs_the_shared_accounting_measurement(project):
    from lime import diff_accounting

    run = make_run(project, {"test": "exit 0"})
    run.cfg = _simple_cfg(
        {"test": "exit 0"},
        {"max_added_lines": 100000, "exclude": ["ignored/"], "uncounted": ["gen/"]},
    )
    # committed branch work
    (project / "committed.py").write_text("".join(f"c{i}\n" for i in range(30)))
    subprocess.run(["git", "-C", str(project), "add", "committed.py"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "committed"], capture_output=True, check=True)
    # staged, unstaged, untracked
    (project / "staged.py").write_text("".join(f"s{i}\n" for i in range(20)))
    subprocess.run(["git", "-C", str(project), "add", "staged.py"], capture_output=True, check=True)
    (project / "app.py").write_text("".join(f"u{i}\n" for i in range(10)))
    (project / "new.py").write_text("".join(f"n{i}\n" for i in range(5)))
    # tests/migrations count normally
    (project / "migrations").mkdir()
    (project / "migrations" / "m.py").write_text("".join("m\n" for _ in range(7)))
    # approved uncounted output
    (project / "gen").mkdir()
    (project / "gen" / "out.txt").write_text("".join("g\n" for _ in range(500)))
    subprocess.run(["git", "-C", str(project), "add", "gen/out.txt"], capture_output=True, check=True)
    # excluded output
    (project / "ignored").mkdir()
    (project / "ignored" / "x.txt").write_text("".join("i\n" for _ in range(900)))
    subprocess.run(["git", "-C", str(project), "add", "ignored/x.txt"], capture_output=True, check=True)
    # a binary is an ordinary failure verify surfaces
    (project / "blob.bin").write_bytes(b"\x00\x01\x02")

    envelope = phases.verify(run, 1, base="HEAD")
    measurement = diff_accounting.evaluate(run, "HEAD")
    payload = _verify_log_payload(run)

    assert payload["total_added"] == measurement.counted_total
    assert payload["max_added_lines"] == measurement.max_added_lines
    assert payload["uncounted_files"] == [c.model_dump() for c in measurement.uncounted_files]
    assert sorted(payload["binary_paths"]) == sorted(measurement.binaries)
    assert "blob.bin" in measurement.binaries
    assert not envelope.passed
    assert "exceeds" not in "\n".join(envelope.failures)


def test_final_verify_counts_quality_generated_growth(project):
    # Passing command writes an oversized generated file -> measured and logged, still a pass
    run = make_run(project, {"test": "for i in $(seq 1 50); do echo line$i; done > big.txt"})
    run.cfg = _simple_cfg(
        {"test": "for i in $(seq 1 50); do echo line$i; done > big.txt"},
        {"max_added_lines": 10},
    )
    envelope = phases.verify(run, 1, base="HEAD")
    assert envelope.passed
    assert envelope.failures == []
    assert _verify_log_payload(run)["total_added"] >= 50

    # Failing command writing generated files: a quality failure, size still just logged
    cmd = "for i in $(seq 1 50); do echo line$i; done > big2.txt; exit 1"
    run2 = make_run(project, {"test": cmd})
    run2.cfg = _simple_cfg({"test": cmd}, {"max_added_lines": 10})
    envelope2 = phases.verify(run2, 1, base="HEAD")
    assert not envelope2.passed
    assert any("exit 1" in f or "test" in f for f in envelope2.failures)
    assert "exceeds" not in "\n".join(envelope2.failures)
    assert _verify_log_payload(run2)["total_added"] >= 50


# --- pinned birth identity at every diff consumer (ticket 144) ---


def _commit(project, message):
    git(project, "add", "-A")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message)


def test_pinned_verify_accounting_uses_the_pin(project):
    run = make_run(project, {"test": "exit 0"})
    (project / "inherited.py").write_text("".join(f"i{i}\n" for i in range(500)))
    _commit(project, "inherited")
    pin = git_helper.rev("HEAD")
    run.base_sha = pin
    (project / "own.py").write_text("".join(f"o{i}\n" for i in range(30)))
    _commit(project, "own")

    envelope = phases.verify(run, 1, base="main")

    assert envelope.passed
    assert envelope.head_sha == git_helper.rev("HEAD")
    assert any(
        p.get("merge_base") == git_helper.short_sha(pin)
        for p in _log_payloads(run, "verify_1")
    )
    assert _verify_log_payload(run)["total_added"] == 30  # inherited lines are never charged


def test_pinned_review_context_and_changes_use_the_pin(project):
    run = make_run(project)
    (project / "inherited.py").write_text("".join(f"i{i}\n" for i in range(500)))
    _commit(project, "inherited")
    pin = git_helper.rev("HEAD")
    run.base_sha = pin
    (project / "own.py").write_text("o\n")
    _commit(project, "own")

    step = ReviewStep(prompt="p", checklist="c", base="main")
    context = phases._review_context(run, step)
    assert "own.py" in context
    assert "inherited.py" not in context

    envelope = phases.changes(run, "main")
    assert pin[:7] in envelope.base
    assert "own.py" in envelope.changed_files
    assert "inherited.py" not in envelope.changed_files


def _outside_where(context: str) -> str:
    return context.split("## Outside Where\n", 1)[1].split("\n\n", 1)[0]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_review_context_lists_only_changes_outside_where(project):
    run = make_run(project)
    run.base_sha = git_helper.rev("HEAD")
    ticket = Ticket(
        path=".lime/tickets/41-toy.md", number=41, slug="41-toy", title="Toy Ticket",
        workflow="feature", ask="a",
        where="- `app.py`\n- `pkg/`\n- `docs/guide.md` :12-20 setup notes, see `README.md`",
        done_means="It works.",
    )
    _write(project / "app.py", "v2\n")
    _write(project / "pkg" / "sub" / "mod.py", "m\n")
    _write(project / "docs" / "guide.md", "g\n")
    _write(project / "pkgextra" / "x.py", "x\n")
    _write(project / "docs" / "cookbook.md", "c\n")
    _write(project / "README.md", "r\n")
    _write(project / ".lime" / "tickets" / "41-toy.md", "# 41 — Toy Ticket\n")
    _write(project / ".lime" / "tickets" / "40-other.md", "# 40 — Other\n")
    _commit(project, "changes")
    _write(project / "notes.md", "n\n")

    context = phases._review_context(
        run, ReviewStep(prompt=tracker.prompt(ticket), checklist="c", base="main")
    )

    assert set(_outside_where(context).splitlines()) == {
        "- pkgextra/x.py",
        "- docs/cookbook.md",
        "- README.md",
        "- .lime/tickets/40-other.md",
        "- notes.md",
    }
    assert ".lime/tickets/41-toy.md" not in _outside_where(context)
    assert "- .lime/tickets/41-toy.md" in context
    assert (
        context.index("## Changed files")
        < context.index("## Outside Where")
        < context.index("## Tests")
    )


def test_review_context_outside_where_is_none_when_every_change_sits_inside(project):
    run = make_run(project)
    run.base_sha = git_helper.rev("HEAD")
    ticket = Ticket(
        path=".lime/tickets/41-toy.md", number=41, slug="41-toy", title="Toy Ticket",
        workflow="feature", ask="a", where="- `app.py`", done_means="It works.",
    )
    _write(project / "app.py", "v2\n")
    _write(project / ".lime" / "tickets" / "41-toy.md", "# 41 — Toy Ticket\n")
    _commit(project, "changes")

    context = phases._review_context(
        run, ReviewStep(prompt=tracker.prompt(ticket), checklist="c", base="main")
    )

    assert _outside_where(context) == "none"


@pytest.mark.parametrize("where", ["", "- the parser module, no path here"])
def test_review_with_no_parsed_where_shows_none_parsed_and_still_runs(project, monkeypatch, where):
    run = make_run(project)
    ticket = Ticket(
        path=".lime/tickets/41-toy.md", number=41, slug="41-toy", title="Toy Ticket",
        workflow="feature", ask="a", where=where, done_means="It works.",
    )
    (project / "app.py").write_text("v2\n")
    adapter = stub_adapter(
        {
            "status": "success", "summary": "reviewed", "approved": True, "findings": [],
            "blocking": [], "trust_violations": [], "artifacts": [], "notes_for_next_agent": "",
        }
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    envelope = phases.review(
        run, ReviewStep(prompt=tracker.prompt(ticket), checklist="It works.", base="main")
    )

    assert envelope.approved
    assert "## Outside Where\nWhere: none parsed\n\n" in adapter.calls[0].prompt


def test_pinned_pr_body_stat_uses_the_pin_and_open_pr_gets_the_branch_name(project, monkeypatch):
    run = make_run(project)
    (project / "inherited.py").write_text("".join(f"i{i}\n" for i in range(500)))
    _commit(project, "inherited")
    pin = git_helper.rev("HEAD")
    run.base_sha = pin
    (project / "own.py").write_text("".join(f"o{i}\n" for i in range(5)))
    _commit(project, "own")

    calls = {}
    monkeypatch.setattr(git_helper, "push", lambda branch: calls.setdefault("push", branch))

    def fake_open_pr(title, body, base):
        calls["pr"] = (body, base)
        return "https://github.com/x/y/pull/1"

    monkeypatch.setattr(git_helper, "open_pr", fake_open_pr)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")

    phases.push_pr(run, TICKET, "main")

    body, base = calls["pr"]
    assert base == "main"  # the default branch NAME, not the pin
    assert "own.py" in body
    assert "inherited.py" not in body
    assert calls["push"] == "lime/feature/41-toy"


def test_invalid_pin_cannot_replay_a_success_or_run_quality(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    pin = git_helper.rev("HEAD")
    run.base_sha = pin
    assert phases.verify(run, 1, base="main").passed

    calls = _spy_quality(monkeypatch)
    bad = make_run(project, {"test": "exit 0"})
    bad.base_sha = "0" * 40
    bad.replay = bad.tracer.replay_map("R1")
    assert "verify_1" in bad.replay  # a success is available to replay
    with pytest.raises(git_helper.InvalidRunBase, match="rebased/merged past its base"):
        phases.verify(bad, 1, base="main")
    assert calls == []
    assert bad.phases == []


def test_review_changes_and_push_guards_reject_an_invalid_pin(project, monkeypatch):
    run = make_run(project)
    run.base_sha = "0" * 40

    with pytest.raises(git_helper.InvalidRunBase):
        phases.changes(run, "main")
    with pytest.raises(git_helper.InvalidRunBase):
        phases.review(run, ReviewStep(prompt="p", checklist="c", base="main"))

    side_effects = []
    monkeypatch.setattr(git_helper, "push", lambda branch: side_effects.append(branch))
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: side_effects.append("pr"))
    monkeypatch.setattr(git_helper, "current_branch", lambda: "b")
    with pytest.raises(git_helper.InvalidRunBase):
        phases.push_pr(run, TICKET, "main")
    assert side_effects == []  # no network side effect before validation


def test_legacy_run_inspection_error_is_still_returned_not_raised(project, monkeypatch):
    from lime import diff_accounting

    run = make_run(project)
    assert run.base_sha is None

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(git_helper, "merge_base", boom)
    measurement = diff_accounting.evaluate(run, "main")
    assert measurement.inspection_error


# --- ticket 129: limits-aware planning contract, identity and invalidation ---


def _limits_cfg(*, builder_timeout=None, builder_budget=None, commit=None, budget=None):
    cfg_ = {
        "defaults": {"runtime": "pi", "model": "openrouter/x/y", "thinking": "medium"},
        "agents": [
            {"name": "scout", "prompt_engineering": {}, "tools": ["read", "write"], "writes": []},
            {"name": "planner", "prompt_engineering": {}, "tools": ["read", "write"], "writes": [], "timeout_s": 111},
            {"name": "builder", "prompt_engineering": {}, "tools": ["read", "edit", "write"],
             "writes": ["AGENTS.md"]},
            {"name": "reviewer", "prompt_engineering": {}, "tools": ["read", "write"], "writes": []},
            {"name": "documenter", "prompt_engineering": {}, "tools": ["read", "edit", "write"],
             "writes": ["AGENTS.md"]},
        ],
        "commands": {},
    }
    if builder_timeout is not None:
        cfg_["agents"][2]["timeout_s"] = builder_timeout
    if builder_budget is not None:
        cfg_["agents"][2]["budget"] = builder_budget
    if commit is not None:
        cfg_["commit"] = commit
    if budget is not None:
        cfg_["budget"] = budget
    return cfg_


def test_plan_prompt_contains_resolved_builder_limits_once(project, monkeypatch):
    from lime import limits

    raw = _limits_cfg(builder_timeout=900, builder_budget={"warn": 120000}, budget={"stop": 400000})
    run = session.ensure(raw, "R1", runs_dir=project / "runs")
    # window 300k: stop = min(400000, 300000 - 2*20000) = 260000
    monkeypatch.setattr(agents, "_context_window", lambda a: 300_000)
    plan_path = run.context_handoff_dir / "plan.md"
    adapter = stub_adapter(
        {"status": "success", "summary": "planned", "artifacts": [str(plan_path)]},
        write={str(plan_path): VALID_PLAN},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    phases.plan(run, "do X")

    sent = adapter.calls[0].prompt
    expected = (
        "Builder limits: timeout_s=900; token_warn=120000; token_stop=260000;"
        " max_added_lines=1500; uncounted=[]"
    )
    assert sent.count("Builder limits:") == 1
    assert expected in sent
    assert "timeout_s=111" not in sent  # the planner's own timeout never leaks in
    # provenance stores the exact augmented prompt
    prov_row = run.tracer.con.execute(
        "SELECT payload_json FROM events WHERE run_id='R1' AND name='plan_provenance'"
        " ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    assert expected in prov_row["payload_json"]
    # no dynamic fields belong in the static line
    line = limits.planner_line(run.agent("builder"), raw)
    for absent in ("elapsed", "remaining", "context_tokens", "git ", "usage", "cache"):
        assert absent not in line


def test_limits_line_defaults_zero_cap_and_stable_uncounted(monkeypatch):
    from lime import limits
    from lime.data_types import AgentConfig

    monkeypatch.setattr(agents, "_context_window", lambda a: 1_000_000)
    default = AgentConfig(name="builder", model="openrouter/x/y")
    assert limits.planner_line(default, {}) == (
        "Builder limits: timeout_s=1800; token_warn=150000; token_stop=250000;"
        " max_added_lines=1500; uncounted=[]"
    )
    assert limits.planner_line(default, {"commit": {"max_added_lines": 0}}) == (
        "Builder limits: timeout_s=1800; token_warn=150000; token_stop=250000;"
        " max_added_lines=0; uncounted=[]"
    )
    a = limits.planner_line(default, {"commit": {"uncounted": ["b", "a", "a"]}})
    b = limits.planner_line(default, {"commit": {"uncounted": ["a", "b"]}})
    assert a == b
    assert 'uncounted=["a","b"]' in a
    # the builder's own line never carries the sizing budget
    assert limits.builder_line(default, {"commit": {"max_added_lines": 0, "uncounted": ["a"]}}) == (
        "Builder limits: timeout_s=1800; token_warn=150000; token_stop=250000"
    )


def test_limits_line_clamps_stop_to_model_window(monkeypatch):
    from lime import limits
    from lime.data_types import AgentConfig

    monkeypatch.setattr(agents, "_context_window", lambda a: 200_000)
    agent = AgentConfig(name="builder", model="openrouter/x/y")
    assert "token_stop=160000" in limits.planner_line(agent, {})
    assert "token_stop=160000" in limits.builder_line(agent, {})


def _stub_plan_writer(run, plan_md=VALID_PLAN):
    plan_path = run.context_handoff_dir / "plan.md"
    return stub_adapter(
        {"status": "success", "summary": "planned", "artifacts": [str(plan_path)]},
        write={str(plan_path): plan_md},
    )


def test_plan_unchanged_effective_limits_replays(project, monkeypatch):
    run = make_run(project)
    monkeypatch.setattr(agents, "_context_window", lambda a: 200_000)
    monkeypatch.setitem(agents.ADAPTERS, "pi", _stub_plan_writer(run))
    phases.plan(run, "do X", ticket_sha256="h")

    resumed = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    resumed.replay = resumed.tracer.replay_map("R1")

    def exploding(req, on_event=None, on_spawn=None, on_exit=None):
        raise AssertionError("unchanged limits must replay")

    monkeypatch.setitem(agents.ADAPTERS, "pi", exploding)
    phases.plan(resumed, "do X", ticket_sha256="h")
    assert resumed.phases == []  # replayed, no new phase


@pytest.mark.parametrize("mutate", ["timeout", "warn", "stop", "cap", "uncounted", "prompt"])
def test_plan_limit_change_invalidates_dependent_builds(project, monkeypatch, mutate):
    run = make_run(project)
    monkeypatch.setattr(agents, "_context_window", lambda a: 200_000)
    monkeypatch.setitem(agents.ADAPTERS, "pi", _stub_plan_writer(run))
    phases.plan(run, "do X", ticket_sha256="h")

    build_adapter = stub_adapter(
        {"status": "success", "summary": "built", "changed_files": [], "artifacts": [],
         "commit_message": "", "notes_for_next_agent": ""},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", build_adapter)
    phases.build(run, BuildStep(step_no=1, prompt="p"))

    if mutate == "prompt":
        new_cfg, new_prompt = cfg(), "do Y"
    else:
        extra = {"timeout": {"builder_timeout": 1200},
                 "warn": {"builder_budget": {"warn": 7}},
                 "stop": {"budget": {"stop": 9}},
                 "cap": {"commit": {"max_added_lines": 12}},
                 "uncounted": {"commit": {"uncounted": ["gen/"]}}}[mutate]
        new_cfg, new_prompt = _limits_cfg(**extra), "do X"

    resumed = session.ensure(new_cfg, "R1", runs_dir=project / "runs")
    resumed.replay = resumed.tracer.replay_map("R1")
    assert "plan" in resumed.replay and "build_1" in resumed.replay

    adapter = _stub_plan_writer(resumed)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    phases.plan(resumed, new_prompt, ticket_sha256="h")

    assert adapter.calls, "changed limits must re-plan"
    assert "build_1" not in resumed.replay, "stale dependent build must be invalidated"


def test_plan_old_provenance_is_not_replayable(project, monkeypatch):
    run = make_run(project)
    monkeypatch.setattr(agents, "_context_window", lambda a: 200_000)
    monkeypatch.setitem(agents.ADAPTERS, "pi", _stub_plan_writer(run))
    phases.plan(run, "do X", ticket_sha256="h")

    resumed = session.ensure(cfg(), "R1", runs_dir=project / "runs")
    resumed.replay = resumed.tracer.replay_map("R1")
    resumed.tracer.con.execute(
        "DELETE FROM events WHERE run_id='R1' AND name='plan_provenance'"
    )
    resumed.tracer.con.commit()

    adapter = _stub_plan_writer(resumed)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    phases.plan(resumed, "do X", ticket_sha256="h")
    assert adapter.calls  # missing provenance fails closed


@pytest.mark.parametrize(
    "case",
    [
        "unchanged",
        "no_provenance",
        "malformed",
        "version",
        "ticket",
        "prompt",
        "plan_missing",
        "plan_changed",
    ],
)
def test_stale_plan_reason_names_each_check(project, monkeypatch, case):
    """The single staleness decision fails closed and names each check with its exact text."""
    run = make_run(project)
    monkeypatch.setattr(agents, "_context_window", lambda a: 200_000)
    monkeypatch.setitem(agents.ADAPTERS, "pi", _stub_plan_writer(run))
    phases.plan(run, "do X", ticket_sha256="h")
    phase_id = run.tracer.replay_map("R1")["plan"]["phase_id"]
    prov = phases.load_plan_provenance(run.tracer, "R1", phase_id)
    plan_path = run.context_handoff_dir / "plan.md"

    ticket_sha256 = "h"
    prompt = prov.prompt
    if case == "no_provenance":
        run.tracer.con.execute(
            "DELETE FROM events WHERE run_id='R1' AND name='plan_provenance'"
        )
        run.tracer.con.commit()
    elif case == "malformed":
        run.tracer.con.execute(
            "UPDATE events SET payload_json='not json'"
            " WHERE run_id='R1' AND name='plan_provenance'"
        )
        run.tracer.con.commit()
    elif case == "version":
        run.tracer.event(
            "R1", "handoff", phase_id=phase_id, name="plan_provenance",
            payload={**prov.model_dump(), "version": 1},
        )
    elif case == "ticket":
        ticket_sha256 = "other"
    elif case == "prompt":
        prompt = prov.prompt + " changed limits"
    elif case == "plan_missing":
        plan_path.unlink()
    elif case == "plan_changed":
        plan_path.write_text(plan_path.read_text() + "\nchanged\n")

    reason = phases.stale_plan_reason(
        run.tracer, "R1", phase_id,
        ticket_sha256=ticket_sha256, prompt=prompt, plan_path=plan_path,
    )

    expected = {
        "unchanged": None,
        "no_provenance": (
            "the cached plan has no provenance (recorded before provenance existed)"
        ),
        "malformed": (
            "the cached plan has no provenance (recorded before provenance existed)"
        ),
        "version": "the cached plan's provenance version 1 is unsupported",
        "ticket": "the ticket changed since it was planned",
        "prompt": "the planning inputs or resolved limits changed since it was planned",
        "plan_missing": "the cached plan.md is missing",
        "plan_changed": "the cached plan.md changed since it was planned",
    }[case]
    assert reason == expected


def test_plan_replayable_is_defined_by_no_stale_reason(project, monkeypatch):
    """`_plan_replayable` compares nothing of its own: it is the `payload_json`
    precondition plus "no stale reason"."""
    run = make_run(project)
    monkeypatch.setattr(agents, "_context_window", lambda a: 200_000)
    monkeypatch.setitem(agents.ADAPTERS, "pi", _stub_plan_writer(run))
    phases.plan(run, "do X", ticket_sha256="h")
    entry = run.tracer.replay_map("R1")["plan"]

    monkeypatch.setattr(phases, "stale_plan_reason", lambda *a, **k: None)
    assert phases._plan_replayable(run, entry, "anything", "h") is True

    monkeypatch.setattr(phases, "stale_plan_reason", lambda *a, **k: "x")
    assert phases._plan_replayable(run, entry, "anything", "h") is False

    monkeypatch.setattr(phases, "stale_plan_reason", lambda *a, **k: None)
    assert phases._plan_replayable(run, {**entry, "payload_json": None}, "anything", "h") is False


def test_plan_provenance_has_one_loader_and_one_staleness_check(project, monkeypatch):
    """Plan `writers:`: `_record_plan_provenance` writes the `plan_provenance` events this
    step reads. The AST guard proves one loader (`load_plan_provenance`) and one staleness
    decision (`stale_plan_reason`) — no SQL, version read or reason text anywhere else —
    and the behavioural round trip drives the writer through the one loader."""
    import ast
    import hashlib

    from lime import limits
    from lime.data_types import PLAN_PROVENANCE_VERSION, PlanProvenance

    lime_root = Path(phases.__file__).resolve().parents[1]
    sources = [
        p for p in sorted(lime_root.rglob("*.py"))
        if "skill" not in p.relative_to(lime_root).parts
    ]
    assert sources  # the guard must actually scan the package

    def enclosing(tree):
        owners = {}

        def visit(node, func):
            for child in ast.iter_child_nodes(node):
                name = (
                    child.name
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else func
                )
                owners[id(child)] = name
                visit(child, name)

        visit(tree, None)
        return owners

    sql_offenders: list[str] = []
    version_offenders: list[str] = []
    text_offenders: list[str] = []
    for path in sources:
        rel = str(path.relative_to(lime_root))
        tree = ast.parse(path.read_text())
        owners = enclosing(tree)
        for node in ast.walk(tree):
            owner = owners.get(id(node))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "name='plan_provenance'" in node.value and not (
                    rel == "workflows/phases.py" and owner == "load_plan_provenance"
                ):
                    sql_offenders.append(f"{rel}:{node.lineno}:{owner}")
                if any(
                    text in node.value
                    for text in (
                        "since it was planned",
                        "has no provenance",
                        "provenance version",
                    )
                ) and owner != "stale_plan_reason":
                    text_offenders.append(f"{rel}:{node.lineno}:{owner}")
            elif (
                isinstance(node, ast.Name)
                and node.id == "PLAN_PROVENANCE_VERSION"
                and isinstance(node.ctx, ast.Load)
                and rel != "data_types.py"
                and owner != "stale_plan_reason"
            ):
                version_offenders.append(f"{rel}:{node.lineno}:{owner}")

    assert sql_offenders == [], (
        f"plan_provenance SQL outside load_plan_provenance: {sql_offenders}"
    )
    assert version_offenders == [], (
        f"PLAN_PROVENANCE_VERSION read outside stale_plan_reason: {version_offenders}"
    )
    assert text_offenders == [], (
        f"staleness reason text outside stale_plan_reason: {text_offenders}"
    )

    cli_tree = ast.parse((lime_root / "cli.py").read_text())
    cli_funcs = {
        n.name for n in ast.walk(cli_tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_latest_plan_provenance" not in cli_funcs
    assert "_stale_plan_reason" not in cli_funcs

    run = make_run(project)
    monkeypatch.setattr(agents, "_context_window", lambda a: 200_000)
    monkeypatch.setitem(agents.ADAPTERS, "pi", _stub_plan_writer(run))
    phases.plan(run, "do X", ticket_sha256="h")
    phase_id = run.tracer.replay_map("R1")["plan"]["phase_id"]
    plan_path = run.context_handoff_dir / "plan.md"
    prov = phases.load_plan_provenance(run.tracer, "R1", phase_id)
    assert prov == PlanProvenance(
        ticket_sha256="h",
        prompt=limits.append_planner("do X", run.agent("builder"), run.cfg),
        plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
    )
    assert prov.version == PLAN_PROVENANCE_VERSION


def test_revise_prompt_carries_the_limits_line_once(project, monkeypatch):
    from lime import limits
    from lime.data_types import ReviewOutput

    run = make_run(project)
    adapter = stub_adapter(
        {"status": "success", "summary": "fixed", "changed_files": [], "artifacts": [],
         "commit_message": "", "notes_for_next_agent": ""},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    phases.revise(run, ReviewOutput(status="success", approved=False, blocking=["x"]), attempt=1)
    prompt = adapter.calls[0].prompt
    assert prompt.count("Builder limits:") == 1
    assert limits.builder_line(run.agent("builder"), run.cfg) in prompt
    assert "max_added_lines" not in prompt
    assert "uncounted=" not in prompt


# --- commit_docs: shared prototype directories use the main checkout (ticket 169) ---


def _write_ticket(directory, name, *, status="open", prototype=None, body_only=False):
    """One minimal ticket file with optional header Status/Prototype."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    header = [f"# {name}", "", "Workflow: feature"]
    if status is not None:
        header.append(f"Status: {status}")
    if prototype is not None and not body_only:
        header.append(f"Prototype: {prototype}")
    body = ["", "## Ask", "do it", "", "## Where", "app.py", "", "## Done means", "works"]
    if prototype is not None and body_only:
        body += ["", "## Notes", f"Prototype: {prototype}"]
    (directory / name).write_text("\n".join(header + body) + "\n")


def _primary_store(head, tmp_path, tickets):
    """A separate committed primary checkout with `.lime/tickets` files."""
    primary = tmp_path / head
    primary.mkdir()
    git(primary, "init", "-q", "-b", "trunk")
    (primary / "marker.txt").write_text("marker\n")
    git(primary, "add", "-A")
    git(primary, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed")
    store = primary / ".lime" / "tickets"
    for name, kwargs in tickets.items():
        _write_ticket(store, name, **kwargs)
    git(primary, "add", "-A")
    git(primary, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "tickets")
    return primary, store


def _seed_route(root, name="prototypes/home"):
    route = root / name
    route.mkdir(parents=True)
    (route / "page.tsx").write_text("proto\n")
    (route / "NOTES.md").write_text("fixed: layout\n")
    return route


def test_commit_docs_keeps_shared_directory_and_logs_all_holders(trunk_project, tmp_path):
    """An open ticket in the primary store that names the directory (or a nested
    directory/file) keeps the Run's prototype folder and is logged."""
    run = make_run(trunk_project)
    route_dir = _seed_route(trunk_project)
    ticket_path = trunk_project / ".lime" / "tickets" / "41-front.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.write_text("close me\n")
    (trunk_project / "AGENTS.md").write_text("docs\n")
    primary, store = _primary_store("primary", tmp_path, {
        "41-front.md": {"prototype": "prototypes/home"},        # own copy — excluded by name
        "42-equal.md": {"prototype": "prototypes/home"},
        "43-nested-dir.md": {"prototype": "prototypes/home/sub"},
        "44-nested-file.md": {"prototype": "prototypes/home/sub/page.tsx"},
    })
    primary_head = _git_rev(primary)
    primary_status = subprocess.run(
        ["git", "-C", str(primary), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    other_before = (store / "42-equal.md").read_bytes()

    sha = phases.commit_docs(run, "Close ticket", ticket_path, route_dir, tickets_dir=store)

    assert sha
    assert not ticket_path.exists()  # the Run's own ticket is removed
    assert route_dir.exists()  # the shared folder is retained
    assert (route_dir / "page.tsx").read_bytes() == b"proto\n"
    log = subprocess.run(
        ["git", "-C", str(trunk_project), "log", "-1", "--pretty=%B"], capture_output=True, text=True
    ).stdout
    assert "Lime-Role: documenter" in log
    payloads = _log_payloads(run, "commit_docs")
    holder_payloads = [p for p in payloads if p.get("holding_tickets")]
    assert holder_payloads, payloads
    holders = holder_payloads[0]["holding_tickets"]
    assert str(store / "42-equal.md") in holders
    assert str(store / "43-nested-dir.md") in holders
    assert str(store / "44-nested-file.md") in holders
    assert str(store / "41-front.md") not in holders  # own ticket excluded
    assert any(str(route_dir) in (p.get("kept") or []) for p in payloads)
    assert all(str(route_dir) not in (p.get("deleted") or []) for p in payloads)
    # the primary checkout is a read-only source
    assert (store / "42-equal.md").read_bytes() == other_before
    assert _git_rev(primary) == primary_head
    assert subprocess.run(
        ["git", "-C", str(primary), "status", "--porcelain"], capture_output=True, text=True
    ).stdout == primary_status


@pytest.mark.parametrize(
    "spelling",
    [
        "prototypes/home",
        "prototypes/home/",
        "./prototypes/home/",
        "prototypes/home/sub/.././home",
    ],
)
@pytest.mark.parametrize("as_str", [False, True], ids=["path", "str"])
def test_commit_docs_dir_holder_spelling_and_route_dir_type(
    trunk_project, tmp_path, spelling, as_str
):
    """Every lexical spelling that resolves to the same folder holds it, and
    `route_dir` may be a Path or a string."""
    run = make_run(trunk_project)
    route_dir = _seed_route(trunk_project)
    ticket_path = trunk_project / ".lime" / "tickets" / "41-front.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.write_text("close me\n")
    (trunk_project / "AGENTS.md").write_text("docs\n")
    _, store = _primary_store("primary", tmp_path, {
        "42-holder.md": {"prototype": spelling},
    })

    passed = str(route_dir) if as_str else route_dir
    sha = phases.commit_docs(run, "Close ticket", ticket_path, passed, tickets_dir=store)

    assert sha
    assert route_dir.exists()
    payloads = _log_payloads(run, "commit_docs")
    assert any(p.get("holding_tickets") for p in payloads)


def test_commit_docs_deletes_without_other_open_holders(trunk_project, tmp_path):
    """Only an open header with a nonempty Prototype naming the folder (or a
    descendant) holds it; everything else, and an empty store, permit deletion."""
    run = make_run(trunk_project)
    route_dir = _seed_route(trunk_project)
    ticket_path = trunk_project / ".lime" / "tickets" / "41-front.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.write_text("close me\n")
    (trunk_project / "AGENTS.md").write_text("docs\n")
    _, store = _primary_store("primary", tmp_path, {
        "41-front.md": {"prototype": "prototypes/home"},          # own, excluded
        "42-closed.md": {"status": "closed", "prototype": "prototypes/home"},
        "43-no-proto.md": {"status": "open"},
        "44-no-status.md": {"status": None, "prototype": "prototypes/home"},
        "45-claimed.md": {"status": "claimed", "prototype": "prototypes/home"},
        "46-sibling.md": {"status": "open", "prototype": "prototypes/home-other"},
        "47-parent.md": {"status": "open", "prototype": "prototypes"},
        "48-body-only.md": {"status": "open", "prototype": "prototypes/home", "body_only": True},
        "49-unrelated.md": {"status": "open", "prototype": "prototypes/elsewhere"},
    })

    sha = phases.commit_docs(run, "Close ticket", ticket_path, route_dir, tickets_dir=store)

    assert sha
    assert not route_dir.exists()
    payloads = _log_payloads(run, "commit_docs")
    assert all(str(route_dir) not in (p.get("holding_tickets") or []) for p in payloads)
    assert any(str(route_dir) in (p.get("deleted") or []) for p in payloads)

    # an empty but readable store also permits deletion
    run2 = session.ensure(cfg(), "R2", runs_dir=trunk_project / "runs")
    route2 = _seed_route(trunk_project, "prototypes/empty")
    ticket2 = trunk_project / ".lime" / "tickets" / "50-front.md"
    ticket2.write_text("close me\n")
    (trunk_project / "AGENTS.md").write_text("docs v2\n")
    empty = tmp_path / "empty" / ".lime" / "tickets"
    empty.mkdir(parents=True)
    sha2 = phases.commit_docs(run2, "Close ticket", ticket2, route2, tickets_dir=empty)
    assert sha2
    assert not route2.exists()


def _unavailable_store_case(tmp_path, store_mode, monkeypatch):
    """Build the ticket/route skeleton and mutate the source per case."""
    root = tmp_path / "run"
    root.mkdir()
    store = root / ".lime" / "tickets"
    store.mkdir(parents=True)
    route = _seed_route(root)
    ticket = store / "41-front.md"
    ticket.write_text("close me\n")
    if store_mode == "none":
        return root, route, ticket, None
    if store_mode == "missing":
        return root, route, ticket, tmp_path / "absent" / "tickets"
    if store_mode == "enum-permission":
        real_iterdir = Path.iterdir

        def denied(self):
            raise PermissionError("blocked")

        monkeypatch.setattr(Path, "iterdir", denied)
        return root, route, ticket, store
    if store_mode == "read-permission":
        real_read = Path.read_text

        def fake_read(self, *a, **k):
            if self.name == "42-holder.md":
                raise PermissionError("blocked")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", fake_read)
        _write_ticket(store, "42-holder.md", prototype="prototypes/home")
        return root, route, ticket, store
    if store_mode == "disappearing":
        real_read = Path.read_text

        def fake_read(self, *a, **k):
            if self.name == "42-holder.md":
                raise FileNotFoundError("gone")
            return real_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", fake_read)
        _write_ticket(store, "42-holder.md", prototype="prototypes/home")
        return root, route, ticket, store
    if store_mode == "invalid-encoding":
        (store / "42-holder.md").write_bytes(b"# 42\n\nStatus: open\n\n\xff\xfe not utf8\n")
        return root, route, ticket, store
    raise AssertionError(store_mode)


@pytest.mark.parametrize(
    "store_mode",
    ["none", "missing", "enum-permission", "read-permission", "disappearing", "invalid-encoding"],
)
def test_commit_docs_keeps_when_ticket_source_unavailable(tmp_path, monkeypatch, store_mode):
    """Any uncertainty about the authoritative ticket list keeps the directory
    while the ticket deletion and docs commit still happen."""
    root, route, ticket, store_arg = _unavailable_store_case(tmp_path, store_mode, monkeypatch)
    # make the route a committed tracked change so the docs commit has content
    git(root, "init", "-q", "-b", "trunk")
    (root / "AGENTS.md").write_text("docs\n")
    git(root, "add", "-A")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed")
    (root / "AGENTS.md").write_text("docs v2\n")
    monkeypatch.chdir(root)  # commit_all operates on the process cwd

    from types import SimpleNamespace
    from lime import session as session_mod

    run = session_mod.ensure(cfg({"test": "exit 0"}), "R1", runs_dir=root / "runs")
    run.repo_root = root  # deterministic Run root for the mapping

    sha = phases.commit_docs(run, "Close ticket", ticket, route, tickets_dir=store_arg)

    assert sha
    assert not ticket.exists()
    assert route.exists() and (route / "page.tsx").read_bytes() == b"proto\n"
    payloads = _log_payloads(run, "commit_docs")
    keep = [p for p in payloads if p.get("kept")]
    assert keep, payloads
    assert keep[0]["reason"]  # actionable reason
    assert all(str(route) not in (p.get("deleted") or []) for p in payloads)


def test_commit_docs_last_close_removes_previously_shared_directory(trunk_project, tmp_path):
    """First close keeps the folder while another open ticket names it; the last
    close, once that ticket is gone, removes it."""
    route_dir = _seed_route(trunk_project)
    ticket_path = trunk_project / ".lime" / "tickets" / "41-front.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.write_text("close me\n")
    (trunk_project / "AGENTS.md").write_text("docs\n")
    _, store = _primary_store("primary", tmp_path, {
        "42-holder.md": {"prototype": "prototypes/home"},
    })

    run1 = session.ensure(cfg({"test": "exit 0"}), "R1", runs_dir=trunk_project / "runs")
    sha1 = phases.commit_docs(run1, "Close ticket", ticket_path, route_dir, tickets_dir=store)
    assert sha1
    assert route_dir.exists()  # held by 42-holder.md

    (store / "42-holder.md").unlink()  # the holder closes/disappears
    (trunk_project / "AGENTS.md").write_text("docs v2\n")  # a fresh docs change to commit
    run2 = session.ensure(cfg({"test": "exit 0"}), "R2", runs_dir=trunk_project / "runs")
    sha2 = phases.commit_docs(run2, "Close ticket", ticket_path, route_dir, tickets_dir=store)

    assert sha2
    assert not route_dir.exists()  # last close removes it
    payloads = _log_payloads(run2, "commit_docs")
    assert any(str(route_dir) in (p.get("deleted") or []) for p in payloads)


def test_commit_docs_successful_replay_never_scans_or_deletes_a_directory(
    trunk_project, monkeypatch
):
    """A directory prototype on a replayed commit_docs is left completely alone."""
    run = make_run(trunk_project)
    run.replay = {"commit_docs": {"phase_id": "R1:11", "output_type": None, "payload_json": None}}
    route_dir = _seed_route(trunk_project)
    ticket_path = trunk_project / ".lime" / "tickets" / "41-front.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.write_text("still here\n")

    def explode(*a, **kw):
        raise AssertionError("a replayed commit_docs must not scan or delete")

    monkeypatch.setattr(phases, "_directory_prototype_holders", explode)
    monkeypatch.setattr(git_helper, "commit_all", explode)

    result = phases.commit_docs(run, "msg", ticket_path, route_dir, tickets_dir=None)
    assert result == ""
    assert run.phases == []
    assert ticket_path.exists()
    assert route_dir.exists()


def test_commit_docs_recovers_after_interrupted_shared_directory_state(
    trunk_project, tmp_path
):
    """The interrupted state: own ticket already unlinked. A fresh invocation scans
    the current primary tickets and commits successfully."""
    run = make_run(trunk_project)
    route_dir = _seed_route(trunk_project)
    ticket_path = trunk_project / ".lime" / "tickets" / "41-front.md"
    ticket_path.parent.mkdir(parents=True, exist_ok=True)
    ticket_path.unlink(missing_ok=True)  # the crash already removed it
    (trunk_project / "AGENTS.md").write_text("docs\n")
    _, store = _primary_store("primary", tmp_path, {
        "42-holder.md": {"prototype": "prototypes/home"},
    })

    sha = phases.commit_docs(run, "Close ticket", ticket_path, route_dir, tickets_dir=store)

    assert sha
    assert route_dir.exists()  # the live primary ticket still holds it
    payloads = _log_payloads(run, "commit_docs")
    assert any(p.get("holding_tickets") for p in payloads)


def test_rereview_replays_every_earlier_verify_and_reruns_only_the_dropped_one(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    phases.verify(run, 1)
    (project / "extra.py").write_text("v1\n")
    for name, output_type, payload in (
        ("verify_2", "VerifyOutput", {"status": "success", "summary": "ok", "passed": True}),
        ("review", "ReviewOutput", _OK_REVIEW),
    ):
        pid = run.tracer.add_phase("R1", seq=run.tracer.max_phase_seq("R1") + 1,
                                   name=name, stage="Build", kind="code", owner="git")
        run.tracer.set_phase_status(pid, "running")
        run.tracer.set_phase_status(pid, "success")
        run.tracer.record_envelope(
            "R1", pid, agent="git", output_type=output_type,
            payload_json=json.dumps(payload), valid=True, attempt=1,
        )
    assert run.tracer.rereview("R1", engineer="t")[0] == "verify_2"

    (project / "extra.py").write_text("changed content\n")
    run2 = make_run(project, {"test": "exit 0"})
    run2.replay = run2.tracer.replay_map("R1")
    assert "verify_1" in run2.replay
    spy = []
    real = phases.quality.run_quality
    monkeypatch.setattr(phases.quality, "run_quality", lambda r: spy.append(1) or real(r))
    recorded = json.loads(run.tracer.con.execute(
        "SELECT payload_json FROM envelopes WHERE output_type='VerifyOutput' ORDER BY envelope_id"
    ).fetchone()["payload_json"])
    got = phases.verify(run2, 1)
    assert got.model_dump(mode="json") == type(got).model_validate(recorded).model_dump(mode="json")
    assert run2.phases == [] and spy == []
    row = run2.tracer.con.execute(
        "SELECT payload_json FROM events WHERE name='verify_1' AND type='log'"
        " ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    assert json.loads(row["payload_json"]) == {"replayed": True}

    # the dropped verify re-runs for real and opens a fresh row
    phases.verify(run2, 2)
    assert spy == [1]
    assert [p.params.name for p in run2.phases] == ["verify_2"]


def test_resume_reruns_the_latest_verify_against_the_current_tree(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    assert phases.verify(run, 1).passed
    (project / "app.py").write_text("v2\n")  # change the tree after the recorded pass
    calls = _spy_quality(monkeypatch)
    run2 = make_run(project, {"test": "exit 0"})
    run2.replay = run2.tracer.replay_map("R1")
    envelope = phases.verify(run2, 1)
    assert calls == [1], "the latest verify re-runs against the current tree"
    assert [p.params.name for p in run2.phases] == ["verify_1"]
    assert envelope.head_sha == git_helper.rev("HEAD")


def test_resume_replays_an_earlier_failed_verify_as_recorded(project, monkeypatch):
    assert not phases.verify(make_run(project, {"test": "exit 1"}), 1).passed
    assert phases.verify(make_run(project, {"test": "exit 0"}), 1, attempt=2).passed  # the latest

    calls = _spy_quality(monkeypatch)
    run3 = make_run(project, {"test": "exit 0"})
    run3.replay = run3.tracer.replay_map("R1")
    result = phases.verify(run3, 1)
    assert result.passed is False, "the earlier failure replays as recorded"
    assert calls == [] and run3.phases == []


def test_passing_rerun_of_the_latest_verify_keeps_later_successes(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    assert phases.verify(run, 1).passed
    pid = run.tracer.add_phase("R1", seq=run.tracer.max_phase_seq("R1") + 1,
                               name="review", stage="Build", kind="agent", owner="reviewer")
    run.tracer.set_phase_status(pid, "running")
    run.tracer.set_phase_status(pid, "success")
    run.tracer.record_envelope(
        "R1", pid, agent="reviewer", output_type="ReviewOutput",
        payload_json=json.dumps(_OK_REVIEW), valid=True, attempt=1,
    )
    run2 = make_run(project, {"test": "exit 0"})
    run2.replay = run2.tracer.replay_map("R1")
    assert phases.verify(run2, 1).passed
    assert "review" in run2.replay, "a pass keeps later successes replayable"
    n = run2.tracer.con.execute(
        "SELECT COUNT(*) FROM events WHERE type='log' AND name='phase_invalidated'"
    ).fetchone()[0]
    assert n == 0


def test_failed_rerun_of_the_latest_verify_drops_its_stale_dependents(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    assert phases.verify(run, 1).passed
    old_id = run.tracer.replay_map("R1")["verify_1"]["phase_id"]
    pid = run.tracer.add_phase("R1", seq=run.tracer.max_phase_seq("R1") + 1,
                               name="review", stage="Build", kind="agent", owner="reviewer")
    run.tracer.set_phase_status(pid, "running")
    run.tracer.set_phase_status(pid, "success")
    run.tracer.record_envelope(
        "R1", pid, agent="reviewer", output_type="ReviewOutput",
        payload_json=json.dumps(_OK_REVIEW), valid=True, attempt=1,
    )

    run2 = make_run(project, {"test": "exit 1"})
    run2.replay = run2.tracer.replay_map("R1")
    assert not phases.verify(run2, 1).passed
    assert "review" not in run2.replay, "a failed re-run drops the stale suffix"
    invalidated = {
        r["phase_id"] for r in run2.tracer.con.execute(
            "SELECT phase_id FROM events WHERE type='log' AND name='phase_invalidated'"
        )
    }
    assert old_id in invalidated and pid in invalidated
    # the new failed row is still running at invalidation time, so it stays live
    assert run2.tracer.replay_map("R1")["verify_1"]["phase_id"] == run2.phases[-1].phase_id


def test_failed_rerun_drops_dependents_that_precede_a_passing_resume_row(project, monkeypatch):
    """A passing resume preserves the earlier review/commit; the *next* resume's
    failing latest verify must still drop those stale dependents even though they
    sit before the newer passing verify row (ticket 188)."""
    run = make_run(project, {"test": "exit 0"})
    assert phases.verify(run, 1).passed
    first_verify_id = run.tracer.replay_map("R1")["verify_1"]["phase_id"]
    review_id = run.tracer.add_phase("R1", seq=run.tracer.max_phase_seq("R1") + 1,
                                     name="review", stage="Build", kind="agent", owner="reviewer")
    run.tracer.set_phase_status(review_id, "running")
    run.tracer.set_phase_status(review_id, "success")
    run.tracer.record_envelope(
        "R1", review_id, agent="reviewer", output_type="ReviewOutput",
        payload_json=json.dumps(_OK_REVIEW), valid=True, attempt=1,
    )

    # a passing resume re-runs the latest verify and appends a newer row of the same name
    passing = make_run(project, {"test": "exit 0"})
    passing.replay = passing.tracer.replay_map("R1")
    assert phases.verify(passing, 1).passed
    passing_verify_id = passing.tracer.replay_map("R1")["verify_1"]["phase_id"]
    assert passing_verify_id != first_verify_id

    # the next resume's latest verify fails against the current tree
    run3 = make_run(project, {"test": "exit 1"})
    run3.replay = run3.tracer.replay_map("R1")
    assert not phases.verify(run3, 1).passed
    assert "review" not in run3.replay, "a failed re-run drops the preserved review"
    invalidated = {
        r["phase_id"] for r in run3.tracer.con.execute(
            "SELECT phase_id FROM events WHERE type='log' AND name='phase_invalidated'"
        )
    }
    assert review_id in invalidated, "the review preserved by the passing resume must drop"
    assert first_verify_id in invalidated and passing_verify_id in invalidated


def test_an_interrupted_rerun_still_reruns_the_latest_verify(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    assert phases.verify(run, 1).passed
    pid = run.tracer.add_phase("R1", seq=run.tracer.max_phase_seq("R1") + 1,
                               name="review", stage="Build", kind="agent", owner="reviewer")
    run.tracer.set_phase_status(pid, "running")
    run.tracer.set_phase_status(pid, "success")
    run.tracer.record_envelope(
        "R1", pid, agent="reviewer", output_type="ReviewOutput",
        payload_json=json.dumps(_OK_REVIEW), valid=True, attempt=1,
    )
    # a hard kill left a newer verify_1 row running — it is the latest verify
    intr = run.tracer.add_phase("R1", seq=run.tracer.max_phase_seq("R1") + 1,
                                name="verify_1", stage="Build", kind="code", owner="git")
    run.tracer.set_phase_status(intr, "running")

    calls = _spy_quality(monkeypatch)
    run2 = make_run(project, {"test": "exit 0"})
    run2.replay = run2.tracer.replay_map("R1")
    assert phases.verify(run2, 1).passed
    assert calls == [1], "an interrupted re-run still re-runs, not replays the old pass"


def test_resume_loads_and_resumes_a_pre_change_trace(project, monkeypatch):
    """A trace recorded before the provenance model was deleted still loads and
    resumes under the current rule: the earlier verify replays as recorded, the
    latest verify re-runs, and the old provenance keys and cache-miss events are inert."""
    run = make_run(project, {"test": "exit 0"})
    old_verification = {
        "version": 1, "content_fingerprint": "x",
        "head_sha": "a" * 40, "policy": {}, "commands": [],
    }
    ids = {}
    for seq, name, output_type, payload in (
        (1, "verify_1", "VerifyOutput",
         {"status": "success", "passed": True, "verification": old_verification}),
        (2, "verify_2", "VerifyOutput",
         {"status": "success", "passed": True, "verification": old_verification}),
        (3, "review", "ReviewOutput", _OK_REVIEW),
    ):
        pid = run.tracer.add_phase("R1", seq=seq, name=name, stage="Build", kind="code", owner="git")
        run.tracer.set_phase_status(pid, "running")
        run.tracer.set_phase_status(pid, "success")
        run.tracer.record_envelope(
            "R1", pid, agent="git", output_type=output_type,
            payload_json=json.dumps(payload), valid=True, attempt=1,
        )
        ids[name] = pid
    # old cache-miss and provenance log events from the deleted model — inert on resume
    run.tracer.event("R1", "log", phase_id=ids["verify_1"], name="verify_cache_miss", payload={})
    run.tracer.event("R1", "log", phase_id=ids["verify_1"], name="verify_1",
                     payload={"replayed": True, "provenance": "matched"})

    calls = _spy_quality(monkeypatch)
    run2 = make_run(project, {"test": "exit 0"})
    run2.replay = run2.tracer.replay_map("R1")
    earlier = phases.verify(run2, 1)
    assert earlier.passed is True, "the earlier verify replays as recorded"
    assert calls == [] and run2.phases == []
    assert phases.verify(run2, 2).passed
    assert calls == [1], "the latest verify re-runs against the current tree"
    assert "review" in run2.replay, "a pass keeps later successes replayable"


# --- no layout code path (ticket 192) ---


def test_no_lime_module_imports_visual_or_names_layout_output():
    root = Path(phases.__file__).parents[1]  # the lime/ package
    assert not (root / "visual.py").exists()
    assert not (root / "browser").exists()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "lime.visual", f"{path}: imports {alias.name}"
                    assert alias.asname != "visual", f"{path}: aliases {alias.name} as visual"
            elif isinstance(node, ast.ImportFrom):
                assert node.module not in ("visual", "lime.visual"), f"{path}: from {node.module} import"
                for alias in node.names:
                    assert alias.name != "visual", f"{path}: imports visual from {node.module}"
                    assert alias.asname != "visual", f"{path}: aliases visual from {node.module}"
            elif isinstance(node, ast.Name):
                assert node.id != "LayoutOutput", f"{path}: names LayoutOutput"
            elif isinstance(node, ast.Attribute):
                assert node.attr != "LayoutOutput", f"{path}: attribute LayoutOutput"
            elif isinstance(node, ast.alias):
                assert alias.name != "LayoutOutput" and alias.asname != "LayoutOutput", (
                    f"{path}: aliases LayoutOutput"
                )
    assert not hasattr(phases, "layout")
    assert not hasattr(gates, "layout_required")


def test_document_gate_reprompts_on_a_long_new_glossary_entry(project, monkeypatch):
    """A long new CONTEXT.md entry fails the glossary gate once; the correction
    lands a short entry, and only then is the documenter envelope accepted."""
    run = make_run(project)
    run.base_sha = git_helper.rev("HEAD")
    context_path = project / "CONTEXT.md"
    calls = []
    replies = iter([
        (_DOC_REPLY, "- **Sprawl** — " + "x" * 400 + "\n"),
        (_DOC_REPLY, "- **Sprawl** — a short definition of sprawl.\n"),
    ])

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        calls.append(req)
        reply, content = next(replies)
        context_path.write_text(content)
        return AgentResult(
            status="ok", text="", session_id=req.session_id, usage=usage(),
            cost=0.01, context_tokens=150, context_window=100_000, envelope=reply,
        )

    adapter.calls = calls
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    changes_envelope = ChangesOutput(status="success", changed_files=["app.py"], diff_path="x")
    envelope = phases.document(run, "do X", changes_envelope)
    assert envelope.status == "success"
    assert len(calls) == 2
    assert "**Sprawl**" in calls[1].prompt
    assert "characters" in calls[1].prompt
    rows = run.tracer.con.execute(
        "SELECT attempt, gate, passed FROM gate_results WHERE phase_id=? "
        "AND gate='glossary_entries_capped' ORDER BY id",
        (run.phases[0].phase_id,),
    ).fetchall()
    assert [bool(r["passed"]) for r in rows] == [False, True]
