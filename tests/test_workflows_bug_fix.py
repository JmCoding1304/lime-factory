"""bug_fix.py chain assembly (spec §7): same phases as feature.py, with the
reproduce-first/root-cause framing threaded into the planner and reviewer
prompts. A `Workflow: bug-fix` ticket run through `feature.run()` (the one
call site `lime run` uses) is dispatched here automatically."""

import json
import subprocess
from pathlib import Path

import pytest

from lime import agents, git_helper, session
from lime.data_types import AgentResult, CostBreakdown, UsageBreakdown
from lime.tracker import Ticket
from lime.workflows import bug_fix, feature
from stage_helpers import assert_table_stages


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


BUG_TICKET_MD = (
    "# 42 — Greet Crashes On Empty Name\n\nWorkflow: bug-fix\nStatus: open\n\n"
    "## Ask\ngreet('') raises instead of returning a default.\n\n## Where\napp.py\n\n"
    "## Done means\nA regression test covers the empty-name case and commands.test is green.\n\n"
    "## Out of scope\nNothing.\n"
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("v1\n")
    (tmp_path / ".gitignore").write_text("runs/\n")
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "42-bug.md").write_text(BUG_TICKET_MD)
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def cfg(commands):
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
        "commands": commands,
    }


def make_run(project, commands):
    return session.ensure(cfg(commands), "R1", runs_dir=project / "runs")


def usage() -> UsageBreakdown:
    return UsageBreakdown(input=100, output=50, total=150, cost=CostBreakdown(total=0.01))


BUG_TICKET = Ticket(
    path=".lime/tickets/42-bug.md", number=42, slug="42-bug", title="Greet Crashes On Empty Name",
    workflow="bug-fix", ask="greet('') raises instead of returning a default.",
    done_means="A regression test covers the empty-name case and commands.test is green.",
)


def make_agent_stub(run, plan_md: str, replies: list, *, scout: bool = True):
    """Same shape as test_workflows_feature.py's stub — scout first, then planner, then each
    further reply in call order."""
    plan_path = run.context_handoff_dir / "plan.md"
    normalized = [r if isinstance(r, tuple) else (r, {}) for r in replies]
    queue = [
        (
            {"status": "success", "summary": "planned", "artifacts": [str(plan_path)]},
            {str(plan_path): plan_md},
        ),
    ] + [(reply, write) for reply, write in normalized]
    if scout:
        brief_path = run.context_handoff_dir / "scout_findings.md"
        queue.insert(0, (
            {"status": "success", "summary": "scouted", "findings": [{"file": "app.py", "note": "greet lives here"}], "artifacts": [str(brief_path)]},
            {str(brief_path): "# brief\napp.py — greet() lives here\n"},
        ))
    calls = []

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        reply, write = queue[len(calls)]
        calls.append(req)
        for rel_path, content in write.items():
            Path(rel_path).parent.mkdir(parents=True, exist_ok=True)
            Path(rel_path).write_text(content)
        return AgentResult(
            status="ok", text="", session_id=req.session_id, usage=usage(),
            cost=0.01, context_tokens=150, context_window=100_000, envelope=reply,
        )

    adapter.calls = calls
    return adapter


def build_reply(commit_message="Handle empty name") -> dict:
    return {
        "status": "success", "summary": "built", "changed_files": [],
        "artifacts": [], "commit_message": commit_message, "notes_for_next_agent": "",
    }


def review_reply(approved=True) -> dict:
    return {
        "status": "success", "summary": "reviewed", "approved": approved, "findings": [],
        "blocking": [], "trust_violations": [], "artifacts": [], "notes_for_next_agent": "",
    }


def document_reply() -> dict:
    return {
        "status": "success", "summary": "documented", "changed_docs": [],
        "adrs": [], "commit_message": "Update docs", "notes_for_next_agent": "",
    }


# Bug-fix plans are one step: the regression test and the fix that makes it pass land together (ticket 65).
PLAN_WITH_REGRESSION_TEST = (
    "## Repo facts\nnone\n\n## Step 1\nAdd a failing regression test for greet('') in"
    " test_app.py, then fix greet() in app.py so it passes.\nfailure: pure\nwriters: none\n"
)


