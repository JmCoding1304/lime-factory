"""Tests for lime/summaries.py — the summarizer Role and its Run-less Sessions."""

from __future__ import annotations

import ast
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lime import agents, cli, git_helper, home, status, summaries, tracer
from lime.data_types import AgentResult

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=timezone(timedelta(hours=-3)))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def write_ticket(root: Path, stem: str) -> Path:
    d = root / ".lime" / "tickets"
    d.mkdir(parents=True, exist_ok=True)
    number, _, title = stem.partition("-")
    path = d / f"{stem}.md"
    path.write_text(f"# {number} — {title or stem}\nType: task\nStatus: open\n\n## Ask\nx\n")
    return path


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "alpha"
    (root / ".lime" / "tickets").mkdir(parents=True)
    return root


class FakeAdapter:
    """Records every AgentRequest, emits one `turn`, returns a scripted result."""

    def __init__(self):
        self.requests: list = []
        self.responses: list = []
        self.default_text = "Do the next thing."
        self.cost = 0.01

    def __call__(self, req, on_event=None, on_spawn=None, on_exit=None):
        self.requests.append(req)
        if on_event:
            on_event(
                {
                    "type": "turn",
                    "model": req.model,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "context_tokens": 10,
                    "cost": self.cost,
                }
            )
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return AgentResult(status="ok", text=self.default_text)


@pytest.fixture
def fake(monkeypatch):
    adapter = FakeAdapter()
    monkeypatch.setitem(agents.ADAPTERS, "claude_code", adapter)
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)
    return adapter


def ticket_item(root: Path, stem: str, since: str, **over):
    write_ticket(root, stem)
    item = status._item(
        project=root.name, group="needs_you", reason="ticket", ticket=stem,
        title=f"# {stem}", since=since, kind="task",
    )
    item.update(over)
    return item


def failed_item(root: Path, **over):
    item = status._item(
        project=root.name, group="failed", reason="fail", run_id="alpha-R1",
        since=iso(T0 - timedelta(hours=1)), text="builder: boom", cause="crash_detected",
        actions=["lime run --resume alpha-R1", "lime runs rm alpha-R1"],
    )
    item.update(over)
    return item


def ready_item(root: Path, **over):
    item = status._item(
        project=root.name, group="ready", reason="ready", run_id="alpha-R2",
        title="# Ship it", since=iso(T0 - timedelta(hours=2)),
        pr={"number": 7, "url": "u", "state": "OPEN", "ci": "green"},
        actions=["lime deploy 30-r"],
    )
    item.update(over)
    return item


def db_rows(root: Path) -> dict:
    con = tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    try:
        return {
            row["item_key"]: dict(row)
            for row in con.execute("SELECT item_key, state, summary, error FROM summaries")
        }
    finally:
        con.close()


def test_summarize_writes_changed_items_and_calls_no_agent_when_unchanged(project, fake):
    ticket = ticket_item(project, "10-a-task", iso(T0 - timedelta(days=3)))
    items = [ticket, failed_item(project), ready_item(project)]

    counts = summaries.summarize_project(project, items, T0)
    assert counts == {"written": 3, "failed": 0, "unchanged": 0}
    assert len(fake.requests) == 3
    rows = db_rows(project)
    assert len(rows) == 3
    assert all(row["state"] == "ok" for row in rows.values())

    counts = summaries.summarize_project(project, items, T0)
    assert counts == {"written": 0, "failed": 0, "unchanged": 3}
    assert len(fake.requests) == 3

    write_ticket(project, "10-a-task").write_text("changed body\n")
    counts = summaries.summarize_project(project, items, T0)
    assert counts == {"written": 1, "failed": 0, "unchanged": 2}
    assert len(fake.requests) == 4


