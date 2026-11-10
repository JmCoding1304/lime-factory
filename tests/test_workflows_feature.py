"""feature.py chain assembly: engineer -> setup -> plan -> build/verify loop ->
review/revise loop -> commit(code) -> changes -> document -> commit(docs) -> push_pr."""

import json
import subprocess
from pathlib import Path

import pytest

from lime import agents, git_helper, session
from lime.data_types import AgentResult, CostBreakdown, UsageBreakdown
from lime.tracker import Ticket
from lime.workflows import feature
from stage_helpers import assert_table_stages


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


TICKET_MD = (
    "# 41 — Toy Ticket\n\nWorkflow: feature\nStatus: open\n\n"
    "## Ask\nWrite greet() in app.py.\n\n## Where\napp.py\n\n"
    "## Done means\ncommands.test is green.\n\n## Out of scope\nNothing.\n"
)


@pytest.fixture
def project(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("v1\n")
    (tmp_path / ".gitignore").write_text("runs/\n")
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "41-toy.md").write_text(TICKET_MD)
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


def _cfg_with_cap(commands, cap):
    """`cfg()` plus an explicit `commit.max_added_lines` (ticket 130 overflow tests)."""
    c = cfg(commands)
    c["commit"] = {"max_added_lines": cap}
    return c


def _big_file_text(added: int) -> str:
    """`added` distinct lines, generated — never a checked-in fixture file."""
    return "".join(f"line {i}\n" for i in range(added))


def usage() -> UsageBreakdown:
    return UsageBreakdown(input=100, output=50, total=150, cost=CostBreakdown(total=0.01))


def test_build_prompt_adds_fresh_session_context_only_for_later_work(project, monkeypatch):
    """Step 1's first attempt has neither fresh-session line nor git facts;
    step 2 and retries have exactly one block each."""
    sentinel = "SENTINEL_GIT_FACTS"
    called_with = []

    def fake_git_facts(repo_root, continuation):
        called_with.append((repo_root, continuation))
        return sentinel

    monkeypatch.setattr(feature.budget, "git_facts", fake_git_facts)

    run = make_run(project, {})
    run.repo_root = project
    step_text = "## Step 1\nDo the thing.\n"

    # (step_no, retry) = (1, False) — first attempt of step 1
    prompt11 = feature._build_prompt(run, 1, step_text, retry=False)
    assert sentinel not in prompt11
    assert "Read the plan at" in prompt11
    assert "Do the thing." in prompt11
    assert called_with == []

    # (step_no, retry) = (2, False) — step 2 first attempt
    step2_text = "## Step 2\nDo another thing.\n"
    prompt21 = feature._build_prompt(run, 2, step2_text, retry=False)
    assert sentinel in prompt21
    assert "Read the plan at" in prompt21
    assert "Do another thing." in prompt21
    assert "fresh session" in prompt21
    assert called_with == [(run.repo_root, 0)]
    called_with.clear()

    # (step_no, retry) = (1, True) — step 1 retry
    prompt12 = feature._build_prompt(run, 1, step_text, retry=True)
    assert sentinel in prompt12
    assert "still fails verification" in prompt12
    assert "fresh session" in prompt12
    assert called_with == [(run.repo_root, 0)]
    called_with.clear()

    # (step_no, retry) = (2, True) — step 2 retry
    prompt22 = feature._build_prompt(run, 2, step2_text, retry=True)
    assert sentinel in prompt22
    assert "fresh session" in prompt22
    assert called_with == [(run.repo_root, 0)]


TICKET = Ticket(
    path=".lime/tickets/41-toy.md", number=41, slug="41-toy", title="Toy Ticket",
    workflow="feature", ask="Write greet() in app.py.", done_means="commands.test is green.",
)


def make_agent_stub(run, plan_md: str, replies: list, *, scout: bool = True):
    """One adapter for every role: scout first, then planner first, then each further reply
    (build, review, revise, document, …) in call order. Good enough for a
    chain-assembly test — no gate retries. Each entry is either a bare reply
    dict (no file write) or a `(reply, write)` tuple.
    `scout=False` is for the resumed run whose scout is replayed."""
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


def build_reply(changed_files=None, commit_message="Add greet()") -> dict:
    return {
        "status": "success", "summary": "built", "changed_files": changed_files or [],
        "artifacts": [], "commit_message": commit_message, "notes_for_next_agent": "",
    }


def review_reply(approved=True, blocking=None, trust_violations=None) -> dict:
    blocking = blocking or []
    trust_violations = trust_violations or []
    findings = [] if approved else [{"requirement": "it works", "met": False, "evidence": "missing"}]
    return {
        "status": "success", "summary": "reviewed", "approved": approved, "findings": findings,
        "blocking": blocking, "trust_violations": trust_violations, "artifacts": [],
        "notes_for_next_agent": "",
    }


def document_reply(changed_docs=None, commit_message="Update docs") -> dict:
    return {
        "status": "success", "summary": "documented", "changed_docs": changed_docs or [],
        "adrs": [], "commit_message": commit_message, "notes_for_next_agent": "",
    }


PLAN_ONE_STEP = (
    "## Repo facts\nnone\n\n## Step 1\nAdd greet() to app.py.\n"
    "failure: pure\nwriters: none\n"
)

# What ships after every build/verify loop in the happy path: an approving review,
# then a documenter that changes nothing (delete-on-close alone still commits).
APPROVED_TAIL = [(review_reply(), {}), (document_reply(), {})]


def failing_scout_stub(run, scout_reply):
    """A chain adapter whose scout always fails: an Exception raises, an envelope is
    returned verbatim (writing no file) on every scout call, while every other role runs
    `inner` (the happy path)."""
    inner = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL, scout=False)
    scout_calls = []

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        if "# Scout Agent" in req.system_prompt:
            scout_calls.append(req)
            if isinstance(scout_reply, Exception):
                raise scout_reply
            return AgentResult(
                status="ok", text="", session_id=req.session_id, usage=usage(),
                cost=0.01, context_tokens=150, context_window=100_000, envelope=scout_reply,
            )
        return inner(req, on_event, on_spawn, on_exit)

    adapter.calls = inner.calls
    adapter.scout_calls = scout_calls
    return adapter


def test_happy_path_runs_every_step_commits_and_opens_a_pr(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")  # the "builder" made a change

    code = feature.run(run, TICKET, "main")

    assert code == 0
    names = [p.params.name for p in run.phases]
    assert names == [
        "engineer", "setup", "scout", "plan", "build_1", "verify_1", "review",
        "commit_code", "changes", "document", "commit_docs", "push_pr",
    ]
    assert all(p.status == "success" for p in run.phases)
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "success"
    log = subprocess.run(
        ["git", "-C", str(project), "log", "-1", "--pretty=%B"], capture_output=True, text=True
    ).stdout
    assert "Update docs" in log
    assert f"Lime-Run: {run.run_id}" in log
    assert "Lime-Role: documenter" in log
    assert not (project / ".lime" / "tickets" / "41-toy.md").exists()  # delete-on-close

    # the plan phase ran both claim gates, and the scout got the writer/reader sentence
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


@pytest.mark.parametrize("case", ["fail", "blocked", "gate_miss", "raised"])
def test_a_failed_scout_never_stops_the_run(project, monkeypatch, capsys, case):
    """Every kind of Chain scout failure (reported fail, blocked, gate miss, raised) fails
    only the scout Phase: the Run ends success, the planner runs on the no-brief file, and
    `lime trace` still shows the scout's `fail` (ticket 197)."""
    run = make_run(project, {"test": "exit 0"})
    brief = run.context_handoff_dir / "scout_findings.md"
    if case == "fail":
        scout_reply = {"status": "fail", "summary": "spec 12 must land first", "findings": [], "artifacts": []}
        reason = "spec 12 must land first"
    elif case == "blocked":
        scout_reply = {"status": "partial", "summary": "cannot repro",
                       "notes_for_next_agent": "needs a browser repro", "findings": [], "artifacts": []}
        reason = "needs a browser repro"
    elif case == "gate_miss":
        # success envelope but the named artifact was never written: the gate misses twice
        scout_reply = {"status": "success", "summary": "scouted", "findings": [], "artifacts": [str(brief)]}
        reason = "failed gates"
    else:
        scout_reply = RuntimeError("scout crashed")
        reason = "scout crashed"

    adapter = failing_scout_stub(run, scout_reply)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, TICKET, "main")

    assert code == 0
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "success"
    names = [p.params.name for p in run.phases]
    assert names[:4] == ["engineer", "setup", "scout", "plan"]
    assert "push_pr" in names
    scout_phase = next(p for p in run.phases if p.params.name == "scout")
    assert scout_phase.status == "fail"
    assert all(p.status == "success" for p in run.phases if p.params.name != "scout")
    assert any("# Planner Agent" in c.system_prompt for c in adapter.calls)
    text = brief.read_text()
    assert "Scout did not finish" in text
    assert "no brief" in text
    assert reason in text
    assert "scout" not in run.tracer.replay_map("R1")

    from lime import cli

    capsys.readouterr()  # drop the chain's own console output
    cli._print_trace(run.tracer.con, "R1")
    lines = capsys.readouterr().out.splitlines()
    assert any("scout" in line and "fail" in line for line in lines)
    assert "success" in lines[0]


