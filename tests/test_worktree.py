"""Per-Run worktree + branch lifecycle: create, fail fast, red setup, teardown (spec §8)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lime import worktree


def git(cwd, *args) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def project(tmp_path):
    """A clone named `project`, checked out on a side branch — the default branch is main."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(tmp_path, "init", "-q", "-b", "main", str(origin))
    (origin / "app.py").write_text("hello\n")
    git(origin, "add", "-A")
    git(origin, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    clone = tmp_path / "project"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "config", "user.email", "t@t")
    git(clone, "config", "user.name", "t")
    git(clone, "switch", "-q", "-c", "other")
    (clone / "app.py").write_text("drifted\n")
    git(clone, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "drift")
    return clone


@pytest.fixture
def base(tmp_path):
    return tmp_path / "worktrees"


def test_branch_name_is_lime_workflow_ticket_stem():
    assert worktree.branch_name("feature", "26-sandbox") == "lime/feature/26-sandbox"


def test_create_branches_off_the_default_branch_current_commit(project, base):
    path = worktree.create(project, "run-1", "lime/feature/26-sandbox", base=base)

    assert path == base / "project" / "run-1"
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == "lime/feature/26-sandbox"
    # off main's commit, not the side branch the project happens to sit on
    assert git(path, "rev-parse", "HEAD") == git(project, "rev-parse", "origin/main")


def test_existing_branch_fails_fast(project, base):
    git(project, "branch", "lime/feature/26-sandbox")

    with pytest.raises(worktree.WorktreeError, match="already exists"):
        worktree.create(project, "run-1", "lime/feature/26-sandbox", base=base)
    assert not (base / "project" / "run-1").exists()


def test_setup_runs_in_the_worktree_with_the_run_env(project, base):
    path = worktree.create(
        project, "run-1", "lime/feature/26-sandbox",
        setup='printf "%s" "$LIME_RUN_ID" > from-setup.txt',
        env={"LIME_RUN_ID": "run-1"}, base=base,
    )
    assert (path / "from-setup.txt").read_text() == "run-1"


def test_setup_env_strips_the_operator_s_venv(project, base, monkeypatch):
    venv_bin = str(Path("/fake/venv/bin"))
    monkeypatch.setenv("VIRTUAL_ENV", "/fake/venv")
    monkeypatch.setenv("PATH", venv_bin + os.pathsep + "/usr/bin")

    path = worktree.create(
        project, "run-1", "lime/feature/26-sandbox",
        setup="env > env.txt", base=base,
    )

    dumped = (path / "env.txt").read_text()
    assert "VIRTUAL_ENV=" not in dumped
    assert venv_bin not in dumped


def test_red_setup_means_no_run_and_nothing_left_behind(project, base):
    with pytest.raises(worktree.WorktreeError, match="setup"):
        worktree.create(
            project, "run-1", "lime/feature/26-sandbox",
            setup="echo boom >&2; exit 1", base=base,
        )
    assert not (base / "project" / "run-1").exists()
    assert (
        subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--verify", "--quiet",
             "refs/heads/lime/feature/26-sandbox"],
            capture_output=True,
        ).returncode
        != 0
    )


def test_session_dir_resolves_a_relative_path_before_encoding(tmp_path, monkeypatch):
    cc_projects = tmp_path / "cc-projects"
    monkeypatch.setattr(worktree, "CC_PROJECTS", cc_projects)
    monkeypatch.chdir(tmp_path)
    relative = Path("worktrees/project/run-1")

    assert worktree.session_dir(relative) == cc_projects / str(
        (tmp_path / relative).resolve()
    ).replace("/", "-")


def test_teardown_removes_worktree_branch_and_cc_session_dir(project, base, tmp_path, monkeypatch):
    cc_projects = tmp_path / "cc-projects"
    monkeypatch.setattr(worktree, "CC_PROJECTS", cc_projects)
    path = worktree.create(project, "run-1", "lime/feature/26-sandbox", base=base)
    session_dir = worktree.session_dir(path)
    assert session_dir == cc_projects / str(path.resolve()).replace("/", "-")
    session_dir.mkdir(parents=True)
    (session_dir / "transcript.jsonl").write_text("{}")
    marker = tmp_path / "torn-down"

    problems = worktree.teardown(
        project, "run-1", "lime/feature/26-sandbox",
        command='touch "$MARKER"', env={"MARKER": str(marker)}, base=base,
    )

    assert problems == []
    assert marker.exists()  # commands.teardown ran in the worktree before removal
    assert not path.exists()
    assert not session_dir.exists()
    assert "lime/feature/26-sandbox" not in git(project, "branch", "--list", "lime/*")


def test_teardown_still_deletes_the_branch_when_the_worktree_dir_is_already_gone(project, base):
    path = worktree.create(project, "run-1", "lime/feature/26-sandbox", base=base)
    shutil.rmtree(path)

    problems = worktree.teardown(project, "run-1", "lime/feature/26-sandbox", base=base)

    assert problems == []
    assert "lime/feature/26-sandbox" not in git(project, "branch", "--list", "lime/*")