def test_summarizer_session_stores_the_resolved_budget(project, fake):
    (project / ".lime" / "runs").mkdir(parents=True, exist_ok=True)
    ticket = ticket_item(project, "10-a-task", iso(T0 - timedelta(days=3)))
    summaries.summarize_project(project, [ticket], T0)

    con = tracer.connect(project / ".lime" / "runs" / "lime.db", readonly=True)
    try:
        row = con.execute(
            "SELECT budget_warn, budget_stop FROM sessions WHERE role='summarizer'"
        ).fetchone()
    finally:
        con.close()
    # claude_code's window is unknown, so the uniform defaults stand.
    assert (row["budget_warn"], row["budget_stop"]) == (150_000, 250_000)


def test_summarizer_session_stores_the_agent_budget_over_its_run_budget(project, fake):
    (project / ".lime" / "config.yaml").write_text(
        "budget:\n"
        "  warn: 100000\n"
        "  stop: 200000\n"
        "agents:\n"
        "  - name: summarizer\n"
        "    runtime: claude_code\n"
        "    model: m\n"
        "    budget:\n"
        "      warn: 80000\n"
        "      stop: 180000\n"
    )
    ticket = ticket_item(project, "10-a-task", iso(T0 - timedelta(days=3)))
    summaries.summarize_project(project, [ticket], T0)

    con = tracer.connect(project / ".lime" / "runs" / "lime.db", readonly=True)
    try:
        row = con.execute(
            "SELECT budget_warn, budget_stop FROM sessions WHERE role='summarizer'"
        ).fetchone()
    finally:
        con.close()
    assert (row["budget_warn"], row["budget_stop"]) == (80_000, 180_000)


def test_prompt_holds_today_age_and_source(project, fake):
    ticket = ticket_item(project, "10-a-task", iso(T0 - timedelta(days=57)))
    ticket_path = project / ".lime" / "tickets" / "10-a-task.md"
    ticket_text = ticket_path.read_text()
    items = [ticket, failed_item(project), ready_item(project)]

    summaries.summarize_project(project, items, T0)
    prompts = [req.prompt for req in fake.requests]
    assert len(prompts) == 3

    ticket_prompt = next(p for p in prompts if ticket_text in p)
    assert "2026-11-05" in ticket_prompt
    assert "57 days" in ticket_prompt

    failed_prompt = next(p for p in prompts if "builder: boom" in p)
    assert "Cause: crash_detected" in failed_prompt

    ready_prompt = next(p for p in prompts if "# Ship it" in p or "Ship it" in p)
    assert "Title: Ship it" in ready_prompt
    assert "PR: #7 OPEN · CI green · u" in ready_prompt


def test_trim_two_sentences_280_chars_never_at_pi():
    assert summaries.trim("Ask the P.I. to sign. Then upload it. Third.") == (
        "Ask the P.I. to sign. Then upload it."
    )
    assert summaries.trim("One line.\n\nSecond line. Third.") == "One line. Second line."
    long = "word " * 80
    trimmed = summaries.trim(long)
    assert len(trimmed) <= summaries.MAX_CHARS
    assert trimmed.endswith("…")
    assert trimmed[:-1].split()[-1] == "word"  # no split word
    assert summaries.trim("   ") == ""


def test_failed_or_timed_out_call_stores_failed_and_next_run_retries(project, fake):
    items = [
        ticket_item(project, "10-a-task", iso(T0)),
        failed_item(project),
        ready_item(project),
    ]
    fake.responses = [
        AgentResult(status="timeout"),
        AgentResult(status="timeout"),
        RuntimeError("kaboom"),
        AgentResult(status="ok", text="   "),
    ]

    counts = summaries.summarize_project(project, items, T0)
    assert counts == {"written": 0, "failed": 3, "unchanged": 0}
    rows = db_rows(project)
    assert len(rows) == 3
    assert all(row["state"] == "failed" and row["summary"] is None for row in rows.values())
    assert all(row["error"] for row in rows.values())
    errors = {row["error"] for row in rows.values()}
    assert "timeout after 120s" in errors
    assert "RuntimeError: kaboom" in errors
    assert "empty answer" in errors

    fake.responses = []
    fake.default_text = "Now do it."
    counts = summaries.summarize_project(project, items, T0)
    assert counts == {"written": 3, "failed": 0, "unchanged": 0}
    assert len(fake.requests) == 7
    assert all(row["state"] == "ok" for row in db_rows(project).values())