def test_scout_lead_reports_facts_not_verdicts():
    """The scout lead asks for path:line facts, never a verdict on the ticket (ticket 197)."""
    assert (
        "Report what the code does today as path:line facts; never whether the ticket is done"
        " or blocked, or how to plan it — the planner judges that."
        in feature.SCOUT_LEAD
    )


def test_a_red_verify_retries_the_build_then_succeeds(project, monkeypatch):
    calls_to_test = {"n": 0}

    # first "test" run fails, second passes — the builder "fixed" it on retry
    def fake_run_check(run_, name):
        from lime import quality

        if name != "test":
            return None
        calls_to_test["n"] += 1
        command = "exit 1" if calls_to_test["n"] == 1 else "exit 0"
        spec = quality.QualityCheckSpec(name="test", command=command)
        return quality._run(spec, run_)

    monkeypatch.setattr("lime.quality.run_check", fake_run_check)

    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(
        run, PLAN_ONE_STEP,
        [build_reply(), build_reply(commit_message="Fix greet()")] + APPROVED_TAIL,
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")  # the "builder" made a change

    code = feature.run(run, TICKET, "main")

    assert code == 0
    names = [p.params.name for p in run.phases]
    assert names == [
        "engineer", "setup", "scout", "plan",
        "build_1", "verify_1", "build_1_retry1", "verify_1_retry1",
        "review", "commit_code", "changes", "document", "commit_docs", "push_pr",
    ]

    # Inspect builder requests
    # adapter.calls: [scout, plan, build_1, build_1_retry1, review, document, ...]
    assert len(adapter.calls) >= 6
    build1_call = adapter.calls[2]  # after scout (0) and plan (1)
    retry_call = adapter.calls[3]  # build_1_retry1

    # Step 1's opening prompt (first attempt) has NO fresh-session block
    build1_prompt = build1_call.prompt
    assert "This is a fresh session" not in build1_prompt
    assert "## Git facts" not in build1_prompt
    assert "Read the plan at" in build1_prompt

    # Retry prompt has fresh-session block and Git facts
    retry_prompt = retry_call.prompt
    assert "This is a fresh session" in retry_prompt
    assert "## Git facts" in retry_prompt
    assert "still fails verification" in retry_prompt

    # Retry has new session_id and resume=False
    assert retry_call.session_id != build1_call.session_id
    assert retry_call.resume is False
    assert_table_stages(run.tracer.con, run.run_id)


def test_verify_still_red_after_max_attempts_finishes_unaccepted_without_pushing(project, monkeypatch):
    run = make_run(project, {"test": "echo boom && exit 1"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [(build_reply(), {})] * feature.MAX_BUILD_ATTEMPTS)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    pushed = []
    monkeypatch.setattr(git_helper, "push", lambda branch: pushed.append(branch))

    code = feature.run(run, TICKET, "main")

    assert code == 1
    assert pushed == []
    names = [p.params.name for p in run.phases]
    assert "commit_code" not in names
    assert "push_pr" not in names
    build_names = [n for n in names if n.startswith("build_")]
    assert len(build_names) == feature.MAX_BUILD_ATTEMPTS
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "fail"
    # every attempted phase still succeeded as a PHASE — only the Run's acceptance failed
    assert all(p.status == "success" for p in run.phases)


# --- ticket 49: exhaustion drops the plan from the replay map ---


def test_step_exhaustion_drops_the_plan_and_its_downstream_from_the_replay_map(project, monkeypatch):
    run = make_run(project, {"test": "echo boom && exit 1"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [(build_reply(), {})] * feature.MAX_BUILD_ATTEMPTS)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    code = feature.run(run, TICKET, "main")

    assert code == 1
    replay = run.tracer.replay_map("R1")
    assert set(replay) == {"engineer", "setup", "scout"}
    assert not (run.context_handoff_dir / "plan.md").exists()


def _ast_write_targets(source: str) -> list[str]:
    """String literals handed to file-writing calls (`open(..., 'w')`, `write_text`,
    `write_bytes`) — an AST guard over what a module can write."""
    import ast

    targets: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = ast.unparse(node.func)
        args = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        args += [k.value.value for k in node.keywords
                 if isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)]
        writes = func.endswith("write_text") or func.endswith("write_bytes")
        if func.endswith("open") and any("w" in a for a in args):
            writes = True
        if writes:
            targets.extend(args)
    return targets


def test_plan_md_deletion_writer_is_guarded(project, monkeypatch):
    """Plan `writers:`: `lime/workflows/feature.py` deletes plan.md on step-attempt
    exhaustion. An AST guard on the writer plus the behavioural exhaustion run."""
    import ast

    tree = ast.parse(Path(feature.__file__).read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_invalidate_for_replan"
    )
    unlinks = [n for n in ast.walk(fn)
               if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(".unlink")]
    assert unlinks, "feature.py must still delete the stale plan.md"
    assert any("plan.md" in ast.unparse(n.func) for n in unlinks)

    # behavioural: exhausting a step's build attempts deletes plan.md
    run = make_run(project, {"test": "echo boom && exit 1"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [(build_reply(), {})] * feature.MAX_BUILD_ATTEMPTS)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    assert feature.run(run, TICKET, "main") == 1
    assert not (run.context_handoff_dir / "plan.md").exists()


def test_context_md_has_no_code_writer():
    """Plan Step 1 `writers:`: CONTEXT.md has no code writer. AST guard over the whole
    package — no module opens or writes it; only the documenter agent edits it."""
    root = Path(feature.__file__).resolve().parents[1]  # lime/
    offenders = [
        str(p.relative_to(root))
        for p in sorted(root.rglob("*.py"))
        if any("CONTEXT.md" in target for target in _ast_write_targets(p.read_text()))
    ]
    assert offenders == []


def test_documenter_edits_context_md_after_review_on_the_builder_bytes(project, monkeypatch):
    """Plan Step 1 `writers:`: the documenter phase edits CONTEXT.md after review,
    sequentially, on the bytes the build steps left — no code resets it."""
    run = make_run(project, {"test": "exit 0"})
    (project / "CONTEXT.md").write_text("builder glossary\n")
    (project / "app.py").write_text("v2\n")
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")

    assert feature.run(run, TICKET, "main") == 0
    names = [p.params.name for p in run.phases]
    assert names.index("review") < names.index("document")
    assert (project / "CONTEXT.md").read_text() == "builder glossary\n"


def test_resume_after_exhaustion_replans_from_the_edited_ticket(project, monkeypatch):
    from lime import agents, git_helper, session

    run = make_run(project, {"test": "echo boom && exit 1"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [(build_reply(), {})] * feature.MAX_BUILD_ATTEMPTS)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    code = feature.run(run, TICKET, "main")
    assert code == 1

    edited = TICKET.model_copy(update={"ask": "Write farewell() in app.py."})

    run.tracer.set_run_status("R1", "running")
    run2 = make_run(project, {"test": "exit 0"})
    run2.replay = run2.tracer.replay_map("R1")

    adapter2 = make_agent_stub(
        run2,
        "## Repo facts\nnone\n\n## Step 1\nAdd farewell() to app.py.\n"
        "failure: pure\nwriters: none\n",
        [build_reply()] + APPROVED_TAIL,
        scout=False,
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter2)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v3\n")

    code2 = feature.run(run2, edited, "main")

    assert len(adapter2.calls) > 0 and "farewell" in adapter2.calls[0].prompt
    names2 = [p.params.name for p in run2.phases]
    assert "plan" in names2
    assert "engineer" not in names2
    assert "setup" not in names2
    assert "scout" not in names2
    assert "farewell" in (run2.context_handoff_dir / "plan.md").read_text()
    assert code2 == 0


def test_plan_without_a_step_heading_raises(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    # The planner's correction answers with the same step-less plan, so the plan gate
    # exhausts and the chain stops before any build (ticket 132: negative plan path).
    adapter = make_agent_stub(
        run,
        "## Repo facts\nnone, no steps here\n",
        [{"status": "success", "summary": "planned", "artifacts": []}],
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    with pytest.raises(RuntimeError, match="no `## Step N`"):
        feature.run(run, TICKET, "main")


def test_multiple_steps_each_get_their_own_build_and_verify_phase(project, monkeypatch):
    plan_md = (
        "## Repo facts\nnone\n\n## Step 1\nFirst.\nfailure: pure\nwriters: none\n\n"
        "## Step 2\nSecond.\nfailure: pure\nwriters: none\n"
    )
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(run, plan_md, [build_reply(), build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, TICKET, "main")

    assert code == 0
    names = [p.params.name for p in run.phases]
    assert names == [
        "engineer", "setup", "scout", "plan",
        "build_1", "verify_1", "build_2", "verify_2",
        "review", "commit_code", "changes", "document", "commit_docs", "push_pr",
    ]

    # Inspect builder requests
    # adapter.calls: [scout, plan, build_1, build_2, review, document, ...]
    assert len(adapter.calls) >= 5
    build1_call = adapter.calls[2]  # after scout (0) and plan (1)
    build2_call = adapter.calls[3]  # build_2

    # Step 1's opening prompt has NO fresh-session block
    assert "This is a fresh session" not in build1_call.prompt
    assert "## Git facts" not in build1_call.prompt
    assert "Read the plan at" in build1_call.prompt
    assert "First." in build1_call.prompt

    # Step 2's prompt HAS the fresh-session block and Git facts
    assert "This is a fresh session" in build2_call.prompt
    assert "## Git facts" in build2_call.prompt
    assert "Read the plan at" in build2_call.prompt
    assert "Second." in build2_call.prompt

    # Step 2 has new session_id and resume=False
    assert build2_call.session_id != build1_call.session_id
    assert build2_call.resume is False


# --- review / revise ---


def test_a_rejected_review_revises_then_re_reviews_and_proceeds(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(
        run, PLAN_ONE_STEP,
        [
            build_reply(),
            (review_reply(approved=False, blocking=["missing greet()"]), {}),
            build_reply(commit_message="Actually add greet()"),  # the revise
            (review_reply(approved=True), {}),
        ]
        + APPROVED_TAIL[1:],  # document only — review already happened
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, TICKET, "main")

    assert code == 0
    names = [p.params.name for p in run.phases]
    assert names == [
        "engineer", "setup", "scout", "plan", "build_1", "verify_1",
        "review", "revise_1", "verify_1_retry3", "review_retry1",
        "commit_code", "changes", "document", "commit_docs", "push_pr",
    ]
    log = subprocess.run(
        ["git", "-C", str(project), "log", "--pretty=%B", "-1", "HEAD~1"],  # the code commit
        capture_output=True, text=True,
    ).stdout
    assert "Actually add greet()" in log  # the revise's fix, not the original (rejected) build

    # Inspect builder requests: adaptor.calls = [scout, plan, build_1, review, revise_1, review_retry1, document]
    assert len(adapter.calls) >= 6
    build_call = adapter.calls[2]   # build_1
    revise_call = adapter.calls[4]   # revise_1

    # Revise has new ID and resume=False relative to build
    assert revise_call.session_id != build_call.session_id
    assert revise_call.resume is False

    # Revise prompt has fresh-session line and Git facts
    assert "This is a fresh session" in revise_call.prompt
    assert "## Git facts" in revise_call.prompt

    # Build prompt lacks fresh-session block (step 1 first attempt)
    assert "This is a fresh session" not in build_call.prompt
    assert "## Git facts" not in build_call.prompt
    assert_table_stages(run.tracer.con, run.run_id)


# --- greenfield (Spec:) and front-end (Prototype:) wiring ---


GREENFIELD_TICKET = TICKET.model_copy(update={"spec": ".lime/specs/foundation.md"})

FRONTEND_TICKET = TICKET.model_copy(update={"prototype": "app/proto-toy"})


def test_greenfield_spec_field_reaches_the_planner_prompt(project, monkeypatch):
    """Greenfield = feature + `Spec:` — tracker.prompt() already threads it in;
    no chain code differs, so the only thing to prove is it reaches plan/review."""
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, GREENFIELD_TICKET, "main")

    assert code == 0
    plan_call = adapter.calls[1]  # calls[0] is the scout
    assert "Spec: .lime/specs/foundation.md" in plan_call.prompt


def test_frontend_prototype_field_tells_the_planner_to_read_notes_then_the_route(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    feature.run(run, FRONTEND_TICKET, "main")

    plan_call = adapter.calls[1]  # calls[0] is the scout
    assert "Prototype: app/proto-toy" in plan_call.prompt
    assert "app/proto-toy/NOTES.md" in plan_call.prompt
    assert "then the route" in plan_call.prompt


def test_frontend_docs_commit_deletes_the_consumed_route_dir(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")
    route_dir = project / "app" / "proto-toy"
    route_dir.mkdir(parents=True)
    (route_dir / "page.tsx").write_text("proto\n")
    (route_dir / "NOTES.md").write_text("fixed: layout\n")

    code = feature.run(run, FRONTEND_TICKET, "main")

    assert code == 0
    assert not route_dir.exists()
    names = [p.params.name for p in run.phases]
    assert names == [
        "engineer", "setup", "scout", "plan", "build_1", "verify_1", "review",
        "commit_code", "changes", "document", "commit_docs", "push_pr",
    ]


def test_feature_ticket_without_a_prototype_never_touches_route_dir(project, monkeypatch):
    """Plain feature/bug-fix/greenfield tickets pass `route_dir=None` through — nothing
    to delete, nothing to break."""
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, TICKET, "main")

    assert code == 0


def test_frontend_file_prototype_finishes_and_is_kept(project, monkeypatch):
    """A `Prototype:` that names a file survives close: the chain finishes, the ticket
    is deleted, the file stays, and the planner is sent to the sibling notes."""
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    pushed = []
    monkeypatch.setattr(git_helper, "push", lambda branch: pushed.append(branch))
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")
    proto = project / "app" / "proto-toy" / "tokens.css"
    proto.parent.mkdir(parents=True)
    proto.write_text(":root {}\n")
    (proto.parent / "NOTES.md").write_text("fixed: layout\n")
    ticket = TICKET.model_copy(update={"prototype": "app/proto-toy/tokens.css"})

    code = feature.run(run, ticket, "main")

    assert code == 0
    names = [p.params.name for p in run.phases]
    assert names[-2:] == ["commit_docs", "push_pr"]
    assert all(p.status == "success" for p in run.phases)
    assert pushed and len(pushed) == 1  # push_pr ran (push mocked)
    assert not (project / ".lime" / "tickets" / "41-toy.md").exists()  # delete-on-close
    assert proto.read_bytes() == b":root {}\n"
    plan_call = adapter.calls[1]  # calls[0] is the scout
    assert "Prototype: app/proto-toy/tokens.css" in plan_call.prompt
    assert "Read app/proto-toy/NOTES.md first" in plan_call.prompt
    assert "tokens.css/NOTES.md" not in plan_call.prompt


def test_trust_violations_still_present_after_max_revises_fails_the_run_unpushed(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    violation_review = (
        review_reply(approved=False, trust_violations=["app.py:1 — hard-codes a secret"]), {}
    )
    adapter = make_agent_stub(
        run, PLAN_ONE_STEP,
        [
            build_reply(),
            violation_review,
            build_reply(commit_message="revise 1"),
            violation_review,
            build_reply(commit_message="revise 2"),
            violation_review,
        ],
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    pushed = []
    monkeypatch.setattr(git_helper, "push", lambda branch: pushed.append(branch))
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, TICKET, "main")

    assert code == 1
    assert pushed == []
    names = [p.params.name for p in run.phases]
    assert names == [
        "engineer", "setup", "scout", "plan", "build_1", "verify_1",
        "review", "revise_1", "verify_1_retry3", "review_retry1",
        "revise_2", "verify_1_retry4", "review_retry2",
    ]
    assert "commit_code" not in names
    assert (project / ".lime" / "tickets" / "41-toy.md").exists()  # nothing committed, ticket survives
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "fail"


def test_scout_briefs_the_planner_before_plan_with_the_bare_ticket(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, TICKET, "main")

    assert code == 0
    names = [p.params.name for p in run.phases]
    assert names.index("scout") == names.index("plan") - 1
    scout_call, plan_call = adapter.calls[0], adapter.calls[1]
    assert "# Scout Agent" in scout_call.system_prompt
    assert "# Planner Agent" in plan_call.system_prompt
    assert feature.SCOUT_LEAD in scout_call.prompt
    assert TICKET.ask in scout_call.prompt
    assert (run.context_handoff_dir / "scout_findings.md").exists()
    # a successful scout's brief is never touched by code — the writer guard's other half
    scout_phase = next(p for p in run.phases if p.params.name == "scout")
    assert scout_phase.status == "success"
    assert (run.context_handoff_dir / "scout_findings.md").read_text() == "# brief\napp.py — greet() lives here\n"


# --- no layout phase (ticket 192) ---

DASHBOARD_TICKET = TICKET.model_copy(update={
    "where": "- `dashboard/src/components/CostPage.vue` — the chart",
    "done_means": "`#/cost` renders variant C without overlap.",
})


def test_dashboard_ticket_runs_the_plain_chain_without_a_layout_phase(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, DASHBOARD_TICKET, "main")

    assert code == 0
    names = [p.params.name for p in run.phases]
    assert names == [
        "engineer", "setup", "scout", "plan", "build_1", "verify_1",
        "review", "commit_code", "changes", "document", "commit_docs", "push_pr",
    ]
    review_call = adapter.calls[3]  # 0=scout, 1=plan, 2=build, 3=review
    # The reviewer's user.md template still names `## Layout` until ticket 192 Step 3;
    # what Step 1 drops is the injected block `_review_context` splices in.
    assert "## Layout\nscreenshots:" not in review_call.prompt
    assert review_call.attachments == []


def test_no_workflow_calls_the_layout_phase():
    """AST guard (ticket 192): no workflow module calls `phases.layout` or passes a
    `layout=` keyword to a `ReviewStep`."""
    import ast

    root = Path(feature.__file__).parent
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            assert not (
                isinstance(func, ast.Attribute)
                and func.attr == "layout"
                and isinstance(func.value, ast.Name)
                and func.value.id == "phases"
            ), f"{path.name} calls phases.layout()"
            if isinstance(func, ast.Name) and func.id == "ReviewStep":
                assert not any(kw.arg == "layout" for kw in node.keywords), (
                    f"{path.name} passes layout= to ReviewStep"
                )


def test_resume_of_a_pre_192_run_skips_its_recorded_layout_phase(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    plan_path = run.context_handoff_dir / "plan.md"
    brief_path = run.context_handoff_dir / "scout_findings.md"
    calls = []
    state = {"interrupt": True}

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        calls.append(req)
        role = _role(req)
        if role == "Scout":
            brief_path.write_text("# brief\n")
            return _reply(req, {"status": "success", "summary": "scouted",
                                "findings": [{"file": "app.py", "note": "x"}], "artifacts": [str(brief_path)]})
        if role == "Planner":
            plan_path.write_text(PLAN_ONE_STEP)
            return _reply(req, {"status": "success", "summary": "planned", "artifacts": [str(plan_path)]})
        if role == "Builder":
            return _reply(req, build_reply())
        if role == "Reviewer":
            return _reply(req, review_reply())
        if role == "Documenter":
            if state["interrupt"]:
                state["interrupt"] = False
                raise RuntimeError("interrupted")
            return _reply(req, document_reply())
        raise AssertionError(role)

    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    (project / "app.py").write_text("v2\n")  # the builder's change, committed before the document
    with pytest.raises(RuntimeError, match="interrupted"):
        feature.run(run, DASHBOARD_TICKET, "main")

    # What a pre-192 Run left behind: a `layout` phase row + `LayoutOutput` envelope,
    # and a recorded `ReviewOutput` carrying a `layout` verdict.
    pid = run.tracer.add_phase("R1", seq=99, name="layout", stage="Build", kind="code", owner="git")
    run.tracer.set_phase_status(pid, "running")
    run.tracer.set_phase_status(pid, "success")
    run.tracer.record_envelope(
        "R1", pid, agent="git", output_type="LayoutOutput",
        payload_json=json.dumps({"status": "success", "passed": True, "text": "passed: true\n",
                                 "screenshots": ["/r/visual/cost.png"], "problems": []}),
        valid=True, attempt=1,
    )
    run.tracer.con.execute(
        "UPDATE envelopes SET payload_json = json_set(payload_json, '$.layout', 'pass')"
        " WHERE run_id='R1' AND output_type='ReviewOutput'"
    )
    run.tracer.con.commit()

    calls2 = []

    def adapter2(req, on_event=None, on_spawn=None, on_exit=None):
        calls2.append(req)
        role = _role(req)
        if role == "Documenter":
            return _reply(req, document_reply())
        raise AssertionError(f"{role} must replay, not re-run")

    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter2)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")

    run2 = make_run(project, {"test": "exit 0"})
    run2.tracer.set_run_status("R1", "running")
    run2.replay = run2.tracer.replay_map("R1")
    assert "layout" in run2.replay, "the old layout row still loads"
    assert feature.run(run2, DASHBOARD_TICKET, "main") == 0

    names = [p.params.name for p in run2.phases]
    assert not any(n.startswith("layout") for n in names), "no layout phase re-runs"
    assert "review" not in names, "the approved review replays"
    assert all(_role(c) != "Reviewer" for c in calls2)


# --- diff gate (ticket 122) ---


@pytest.mark.parametrize("added", [2217, 2994])  # the recorded ERP counts (ticket 130)
def test_over_cap_run_goes_on_through_review_docs_and_pr(project, monkeypatch, added):
    """An over-budget verify is measured and logged but never stops the chain: the run
    still reviews, commits, documents and pushes (ticket 193)."""
    run = session.ensure(_cfg_with_cap({"test": "exit 0"}, 2000), "R1", runs_dir=project / "runs")
    adapter = make_agent_stub(
        run, PLAN_ONE_STEP,
        [(build_reply(changed_files=["big.py"]), {"big.py": _big_file_text(added)})] + APPROVED_TAIL,
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    pushed = []
    monkeypatch.setattr(git_helper, "push", lambda branch: pushed.append(branch))
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")

    code = feature.run(run, TICKET, "main")

    assert code == 0
    assert pushed
    names = [p.params.name for p in run.phases]
    assert names == [
        "engineer", "setup", "scout", "plan", "build_1", "verify_1", "review",
        "commit_code", "changes", "document", "commit_docs", "push_pr",
    ]
    assert run.tracer.con.execute(
        "SELECT status FROM runs WHERE run_id='R1'"
    ).fetchone()["status"] == "success"
    assert not (run.context_handoff_dir / "needs_rescope.json").exists()
    assert run.tracer.con.execute(
        "SELECT 1 FROM events WHERE run_id='R1' AND type='handoff' AND name='needs_rescope'"
    ).fetchone() is None
    log = next(p for p in _log_payloads(run, "verify_1") if "total_added" in p)
    assert log["total_added"] == added
    assert log["max_added_lines"] == 2000


def test_exact_diff_cap_can_complete_normally(project, monkeypatch):
    """Exactly at the cap (strict `>`) still ships — measured equality is a pass."""
    run = session.ensure(_cfg_with_cap({"test": "exit 0"}, 2000), "R1", runs_dir=project / "runs")
    adapter = make_agent_stub(
        run, PLAN_ONE_STEP,
        [(build_reply(changed_files=["big.py"]), {"big.py": _big_file_text(2000)})] + APPROVED_TAIL,
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")

    code = feature.run(run, TICKET, "main")

    assert code == 0
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "success"
    assert not (run.context_handoff_dir / "needs_rescope.json").exists()


def test_diff_gate_exhaustion_never_reviews_commits_or_pushes(project, monkeypatch):
    """The builder never removes the oversized artefact: verify stays red after all
    attempts, the run ends unaccepted, no review / commit / push ever runs."""
    from lime import quality

    monkeypatch.setattr(
        "lime.quality.run_check",
        lambda run_, name: (
            None if name != "test"
            else quality._run(quality.QualityCheckSpec(name="test", command="exit 0"), run_)
        ),
    )

    gate_calls = {"n": 0}

    def fake_diff_stats(base, exclude_patterns=None):
        gate_calls["n"] += 1
        return [{"path": "out.bin", "added_lines": 0, "binary": True, "deleted": False}]

    monkeypatch.setattr(git_helper, "diff_stats", fake_diff_stats)

    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] * feature.MAX_BUILD_ATTEMPTS)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    pushed = []
    monkeypatch.setattr(git_helper, "push", lambda branch: pushed.append(branch))
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, TICKET, "main")

    assert code == 1
    assert pushed == []
    names = [p.params.name for p in run.phases]
    assert "review" not in names
    assert "commit_code" not in names
    assert "push_pr" not in names
    build_names = [n for n in names if n.startswith("build_")]
    assert len(build_names) == feature.MAX_BUILD_ATTEMPTS
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "fail"


def test_verify_receives_nondefault_base_branch(project, monkeypatch):
    """feature.run passes its base_branch into every verify, not the default HEAD."""
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")

    seen_bases = {}
    orig_merge_base = git_helper.merge_base

    def fake_merge_base(ref, other="HEAD"):
        seen_bases[ref] = other
        return orig_merge_base(ref, other)

    monkeypatch.setattr(git_helper, "merge_base", fake_merge_base)
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, TICKET, "main")

    assert code == 0
    assert "main" in seen_bases, "verify must resolve via merge_base(base_branch, HEAD)"


def test_over_cap_revise_still_rereviews_and_ships(project, monkeypatch):
    """First build passes; the reviewer requests a revise; the revise's re-verify is
    over budget — the size is logged and the chain still re-reviews and ships (ticket 193)."""
    run = make_run(project, {"test": "exit 0"})
    stats_count = {"n": 0}
    orig_stats = git_helper.diff_stats

    def fake_stats(base, exclude_patterns=None):
        stats_count["n"] += 1
        # call 1 is verify_1; call 2 is the re-verify after revise_1
        if stats_count["n"] == 2:
            return [
                {"path": "app.py", "added_lines": 1, "binary": False, "deleted": False},
                {"path": "measurements.tsv", "added_lines": 2000, "binary": False, "deleted": False},
            ]
        return orig_stats(base, exclude_patterns)

    monkeypatch.setattr(git_helper, "diff_stats", fake_stats)

    adapter = make_agent_stub(run, PLAN_ONE_STEP, [
        build_reply(),
        (review_reply(approved=False, blocking=["rename greet()"]), {}),
        (build_reply(commit_message="revise 1"), {}),
        (review_reply(), {}),
        (document_reply(), {}),
    ])
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, TICKET, "main")

    assert code == 0
    names = [p.params.name for p in run.phases]
    assert names == [
        "engineer", "setup", "scout", "plan", "build_1", "verify_1",
        "review", "revise_1", "verify_1_retry3", "review_retry1",
        "commit_code", "changes", "document", "commit_docs", "push_pr",
    ]
    assert run.tracer.con.execute(
        "SELECT status FROM runs WHERE run_id='R1'"
    ).fetchone()["status"] == "success"
    log = next(p for p in _log_payloads(run, "verify_1_retry3") if "total_added" in p)
    assert log["total_added"] == 2001


def test_non_size_revision_failure_still_retries(project, monkeypatch):
    """A non-size failure after a revise (here: a binary) still retries ordinarily —
    it never becomes `needs_rescope` by message matching."""
    run = make_run(project, {"test": "exit 0"})
    stats_count = {"n": 0}
    orig_stats = git_helper.diff_stats

    def fake_stats(base, exclude_patterns=None):
        stats_count["n"] += 1
        n = stats_count["n"]
        # call 1 is verify_1; call 2 is the re-verify after revise_1 -> the binary
        # ordinary failure lands here.
        if n == 2:
            return [
                {"path": "app.py", "added_lines": 1, "binary": False, "deleted": False},
                {"path": "asset.bin", "added_lines": 0, "binary": True, "deleted": False},
            ]
        return orig_stats(base, exclude_patterns)

    monkeypatch.setattr(git_helper, "diff_stats", fake_stats)

    adapter = make_agent_stub(run, PLAN_ONE_STEP, [
        build_reply(),
        (review_reply(approved=False, blocking=["rename greet()"]), {}),
        (build_reply(commit_message="revise 1"), {}),
        (build_reply(commit_message="revise 2"), {}),
        (review_reply(approved=True), {}),
    ] + APPROVED_TAIL[1:])
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, TICKET, "main")

    assert code == 0, f"expected success, got phases {[p.params.name for p in run.phases]}"
    names = [p.params.name for p in run.phases]
    # After the revise, verify runs before re-review
    assert "verify_1_retry3" in names, "re-verify after revise must run"
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "success"
    assert names == [
        "engineer", "setup", "scout", "plan", "build_1", "verify_1",
        "review", "revise_1", "verify_1_retry3", "revise_2", "verify_1_retry4",
        "review_retry2",
        "commit_code", "changes", "document", "commit_docs", "push_pr",
    ]


# --- ticket 132: early accounting, advisory feedback and provenance resume ---


def _install_quality_spy(monkeypatch):
    """Count quality-block invocations and run no real subprocess."""
    from lime import quality as qmod

    calls = []
    real = qmod.run_quality

    def spy(run_):
        calls.append(1)
        return real(run_)

    monkeypatch.setattr(qmod, "run_quality", spy)
    monkeypatch.setattr(qmod, "run_check", lambda run_, name: None)
    return calls


def test_cumulative_growth_over_cap_runs_every_step_and_ships(project, monkeypatch):
    """Two builds whose cumulative added lines exceed the cap still run every step and
    ship; verify_2 logs the cumulative total (ticket 193)."""
    plan_md = (
        "## Repo facts\nnone\n\n## Step 1\nSmall.\nfailure: pure\nwriters: none\n\n"
        "## Step 2\nMore.\nfailure: pure\nwriters: none\n"
    )
    run = session.ensure(_cfg_with_cap({"test": "exit 0"}, 100), "R1", runs_dir=project / "runs")
    adapter = make_agent_stub(run, plan_md, [
        (build_reply(changed_files=["a.py"]), {"a.py": _big_file_text(60)}),
        (build_reply(changed_files=["b.py"]), {"b.py": _big_file_text(60)}),
    ] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    quality_calls = _install_quality_spy(monkeypatch)
    pushed = []
    monkeypatch.setattr(git_helper, "push", lambda branch: pushed.append(branch))
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")

    code = feature.run(run, TICKET, "main")

    assert code == 0
    assert len(quality_calls) == 2
    names = [p.params.name for p in run.phases]
    assert names == [
        "engineer", "setup", "scout", "plan", "build_1", "verify_1", "build_2", "verify_2",
        "review", "commit_code", "changes", "document", "commit_docs", "push_pr",
    ]
    log = next(p for p in _log_payloads(run, "verify_2") if "total_added" in p)
    assert log["total_added"] == 120
    assert log["max_added_lines"] == 100
    assert run.tracer.con.execute(
        "SELECT status FROM runs WHERE run_id='R1'"
    ).fetchone()["status"] == "success"


def test_the_size_stop_machinery_is_gone():
    from lime import data_types, diff_accounting
    from lime.workflows import phases as phases_mod

    assert not hasattr(feature, "_stop_for_diff_cap_overflow")
    assert not hasattr(feature, "_early_stop")
    assert not hasattr(feature, "_completed_work_reports")
    assert not hasattr(feature, "NEEDS_RESCOPE_SCHEMA_VERSION")
    assert not hasattr(phases_mod, "early_diff_check")
    assert not hasattr(diff_accounting, "advisory_for")
    assert not hasattr(diff_accounting, "_overflow_failure")
    assert not hasattr(diff_accounting, "_advisory_warning")
    assert not hasattr(data_types, "DiffCapOverflow")


def test_near_budget_build_sends_no_advisory_to_the_next_builder(project, monkeypatch):
    plan_md = (
        "## Repo facts\nnone\n\n## Step 1\nSmall.\nfailure: pure\nwriters: none\n\n"
        "## Step 2\nMore.\nfailure: pure\nwriters: none\n"
    )
    run = session.ensure(_cfg_with_cap({"test": "exit 0"}, 50), "R1", runs_dir=project / "runs")
    adapter = make_agent_stub(run, plan_md, [
        (build_reply(changed_files=["a.py"]), {"a.py": _big_file_text(40)}),
        (build_reply(changed_files=["b.py"]), {"b.py": _big_file_text(5)}),
    ] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")

    code = feature.run(run, TICKET, "main")

    assert code == 0
    # calls: 0 scout, 1 plan, 2 build_1, 3 build_2 — no advisory reaches build_2
    build2_prompt = adapter.calls[3].prompt
    assert "advisory" not in build2_prompt.lower()
    assert "does not lower the cap" not in build2_prompt
    # no retry happened for the advisory
    assert "build_1_retry1" not in [p.params.name for p in run.phases]


def test_resume_failed_rerun_of_the_latest_verify_drops_the_approved_review(project, monkeypatch):
    """A failed re-run of the latest verify invalidates the stale verify and its
    dependent approved review, so the chain cannot replay the old approval."""
    run = make_run(project, {"test": "exit 0"})
    assert feature.phases.verify(run, 1, base="HEAD").passed

    # A recorded, approved review sits downstream of the passing verify.
    seq = run.tracer.max_phase_seq("R1") + 1
    review_id = run.tracer.add_phase("R1", seq=seq, name="review", stage="Build", kind="agent", owner="reviewer")
    run.tracer.set_phase_status(review_id, "running")
    run.tracer.set_phase_status(review_id, "success")
    run.tracer.record_envelope(
        "R1", review_id, agent="reviewer", output_type="ReviewOutput",
        payload_json=json.dumps({"status": "success", "approved": True}), valid=True, attempt=1,
    )

    (project / "app.py").write_text("v2\n")
    run2 = make_run(project, {"test": "exit 1"})  # the re-run will fail
    run2.replay = run2.tracer.replay_map("R1")
    assert "review" in run2.replay and "verify_1" in run2.replay

    assert not feature.phases.verify(run2, 1, base="HEAD").passed

    assert "review" not in run2.replay, "a failed re-run must drop the approved review"
    assert run2.phases[-1].params.name == "verify_1"



# --- pinned birth identity through the whole chain (ticket 144) ---

BUG_TICKET = Ticket(
    path=".lime/tickets/65-bug.md", number=65, slug="65-bug", title="Bug Ticket",
    workflow="bug-fix", ask="Fix the bug.", done_means="commands.test is green.",
)


def test_pinned_run_diff_uses_the_pin_but_pr_target_stays_main(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    run.base_sha = git_helper.rev("HEAD")
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    def _no_merge_base(*args, **kwargs):
        raise AssertionError("a pinned Run must not call the ordinary merge base")

    monkeypatch.setattr(git_helper, "merge_base", _no_merge_base)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    opened = {}
    monkeypatch.setattr(
        git_helper, "open_pr",
        lambda title, body, base: opened.setdefault("base", base) or "url",
    )
    (project / "app.py").write_text("v2\n")

    assert feature.run(run, TICKET, "main") == 0
    assert opened["base"] == "main"  # the PR target name, not the pin


def test_pinned_run_pin_reaches_verify_review_and_changes(project, monkeypatch):
    from lime.workflows import phases as phases_mod

    run = make_run(project, {"test": "exit 0"})
    pin = git_helper.rev("HEAD")
    run.base_sha = pin
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [
        build_reply(),
        (review_reply(approved=False, blocking=["rename greet()"]), {}),
        (build_reply(commit_message="revise 1"), {}),
        (review_reply(), {}),
        (document_reply(), {}),
    ])
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "url")

    bases = []
    orig_verify = phases_mod.verify
    orig_review = phases_mod.review
    orig_changes = phases_mod.changes
    monkeypatch.setattr(
        phases_mod, "verify",
        lambda run_, step_no, *, attempt=1, base="HEAD": (
            bases.append(("verify", base)) or orig_verify(run_, step_no, attempt=attempt, base=base)
        ),
    )
    monkeypatch.setattr(
        phases_mod, "review",
        lambda run_, step: bases.append(("review", step.base)) or orig_review(run_, step),
    )
    monkeypatch.setattr(
        phases_mod, "changes",
        lambda run_, base: bases.append(("changes", base)) or orig_changes(run_, base),
    )
    (project / "app.py").write_text("v2\n")

    assert feature.run(run, TICKET, "main") == 0
    kinds = [kind for kind, _ in bases]
    assert kinds.count("verify") == 2  # build verify and re-verify after revise
    assert "review" in kinds and "changes" in kinds
    assert all(base == pin for _, base in bases), bases


def test_bug_fix_delegation_keeps_the_same_pin(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    pin = git_helper.rev("HEAD")
    run.base_sha = pin
    (project / ".lime" / "tickets" / "65-bug.md").write_text("# 65\n")
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    opened = {}
    monkeypatch.setattr(
        git_helper, "open_pr",
        lambda title, body, base: opened.setdefault("base", base) or "url",
    )

    def _no_merge_base(*args, **kwargs):
        raise AssertionError("bug-fix delegation must keep the pin")

    monkeypatch.setattr(git_helper, "merge_base", _no_merge_base)
    (project / "app.py").write_text("v2\n")

    assert feature.run(run, BUG_TICKET, "main") == 0
    assert opened["base"] == "main"
    assert run.base_sha == pin


def test_pinned_2379_over_1500_ships_and_logs_the_count(project, monkeypatch):
    run = session.ensure(_cfg_with_cap({"test": "exit 0"}, 1500), "R1", runs_dir=project / "runs")
    run.base_sha = git_helper.rev("HEAD")
    adapter = make_agent_stub(
        run, PLAN_ONE_STEP,
        [(build_reply(changed_files=["big.py"]), {"big.py": _big_file_text(2379)})] + APPROVED_TAIL,
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")

    assert feature.run(run, TICKET, "main") == 0
    assert run.tracer.con.execute(
        "SELECT status FROM runs WHERE run_id='R1'"
    ).fetchone()["status"] == "success"
    log = next(p for p in _log_payloads(run, "verify_1") if "total_added" in p)
    assert log["total_added"] == 2379
    assert log["max_added_lines"] == 1500
    assert log["merge_base"] == git_helper.short_sha(run.base_sha)
    assert not (run.context_handoff_dir / "needs_rescope.json").exists()


def test_history_rewritten_during_the_run_stops_the_chain_fail(project, monkeypatch, capsys):
    from lime.workflows import phases as phases_mod

    run = make_run(project, {"test": "exit 0"})
    run.base_sha = git_helper.rev("HEAD")
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    orig_build = phases_mod.build

    def building(run_, step):
        output = orig_build(run_, step)
        # a stub builder that rewrites history: amend HEAD so the pin is orphaned
        subprocess.run(
            ["git", "-C", str(project), "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "--allow-empty", "--amend", "-m", "rewritten by the stub builder"],
            capture_output=True, check=True,
        )
        return output

    monkeypatch.setattr(phases_mod, "build", building)
    side_effects = []
    monkeypatch.setattr(git_helper, "push", lambda branch: side_effects.append(branch))
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: side_effects.append("pr"))

    assert feature.run(run, TICKET, "main") == 1

    # finished fail, immediately
    row = run.tracer.con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "fail"
    # the required message is in the trace and on the console
    error = run.tracer.con.execute(
        "SELECT payload_json FROM events WHERE run_id='R1' AND type='error' AND name='invalid_run_base'"
    ).fetchone()
    assert error is not None and "rebased/merged past its base" in error["payload_json"]
    assert "rebased/merged past its base" in capsys.readouterr().out
    # no second build/revise/review/commit/document/push
    names = [p.params.name for p in run.phases]
    assert names == ["engineer", "setup", "scout", "plan", "build_1"]
    assert side_effects == []
    # no overflow handoff and no plan invalidation
    assert not (run.context_handoff_dir / "needs_rescope.json").exists()
    assert run.tracer.con.execute(
        "SELECT 1 FROM events WHERE run_id='R1' AND type='log' AND name='phase_invalidated'"
    ).fetchone() is None
    assert (run.context_handoff_dir / "plan.md").exists()


# --- ticket 129: limits-aware planning contract and feasibility scenarios ---


def _role(req) -> str:
    for role in ("Scout", "Planner", "Builder", "Reviewer", "Documenter"):
        if f"# {role} Agent" in (req.system_prompt or ""):
            return role
    return "?"


def _reply(req, envelope):
    return AgentResult(
        status="ok", text="", session_id=req.session_id, usage=usage(),
        cost=0.01, context_tokens=150, context_window=100_000, envelope=envelope,
    )


def test_build_prompt_carries_exactly_one_limits_line(project, monkeypatch):
    from lime import limits

    run = make_run(project, {})
    run.repo_root = project
    monkeypatch.setattr(feature.budget, "git_facts", lambda *a: "SENTINEL_FACTS")
    expected = limits.builder_line(run.agent("builder"), run.cfg)
    for step_no, retry in [(1, False), (2, False), (1, True), (2, True)]:
        prompt = feature._build_prompt(run, step_no, f"## Step {step_no}\nDo.\n", retry=retry)
        assert prompt.count("Builder limits:") == 1
        assert expected in prompt
        assert "max_added_lines" not in prompt
        assert "uncounted=" not in prompt


def test_only_the_planner_sees_the_added_lines_budget(project, monkeypatch):
    """The planner's limits line carries the sizing budget; the builder's never does."""
    run = session.ensure(_cfg_with_cap({"test": "exit 0"}, 777), "R1", runs_dir=project / "runs")
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    assert feature.run(run, TICKET, "main") == 0
    assert "max_added_lines=777" in adapter.calls[1].prompt  # the planner
    assert adapter.calls[2].prompt.count("Builder limits:") == 1  # build_1
    assert "max_added_lines" not in adapter.calls[2].prompt


def test_multistep_resume_with_unchanged_limits_replays_completed_build(project, monkeypatch):
    plan_md = (
        "## Repo facts\nnone\n\n## Step 1\nFirst.\nfailure: pure\nwriters: none\n\n"
        "## Step 2\nSecond.\nfailure: pure\nwriters: none\n"
    )
    run = make_run(project, {"test": "exit 0"})
    plan_path = run.context_handoff_dir / "plan.md"
    brief_path = run.context_handoff_dir / "scout_findings.md"
    calls = []
    interrupt = {"build2": True}

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        calls.append(req)
        role = _role(req)
        if role == "Scout":
            brief_path.write_text("# brief\n")
            return _reply(req, {"status": "success", "summary": "scouted",
                                "findings": [{"file": "app.py", "note": "x"}], "artifacts": [str(brief_path)]})
        if role == "Planner":
            plan_path.write_text(plan_md)
            return _reply(req, {"status": "success", "summary": "planned", "artifacts": [str(plan_path)]})
        if role == "Builder":
            if "Second." in req.prompt and interrupt["build2"]:
                interrupt["build2"] = False
                raise RuntimeError("interrupted during step 2")
            return _reply(req, build_reply())
        if role == "Reviewer":
            return _reply(req, review_reply())
        if role == "Documenter":
            return _reply(req, document_reply())
        raise AssertionError(role)

    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    with pytest.raises(RuntimeError, match="interrupted during step 2"):
        feature.run(run, TICKET, "main")

    run2 = make_run(project, {"test": "exit 0"})
    run2.tracer.set_run_status("R1", "running")
    run2.replay = run2.tracer.replay_map("R1")
    assert {"plan", "build_1", "verify_1"} <= set(run2.replay)

    # Live token usage must not change the plan identity.
    plan_phase = run2.replay["plan"]["phase_id"]
    run2.tracer.event("R1", "turn", phase_id=plan_phase, name="turn",
                      payload={"tokens": 999_999, "cost": 42.0})
    run2.replay = run2.tracer.replay_map("R1")
    assert "plan" in run2.replay

    calls_before = len(calls)
    assert feature.run(run2, TICKET, "main") == 0
    new_roles = [_role(c) for c in calls[calls_before:]]
    assert "Planner" not in new_roles, "unchanged limits must replay the plan"
    assert new_roles.count("Builder") == 1, "only step 2's unfinished build re-runs"


def test_missing_auth_plan_fail_stops_before_build(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    plan_path = run.context_handoff_dir / "plan.md"
    plan_path.write_text(
        "## Repo facts\nold\n\n## Step 1\nOld step.\nfailure: pure\nwriters: none\n"
    )  # stale plan exists
    brief_path = run.context_handoff_dir / "scout_findings.md"
    calls = []

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        calls.append(req)
        role = _role(req)
        if role == "Scout":
            brief_path.write_text("# brief\n")
            return _reply(req, {"status": "success", "summary": "scouted",
                                "findings": [], "artifacts": [str(brief_path)]})
        if role == "Planner":
            return _reply(req, {
                "status": "fail", "summary": "external auth required",
                "notes_for_next_agent": "Blocked by: provision API_TOKEN (or split the ticket);"
                " no local build can proceed without it.",
            })
        raise AssertionError(f"planner fail must stop the chain before {role}")

    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    with pytest.raises(RuntimeError, match="reported status='fail'"):
        feature.run(run, TICKET, "main")

    names = [p.params.name for p in run.phases]
    assert "plan" in names
    for forbidden in ("build_1", "verify_1", "review", "commit_code", "push_pr"):
        assert forbidden not in names
    assert all(r != "Builder" for r in (_role(c) for c in calls))


def test_plan_may_create_its_required_test(project, monkeypatch):
    plan_md = (
        "## Repo facts\nnone\n\n## Step 1\nCreate tests/test_new.py; verify runs it.\n"
        "failure: pure\nwriters: none\n"
    )
    run = make_run(project, {"test": "test -f tests/test_new.py"})
    adapter = make_agent_stub(run, plan_md, [
        (build_reply(changed_files=["tests/test_new.py"]),
         {"tests/test_new.py": "def test_ok():\n    assert True\n"}),
    ] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")

    code = feature.run(run, TICKET, "main")

    assert code == 0
    assert (project / "tests" / "test_new.py").exists()
    names = [p.params.name for p in run.phases]
    assert "build_1" in names and "verify_1" in names


def test_ratified_downstream_ci_does_not_block_local_build(project, monkeypatch):
    ticket = Ticket(
        path=".lime/tickets/41-toy.md", number=41, slug="41-toy", title="Toy Ticket",
        workflow="feature", ask="Write greet() in app.py.",
        done_means="commands.test is green locally; CI runs after push in deploy.",
    )
    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    pushed = []
    monkeypatch.setattr(git_helper, "push", lambda branch: pushed.append(branch))
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")

    code = feature.run(run, ticket, "main")

    assert code == 0
    assert pushed  # local build/verify completed, then the push happened once
    assert "verify_1" in [p.params.name for p in run.phases]


# --- shared prototype folder uses the main checkout (ticket 169) ---


def _log_payloads(run, name):
    return [
        json.loads(r["payload_json"] or "{}")
        for r in run.tracer.con.execute(
            "SELECT payload_json FROM events WHERE run_id=? AND type='log' AND name=? ORDER BY event_id",
            (run.run_id, name),
        ).fetchall()
    ]


def _out(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def linked_primary(tmp_path, monkeypatch):
    """A primary checkout on `trunk` with a linked Run worktree, and an open ticket
    that names the prototype only in the primary checkout."""
    primary = tmp_path / "primary"
    primary.mkdir()
    git(primary, "init", "-q", "-b", "trunk")
    (primary / "app.py").write_text("v1\n")
    (primary / ".gitignore").write_text("runs/\n")
    tickets = primary / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "41-toy.md").write_text(TICKET_MD)
    (tickets / "50-other.md").write_text(
        "# 50 — Other\n\nWorkflow: feature\nStatus: open\nPrototype: app/proto-toy\n\n"
        "## Ask\nKeep the prototype.\n\n## Where\napp/proto-toy\n\n"
        "## Done means\nit holds.\n\n## Out of scope\nnone\n"
    )
    (primary / "app" / "proto-toy").mkdir(parents=True)
    (primary / "app" / "proto-toy" / "page.tsx").write_text("primary proto\n")
    git(primary, "add", "-A")
    git(primary, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    linked = tmp_path / "linked"
    git(primary, "worktree", "add", "-q", "-b", "ticket-169", str(linked))
    (linked / "app" / "proto-toy").mkdir(parents=True, exist_ok=True)
    (linked / "app" / "proto-toy" / "page.tsx").write_text("proto\n")
    (linked / "app" / "proto-toy" / "NOTES.md").write_text("fixed: layout\n")
    monkeypatch.chdir(linked)
    return primary, linked


def test_frontend_shared_prototype_uses_primary_checkout_tickets(linked_primary, monkeypatch):
    primary, linked = linked_primary
    run = make_run(linked, {"test": "exit 0"})
    assert run.repo_root == linked.resolve()  # the Run really operates in the linked tree
    adapter = make_agent_stub(run, PLAN_ONE_STEP, [build_reply()] + APPROVED_TAIL)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    pushed = []
    monkeypatch.setattr(git_helper, "push", lambda branch: pushed.append(branch))
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (linked / "app.py").write_text("v2\n")

    ticket = TICKET.model_copy(update={"prototype": "app/proto-toy"})
    code = feature.run(run, ticket, "trunk")

    assert code == 0
    assert pushed
    assert not (linked / ".lime" / "tickets" / "41-toy.md").exists()  # own ticket deleted
    assert (linked / "app" / "proto-toy" / "page.tsx").read_text() == "proto\n"  # folder kept
    payloads = _log_payloads(run, "commit_docs")
    holders = [p for p in payloads if p.get("holding_tickets")]
    assert holders, payloads
    assert str(primary / ".lime" / "tickets" / "50-other.md") in holders[0]["holding_tickets"]
    # the main checkout is a read-only source
    assert (primary / ".lime" / "tickets" / "50-other.md").exists()
    assert (primary / "app" / "proto-toy" / "page.tsx").read_text() == "primary proto\n"


@pytest.mark.parametrize("mode", ["lookup-none", "dir-missing"])
def test_frontend_missing_primary_ticket_source_keeps_directory(project, monkeypatch, mode):
    if mode == "lookup-none":
        monkeypatch.setattr(git_helper, "main_checkout_root", lambda root: None)
    else:
        import shutil

        shutil.rmtree(project / ".lime" / "tickets")

    run = make_run(project, {"test": "exit 0"})
    adapter = make_agent_stub(
        run, PLAN_ONE_STEP,
        [build_reply(), review_reply(),
         (document_reply(changed_docs=["AGENTS.md"]), {"AGENTS.md": "docs\n"})],
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    pushed = []
    monkeypatch.setattr(git_helper, "push", lambda branch: pushed.append(branch))
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")
    (project / "app.py").write_text("v2\n")
    route_dir = project / "app" / "proto-toy"
    route_dir.mkdir(parents=True)
    (route_dir / "page.tsx").write_text("proto\n")
    (route_dir / "NOTES.md").write_text("fixed: layout\n")
    ticket = TICKET.model_copy(update={"prototype": "app/proto-toy"})

    code = feature.run(run, ticket, "main")

    assert code == 0
    assert pushed
    assert route_dir.exists()  # unknown source keeps the directory
    payloads = _log_payloads(run, "commit_docs")
    assert any(p.get("kept") and p.get("reason") for p in payloads)


def test_cookbook_copies_agree_on_shared_folder_policy():
    root = Path(__file__).resolve().parents[1]
    lime_copy = (root / "lime" / "skill" / "lime" / "cookbooks" / "prototype.md").read_text()
    claude_copy = (root / ".claude" / "skills" / "lime" / "cookbooks" / "prototype.md").read_text()
    assert lime_copy == claude_copy
    for text in (lime_copy, claude_copy):
        assert "removes a route directory only when no other open ticket still names it" in text
        assert "uncertainty about the main checkout's ticket list also keeps it" in text
        assert "Never delete the shared route yourself" in text


def test_rereview_replays_every_build_and_reruns_verify_and_review(project, monkeypatch):
    plan_md = (
        "## Repo facts\nnone\n\n## Step 1\nFirst.\nfailure: pure\nwriters: none\n\n"
        "## Step 2\nSecond.\nfailure: pure\nwriters: none\n"
    )
    run = make_run(project, {"test": "exit 0"})
    plan_path = run.context_handoff_dir / "plan.md"
    brief_path = run.context_handoff_dir / "scout_findings.md"
    calls = []
    files = iter(["a.py", "b.py", "c.py", "d.py"])

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        calls.append(req)
        role = _role(req)
        if role == "Scout":
            brief_path.write_text("# brief\n")
            return _reply(req, {"status": "success", "summary": "scouted",
                                "findings": [{"file": "app.py", "note": "x"}], "artifacts": [str(brief_path)]})
        if role == "Planner":
            plan_path.write_text(plan_md)
            return _reply(req, {"status": "success", "summary": "planned", "artifacts": [str(plan_path)]})
        if role == "Builder":
            name = next(files)
            (project / name).write_text(f"{name}\n")
            return _reply(req, build_reply(changed_files=[name]))
        if role == "Reviewer":
            return _reply(req, review_reply(approved=False, blocking=["attach command-specific verifier results"]))
        raise AssertionError(role)

    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    assert feature.run(run, TICKET, "main") == 1

    dropped = run.tracer.rereview("R1", engineer="t")
    assert dropped[0] == "verify_2"

    state = {"interrupt": True}
    calls2 = []

    def adapter2(req, on_event=None, on_spawn=None, on_exit=None):
        calls2.append(req)
        role = _role(req)
        if role == "Reviewer":
            if state["interrupt"]:
                state["interrupt"] = False
                raise RuntimeError("interrupted")
            return _reply(req, review_reply())
        if role == "Documenter":
            return _reply(req, document_reply())
        raise AssertionError(f"{role} must replay, not re-run")

    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter2)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")

    run2 = make_run(project, {"test": "exit 0"})
    run2.tracer.set_run_status("R1", "running")
    run2.replay = run2.tracer.replay_map("R1")
    with pytest.raises(RuntimeError, match="interrupted"):
        feature.run(run2, TICKET, "main")
    assert [p.params.name for p in run2.phases][:2] == ["verify_2", "review"]

    run3 = make_run(project, {"test": "exit 0"})
    run3.tracer.set_run_status("R1", "running")
    run3.replay = run3.tracer.replay_map("R1")
    assert feature.run(run3, TICKET, "main") == 0

    assert {_role(c) for c in calls2} <= {"Reviewer", "Documenter"}
    review_prompts = [c.prompt for c in calls2 if _role(c) == "Reviewer"]
    assert "Recorded by the Spine" in review_prompts[-1]
    assert "verify_1" not in [p.params.name for p in run2.phases + run3.phases]


def test_resume_at_review_reruns_only_the_latest_verify(project, monkeypatch):
    plan_md = (
        "## Repo facts\nnone\n\n## Step 1\nFirst.\nfailure: pure\nwriters: none\n\n"
        "## Step 2\nSecond.\nfailure: pure\nwriters: none\n"
    )
    run = make_run(project, {"test": "exit 0"})
    plan_path = run.context_handoff_dir / "plan.md"
    brief_path = run.context_handoff_dir / "scout_findings.md"
    calls = []

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        calls.append(req)
        role = _role(req)
        if role == "Scout":
            brief_path.write_text("# brief\n")
            return _reply(req, {"status": "success", "summary": "scouted",
                                "findings": [{"file": "app.py", "note": "x"}], "artifacts": [str(brief_path)]})
        if role == "Planner":
            plan_path.write_text(plan_md)
            return _reply(req, {"status": "success", "summary": "planned", "artifacts": [str(plan_path)]})
        if role == "Builder":
            return _reply(req, build_reply())
        if role == "Reviewer":
            raise RuntimeError("interrupted")
        raise AssertionError(role)

    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    (project / "app.py").write_text("v2\n")  # the builder's change, committed on resume
    with pytest.raises(RuntimeError, match="interrupted"):
        feature.run(run, TICKET, "main")

    quality_calls = _install_quality_spy(monkeypatch)
    calls2 = []

    def adapter2(req, on_event=None, on_spawn=None, on_exit=None):
        calls2.append(req)
        role = _role(req)
        if role == "Reviewer":
            return _reply(req, review_reply())
        if role == "Documenter":
            return _reply(req, document_reply())
        raise AssertionError(f"{role} must replay, not re-run")

    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter2)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")

    run2 = make_run(project, {"test": "exit 0"})
    run2.tracer.set_run_status("R1", "running")
    run2.replay = run2.tracer.replay_map("R1")
    assert feature.run(run2, TICKET, "main") == 0

    assert len(quality_calls) == 1, "only the latest verify re-runs"
    names = [p.params.name for p in run2.phases]
    assert names.count("verify_2") == 1
    assert "verify_1" not in names, "the earlier verify replays as recorded"
    assert not any(n.startswith("build") for n in names), "builds replay"
    assert all(_role(c) != "Builder" for c in calls2)


def test_resume_at_document_replays_review(project, monkeypatch):
    run = make_run(project, {"test": "exit 0"})
    plan_path = run.context_handoff_dir / "plan.md"
    brief_path = run.context_handoff_dir / "scout_findings.md"
    calls = []
    state = {"interrupt": True}

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        calls.append(req)
        role = _role(req)
        if role == "Scout":
            brief_path.write_text("# brief\n")
            return _reply(req, {"status": "success", "summary": "scouted",
                                "findings": [{"file": "app.py", "note": "x"}], "artifacts": [str(brief_path)]})
        if role == "Planner":
            plan_path.write_text(PLAN_ONE_STEP)
            return _reply(req, {"status": "success", "summary": "planned", "artifacts": [str(plan_path)]})
        if role == "Builder":
            return _reply(req, build_reply())
        if role == "Reviewer":
            return _reply(req, review_reply())
        if role == "Documenter":
            if state["interrupt"]:
                state["interrupt"] = False
                raise RuntimeError("interrupted")
            return _reply(req, document_reply())
        raise AssertionError(role)

    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    (project / "app.py").write_text("v2\n")  # the builder's change, committed before the document
    with pytest.raises(RuntimeError, match="interrupted"):
        feature.run(run, TICKET, "main")

    quality_calls = _install_quality_spy(monkeypatch)
    calls2 = []

    def adapter2(req, on_event=None, on_spawn=None, on_exit=None):
        calls2.append(req)
        role = _role(req)
        if role == "Documenter":
            return _reply(req, document_reply())
        raise AssertionError(f"{role} must replay, not re-run")

    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter2)
    monkeypatch.setattr(git_helper, "current_branch", lambda: "lime/feature/41-toy")
    monkeypatch.setattr(git_helper, "push", lambda branch: None)
    monkeypatch.setattr(git_helper, "open_pr", lambda **kw: "https://example.com/pull/1")

    run2 = make_run(project, {"test": "exit 0"})
    run2.tracer.set_run_status("R1", "running")
    run2.replay = run2.tracer.replay_map("R1")
    assert feature.run(run2, TICKET, "main") == 0

    assert len(quality_calls) == 1
    names = [p.params.name for p in run2.phases]
    assert "verify_1" in names, "the latest verify re-runs on resume"
    assert "review" not in names, "the approved review replays"
    assert all(_role(c) != "Reviewer" for c in calls2)