def test_teardown_deletes_the_pushed_remote_branch_too(project, base):
    """A discarded Run that reached push_pr must not leave origin holding its branch —
    the next Run of the same ticket would be rejected as non-fast-forward (§8)."""
    path = worktree.create(project, "run-1", "lime/feature/26-sandbox", base=base)
    git(path, "push", "-q", "-u", "origin", "lime/feature/26-sandbox")

    problems = worktree.teardown(project, "run-1", "lime/feature/26-sandbox", base=base)

    assert problems == []
    assert (
        subprocess.run(
            ["git", "-C", str(project), "ls-remote", "--exit-code", "origin",
             "refs/heads/lime/feature/26-sandbox"],
            capture_output=True,
        ).returncode
        == 2
    )


def test_teardown_twice_reports_no_problems(project, base):
    """Re-running teardown on an already torn-down Run reports nothing (§8)."""
    path = worktree.create(project, "run-1", "lime/feature/26-sandbox", base=base)
    git(path, "push", "-q", "-u", "origin", "lime/feature/26-sandbox")

    assert worktree.teardown(project, "run-1", "lime/feature/26-sandbox", base=base) == []
    assert worktree.teardown(project, "run-1", "lime/feature/26-sandbox", base=base) == []

    assert "lime/feature/26-sandbox" not in git(project, "branch", "--list", "lime/*")
    assert (
        subprocess.run(
            ["git", "-C", str(project), "ls-remote", "--exit-code", "origin",
             "refs/heads/lime/feature/26-sandbox"],
            capture_output=True,
        ).returncode
        == 2
    )
    assert not path.exists()