def test_timeout_spends_one_reporting_nudge_in_the_same_session(project, fake):
    item = ticket_item(project, "10-a-task", iso(T0))
    fake.responses = [
        AgentResult(status="timeout"),
        AgentResult(status="ok", text="Nudged answer."),
    ]

    counts = summaries.summarize_project(project, [item], T0)
    assert counts == {"written": 1, "failed": 0, "unchanged": 0}
    assert len(fake.requests) == 2
    assert fake.requests[0].session_id == fake.requests[1].session_id
    assert "Time is up" in fake.requests[1].prompt
    assert next(iter(db_rows(project).values()))["state"] == "ok"


def test_pi_retry_timeout_spends_one_reporting_nudge_in_the_same_session(project, fake, monkeypatch):
    (project / ".lime" / "config.yaml").write_text(
        "agents:\n"
        "  - name: summarizer\n"
        "    runtime: pi\n"
        "    model: openrouter/x/y\n"
    )
    monkeypatch.setattr(summaries.time, "sleep", lambda _s: None)
    item = ticket_item(project, "10-a-task", iso(T0))
    fake.responses = [
        AgentResult(status="error", error="connection reset"),
        AgentResult(status="timeout"),
        AgentResult(status="ok", text="Nudged answer."),
    ]

    counts = summaries.summarize_project(project, [item], T0)
    assert counts == {"written": 1, "failed": 0, "unchanged": 0}
    assert len(fake.requests) == 3
    assert {req.session_id for req in fake.requests} == {fake.requests[0].session_id}
    assert "Time is up" in fake.requests[2].prompt
    assert "transient provider error" in fake.requests[1].prompt
    assert next(iter(db_rows(project).values()))["state"] == "ok"


def test_pi_retry_timeout_fails_after_the_nudge_also_times_out(project, fake, monkeypatch):
    (project / ".lime" / "config.yaml").write_text(
        "agents:\n"
        "  - name: summarizer\n"
        "    runtime: pi\n"
        "    model: openrouter/x/y\n"
    )
    monkeypatch.setattr(summaries.time, "sleep", lambda _s: None)
    item = ticket_item(project, "10-a-task", iso(T0))
    fake.responses = [
        AgentResult(status="error", error="connection reset"),
        AgentResult(status="timeout"),
        AgentResult(status="timeout"),
    ]

    counts = summaries.summarize_project(project, [item], T0)
    assert counts == {"written": 0, "failed": 1, "unchanged": 0}
    assert len(fake.requests) == 3
    assert {req.session_id for req in fake.requests} == {fake.requests[0].session_id}
    assert "Time is up" in fake.requests[2].prompt
    assert next(iter(db_rows(project).values()))["error"] == "timeout after 1800s"


def test_pi_transient_error_retries_in_place_then_once_in_a_fresh_session(project, fake, monkeypatch):
    (project / ".lime" / "config.yaml").write_text(
        "agents:\n"
        "  - name: summarizer\n"
        "    runtime: pi\n"
        "    model: openrouter/x/y\n"
    )
    monkeypatch.setattr(summaries.time, "sleep", lambda _s: None)
    item = ticket_item(project, "10-a-task", iso(T0))
    fake.responses = [
        AgentResult(status="error", error="connection reset"),
        AgentResult(status="error", error="connection reset"),
        AgentResult(status="error", error="connection reset"),
        AgentResult(status="ok", text="Do it."),
    ]

    counts = summaries.summarize_project(project, [item], T0)
    assert counts == {"written": 1, "failed": 0, "unchanged": 0}
    assert len(fake.requests) == 4
    in_place = {req.session_id for req in fake.requests[:3]}
    assert len(in_place) == 1
    assert fake.requests[3].session_id not in in_place
    assert "transient provider error" in fake.requests[1].prompt