def test_bug_fix_ticket_run_through_feature_dispatches_to_bug_fix(project, monkeypatch):
    """The one call site (`lime run` -> `feature.run`) has to land here on its own —
    nothing else in the CLI knows bug-fix from feature."""
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(
        run, PLAN_WITH_REGRESSION_TEST, [build_reply(), (review_reply(), {}), (document_reply(), {})]
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, BUG_TICKET, "main")

    assert code == 0
    names = [p.params.name for p in run.phases]
    assert names == [
        "engineer", "setup", "scout", "plan", "build_1", "verify_1", "review",
        "commit_code", "changes", "document", "commit_docs", "push_pr",
    ]
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "success"

    # bug-fix shares feature's plan gate through the single phases.plan integration point
    plan_phase = next(p for p in run.phases if p.params.name == "plan")
    gate_rows = run.tracer.con.execute(
        "SELECT gate, passed FROM gate_results WHERE phase_id=?", (plan_phase.phase_id,)
    ).fetchall()
    plan_gates = {r["gate"]: bool(r["passed"]) for r in gate_rows}
    assert plan_gates["artifacts_exist"] is True
    assert plan_gates["plan_has_failure_lines"] is True
    assert (
        "For every table column, file or queue the ticket writes, list every existing writer"
        " and reader as path:line — admin, signals, jobs, API and tests included."
        in adapter.calls[0].prompt
    )
    assert_table_stages(run.tracer.con, run.run_id)


def test_bug_fix_plan_prompt_asks_for_one_green_step_with_the_regression_test(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(
        run, PLAN_WITH_REGRESSION_TEST, [build_reply(), (review_reply(), {}), (document_reply(), {})]
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    bug_fix.run(run, BUG_TICKET, "main")

    plan_call = adapter.calls[1]  # scout, plan
    assert "reproduce" in plan_call.prompt.lower()
    assert "root cause" in plan_call.prompt.lower()
    assert "one step" in plan_call.prompt.lower() and "green" in plan_call.prompt.lower()
    assert "failing regression test" not in plan_call.prompt.lower()   # ticket 65: a red step burns verify
    assert "step 1 is" not in plan_call.prompt.lower()
    assert "Ask" in plan_call.prompt and BUG_TICKET.ask in plan_call.prompt  # base prompt still present


def test_bug_fix_review_prompt_asks_for_root_cause(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(
        run, PLAN_WITH_REGRESSION_TEST, [build_reply(), (review_reply(), {}), (document_reply(), {})]
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    bug_fix.run(run, BUG_TICKET, "main")

    review_call = adapter.calls[3]  # scout, plan, build_1, review
    assert "root cause" in review_call.prompt.lower()
    assert "regression test" in review_call.prompt.lower()


def test_engineer_phase_still_logs_the_bare_ticket_ask_not_the_addendum(project, monkeypatch):
    """The record of what was asked stays the ticket's own words — only plan/review
    get the bug-fix framing bolted on."""
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(
        run, PLAN_WITH_REGRESSION_TEST, [build_reply(), (review_reply(), {}), (document_reply(), {})]
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    feature.run(run, BUG_TICKET, "main")

    events = run.tracer.con.execute("SELECT * FROM events WHERE type='log'").fetchall()
    logged = "\n".join(e["payload_json"] or "" for e in events)
    assert BUG_TICKET.ask in logged
    assert "reproduce first" not in logged.lower()
    assert bug_fix.PLAN_ADDENDUM.strip().lower() not in logged.lower()


def test_bug_fix_scout_gets_the_bare_ticket_not_the_plan_addendum(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(
        run, PLAN_WITH_REGRESSION_TEST, [build_reply(), (review_reply(), {}), (document_reply(), {})]
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    bug_fix.run(run, BUG_TICKET, "main")

    scout_call = adapter.calls[0]
    assert BUG_TICKET.ask in scout_call.prompt
    assert "reproduce" not in scout_call.prompt.lower()
    assert bug_fix.PLAN_ADDENDUM.strip() not in scout_call.prompt
    assert "reproduce" in adapter.calls[1].prompt.lower()
