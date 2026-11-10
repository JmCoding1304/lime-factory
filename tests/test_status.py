"""Tests for lime/status.py — the report (step 1: tickets, ADRs, home, ranking)."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lime import cli, git_helper, home, status, tracer

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=timezone(timedelta(hours=-3)))

BANNED = {
    "Tracer",
    "write",
    "write_text",
    "write_bytes",
    "unlink",
    "mkdir",
    "touch",
    "rename",
    "executescript",
    "commit",
    "locked",
    "push",
    "push_lease",
    "commit_merge",
    "visit_command",
    "init_command",
    "merge_pr",
    "fast_forward",
    "open_pr",
    "merged_worktree",
    "set_pr",
    "set_run_status",
    "teardown",
    "create",
}


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


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
    names = ("alpha", "beta", "gamma")
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


def write_ticket(root, stem, *headers):
    d = root / ".lime" / "tickets"
    d.mkdir(parents=True, exist_ok=True)
    number, _, title = stem.partition("-")
    text = f"# {number} — {title or stem}\n" + "\n".join(headers) + "\n\n## Ask\nx\n"
    (d / f"{stem}.md").write_text(text)
    return d / f"{stem}.md"


def write_adr(root, stem, first_line):
    d = root / "docs" / "adr"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.md").write_text(f"# ADR {stem}\n\n{first_line}\n\nbody\n")


def make_home(env, focus_body="", last_visit=None, push_error=None):
    h = env.home
    h.mkdir(parents=True, exist_ok=True)
    _git(h, "init", "-q", "-b", "main")
    (h / ".gitignore").write_text(home.GITIGNORE_TEXT)
    (h / "focus.md").write_text(home.FOCUS_HEADER + focus_body)
    if last_visit is not None:
        previous, current = last_visit
        (h / "last-visit").write_text(home._visit_text(previous, current))
    _git(h, "add", "-A")
    _git(h, "commit", "-q", "-m", "dashboard init")
    remote = env.tmp / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )
    _git(h, "remote", "add", "origin", str(remote))
    _git(h, "push", "-q", "-u", "origin", "main")
    if push_error is not None:
        (h / "push-error").write_text(push_error)
    return h


def run_status(capsys, *args):
    assert cli.main(["status", "--json", *args]) == 0
    return json.loads(capsys.readouterr().out)


def test_person_tickets_are_items_with_kind(env, capsys):
    alpha = env.work / "alpha"
    write_ticket(alpha, "10-a-task", "Type: task", "Status: open")
    write_ticket(alpha, "11-b-grill", "Type: grilling (owner)", "Status: open")
    write_ticket(alpha, "12-c-proto", "Type: prototype", "Status: open")
    write_ticket(alpha, "13-d-closed", "Type: task", "Status: closed")
    write_ticket(alpha, "14-e-claimed", "Type: task", "Status: claimed")
    write_ticket(alpha, "15-f-blocked", "Type: task", "Status: open", "Blocked by: block.md")
    (alpha / "block.md").write_text("x")
    write_ticket(alpha, "16-g-research", "Type: research", "Status: open")
    write_ticket(alpha, "17-h-deleted", "Type: task", "Status: open", "Blocked by: gone.md")

    report = run_status(capsys, "--project", "alpha")
    items = {item["ticket"]: item for item in report["items"]}
    assert set(items) == {"10-a-task", "11-b-grill", "12-c-proto", "17-h-deleted"}
    assert items["10-a-task"]["kind"] == "task"
    assert items["11-b-grill"]["kind"] == "grilling"
    assert items["12-c-proto"]["kind"] == "prototype"
    assert all(i["group"] == "needs_you" and i["reason"] == "ticket" for i in items.values())


def test_ticket_close_drops_a_person_ticket_and_reopen_restores_it(env, capsys):
    write_ticket(env.work / "alpha", "10-a-task", "Type: task", "Status: open")

    assert "10-a-task" in {i["ticket"] for i in run_status(capsys, "--project", "alpha")["items"]}

    assert cli.main(["ticket", "close", "alpha", "10"]) == 0
    capsys.readouterr()
    assert "10-a-task" not in {i["ticket"] for i in run_status(capsys, "--project", "alpha")["items"]}

    assert cli.main(["ticket", "reopen", "alpha", "10"]) == 0
    capsys.readouterr()
    assert "10-a-task" in {i["ticket"] for i in run_status(capsys, "--project", "alpha")["items"]}


def test_person_tickets_closed_since_last_visit_are_done_items(env, capsys):
    make_home(env, last_visit=(T0 - timedelta(days=1), T0))
    alpha = env.work / "alpha"
    after = (T0 - timedelta(hours=2)).timestamp()
    before = (T0 - timedelta(days=3)).timestamp()
    a = write_ticket(alpha, "10-a-task", "Type: task", "Status: closed")
    b = write_ticket(alpha, "11-b-grill", "Type: grilling (owner)", "Status: closed")
    old = write_ticket(alpha, "12-c-old", "Type: task", "Status: closed")
    reopened = write_ticket(alpha, "13-d-reopened", "Type: task", "Status: open")
    feature = write_ticket(alpha, "14-e-feature", "Workflow: feature", "Status: closed")
    research = write_ticket(alpha, "15-f-research", "Type: research", "Status: closed")
    os.utime(a, (after, after))
    os.utime(b, (after, after))
    os.utime(old, (before, before))
    os.utime(reopened, (after, after))
    os.utime(feature, (after, after))
    os.utime(research, (after, after))

    report = run_status(capsys, "--project", "alpha")
    done = [i for i in report["items"] if i["group"] == "done"]
    assert [i["ticket"] for i in done] == ["10-a-task", "11-b-grill"]
    assert all(i["reason"] == "closed" for i in done)
    assert [i["kind"] for i in done] == ["task", "grilling"]
    assert [i["title"] for i in done] == ["a-task", "b-grill"]
    since = datetime.fromtimestamp(after, timezone.utc).isoformat()
    for item in done:
        assert item["since"] == since
        assert item["actions"] == []
        assert item["summary"] is None
        assert set(item) == set(status.ITEM_KEYS)
    reopened_item = next(i for i in report["items"] if i["ticket"] == "13-d-reopened")
    assert reopened_item["group"] == "needs_you" and reopened_item["reason"] == "ticket"
    alpha_row = next(p for p in report["projects"] if p["name"] == "alpha")
    assert alpha_row["counts"]["done"] == 2


def test_unreadable_or_status_less_ticket_files_are_skipped_and_the_report_builds(env, capsys):
    make_home(env, last_visit=(T0 - timedelta(days=1), T0))
    alpha = env.work / "alpha"
    after = (T0 - timedelta(hours=2)).timestamp()
    a = write_ticket(alpha, "10-a-task", "Type: task", "Status: closed")
    os.utime(a, (after, after))
    tickets = alpha / ".lime" / "tickets"
    (tickets / "11-dir.md").mkdir(parents=True, exist_ok=True)
    (tickets / "12-bad.md").write_bytes(b"# 12 \xff\nType: task\nStatus: closed\n")
    nostatus = write_ticket(alpha, "13-c-nostatus", "Type: task")
    os.utime(nostatus, (after, after))
    write_ticket(env.work / "beta", "20-b-task", "Type: task", "Status: open")

    report = run_status(capsys)
    done = [(i["project"], i["ticket"]) for i in report["items"] if i["group"] == "done"]
    assert done == [("alpha", "10-a-task")]
    assert not any(i["ticket"] == "13-c-nostatus" for i in report["items"])
    assert any(i["ticket"] == "20-b-task" for i in report["items"])


def test_closed_tickets_need_a_last_visit(env, capsys):
    make_home(env)
    alpha = env.work / "alpha"
    after = (T0 - timedelta(hours=2)).timestamp()
    a = write_ticket(alpha, "10-a-task", "Type: task", "Status: closed")
    os.utime(a, (after, after))

    report = run_status(capsys, "--project", "alpha")
    assert not [i for i in report["items"] if i["group"] == "done"]


def test_ticket_close_lists_it_done_and_reopen_moves_it_back(env, capsys):
    make_home(env, last_visit=(T0 - timedelta(days=3650), T0))
    write_ticket(env.work / "alpha", "10-a-task", "Type: task", "Status: open")

    assert cli.main(["ticket", "close", "alpha", "10"]) == 0
    capsys.readouterr()
    report = run_status(capsys, "--project", "alpha")
    item = next(i for i in report["items"] if i["ticket"] == "10-a-task")
    assert item["group"] == "done" and item["reason"] == "closed"

    assert cli.main(["ticket", "reopen", "alpha", "10"]) == 0
    capsys.readouterr()
    report = run_status(capsys, "--project", "alpha")
    item = next(i for i in report["items"] if i["ticket"] == "10-a-task")
    assert item["group"] == "needs_you" and item["reason"] == "ticket"
    assert not [i for i in report["items"] if i["group"] == "done"]


def test_dashboard_done_reason_union_mirrors_status_done_items():
    tree = ast.parse(Path(status.__file__).read_text())
    emitted: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "_item"):
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        group = kwargs.get("group")
        if not (isinstance(group, ast.Constant) and group.value == "done"):
            continue
        reason = kwargs.get("reason")
        assert isinstance(reason, ast.Constant)
        emitted.add(reason.value)

    types_ts = Path(__file__).resolve().parent.parent / "dashboard" / "shared" / "types.ts"
    text = types_ts.read_text()
    start = text.index("export type DoneReason =")
    end = text.index(";", start)
    block = text[start:end]
    mirrored = set(re.findall(r'"(\w+)"', block))
    assert emitted == mirrored == {"deployed", "merged", "closed"}


def test_proposed_adrs_are_one_count_row_per_project(env, capsys):
    alpha = env.work / "alpha"
    write_adr(alpha, "0001", "Status: proposed")
    write_adr(alpha, "0002", "**Status:** proposed")
    write_adr(alpha, "0003", "Status: **Proposed.** x")
    write_adr(alpha, "0004", "Status: proposed — accepted by the owner at merge")
    write_adr(alpha, "0005", "Status: accepted")

    report = run_status(capsys)
    rows = [i for i in report["items"] if i["reason"] == "adr_proposed"]
    assert len(rows) == 1
    row = rows[0]
    assert row["project"] == "alpha"
    assert row["count"] == 4
    assert row["ticket"] is None
    assert row["since"] is None
    assert not [i for i in report["items"] if i["project"] == "beta"]


def test_items_rank_by_focus_project_then_oldest(env, capsys):
    due = T0.date().isoformat()
    make_home(
        env,
        focus_body=(
            f"- [ ] f1 · A · due {due} · beta\n"
            "- [ ] f2 · B · alpha\n"
            f"- [x] f3 · C · due {due} · alpha\n"
        ),
    )
    beta = env.work / "beta"
    old = write_ticket(beta, "20-beta-old", "Type: task", "Status: open")
    new = write_ticket(beta, "21-beta-new", "Type: task", "Status: open")
    os.utime(old, (T0.timestamp() - 172800, T0.timestamp() - 172800))
    os.utime(new, (T0.timestamp() - 3600, T0.timestamp() - 3600))
    write_ticket(env.work / "alpha", "30-alpha", "Type: task", "Status: open")
    write_adr(env.work / "alpha", "0001", "Status: proposed")
    write_ticket(env.work / "gamma", "40-gamma", "Type: task", "Status: open")

    report = run_status(capsys)
    assert [(i["ticket"], i["focus"]) for i in report["items"]] == [
        ("20-beta-old", "f1"),
        ("21-beta-new", "f1"),
        ("30-alpha", "f2"),
        (None, "f2"),
        ("40-gamma", None),
    ]
    assert report["items"][3]["reason"] == "adr_proposed"


def test_missing_home_repo_and_missing_db_still_report(env, capsys):
    write_ticket(env.work / "alpha", "10-a-task", "Type: task", "Status: open")

    report = run_status(capsys)
    alpha = next(p for p in report["projects"] if p["name"] == "alpha")
    assert alpha["db"] is False
    assert any(i["project"] == "alpha" and i["reason"] == "ticket" for i in report["items"])
    assert all(i["focus"] is None for i in report["items"])
    assert report["home"] == {"ahead": 0, "push_error": None, "last_visit": None}
    assert not env.home.exists()
    assert set(report) == {
        "generated_at", "projects", "items", "cost", "home", "notes", "read_ms",
    }
    assert set(report["read_ms"]) == {"db", "gh", "git", "total"}
    assert set(report["home"]) == {"ahead", "push_error", "last_visit"}
    for item in report["items"]:
        assert set(item) == set(status.ITEM_KEYS)


def test_home_block_reports_ahead_push_error_and_last_visit(env, capsys):
    previous = T0 - timedelta(days=1)
    make_home(env, last_visit=(previous, T0))
    h = env.home
    (h / "extra.txt").write_text("x")
    _git(h, "add", "-A")
    _git(h, "commit", "-q", "-m", "extra")
    (h / "push-error").write_text("fatal: could not read\nmore\n")

    report = run_status(capsys)
    assert report["home"]["ahead"] == 1
    assert report["home"]["push_error"] == "fatal: could not read"
    assert report["home"]["last_visit"] == {
        "previous": home._stamp(previous),
        "current": home._stamp(T0),
    }
    assert "last_visit" not in report


def test_project_filter_and_unknown_project(env, capsys):
    write_ticket(env.work / "beta", "10-b-task", "Type: task", "Status: open")
    report = run_status(capsys, "--project", "beta")
    assert [p["name"] for p in report["projects"]] == ["beta"]
    assert all(i["project"] == "beta" for i in report["items"])

    assert cli.main(["status", "--project", "nope"]) == 2
    err = capsys.readouterr().err
    assert 'lime status: project "nope" is not registered in' in err


def _snapshot(roots, home_repo):
    files = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.name.endswith("-shm") or path.name.endswith("-wal"):
                continue
            rel = f"{root}::{path.relative_to(root)}"
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    files["HEAD"] = _git(home_repo, "rev-parse", "HEAD").stdout.strip()
    files["status"] = _git(home_repo, "--no-optional-locks", "status", "--porcelain").stdout
    return files


def _dump(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return "\n".join(con.iterdump())
    finally:
        con.close()


def test_status_leaves_ledgers_tickets_and_home_unchanged(env, capsys):
    make_home(env, focus_body="- [ ] f1 · A · beta\n", last_visit=(T0 - timedelta(days=1), T0))
    (env.home / "push-error").write_text("fatal: nope\n")
    write_ticket(env.work / "alpha", "10-a-task", "Type: task", "Status: open")
    write_adr(env.work / "alpha", "0001", "Status: proposed")
    write_ticket(env.work / "beta", "20-b-task", "Type: task", "Status: open")

    dbs = []
    for name, stem in (("alpha", "10-a-task"), ("beta", "20-b-task")):
        tr = ledger(env.work / name)
        add_run(
            tr, f"{name}-R1", stem, "fail", iso(T0 - timedelta(hours=2)),
            error="builder: blocked: x [worktree: /w, handoff: /h]",
            ended_at=iso(T0 - timedelta(hours=2)),
        )
        add_turn(tr, f"{name}-R1", 1, "builder", "pi", "openrouter/deepseek/x", 1.0, iso(T0))
        add_refusal(tr, stem, "overlap_refused", run_id="-", started_at=iso(T0))
        tr.start_process(f"{name}-R1", kind="run", name="run", pid=999999, command="lime run")
        tr.claim_summary("test-key", "test-fp")
        tr.finish_summary("test-key", "test-fp", summary="seed", error=None)
        tr.con.commit()
        tr.con.close()
        dbs.append(env.work / name / ".lime" / "runs" / "lime.db")

    tr = ledger(env.work / "alpha")
    add_run(tr, "alpha-R2", "11-a", "deployed", iso(T0 - timedelta(hours=3)),
            ended_at=iso(T0 - timedelta(hours=3)))
    add_deploy_phase(tr, "alpha-R2", iso(T0 - timedelta(hours=2)))
    tr.con.close()

    roots = [env.work, env.home]
    before = _snapshot(roots, env.home)
    dumps_before = {str(db): _dump(db) for db in dbs}
    assert cli.main(["status", "--json"]) == 0
    capsys.readouterr()
    assert _snapshot(roots, env.home) == before
    assert {str(db): _dump(db) for db in dbs} == dumps_before
    assert cli.main(["status"]) == 0
    capsys.readouterr()
    assert _snapshot(roots, env.home) == before
    assert {str(db): _dump(db) for db in dbs} == dumps_before


def _find_item(report, **match):
    return next(
        item
        for item in report["items"]
        if all(item[key] == value for key, value in match.items())
    )


def test_summary_state_pending_ok_failed_and_stale(env, capsys):
    root = env.work / "alpha"
    write_ticket(root, "10-a-task", "Type: task", "Status: open")
    write_adr(root, "0001", "Status: proposed")
    tr = ledger(root)
    add_run(
        tr, "alpha-R1", "11-b", "fail", iso(T0 - timedelta(hours=1)),
        error="builder: boom", ended_at=iso(T0 - timedelta(hours=1)),
    )
    tr.con.close()

    report = run_status(capsys)
    ticket_item = _find_item(report, reason="ticket")
    run_item = _find_item(report, reason="fail")
    count_item = _find_item(report, reason="adr_proposed")
    assert ticket_item["summary_state"] == "pending" and ticket_item["summary"] is None
    assert run_item["summary_state"] == "pending" and run_item["summary"] is None
    assert count_item["summary"] is None and count_item["summary_state"] is None

    tr = ledger(root)
    ticket_key = status.summary_key(ticket_item)
    ticket_fp = status.fingerprint(status.summary_source(ticket_item, root))
    assert tr.claim_summary(ticket_key, ticket_fp) is True
    assert tr.finish_summary(ticket_key, ticket_fp, summary="Do the thing.", error=None) is True
    run_key = status.summary_key(run_item)
    run_fp = status.fingerprint(status.summary_source(run_item, root))
    assert tr.claim_summary(run_key, run_fp) is True
    assert tr.finish_summary(run_key, run_fp, summary=None, error="boom") is True
    tr.con.close()

    report = run_status(capsys)
    ticket_item = _find_item(report, reason="ticket")
    run_item = _find_item(report, reason="fail")
    assert ticket_item["summary_state"] == "ok"
    assert ticket_item["summary"] == "Do the thing."
    assert run_item["summary_state"] == "failed"
    assert run_item["summary"] is None

    # Editing the ticket file body changes the source, so the stored summary is stale.
    write_ticket(root, "10-a-task", "Type: task", "Status: open", "Ask: changed")
    report = run_status(capsys)
    ticket_item = _find_item(report, reason="ticket")
    assert ticket_item["summary_state"] == "pending" and ticket_item["summary"] is None

    # A row left `working` reads pending (its owner is this live process).
    tr = ledger(root)
    ticket_key = status.summary_key(ticket_item)
    ticket_fp = status.fingerprint(status.summary_source(ticket_item, root))
    assert tr.claim_summary(ticket_key, ticket_fp) is True
    tr.con.close()
    report = run_status(capsys)
    assert _find_item(report, reason="ticket")["summary_state"] == "pending"


def test_summary_source_by_kind(env):
    root = env.work / "alpha"
    ticket_path = write_ticket(root, "10-a-task", "Type: task", "Status: open")
    body = ticket_path.read_text()

    ticket_item = status._item(
        group="needs_you", reason="ticket", project="alpha", ticket="10-a-task",
        title="# A task", kind="task",
    )
    source = status.summary_source(ticket_item, root)
    assert body in source

    failed_item = status._item(
        group="failed", reason="fail", project="alpha", run_id="alpha-R1",
        text="boom", cause="crash_detected",
    )
    source = status.summary_source(failed_item, root)
    assert "Error: boom" in source
    assert "Cause: crash_detected" in source

    ready_item = status._item(
        group="ready", reason="ready", project="alpha", ticket="10-a-task",
        title="# Ship it", pr={"number": 7, "url": "u", "state": "OPEN", "ci": "green"},
    )
    source = status.summary_source(ready_item, root)
    assert "Title: Ship it" in source
    assert "PR: #7 OPEN · CI green · u" in source

    same = status.summary_source(dict(failed_item), root)
    assert status.fingerprint(source) == status.fingerprint(source)
    assert status.fingerprint(status.summary_source(failed_item, root)) == status.fingerprint(same)
    assert status.fingerprint(same) != status.fingerprint(same + "x")

    aged = dict(failed_item)
    aged["since"] = "2020-01-01T00:00:00+00:00"
    assert status.summary_source(aged, root) == status.summary_source(failed_item, root)


def test_ledger_without_summaries_table_reads_pending_and_keeps_run_items(env, capsys):
    root = env.work / "alpha"
    tr = ledger(root)
    add_run(
        tr, "alpha-R1", "11-b", "fail", iso(T0 - timedelta(hours=1)),
        error="builder: boom", ended_at=iso(T0 - timedelta(hours=1)),
    )
    tr.con.close()
    db = root / ".lime" / "runs" / "lime.db"
    con = sqlite3.connect(str(db))
    con.execute("DROP TABLE summaries")
    con.commit()
    con.close()

    report = run_status(capsys)
    alpha = next(p for p in report["projects"] if p["name"] == "alpha")
    assert alpha["error"] is None
    run_item = _find_item(report, run_id="alpha-R1")
    assert run_item["group"] == "failed"
    assert run_item["summary_state"] == "pending" and run_item["summary"] is None


def test_plain_output_lists_groups(env, capsys, monkeypatch):
    make_home(env, last_visit=(T0 - timedelta(days=1), T0))
    write_ticket(env.work / "alpha", "50-x", "Type: task", "Status: open")
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "51-d", "deployed", iso(T0 - timedelta(hours=3)),
            ended_at=iso(T0 - timedelta(hours=3)))
    add_deploy_phase(tr, "R1", iso(T0 - timedelta(hours=2)))
    tr.con.close()

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Needs you" in out
    assert "Done since last visit" in out
    assert any("alpha" in line and "50-x" in line for line in out.splitlines())

    assert cli.main(["status", "--no-gh"]) == 0
    out = capsys.readouterr().out
    assert any(line.strip() == "gh: skipped (--no-gh)" for line in out.splitlines())

    empty = env.tmp / "empty-projects"
    empty.write_text("")
    monkeypatch.setenv("LIME_PROJECTS", str(empty))
    assert cli.main(["status"]) == 0
    assert "nothing needs you" in capsys.readouterr().out


def status_violations(source: str):
    tree = ast.parse(source)
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            problems += [
                "import subprocess" for alias in node.names if alias.name == "subprocess"
            ]
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name in BANNED:
                problems.append(f"writer name: {name}")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                fname = func.id
            elif isinstance(func, ast.Attribute):
                fname = func.attr
            else:
                fname = None
            if fname == "open":
                problems.append("call to open")
            if fname == "connect":
                kwargs = {kw.arg for kw in node.keywords}
                if "readonly" not in kwargs:
                    problems.append("connect without readonly=True")
    return problems


def test_status_module_calls_no_writer():
    source = Path(status.__file__).read_text()
    assert status_violations(source) == []
    assert status_violations('def f(h):\n    home.write(h, "m", lambda p: None)\n')


# --- step 2: the ledger ------------------------------------------------------


def ledger(root):
    return tracer.Tracer(root / ".lime" / "runs")


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def add_run(tr, run_id, ticket_stem, status, started_at, *, error=None, ended_at=None,
            base_sha=None, pr_url=None):
    tr.start_run(
        run_id,
        workflow="feature",
        request="x",
        ticket=f".lime/tickets/{ticket_stem}.md",
        branch=f"lime/feature/{ticket_stem}",
    )
    if error is not None:
        phase_id = tr.add_phase(run_id, seq=1, name="build", stage="Build", kind="build")
        tr.set_phase_status(phase_id, "running")
        tr.set_phase_status(phase_id, "fail", error=error)
    if status == "deployed":
        tr.set_run_status(run_id, "success")
        tr.set_run_status(run_id, "deployed")
    elif status != "running":
        tr.set_run_status(run_id, status)
    tr.con.execute(
        "UPDATE runs SET started_at=?, ended_at=? WHERE run_id=?",
        (started_at, ended_at, run_id),
    )
    if base_sha is not None:
        tr.con.execute("UPDATE runs SET base_sha=? WHERE run_id=?", (base_sha, run_id))
    if pr_url is not None:
        tr.set_pr(run_id, git_helper.pr_number(pr_url), pr_url)
    tr.con.commit()
    return tr


def add_deploy_phase(tr, run_id, ended_at):
    phase_id = tr.add_phase(run_id, seq=2, name="deploy", stage="Build", kind="deploy")
    tr.set_phase_status(phase_id, "running")
    tr.set_phase_status(phase_id, "success")
    tr.con.execute(
        "UPDATE phases SET ended_at=? WHERE phase_id=?", (ended_at, phase_id)
    )
    tr.con.commit()
    return phase_id


def add_refusal(tr, ticket, reason, *, run_id=None, started_at=None, **detail):
    event_id = tr.refusal(ticket, reason, run_id=run_id, **detail)
    if started_at is not None:
        tr.con.execute(
            "UPDATE events SET started_at=? WHERE event_id=?", (started_at, event_id)
        )
        tr.con.commit()
    return event_id


def add_turn(tr, run_id, seq, role, runtime, model, cost, started_at):
    session_id = tr.start_session(
        run_id, f"{run_id}:{seq:02d}", seq=seq, role=role, runtime=runtime, model=model
    )
    event_id = tr.event(run_id, "turn", name=session_id, payload={"cost": cost})
    tr.con.execute(
        "UPDATE events SET started_at=? WHERE event_id=?", (started_at, event_id)
    )
    tr.con.commit()


_BREACH = "builder is read-only but modified 1 path(s):\n  - a.py — rolled back"

CLASSIFY_CASES = [
    pytest.param(
        "fail",
        "builder: blocked: STRIPE_KEY missing [worktree: /w, handoff: /h]",
        ("needs_you", "blocked"),
        id="agents.py:1104",
    ),
    pytest.param("fail", _BREACH, ("needs_you", "breach"), id="permissions.py:282-readonly"),
    pytest.param(
        "fail",
        "documenter is limited to ['AGENTS.md'] but modified 2 path(s):\n"
        "  - x — rolled back\n  - y — rolled back",
        ("needs_you", "breach"),
        id="permissions.py:282-limited",
    ),
    pytest.param(
        "fail",
        "builder is barred from ['.lime/trust.yaml'] but modified 1 path(s):\n"
        "  - .lime/trust.yaml — restored",
        ("needs_you", "breach"),
        id="permissions.py:282-barred",
    ),
    pytest.param(
        "fail",
        "builder: no envelope after the same `read` call repeated 3 times in a row"
        " + the 600s nudge window",
        ("failed", "fail"),
        id="agents.py:614-617",
    ),
    pytest.param(
        "fail",
        "builder: no envelope within the 1800s time ceiling + the 600s nudge window",
        ("failed", "fail"),
        id="agents.py:620",
    ),
    pytest.param(
        "fail",
        "builder: claude_code send for session 's' returned after its agent_map entry"
        " was replaced — refusing to book stale spend or overwrite the replacement",
        ("failed", "fail"),
        id="agents.py:767",
    ),
    pytest.param(
        "fail",
        "builder: backstop kill at ~12345 tokens",
        ("failed", "fail"),
        id="agents.py:802",
    ),
    pytest.param(
        "fail",
        "builder: adapter returned status='fail' after 3 attempts + 1 fresh-session"
        " retry: boom [worktree: /w]",
        ("failed", "fail"),
        id="agents.py:886",
    ),
    pytest.param(
        "fail",
        "builder: adapter returned status='fail' after 3 attempts: boom [worktree: /w]",
        ("failed", "fail"),
        id="agents.py:893",
    ),
    pytest.param(
        "fail",
        "builder: adapter returned status='fail' [worktree: /w]",
        ("failed", "fail"),
        id="agents.py:898",
    ),
    pytest.param(
        "fail",
        "builder: tool_call_leak — a text-only pi reply contained `<function=` or"
        " `<tool_call>` without a structured tool call or accepted submit;"
        " stream_fresh exhausted, no recovery [session: s]",
        ("failed", "fail"),
        id="agents.py:918",
    ),
    pytest.param(
        "fail",
        "builder: tool_call_leak — fresh session also leaked a text-only tool call"
        " without structured call or submit [session: s]",
        ("failed", "fail"),
        id="agents.py:936",
    ),
    pytest.param(
        "fail",
        "builder: tool_call_leak recovery returned status='fail' — boom [worktree: /w]",
        ("failed", "fail"),
        id="agents.py:943",
    ),
    pytest.param(
        "fail",
        "builder: a second partial Envelope in one Phase. [worktree: /w]",
        ("failed", "fail"),
        id="agents.py:1097",
    ),
    pytest.param(
        "fail",
        "builder failed gates after 3 attempt(s):\n- done means not met: x",
        ("failed", "fail"),
        id="agents.py:1193-1195",
    ),
    pytest.param(
        "fail",
        "builder reported status='fail': blocked: x",
        ("failed", "fail"),
        id="agents.py:1259",
    ),
    pytest.param(
        "fail",
        "builder never produced valid Plan JSON: boom — 3 replies, the last at 123"
        " context tokens",
        ("failed", "fail"),
        id="agents.py:1510",
    ),
    pytest.param("fail", "something odd", ("failed", "fail"), id="unknown"),
    pytest.param("fail", "", ("failed", "fail"), id="empty"),
    pytest.param("fail", None, ("failed", "fail"), id="none"),
    pytest.param(
        "budget_exhausted",
        "builder: a second partial Envelope in one Phase. [worktree: /w]",
        ("failed", "budget_exhausted"),
        id="budget_exhausted:1097",
    ),
    pytest.param("success", None, None, id="success"),
    pytest.param("deployed", None, None, id="deployed"),
    pytest.param("discarded", None, None, id="discarded"),
]


@pytest.mark.parametrize("run_status,error,expected", CLASSIFY_CASES)
def test_classify(run_status, error, expected):
    assert status.classify(run_status, error) == expected


def test_classifier_tracks_the_raise_sites():
    sources = {
        "lime/agents.py": 'f"{agent.name}: blocked: {notes}"',
        "lime/permissions.py": 'f"{agent.name} is {scope} but modified {len(breaches)} path(s):',
        "lime/tracer.py": 'payload={"ticket": ticket, **detail}',
        "lime/cli.py": "ticket=str(ticket_path),",
    }
    for path, needle in sources.items():
        text = Path(path).read_text()
        assert needle in text, f"{path} no longer carries {needle!r} — update lime/status.py classify"


def test_newest_run_alone_speaks_for_its_ticket(env, capsys):
    alpha = env.work / "alpha"
    tr = ledger(alpha)
    add_run(
        tr, "R1", "50-x", "fail", iso(T0 - timedelta(hours=2)),
        error="builder: blocked: old [worktree: /w, handoff: /h]",
        ended_at=iso(T0 - timedelta(hours=2)),
    )
    add_run(
        tr, "R2", "50-x", "fail", iso(T0 - timedelta(hours=1)),
        error="builder: no envelope within the 1800s time ceiling",
        ended_at=iso(T0 - timedelta(hours=1)),
    )
    add_run(
        tr, "R3", "51-x", "fail", iso(T0 - timedelta(hours=4)),
        error="builder: blocked: old [worktree: /w, handoff: /h]",
        ended_at=iso(T0 - timedelta(hours=4)),
    )
    add_run(tr, "R4", "51-x", "discarded", iso(T0 - timedelta(hours=3)))
    tr.con.close()

    report = run_status(capsys)
    items = [i for i in report["items"] if i["project"] == "alpha"]
    assert [i["run_id"] for i in items] == ["R2"]
    item = items[0]
    assert item["ticket"] == "50-x"
    assert item["group"] == "failed"
    assert item["reason"] == "fail"
    assert item["actions"] == ["lime run --resume R2", "lime runs rm R2"]
    assert not [i for i in report["items"] if i["ticket"] == "51-x"]


def test_breach_blocked_budget_and_stale_running_items(env, capsys, monkeypatch):
    alpha = env.work / "alpha"
    tr = ledger(alpha)
    add_run(
        tr, "R1", "50-x", "fail", iso(T0 - timedelta(hours=5)),
        error=_BREACH, ended_at=iso(T0 - timedelta(hours=5)),
    )
    add_run(tr, "R2", "51-x", "budget_exhausted", iso(T0 - timedelta(hours=4)),
            ended_at=iso(T0 - timedelta(hours=4)))
    kill = tr.event("R2", "timeout_kill")
    tr.con.execute("UPDATE events SET started_at=? WHERE event_id=?", (iso(T0 - timedelta(hours=4)), kill))

    add_run(tr, "R3", "52-x", "running", iso(T0 - timedelta(hours=3)))
    added = tr.event("R3", "log")
    tr.con.execute("UPDATE events SET started_at=? WHERE event_id=?", (iso(T0 - timedelta(hours=2)), added))
    tr.start_process("R3", kind="run", name="run", pid=999999, command="lime run")
    tr.con.commit()

    add_run(tr, "R4", "53-x", "running", iso(T0 - timedelta(hours=3)))
    tr.start_process("R4", kind="run", name="run", pid=os.getpid(), command="lime run")
    tr.con.commit()

    add_run(tr, "R5", "54-x", "success", iso(T0 - timedelta(hours=3)),
            ended_at=iso(T0 - timedelta(hours=3)))
    tr.con.close()

    monkeypatch.setattr(status.tracer, "pid_alive", lambda pid: pid == os.getpid())
    report = run_status(capsys)
    items = {i["run_id"]: i for i in report["items"] if i["project"] == "alpha"}

    assert items["R1"]["group"] == "needs_you"
    assert items["R1"]["reason"] == "breach"
    assert items["R1"]["text"] == _BREACH
    assert items["R2"]["group"] == "failed"
    assert items["R2"]["reason"] == "budget_exhausted"
    assert items["R2"]["cause"] == "timeout_kill"
    assert items["R3"]["reason"] == "stale_running"
    assert items["R3"]["since"] == iso(T0 - timedelta(hours=2))
    assert "R4" not in items
    assert "R5" not in items


def test_stale_running_follows_tracer_liveness(env, capsys, monkeypatch):
    alpha = env.work / "alpha"
    tr = ledger(alpha)
    now = datetime.now(timezone.utc)

    # B1: a fresh beat beats even a dead pid.
    add_run(tr, "B1", "60-a", "running", iso(now - timedelta(minutes=5)))
    b1 = tr.start_process("B1", kind="run", name="run", pid=999999, command="c")
    tr.con.execute(
        "UPDATE processes SET heartbeat_at=? WHERE id=?",
        (iso(now - timedelta(seconds=5)), b1),
    )
    # B2: a stale beat with a live pid is still stale.
    add_run(tr, "B2", "61-b", "running", iso(now - timedelta(minutes=5)))
    b2 = tr.start_process("B2", kind="run", name="run", pid=os.getpid(), command="c")
    tr.con.execute(
        "UPDATE processes SET heartbeat_at=? WHERE id=?",
        (iso(now - timedelta(minutes=2)), b2),
    )
    # B3: no row, a Run born seconds ago is still starting.
    add_run(tr, "B3", "62-c", "running", iso(now - timedelta(seconds=5)))
    # B4: no row, an old Run is stale.
    add_run(tr, "B4", "63-d", "running", iso(now - timedelta(minutes=5)))
    tr.con.commit()
    tr.con.close()

    monkeypatch.setattr(status.tracer, "pid_alive", lambda pid: pid == os.getpid())
    report = run_status(capsys)
    items = {i["run_id"]: i for i in report["items"] if i["project"] == "alpha"}

    assert "B1" not in items
    assert items["B2"]["reason"] == "stale_running"
    assert "B3" not in items
    assert items["B4"]["reason"] == "stale_running"


def test_refusal_rows_until_a_later_launch_or_deploy(env, capsys):
    alpha = env.work / "alpha"
    tr = ledger(alpha)
    add_refusal(tr, "60-a", "overlap_refused", run_id="-", started_at=iso(T0))

    add_run(tr, "R6", "61-b", "success", iso(T0 - timedelta(hours=2)),
            ended_at=iso(T0 - timedelta(hours=2)))
    add_refusal(tr, "61-b", "ci_red", run_id="R6", started_at=iso(T0 - timedelta(hours=1)))

    add_refusal(tr, "62-c", "overlap_held", started_at=iso(T0 - timedelta(hours=2)))
    add_run(tr, "R7", "62-c", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)))

    add_run(tr, "R8", "63-d", "deployed", iso(T0 - timedelta(hours=2)),
            ended_at=iso(T0 - timedelta(hours=2)))
    add_refusal(tr, "63-d", "merge_conflict", run_id="R8", started_at=iso(T0 - timedelta(hours=1)))

    add_run(
        tr, "R9", "64-e", "fail", iso(T0 - timedelta(hours=2)),
        error="builder: blocked: handoff [worktree: /w, handoff: /h]",
        ended_at=iso(T0 - timedelta(hours=2)),
    )
    add_refusal(tr, "64-e", "pr_not_open", run_id="R9", started_at=iso(T0 - timedelta(hours=1)))
    tr.con.close()

    report = run_status(capsys)
    items = [i for i in report["items"] if i["project"] == "alpha"]
    refusals = {i["ticket"]: i for i in items if i["reason"] == "refusal"}

    assert set(refusals) == {"60-a", "61-b"}
    assert refusals["60-a"]["run_id"] is None
    assert refusals["60-a"]["group"] == "needs_you"
    assert refusals["60-a"]["text"].startswith("overlap_refused")
    assert refusals["61-b"]["run_id"] == "R6"
    assert not [i for i in items if i["ticket"] == "62-c" and i["reason"] == "refusal"]
    assert not [i for i in items if i["ticket"] == "63-d"]
    blocked = [i for i in items if i["ticket"] == "64-e"]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "blocked"


def test_person_ticket_with_a_refusal_is_one_item(env, capsys):
    alpha = env.work / "alpha"
    write_ticket(alpha, "60-a-person", "Type: task", "Status: open")
    tr = ledger(alpha)
    add_refusal(tr, "60-a-person", "overlap_refused", started_at=iso(T0))
    tr.con.close()

    report = run_status(capsys)
    items = [i for i in report["items"] if i["project"] == "alpha"]
    assert len(items) == 1
    assert items[0]["ticket"] == "60-a-person"
    assert items[0]["reason"] == "refusal"


def test_not_launched_is_one_count_row_per_project(env, capsys):
    alpha = env.work / "alpha"
    for stem, headers in (
        ("70-a", ["Workflow: feature", "Type: feature", "Status: open"]),
        ("71-b", ["Workflow: feature", "Type: feature", "Status: open"]),
        ("72-c", ["Workflow: feature", "Type: feature", "Status: open"]),
        ("73-d", ["Workflow: feature", "Type: feature", "Status: open", "Blocked by: block.md"]),
        ("74-e", ["Workflow: feature", "Type: feature", "Status: claimed"]),
        ("75-f", ["Workflow: feature", "Type: task", "Status: open"]),
    ):
        write_ticket(alpha, stem, *headers)
    (alpha / "block.md").write_text("x")
    tr = ledger(alpha)
    add_run(tr, "R1", "71-b", "discarded", iso(T0 - timedelta(hours=2)))
    add_run(
        tr, "R2", "72-c", "fail", iso(T0 - timedelta(hours=1)),
        error="builder: no envelope within the 1800s time ceiling",
        ended_at=iso(T0 - timedelta(hours=1)),
    )
    add_run(tr, "R3", "75-f", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)))
    tr.con.close()

    write_ticket(env.work / "beta", "80-x", "Workflow: feature", "Type: feature", "Status: open")
    write_ticket(env.work / "beta", "81-y", "Workflow: feature", "Type: feature", "Status: open")

    report = run_status(capsys)
    rows = [i for i in report["items"] if i["reason"] == "not_launched"]
    by_project = {i["project"]: i for i in rows}
    assert by_project["alpha"]["count"] == 2
    assert by_project["beta"]["count"] == 2
    assert not [i for i in report["items"] if i["ticket"] == "75-f" and i["reason"] == "ticket"]


def test_cost_splits_cash_from_subscription_since_previous_visit(env, capsys):
    previous = T0 - timedelta(days=1)
    make_home(env, last_visit=(previous, T0))
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "50-x", "success", iso(previous - timedelta(days=1)),
            ended_at=iso(previous - timedelta(days=1)))
    add_turn(tr, "R1", 1, "builder", "pi", "openrouter/deepseek/x", 1.00,
             iso(previous - timedelta(hours=1)))
    add_turn(tr, "R1", 2, "builder", "pi", "openrouter/deepseek/x", 0.50,
             iso(previous + timedelta(hours=1)))
    add_turn(tr, "R1", 3, "builder", "claude_code", "claude-opus-5-5", 2.00,
             iso(previous + timedelta(hours=2)))
    add_turn(tr, "R1", 4, "builder", "pi", "openai-codex/gpt-6-astra", 0.25,
             iso(previous + timedelta(hours=3)))
    tr.con.close()

    report = run_status(capsys)
    cost = report["cost"]
    assert cost["window"] == "since_last_visit"
    assert cost["since"] == previous.astimezone(timezone.utc).isoformat()
    assert cost["cash_usd"] == pytest.approx(0.5)
    assert cost["subscription_usd_equiv"] == pytest.approx(2.25)
    assert cost["by_provider"] == {
        "openrouter": pytest.approx(0.5),
        "claude_code": pytest.approx(2.0),
        "openai-codex": pytest.approx(0.25),
    }


def test_cost_without_home_reads_the_whole_ledger(env, capsys):
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "50-x", "success", iso(T0 - timedelta(days=2)),
            ended_at=iso(T0 - timedelta(days=2)))
    add_turn(tr, "R1", 1, "builder", "pi", "openrouter/deepseek/x", 1.00, iso(T0 - timedelta(days=3)))
    add_turn(tr, "R1", 2, "builder", "pi", "openrouter/deepseek/x", 0.50, iso(T0 - timedelta(days=1)))
    tr.con.close()

    report = run_status(capsys)
    assert report["cost"]["window"] == "all"
    assert report["cost"]["since"] is None
    assert report["cost"]["cash_usd"] == pytest.approx(1.5)


def test_ledger_error_reports_the_project_and_carries_on(env, capsys):
    alpha = env.work / "alpha"
    write_ticket(alpha, "10-a-task", "Type: task", "Status: open")
    runs = alpha / ".lime" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "lime.db").write_bytes(b"not a db")
    write_ticket(env.work / "beta", "20-b-task", "Type: task", "Status: open")

    report = run_status(capsys)
    alpha_row = next(p for p in report["projects"] if p["name"] == "alpha")
    assert alpha_row["db"] is True
    assert alpha_row["error"] is not None
    assert any(i["project"] == "alpha" and i["ticket"] == "10-a-task" for i in report["items"])
    assert any(i["project"] == "beta" and i["ticket"] == "20-b-task" for i in report["items"])


def test_done_lists_deployed_runs_after_previous_visit_newest_first(env, capsys):
    make_home(env, last_visit=(T0 - timedelta(days=1), T0))
    tr = ledger(env.work / "alpha")
    add_run(tr, "R10", "10-a", "deployed", iso(T0 - timedelta(hours=3)),
            ended_at=iso(T0 - timedelta(hours=3)))
    add_deploy_phase(tr, "R10", iso(T0 - timedelta(hours=2)))
    add_run(tr, "R11", "11-b", "deployed", iso(T0 - timedelta(hours=6)),
            ended_at=iso(T0 - timedelta(hours=5)))
    add_run(tr, "R12", "12-c", "deployed", iso(T0 - timedelta(days=3)),
            ended_at=iso(T0 - timedelta(days=3)))
    add_deploy_phase(tr, "R12", iso(T0 - timedelta(days=3)))
    tr.con.close()

    report = run_status(capsys)
    done = [i for i in report["items"] if i["group"] == "done"]
    assert [i["ticket"] for i in done] == ["10-a", "11-b"]
    assert all(i["reason"] == "deployed" for i in done)
    assert all(i["actions"] == [] for i in done)
    assert done[0]["since"] == iso(T0 - timedelta(hours=2))
    assert done[1]["since"] == iso(T0 - timedelta(hours=5))
    assert not [i for i in report["items"] if i["ticket"] == "12-c"]
    alpha = next(p for p in report["projects"] if p["name"] == "alpha")
    assert alpha["counts"]["done"] == 2


def test_done_is_empty_without_a_last_visit(env, capsys):
    make_home(env)
    tr = ledger(env.work / "alpha")
    add_run(tr, "R10", "10-a", "deployed", iso(T0 - timedelta(hours=3)),
            ended_at=iso(T0 - timedelta(hours=3)))
    add_deploy_phase(tr, "R10", iso(T0 - timedelta(hours=2)))
    tr.con.close()

    report = run_status(capsys)
    assert not [i for i in report["items"] if i["group"] == "done"]


def test_report_carries_read_ms_and_no_gh_note(env, capsys):
    tr = ledger(env.work / "beta")
    add_run(tr, "B1", "20-b", "fail", iso(T0 - timedelta(hours=1)),
            error="builder: no envelope within the 1800s time ceiling",
            ended_at=iso(T0 - timedelta(hours=1)))
    tr.con.close()

    data = run_status(capsys, "--no-gh")
    assert set(data["read_ms"]) == {"db", "gh", "git", "total"}
    assert all(isinstance(v, int) and v >= 0 for v in data["read_ms"].values())
    assert any(n.startswith("gh: skipped") for n in data["notes"])
    assert any(
        i["project"] == "beta" and i["group"] == "failed" for i in data["items"]
    )

    data = run_status(capsys)
    assert not any(n.startswith("gh: skipped") for n in data["notes"])


# --- step 3: the gh + git pass ----------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_gh(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("unexpected gh call")
    monkeypatch.setattr(status.git_helper, "pr_view", boom)


def _git_ok(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


def git_project(env, name):
    """Turn `work/<name>` into a clone of a fresh bare `origin-<name>.git` whose
    `main` (and `origin/HEAD`) carries one seed commit `a.txt`."""
    root = env.work / name
    bare = env.tmp / f"origin-{name}.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True,
    )
    seed = env.tmp / f"seed-{name}"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)],
                   check=True, capture_output=True)
    (seed / "a.txt").write_text("a\n")
    _git_ok(seed, "add", "-A")
    _git_ok(seed, "commit", "-q", "-m", "seed")
    _git_ok(seed, "push", "-q", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(bare), str(root)],
                   check=True, capture_output=True)
    _git_ok(root, "remote", "set-head", "origin", "main")
    return root


def push_branch(root, branch, files, start="main"):
    _git_ok(root, "checkout", "-q", "-B", branch, start)
    for rel, text in files.items():
        (root / rel).write_text(text)
    _git_ok(root, "add", "-A")
    _git_ok(root, "commit", "-q", "-m", branch)
    _git_ok(root, "push", "-q", "origin", branch)


def merge_side(root, frm="main"):
    _git_ok(root, "checkout", "-q", frm)
    _git_ok(root, "checkout", "-q", "-B", "side", frm)
    (root / "c.txt").write_text("c\n")
    _git_ok(root, "add", "-A")
    _git_ok(root, "commit", "-q", "-m", "side")
    _git_ok(root, "checkout", "-q", frm)
    _git_ok(root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    _git_ok(root, "push", "-q", "origin", frm)


def edit_main(root, text):
    _git_ok(root, "checkout", "-q", "main")
    (root / "a.txt").write_text(text)
    _git_ok(root, "add", "-A")
    _git_ok(root, "commit", "-q", "-m", "main edit")
    _git_ok(root, "push", "-q", "origin", "main")


def fake_pr(state="OPEN", checks=(), merged_at=None, url="https://example.com/pull/7"):
    return {
        "state": state,
        "statusCheckRollup": list(checks),
        "url": url,
        "mergedAt": merged_at,
    }


def test_open_pr_with_green_ci_is_ready_with_base_moved_note(env, capsys, monkeypatch):
    project = git_project(env, "alpha")
    base_sha = _git_ok(project, "rev-parse", "origin/main").stdout.strip()
    push_branch(project, "lime/feature/30-r", {"b.txt": "b\n"})
    merge_side(project)
    url = "https://example.com/pull/7"
    monkeypatch.setattr(status.git_helper, "pr_view", lambda *a, **k: fake_pr(url=url))
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "30-r", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)), base_sha=base_sha, pr_url=url)
    tr.con.close()

    report = run_status(capsys)
    item = next(i for i in report["items"] if i["ticket"] == "30-r")
    assert item["group"] == "ready"
    assert item["reason"] == "ready"
    assert item["note"] == "base moved 1"
    assert item["pr"] == {"number": 7, "url": url, "state": "OPEN", "ci": "green"}
    assert item["actions"] == ["lime deploy 30-r"]


def test_open_pr_with_unmoved_base_is_ready_without_note(env, capsys, monkeypatch):
    project = git_project(env, "beta")
    base_sha = _git_ok(project, "rev-parse", "origin/main").stdout.strip()
    push_branch(project, "lime/feature/31-e", {"b.txt": "b\n"})
    url = "https://example.com/pull/8"
    monkeypatch.setattr(status.git_helper, "pr_view", lambda *a, **k: fake_pr(url=url))
    tr = ledger(env.work / "beta")
    add_run(tr, "R1", "31-e", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)), base_sha=base_sha, pr_url=url)
    tr.con.close()

    report = run_status(capsys)
    item = next(i for i in report["items"] if i["ticket"] == "31-e")
    assert item["group"] == "ready"
    assert item["note"] is None


def test_open_pr_with_red_ci_is_needs_you_ci_red(env, capsys, monkeypatch):
    project = git_project(env, "alpha")
    base_sha = _git_ok(project, "rev-parse", "origin/main").stdout.strip()
    push_branch(project, "lime/feature/32-r", {"b.txt": "b\n"})
    url = "https://example.com/pull/9"
    monkeypatch.setattr(
        status.git_helper, "pr_view",
        lambda *a, **k: fake_pr(checks=[{"conclusion": "FAILURE"}], url=url),
    )
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "32-r", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)), base_sha=base_sha, pr_url=url)
    add_run(tr, "R2", "33-f", "fail", iso(T0 - timedelta(hours=2)),
            error="builder: no envelope within the 1800s time ceiling",
            ended_at=iso(T0 - timedelta(hours=2)))
    tr.con.close()

    report = run_status(capsys)
    items = report["items"]
    red = next(i for i in items if i["ticket"] == "32-r")
    assert red["group"] == "needs_you"
    assert red["reason"] == "ci_red"
    assert red["actions"] == []
    red_idx = next(i for i, x in enumerate(items) if x["ticket"] == "32-r")
    failed_idx = next(i for i, x in enumerate(items) if x["ticket"] == "33-f")
    assert red_idx < failed_idx


def test_merge_tree_conflict_is_needs_you_conflict(env, capsys, monkeypatch):
    project = git_project(env, "alpha")
    base_sha = _git_ok(project, "rev-parse", "origin/main").stdout.strip()
    push_branch(project, "lime/feature/34-c", {"a.txt": "pr\n"})
    edit_main(project, "main\n")
    url = "https://example.com/pull/10"
    monkeypatch.setattr(status.git_helper, "pr_view", lambda *a, **k: fake_pr(url=url))
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "34-c", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)), base_sha=base_sha, pr_url=url)
    tr.con.close()

    report = run_status(capsys)
    item = next(i for i in report["items"] if i["ticket"] == "34-c")
    assert item["group"] == "needs_you"
    assert item["reason"] == "conflict"
    assert item["text"] == "conflict in: a.txt"


def test_pending_ci_without_conflict_is_no_item(env, capsys, monkeypatch):
    project = git_project(env, "alpha")
    base_sha = _git_ok(project, "rev-parse", "origin/main").stdout.strip()
    push_branch(project, "lime/feature/35-p", {"b.txt": "b\n"})
    url = "https://example.com/pull/11"
    monkeypatch.setattr(
        status.git_helper, "pr_view",
        lambda *a, **k: fake_pr(checks=[{"conclusion": ""}], url=url),
    )
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "35-p", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)), base_sha=base_sha, pr_url=url)
    tr.con.close()

    report = run_status(capsys)
    assert not [i for i in report["items"] if i["ticket"] == "35-p"]


def test_green_pr_with_failed_git_read_is_not_ready(env, capsys, monkeypatch):
    project = git_project(env, "alpha")
    push_branch(project, "lime/feature/36-g", {"b.txt": "b\n"})
    url = "https://example.com/pull/12"
    monkeypatch.setattr(status.git_helper, "pr_view", lambda *a, **k: fake_pr(url=url))

    def no_origin(*args, **kwargs):
        raise RuntimeError("no origin")

    monkeypatch.setattr(status.git_helper, "fetch_origin", no_origin)
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "36-g", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)), pr_url=url)
    tr.con.close()

    report = run_status(capsys)
    assert not [i for i in report["items"] if i["ticket"] == "36-g"]
    assert any(n.startswith("git: skipped (alpha:") for n in report["notes"])


def test_red_ci_still_speaks_when_git_read_fails(env, capsys, monkeypatch):
    project = git_project(env, "alpha")
    push_branch(project, "lime/feature/37-g", {"b.txt": "b\n"})
    url = "https://example.com/pull/13"
    monkeypatch.setattr(
        status.git_helper, "pr_view",
        lambda *a, **k: fake_pr(checks=[{"conclusion": "FAILURE"}], url=url),
    )

    def no_origin(*args, **kwargs):
        raise RuntimeError("no origin")

    monkeypatch.setattr(status.git_helper, "fetch_origin", no_origin)
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "37-g", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)), pr_url=url)
    tr.con.close()

    report = run_status(capsys)
    red = next(i for i in report["items"] if i["ticket"] == "37-g")
    assert red["group"] == "needs_you"
    assert red["reason"] == "ci_red"


def test_hand_merged_pr_is_done_and_closed_pr_is_no_item(env, capsys, monkeypatch):
    make_home(env, last_visit=(T0 - timedelta(days=1), T0))
    urls = {
        "40": "https://example.com/pull/40",
        "41": "https://example.com/pull/41",
        "42": "https://example.com/pull/42",
    }

    def stub(selector, *, cwd=None, timeout=None):
        if selector == urls["40"]:
            return fake_pr(state="MERGED", merged_at=iso(T0 - timedelta(hours=2)), url=selector)
        if selector == urls["41"]:
            return fake_pr(state="CLOSED", url=selector)
        return fake_pr(state="MERGED", merged_at=iso(T0 - timedelta(days=3)), url=selector)

    monkeypatch.setattr(status.git_helper, "pr_view", stub)
    tr = ledger(env.work / "alpha")
    add_run(tr, "R40", "40-m", "success", iso(T0 - timedelta(hours=3)),
            ended_at=iso(T0 - timedelta(hours=3)), pr_url=urls["40"])
    add_run(tr, "R41", "41-c", "success", iso(T0 - timedelta(hours=3)),
            ended_at=iso(T0 - timedelta(hours=3)), pr_url=urls["41"])
    add_run(tr, "R42", "42-o", "success", iso(T0 - timedelta(days=3)),
            ended_at=iso(T0 - timedelta(days=3)), pr_url=urls["42"])
    tr.con.close()

    report = run_status(capsys)
    done = [i for i in report["items"] if i["group"] == "done"]
    assert [i["ticket"] for i in done] == ["40-m"]
    assert done[0]["reason"] == "merged"
    assert done[0]["since"] == iso(T0 - timedelta(hours=2))
    assert not [i for i in report["items"] if i["ticket"] in ("41-c", "42-o")]


def test_merged_and_closed_prs_resolve_local_refusals(env, capsys, monkeypatch):
    make_home(env, last_visit=(T0 - timedelta(days=1), T0))
    urls = {
        "43": "https://example.com/pull/43",
        "44": "https://example.com/pull/44",
    }

    def stub(selector, *, cwd=None, timeout=None):
        if selector == urls["43"]:
            return fake_pr(state="MERGED", merged_at=iso(T0 - timedelta(hours=2)), url=selector)
        return fake_pr(state="CLOSED", url=selector)

    monkeypatch.setattr(status.git_helper, "pr_view", stub)
    tr = ledger(env.work / "alpha")
    add_run(tr, "R43", "43-m", "success", iso(T0 - timedelta(hours=4)),
            ended_at=iso(T0 - timedelta(hours=4)), pr_url=urls["43"])
    add_run(tr, "R44", "44-c", "success", iso(T0 - timedelta(hours=4)),
            ended_at=iso(T0 - timedelta(hours=4)), pr_url=urls["44"])
    add_refusal(tr, "43-m", "ci_red", run_id="R43", started_at=iso(T0 - timedelta(hours=3)))
    add_refusal(tr, "44-c", "merge_conflict", run_id="R44", started_at=iso(T0 - timedelta(hours=3)))
    tr.con.close()

    report = run_status(capsys)
    done = [i for i in report["items"] if i["group"] == "done"]
    assert [i["ticket"] for i in done] == ["43-m"]
    assert not [i for i in report["items"] if i["reason"] == "refusal"]
    assert not [i for i in report["items"] if i["ticket"] == "44-c"]


def test_success_run_without_persisted_pr_makes_no_gh_call(env, capsys):
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "50-x", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)))
    tr.con.close()

    report = run_status(capsys)
    assert not [i for i in report["items"] if i["ticket"] == "50-x"]


@pytest.mark.parametrize(
    "message",
    ["gh pr view failed: HTTP 502", "gh pr view timed out after 20s"],
)
def test_gh_failure_or_timeout_skips_pr_reasons_and_keeps_local_groups(
    env, capsys, monkeypatch, message
):
    def stub(selector, *, cwd=None, timeout=None):
        raise RuntimeError(message)

    monkeypatch.setattr(status.git_helper, "pr_view", stub)
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "60-o", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)), pr_url="https://example.com/pull/60")
    tr.con.close()
    tr = ledger(env.work / "beta")
    add_run(tr, "B1", "70-f", "fail", iso(T0 - timedelta(hours=1)),
            error="builder: no envelope within the 1800s time ceiling",
            ended_at=iso(T0 - timedelta(hours=1)))
    tr.con.close()

    report = run_status(capsys)
    reasons = {i["reason"] for i in report["items"]}
    assert not reasons & {"ready", "ci_red", "conflict", "merged"}
    assert any(i["project"] == "beta" and i["group"] == "failed" for i in report["items"])
    assert any(n.startswith("gh: skipped") for n in report["notes"])

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert any(line.startswith("gh: skipped") for line in out.splitlines())


def test_no_gh_never_calls_gh(env, capsys):
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "60-o", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)), pr_url="https://example.com/pull/60")
    tr.con.close()

    report = run_status(capsys, "--no-gh")
    assert not [i for i in report["items"] if i["ticket"] == "60-o"]


def test_gh_reads_run_in_parallel_across_projects(env, capsys, monkeypatch):
    for name, stem in (("alpha", "80-a"), ("beta", "81-b")):
        project = git_project(env, name)
        push_branch(project, f"lime/feature/{stem}", {"b.txt": "b\n"})
        tr = ledger(env.work / name)
        add_run(tr, "R1", stem, "success", iso(T0 - timedelta(hours=1)),
                ended_at=iso(T0 - timedelta(hours=1)),
                pr_url=f"https://example.com/pull/{stem.split('-')[0]}")
        tr.con.close()

    barrier = threading.Barrier(2, timeout=5)

    def stub(selector, *, cwd=None, timeout=None):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            raise AssertionError("gh reads ran serially")
        return fake_pr(url=selector)

    monkeypatch.setattr(status.git_helper, "pr_view", stub)

    report = run_status(capsys)
    ready = {i["project"] for i in report["items"] if i["group"] == "ready"}
    assert ready == {"alpha", "beta"}
    assert isinstance(report["read_ms"]["gh"], int)


def test_status_with_open_pr_leaves_ledger_git_refs_and_checkout_unchanged(
    env, capsys, monkeypatch
):
    project = git_project(env, "alpha")
    base_sha = _git_ok(project, "rev-parse", "origin/main").stdout.strip()
    push_branch(project, "lime/feature/90-o", {"b.txt": "b\n"})
    url = "https://example.com/pull/90"
    monkeypatch.setattr(status.git_helper, "pr_view", lambda *a, **k: fake_pr(url=url))
    tr = ledger(env.work / "alpha")
    add_run(tr, "R1", "90-o", "success", iso(T0 - timedelta(hours=1)),
            ended_at=iso(T0 - timedelta(hours=1)), base_sha=base_sha, pr_url=url)
    tr.con.close()

    db = env.work / "alpha" / ".lime" / "runs" / "lime.db"
    dump_before = _dump(db)
    refs_before = _git_ok(project, "for-each-ref", "refs/heads").stdout
    head_before = _git_ok(project, "rev-parse", "HEAD").stdout
    status_before = _git_ok(project, "--no-optional-locks", "status", "--porcelain").stdout

    assert cli.main(["status", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    capsys.readouterr()

    assert _dump(db) == dump_before
    assert _git_ok(project, "for-each-ref", "refs/heads").stdout == refs_before
    assert _git_ok(project, "rev-parse", "HEAD").stdout == head_before
    assert _git_ok(project, "--no-optional-locks", "status", "--porcelain").stdout == status_before


# --- ticket 257: dead batch --------------------------------------------------


NOW = "NOW"


def local(hour, minute):
    return datetime(2026, 11, 5, hour, minute).astimezone().isoformat()


def add_batch(tr, batch_id, *, beat=None, ended_at=None, retried_at=None,
              retry_of=None, children=()):
    pid_row = tr.start_process(
        batch_id, kind="batch", name="lime", pid=999999, command="lime run",
        retry_of=retry_of,
    )
    tr.con.execute(
        "UPDATE processes SET heartbeat_at=?, ended_at=?, retried_at=? WHERE id=?",
        (beat, ended_at, retried_at, pid_row),
    )
    for position, (ticket, state) in enumerate(children):
        tr.con.execute(
            "INSERT INTO batch_children (batch_id, position, ticket, state, reason,"
            " updated_at) VALUES (?,?,?,?,?,?)",
            (batch_id, position, ticket, state, None, iso(T0)),
        )
    tr.con.commit()


def batch_rows(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        processes = con.execute("SELECT * FROM processes ORDER BY id").fetchall()
        children = con.execute(
            "SELECT * FROM batch_children ORDER BY batch_id, position"
        ).fetchall()
        runs = con.execute("SELECT * FROM runs ORDER BY run_id").fetchall()
    finally:
        con.close()
    return processes, children, runs


def _resolve(value):
    return iso(datetime.now(timezone.utc)) if value == NOW else value


BATCH_CASES = [
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None, "retried_at": None,
             "retry_of": None,
             "children": [("246-a", "queued"), ("247-b", "queued")]},
        ],
        ["batch died 14:02 · not retried · not started: 246, 247"],
        id="not-retried",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": local(14, 3), "retry_of": None,
             "children": [("246-a", "queued")]},
        ],
        ["batch died 14:02 · retry failed to start · not started: 246"],
        id="retry-failed-to-start",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": local(14, 3), "retry_of": None,
             "children": [("246-a", "queued"), ("247-b", "queued")]},
            {"id": "R1", "beat": local(14, 20), "ended_at": None, "retried_at": None,
             "retry_of": "B1",
             "children": [("246-a", "started"), ("247-b", "queued")]},
        ],
        ["batch died 14:02 · retry died 14:20 · not started: 247"],
        id="retry-died",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": NOW, "ended_at": None, "retried_at": None,
             "retry_of": None, "children": [("246-a", "queued")]},
        ],
        [],
        id="live",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 30), "ended_at": local(14, 30),
             "retried_at": None, "retry_of": None,
             "children": [("246-a", "started"), ("247-b", "started")]},
        ],
        [],
        id="finished",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": local(14, 5),
             "retried_at": None, "retry_of": None,
             "children": [("246-a", "started"), ("247-b", "queued")]},
        ],
        [],
        id="ctrl-c",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": local(14, 3), "retry_of": None,
             "children": [("246-a", "queued"), ("247-b", "queued")]},
            {"id": "R1", "beat": NOW, "ended_at": None, "retried_at": None,
             "retry_of": "B1", "children": [("246-a", "started")]},
        ],
        [],
        id="retry-live",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": local(14, 3), "retry_of": None,
             "children": [("246-a", "queued"), ("247-b", "queued")]},
            {"id": "R1", "beat": local(14, 40), "ended_at": local(14, 40),
             "retried_at": None, "retry_of": "B1",
             "children": [("246-a", "started")]},
        ],
        [],
        id="retry-finished",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": local(14, 3), "retry_of": None,
             "children": [("246-a", "queued"), ("247-b", "queued")]},
            {"id": "R1", "beat": local(14, 20), "ended_at": local(14, 21),
             "retried_at": None, "retry_of": "B1",
             "children": [("246-a", "started"), ("247-b", "queued")]},
        ],
        [],
        id="retry-ctrl-c",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None, "retried_at": None,
             "retry_of": None,
             "children": [("246-a", "queued"), ("250-e", "paused"),
                          ("247-b", "dropped"), ("248-c", "started"),
                          ("249-d", "queued")]},
        ],
        ["batch died 14:02 · not retried · not started: 246, 249"],
        id="unknown-child-state",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": None, "ended_at": None, "retried_at": None,
             "retry_of": None, "children": [("246-a", "queued")]},
        ],
        ["batch died · not retried · not started: 246"],
        id="no-beat",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": local(14, 3), "retry_of": None,
             "children": [("246-a", "queued"), ("247-b", "queued")],
             "runs": [("H1", "247-b", "success", local(14, 30))]},
            {"id": "R1", "beat": local(14, 20), "ended_at": None,
             "retried_at": None, "retry_of": "B1",
             "children": [("246-a", "started"), ("247-b", "queued")]},
        ],
        [],
        id="hand-launched-run",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": None, "retry_of": None,
             "children": [("246-a", "queued")],
             "runs": [("H1", "246-a", "success", local(13, 0))]},
        ],
        ["batch died 14:02 · not retried · not started: 246"],
        id="run-before-death",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": None, "retry_of": None,
             "children": [("246-a", "queued")],
             "runs": [("H1", "246-a", "discarded", local(14, 30))]},
        ],
        [],
        id="discarded-run",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": None, "retry_of": None,
             "children": [("246-a", "queued"), ("247-b", "queued")],
             "runs": [("H1", "246-a", "discarded", local(14, 30))]},
        ],
        ["batch died 14:02 · not retried · not started: 247"],
        id="discarded-run-clears-own-ticket",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": None, "retry_of": None,
             "children": [("246-a", "queued"), ("247-b", "queued")],
             "closed": ["246-a"]},
        ],
        ["batch died 14:02 · not retried · not started: 247"],
        id="closed-ticket",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": local(14, 3), "retry_of": None,
             "children": [("246-a", "queued"), ("247-b", "queued"),
                          ("248-c", "queued")],
             "runs": [("H1", "246-a", "success", local(14, 30))],
             "closed": ["247-b"]},
        ],
        ["batch died 14:02 · retry failed to start · not started: 248"],
        id="partly-picked-up",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": local(14, 3), "retry_of": None,
             "children": [("246-a", "started")]},
        ],
        [],
        id="nothing-left-claimed-retry",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": None, "retry_of": None,
             "children": [("246-a", "queued")],
             "gone": ["246-a"]},
        ],
        [],
        id="nothing-left-deleted-ticket",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": local(14, 2), "ended_at": None,
             "retried_at": None, "retry_of": None,
             "children": [("246-a", "started"), ("247-b", "dropped")]},
        ],
        [],
        id="nothing-left-not-retried",
    ),
    pytest.param(
        [
            {"id": "B1", "beat": None, "ended_at": None,
             "retried_at": None, "retry_of": None,
             "children": [("246-a", "queued")],
             "runs": [("H1", "246-a", "success", NOW)]},
        ],
        [],
        id="no-beat-run-after-start",
    ),
]


@pytest.mark.parametrize("batches,expected", BATCH_CASES)
def test_dead_batch_line_follows_the_batch_rows(env, capsys, monkeypatch, batches, expected):
    alpha = env.work / "alpha"
    tr = ledger(alpha)
    for spec in batches:
        add_batch(
            tr, spec["id"],
            beat=_resolve(spec["beat"]),
            ended_at=_resolve(spec["ended_at"]),
            retried_at=_resolve(spec["retried_at"]),
            retry_of=spec["retry_of"],
            children=spec["children"],
        )
    stems = list(
        dict.fromkeys(
            ticket for spec in batches for ticket, _ in spec["children"]
        )
    )
    gone = {s for spec in batches for s in spec.get("gone", ())}
    closed = {s for spec in batches for s in spec.get("closed", ())}
    for stem in stems:
        if stem in gone:
            continue
        write_ticket(
            alpha, stem, "Workflow: bug-fix",
            "Status: closed" if stem in closed else "Status: open",
        )
    for spec in batches:
        for run_id, stem, run_state, started_at in spec.get("runs", ()):
            add_run(tr, run_id, stem, run_state, _resolve(started_at))
    tr.con.close()
    db = alpha / ".lime" / "runs" / "lime.db"
    monkeypatch.setattr(status.tracer, "pid_alive", lambda pid: False)

    tickets_dir = alpha / ".lime" / "tickets"
    files_before = {p.name: p.read_bytes() for p in sorted(tickets_dir.glob("*.md"))}
    before = batch_rows(db)
    report = run_status(capsys, "--project", "alpha")
    items = [i for i in report["items"] if i["reason"] == "dead_batch"]
    assert [i["text"] for i in items] == expected
    b1 = next(spec for spec in batches if spec["id"] == "B1")
    for item in items:
        assert item["group"] == "failed"
        assert item["project"] == "alpha"
        assert item["ticket"] is None
        assert item["run_id"] == "B1"
        assert item["since"] == _resolve(b1["beat"])
        assert item["actions"] == []
        assert item["summary_state"] is None
        assert set(item) == set(status.ITEM_KEYS)
    row = next(p for p in report["projects"] if p["name"] == "alpha")
    assert row["error"] is None
    assert row["counts"]["failed"] == len(expected)
    assert batch_rows(db) == before
    files_after = {p.name: p.read_bytes() for p in sorted(tickets_dir.glob("*.md"))}
    assert files_after == files_before


def test_dead_batch_plain_line(env, capsys, monkeypatch):
    alpha = env.work / "alpha"
    tr = ledger(alpha)
    add_batch(
        tr, "B1", beat=local(14, 2),
        children=[("246-a", "queued"), ("247-b", "queued")],
    )
    write_ticket(alpha, "246-a", "Workflow: bug-fix", "Status: open")
    write_ticket(alpha, "247-b", "Workflow: bug-fix", "Status: open")
    tr.con.close()
    monkeypatch.setattr(status.tracer, "pid_alive", lambda pid: False)

    assert cli.main(["status", "--project", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "Failed" in out
    assert "  alpha · dead_batch · B1 · batch died 14:02 · not retried · not started: 246, 247" in out
    assert "None waiting" not in out


def test_dead_batch_on_a_pre_retry_ledger_keeps_run_items(env, capsys, monkeypatch):
    alpha = env.work / "alpha"
    tr = ledger(alpha)
    add_run(
        tr, "R1", "50-x", "fail", iso(T0 - timedelta(hours=1)), error="boom",
        ended_at=iso(T0 - timedelta(hours=1)),
    )
    add_batch(tr, "B1", beat=local(14, 2), children=[("246-a", "queued")])
    tr.con.execute("ALTER TABLE processes DROP COLUMN retried_at")
    tr.con.execute("ALTER TABLE processes DROP COLUMN retry_of")
    tr.con.execute("DROP TABLE batch_children")
    tr.con.commit()
    tr.con.close()
    monkeypatch.setattr(status.tracer, "pid_alive", lambda pid: False)

    report = run_status(capsys, "--project", "alpha")
    row = next(p for p in report["projects"] if p["name"] == "alpha")
    assert row["error"] is None
    items = [i for i in report["items"] if i["reason"] == "dead_batch"]
    assert [i["text"] for i in items] == ["batch died 14:02 · not retried"]
    assert any(i["run_id"] == "R1" and i["reason"] == "fail" for i in report["items"])