def test_two_summarize_processes_never_summarize_one_item_twice(project, monkeypatch):
    items = [
        ticket_item(project, "10-a-task", iso(T0)),
        ticket_item(project, "11-b-task", iso(T0)),
        ticket_item(project, "12-c-task", iso(T0)),
    ]
    lock = threading.Lock()
    calls: list = []
    two = threading.Event()

    def blocking(req, on_event=None, on_spawn=None, on_exit=None):
        with lock:
            calls.append(req)
            if len(calls) >= 2:
                two.set()
        two.wait(timeout=5)
        if on_event:
            on_event(
                {
                    "type": "turn",
                    "model": req.model,
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                    "context_tokens": 10,
                    "cost": 0.01,
                }
            )
        return AgentResult(status="ok", text="Do it.")

    monkeypatch.setitem(agents.ADAPTERS, "claude_code", blocking)
    monkeypatch.setitem(agents.ADAPTERS, "pi", blocking)

    # Create the ledger (and its WAL journal) up front: the race under test is
    # the claim, not sqlite's one-time journal-mode switch on a fresh database.
    tracer.Tracer(project / ".lime" / "runs").con.close()

    def run():
        summaries.summarize_project(project, items, T0)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)

    keys = []
    for req in calls:
        marker = next(line for line in req.prompt.splitlines() if line.startswith("Ticket: "))
        keys.append(marker.split(": ", 1)[1])
    assert len(keys) == len(items)
    assert len(set(keys)) == len(keys)


def test_summarizer_sessions_count_in_cost_and_never_show_as_runs(project, fake, capsys, monkeypatch):
    ticket_path = project / ".lime" / "tickets" / "10-a-task.md"
    ticket_item(project, "10-a-task", iso(T0))
    before = ticket_path.read_bytes()
    summaries.summarize_project(project, [ticket_item(project, "10-a-task", iso(T0))], T0)

    monkeypatch.chdir(project)
    assert cli.main(["cost"]) == 0
    out = capsys.readouterr().out
    assert "summarizer" in out
    assert "claude_code" in out

    assert cli.main(["runs", "--all"]) == 0
    runs_out = capsys.readouterr().out
    assert "no Runs" in runs_out

    db = project / ".lime" / "runs" / "lime.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM processes").fetchone()[0] == 0
    finally:
        con.close()

    names = {path.name for path in (project / ".lime" / "runs").iterdir()}
    assert all(name.startswith("lime.db") for name in names)
    assert ticket_path.read_bytes() == before


def test_default_role_is_haiku_on_claude_code_with_no_tools_and_no_writes(project, fake):
    summaries.summarize_project(project, [ticket_item(project, "10-a-task", iso(T0))], T0)
    request = fake.requests[-1]
    assert request.runtime == "claude_code"
    assert request.model == "claude-haiku-5-5"
    assert request.tools == []
    assert request.writes == []

    (project / ".lime" / "config.yaml").write_text(
        "agents:\n"
        "  - name: summarizer\n"
        "    runtime: pi\n"
        "    model: openrouter/x/y\n"
        "    tools: [read, bash]\n"
    )
    summaries.summarize_project(project, [ticket_item(project, "11-b-task", iso(T0))], T0)
    request = fake.requests[-1]
    assert request.runtime == "pi"
    assert request.model == "openrouter/x/y"
    assert request.tools == []
    assert request.writes == []


def test_summaries_module_never_opens_a_run():
    tree = ast.parse(Path(summaries.__file__).read_text())
    banned_modules = {"session", "runner", "focus", "cli"}
    banned_names = {"start_run", "start_process", "start_session", "ensure"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in banned_modules
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "") not in banned_modules
            for alias in node.names:
                assert alias.name not in banned_modules
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in banned_names
            elif isinstance(node.func, ast.Attribute):
                assert node.func.attr not in banned_names