def test_teardown_stays_silent_when_the_repo_has_no_origin(tmp_path, base):
    solo = tmp_path / "solo"
    solo.mkdir()
    git(tmp_path, "init", "-q", "-b", "main", str(solo))
    (solo / "app.py").write_text("hello\n")
    git(solo, "add", "-A")
    git(solo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    worktree.create(solo, "run-1", "lime/feature/26-sandbox", base=base)

    problems = worktree.teardown(solo, "run-1", "lime/feature/26-sandbox", base=base)

    assert problems == []  # a local-only repo has no remote copy — and no warning


# --- ticket commit on the Run's branch (ticket 59) ---


def _origin_sha(project: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(project), "rev-parse", "origin/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _local_sha(project: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(project), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_create_branches_off_origin_even_when_local_main_is_ahead(project, base):
    # project is on 'other' — switch to main, commit a local-only change, go back
    git(project, "switch", "-q", "main")
    (project / "local.txt").write_text("local-ahead")
    git(project, "add", "-A")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "local-ahead")
    git(project, "switch", "-q", "other")

    origin_sha = _origin_sha(project)
    local_sha = _local_sha(project)
    assert origin_sha != local_sha  # local is ahead

    path = worktree.create(project, "run-1", "lime/feature/26-sandbox", base=base)
    worktree_head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert worktree_head == origin_sha  # off origin's commit, not local main's
    assert worktree_head != local_sha


def test_create_commits_the_ticket_on_the_run_branch(project, base):
    ticket_dir = project / ".lime" / "tickets"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    ticket = ticket_dir / "26-sandbox.md"
    ticket.write_text("# 26 — Sandbox\n\nAsk: test")

    run_id = "run-1"
    branch = "lime/feature/26-sandbox"
    path = worktree.create(project, run_id, branch, base=base, ticket=ticket)

    # File exists in the worktree
    assert (path / ".lime" / "tickets" / "26-sandbox.md").read_text() == "# 26 — Sandbox\n\nAsk: test"
    # Log message
    log_s = subprocess.run(
        ["git", "-C", str(path), "log", "-1", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert log_s == "ticket: 26-sandbox"
    log_b = subprocess.run(
        ["git", "-C", str(path), "log", "-1", "--format=%b"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert "Lime-Run: run-1" in log_b
    assert "Lime-Role: orchestrator" in log_b
    # Worktree is clean
    status = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert status == ""
    # HEAD~1 is origin/main
    parent = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD~1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert parent == _origin_sha(project)
    # Project's main and origin/main unchanged
    assert _local_sha(project) == subprocess.run(
        ["git", "-C", str(project), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert _origin_sha(project) == subprocess.run(
        ["git", "-C", str(project), "rev-parse", "origin/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_create_makes_no_ticket_commit_when_the_branch_already_has_it(project, base):
    # Commit the ticket on main and update the remote-tracking ref to simulate a push
    origin_path = project.parent / "origin"
    ticket = project / ".lime" / "tickets" / "26-sandbox.md"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text("# 26 — Sandbox\n\nAsk: test")
    git(project, "add", "-A")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "ticket: 26-sandbox")
    # Update the remote-tracking ref so origin/main sees the commit
    main_sha = git(project, "rev-parse", "main")
    subprocess.run(
        ["git", "-C", str(project), "update-ref", "refs/remotes/origin/main", main_sha],
        check=True, capture_output=True,
    )
    # Also commit to the origin repo itself so both have it
    origin_ticket_dir = origin_path / ".lime" / "tickets"
    origin_ticket_dir.mkdir(parents=True, exist_ok=True)
    (origin_ticket_dir / "26-sandbox.md").write_text("# 26 — Sandbox\n\nAsk: test")
    git(origin_path, "add", "-A")
    git(origin_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "ticket: 26-sandbox")

    git(project, "switch", "-q", "other")

    run_id = "run-1"
    branch = "lime/feature/26-sandbox"
    path = worktree.create(project, run_id, branch, base=base, ticket=ticket)

    # HEAD should point to origin/main (no extra commit)
    origin_sha = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "origin/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    worktree_head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert worktree_head == origin_sha


def test_create_carries_an_edited_tracked_ticket(project, base):
    # Commit the ticket on main, update origin tracking ref to match
    origin_path = project.parent / "origin"
    ticket = project / ".lime" / "tickets" / "26-sandbox.md"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text("# 26 — Sandbox\n\nAsk: test")
    git(project, "add", "-A")
    git(project, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "ticket: 26-sandbox")
    main_sha = git(project, "rev-parse", "main")
    subprocess.run(
        ["git", "-C", str(project), "update-ref", "refs/remotes/origin/main", main_sha],
        check=True, capture_output=True,
    )
    # Also commit to origin so the remote also has it
    origin_ticket_dir = origin_path / ".lime" / "tickets"
    origin_ticket_dir.mkdir(parents=True, exist_ok=True)
    (origin_ticket_dir / "26-sandbox.md").write_text("# 26 — Sandbox\n\nAsk: test")
    git(origin_path, "add", "-A")
    git(origin_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "ticket: 26-sandbox")

    git(project, "switch", "-q", "other")

    # Now edit the file in the clone (tracked, but with uncommitted changes)
    ticket.write_text("# 26 — Sandbox\n\nAsk: test\n\nEdited")

    run_id = "run-1"
    branch = "lime/feature/26-sandbox"
    # Pass the edited clone copy as the ticket to carry
    path = worktree.create(project, run_id, branch, base=base, ticket=ticket)

    # The worktree has the edited content
    assert (path / ".lime" / "tickets" / "26-sandbox.md").read_text() == "# 26 — Sandbox\n\nAsk: test\n\nEdited"
    # And one ticket commit on top of origin/main
    log_s = subprocess.run(
        ["git", "-C", str(path), "log", "-1", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert log_s == "ticket: 26-sandbox"
    parent = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD~1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert parent == _origin_sha(project)


def test_create_leaves_nothing_behind_when_the_ticket_cannot_be_carried(project, base):
    branch = "lime/feature/26-sandbox"
    missing = project / ".lime" / "tickets" / "missing.md"
    with pytest.raises(worktree.WorktreeError, match="could not carry the ticket"):
        worktree.create(project, "run-1", branch, base=base, ticket=missing)
    # Worktree dir absent
    assert not (base / "project" / "run-1").exists()
    # Branch absent
    assert (
        subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--verify", "--quiet",
             f"refs/heads/{branch}"],
            capture_output=True,
        ).returncode
        != 0
    )


def test_create_fetches_advanced_origin_and_leaves_stale_local_main_unchanged(project, base):
    """The birth boundary: origin is ahead of local main, and creating the Run must
    branch from the fetched origin tip while local main stays exactly where it was."""
    origin = project.parent / "origin"
    local_main_before = git(project, "rev-parse", "main")

    # advance origin/main on a disposable side branch; never check out or push main
    git(origin, "switch", "-q", "-c", "inherit")
    (origin / "more.txt").write_text("more\n")
    git(origin, "add", "-A")
    git(origin, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "advance")
    advanced = git(origin, "rev-parse", "HEAD")
    git(origin, "update-ref", "refs/heads/main", advanced)
    assert git(origin, "rev-parse", "HEAD") != local_main_before

    ticket = project / ".lime" / "tickets" / "26-sandbox.md"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text("# 26 — Sandbox\n\nAsk: test")

    path = worktree.create(project, "run-1", "lime/feature/26-sandbox", base=base, ticket=ticket)

    birth = git(path, "rev-parse", "HEAD")
    assert birth != advanced  # the ticket commit is the birth HEAD
    assert git(path, "rev-parse", "HEAD~1") == advanced  # its exact origin parent
    assert git(path, "rev-parse", "origin/main") == advanced  # fetch advanced the tracking ref
    assert git(project, "rev-parse", "main") == local_main_before  # local main untouched