# --- Step 4: the `lime focus summarize` command -----------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    home_root = tmp_path / "home"
    home_root.mkdir()
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.delenv("LIME_AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (tmp_path / "gitconfig").write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "t")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "t@t")
    monkeypatch.setattr(home, "_now", lambda: T0)
    work = tmp_path / "work"
    names = ("alpha", "beta")
    for name in names:
        (work / name).mkdir(parents=True)
    projects = tmp_path / "projects"
    projects.write_text("# registry\n\n" + "\n".join(str(work / n) for n in names) + "\n")
    monkeypatch.setenv("LIME_PROJECTS", str(projects))
    return SimpleNamespace(
        tmp=tmp_path,
        home=home_root / ".lime" / "dashboard",
        work=work,
        registry=projects,
    )


def add_failed_run(root: Path, run_id: str, ticket_stem: str, since: str, error: str) -> None:
    tr = tracer.Tracer(root / ".lime" / "runs")
    try:
        tr.start_run(
            run_id,
            workflow="feature",
            request="x",
            ticket=f".lime/tickets/{ticket_stem}.md",
            branch=f"lime/feature/{ticket_stem}",
        )
        phase_id = tr.add_phase(run_id, seq=1, name="build", stage="Build", kind="build")
        tr.set_phase_status(phase_id, "running")
        tr.set_phase_status(phase_id, "fail", error=error)
        tr.set_run_status(run_id, "fail")
        tr.con.execute(
            "UPDATE runs SET started_at=?, ended_at=? WHERE run_id=?",
            (since, since, run_id),
        )
        tr.con.commit()
    finally:
        tr.con.close()


def status_json(capsys) -> dict:
    assert cli.main(["status", "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_cli_summarize_end_to_end_and_status_passes_it_through(env, fake, monkeypatch, capsys):
    alpha = env.work / "alpha"
    beta = env.work / "beta"
    write_ticket(alpha, "10-a-task")
    add_failed_run(alpha, "alpha-R1", "11-b", iso(T0 - timedelta(hours=1)), "builder: boom")
    write_ticket(beta, "20-b-task")

    def boom(*a, **k):
        raise AssertionError("no gh call expected")

    monkeypatch.setattr(status.git_helper, "pr_view", boom)

    assert cli.main(["focus", "summarize"]) == 0
    capsys.readouterr()

    report = status_json(capsys)
    ok_items = [i for i in report["items"] if i["summary_state"] == "ok"]
    assert ok_items
    assert all(i["summary"] == "Do the next thing." for i in ok_items)
    count_rows = [i for i in report["items"] if i["reason"] in status.COUNT_REASONS]
    assert all(i["summary_state"] is None and i["summary"] is None for i in count_rows)

    before = len(fake.requests)
    assert cli.main(["focus", "summarize"]) == 0
    capsys.readouterr()
    assert len(fake.requests) == before

    # Edit both tickets; --project beta must summarize beta and never alpha.
    for root, stem in ((alpha, "10-a-task"), (beta, "20-b-task")):
        path = root / ".lime" / "tickets" / f"{stem}.md"
        path.write_text(path.read_text() + "\nchanged\n")
    before = len(fake.requests)
    assert cli.main(["focus", "summarize", "--project", "beta"]) == 0
    capsys.readouterr()
    new_prompts = [req.prompt for req in fake.requests[before:]]
    assert new_prompts
    assert all("Project: beta" in p for p in new_prompts)
    assert all("Project: alpha" not in p for p in new_prompts)


def test_cli_summarize_unknown_project_exits_2(env, fake, capsys):
    assert cli.main(["focus", "summarize", "--project", "nope"]) == 2
    err = capsys.readouterr().err
    assert 'project "nope" is not registered' in err


def test_cli_summarize_exits_1_when_an_item_failed(env, fake, capsys):
    alpha = env.work / "alpha"
    write_ticket(alpha, "10-a-task")
    fake.responses = [AgentResult(status="error", error="kaboom")]

    assert cli.main(["focus", "summarize"]) == 1
    out = capsys.readouterr()
    assert "1 failed" in out.out
    assert "failed alpha 10-a-task: kaboom" in out.err

    report = status_json(capsys)
    item = next(i for i in report["items"] if i["ticket"] == "10-a-task")
    assert item["summary_state"] == "failed"
    assert item["summary"] is None
