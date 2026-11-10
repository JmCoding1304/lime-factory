"""Tests for lime/git_helper.py — path-scoped uncommitted_state."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from lime import docs_merge, git_helper


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Build a tiny git repo in a tmp dir and chdir into it (git_helper operates on cwd)."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "file.txt").write_text("content\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    monkeypatch.chdir(root)
    return root


def test_uncommitted_state_is_none_for_a_committed_clean_file(repo):
    assert git_helper.uncommitted_state("file.txt") is None


def test_uncommitted_state_reports_an_untracked_file(repo, tmp_path):
    root = tmp_path / "repo"
    (root / "new.txt").write_text("new\n")
    assert git_helper.uncommitted_state("new.txt") == "untracked"


def test_uncommitted_state_reports_a_modified_tracked_file(repo, tmp_path):
    root = tmp_path / "repo"
    (root / "file.txt").write_text("modified\n")
    assert git_helper.uncommitted_state("file.txt") == "modified"


def test_uncommitted_state_treats_staged_but_uncommitted_changes_as_modified(repo, tmp_path):
    root = tmp_path / "repo"
    (root / "file.txt").write_text("staged\n")
    _git(root, "add", "file.txt")
    assert git_helper.uncommitted_state("file.txt") == "modified"


# --- fast_forward (ticket 45) ---


@pytest.fixture
def clone(tmp_path, monkeypatch):
    """A bare origin + a clone chdir'd into — origin advances, the clone catches up."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "file.txt").write_text("v1\n")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    _git(seed, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    # clone is a regular checkout of origin
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "-c", "user.email=t@t", "-c", "user.name=t", "config", "user.email", "t@t")
    _git(work, "-c", "user.email=t@t", "-c", "user.name=t", "config", "user.name", "t")
    monkeypatch.chdir(work)
    return work, origin, seed


def test_fast_forward_advances_the_checked_out_branch_and_the_working_tree(clone):
    work, origin, seed = clone
    # advance origin via seed
    (seed / "file.txt").write_text("v2\n")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "v2")
    _git(seed, "push", "-q", "origin", "main")
    # fast-forward work to origin/main
    sha = git_helper.fast_forward("main")
    assert (work / "file.txt").read_text() == "v2\n"
    assert sha == git_helper.short_sha("main")


def test_fast_forward_advances_a_branch_that_is_not_checked_out(clone):
    work, origin, seed = clone
    # work checks out a side branch so main is not checked out
    _git(work, "checkout", "-q", "-b", "side")
    # advance origin/main
    (seed / "file.txt").write_text("v2\n")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "v2")
    _git(seed, "push", "-q", "origin", "main")
    # fetch the advance onto main (not checked out) — working tree stays on side
    sha = git_helper.fast_forward("main")
    assert git_helper.current_branch() == "side"
    assert (work / "file.txt").read_text() == "v1\n"
    assert sha == git_helper.short_sha("main")


def test_fast_forward_is_a_noop_when_the_branch_is_already_up_to_date(clone):
    work, origin, seed = clone
    sha_before = git_helper.short_sha("main")
    sha = git_helper.fast_forward("main")
    assert sha == sha_before
    assert (work / "file.txt").read_text() == "v1\n"


def test_fast_forward_refuses_a_diverged_local_branch(clone):
    work, origin, seed = clone
    # local commit on main (diverges from origin/main)
    (work / "local.txt").write_text("local\n")
    _git(work, "add", "-A")
    _git(work, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "local")
    # advance origin
    (seed / "file.txt").write_text("v2\n")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "v2")
    _git(seed, "push", "-q", "origin", "main")
    rev_before = git_helper.rev("main")
    with pytest.raises(RuntimeError):
        git_helper.fast_forward("main")
    # local ref untouched, working tree untouched
    assert git_helper.rev("main") == rev_before
    assert (work / "local.txt").read_text() == "local\n"


def test_fast_forward_refuses_when_a_dirty_tree_would_be_overwritten(clone):
    work, origin, seed = clone
    # advance origin (changes file.txt)
    (seed / "file.txt").write_text("v2\n")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "v2")
    _git(seed, "push", "-q", "origin", "main")
    # dirty tree in work — uncommitted edit on file.txt
    (work / "file.txt").write_text("dirty\n")
    rev_before = git_helper.rev("main")
    with pytest.raises(RuntimeError):
        git_helper.fast_forward("main")
    assert git_helper.rev("main") == rev_before
    assert (work / "file.txt").read_text() == "dirty\n"


# --- unpushed_commits (ticket 59) ---


def test_unpushed_commits_lists_local_only_commits_touching_the_path(clone):
    work, origin, seed = clone
    tickets = work / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "42-a.md").write_text("ticket 42")
    _git(work, "add", "-A")
    _git(work, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "ticket: 42-a")
    expected = git_helper.short_sha("main")
    result = git_helper.unpushed_commits("main", ".lime/tickets/42-a.md")
    assert result == [expected]
    _git(work, "push", "-q", "origin", "main")
    assert git_helper.unpushed_commits("main", ".lime/tickets/42-a.md") == []


def test_unpushed_commits_ignores_local_commits_to_other_paths(clone):
    work, origin, seed = clone
    tickets = work / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    # Write the ticket file but DON'T add it — commit only other.txt
    (tickets / "42-a.md").write_text("ticket 42")
    (work / "other.txt").write_text("other")
    _git(work, "add", "other.txt")
    _git(work, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "only other")
    # only touches other.txt, not the ticket path
    assert git_helper.unpushed_commits("main", ".lime/tickets/42-a.md") == []


def test_unpushed_commits_is_empty_without_an_origin(repo, monkeypatch):
    # repo fixture has no origin
    assert git_helper.unpushed_commits("main", "file.txt") == []


# --- diff_hunks (ticket 83) ---


def test_diff_hunks_lists_tracked_changes_then_untracked_files_per_path(repo, tmp_path):
    root = tmp_path / "repo"
    (root / "file.txt").write_text("modified\n")
    (root / "new.txt").write_text("new\n")
    hunks = git_helper.diff_hunks("HEAD", context=3)
    paths = [p for p, _ in hunks]
    assert paths == ["file.txt", "new.txt"]
    assert "-content" in hunks[0][1]
    assert "+modified" in hunks[0][1]
    assert "+new" in hunks[1][1]
    assert "--- /dev/null" in hunks[1][1]


def test_diff_hunks_carries_the_requested_context(repo, tmp_path):
    root = tmp_path / "repo"
    lines = "".join(f"line {i:03d}\n" for i in range(100))
    (root / "file.txt").write_text(lines)
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "hundred lines")
    # Change line 050
    lines = "".join(f"line {i:03d}\n" if i != 50 else "line 050 changed\n" for i in range(100))
    (root / "file.txt").write_text(lines)

    hunks5 = git_helper.diff_hunks("HEAD", context=5)
    text5 = hunks5[0][1]
    assert "line 045" in text5
    assert "line 055" in text5
    assert "line 056" not in text5

    hunks30 = git_helper.diff_hunks("HEAD", context=30)
    text30 = hunks30[0][1]
    assert "line 020" in text30


def test_diff_hunks_is_empty_on_a_clean_tree(repo):
    assert git_helper.diff_hunks("HEAD", context=3) == []


# --- merged_prs + sync_state (ticket 61) ---


def test_sync_state_is_zero_zero_when_up_to_date(clone):
    work, origin, seed = clone
    assert git_helper.sync_state("main") == (0, 0)


def test_sync_state_counts_behind_without_moving_the_branch(clone):
    work, origin, seed = clone
    # advance origin via seed
    (seed / "file.txt").write_text("v2\n")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "v2")
    _git(seed, "push", "-q", "origin", "main")

    rev_before = git_helper.rev("main")
    assert git_helper.sync_state("main") == (0, 1)
    # local branch and working tree untouched
    assert git_helper.rev("main") == rev_before
    assert (work / "file.txt").read_text() == "v1\n"


def test_sync_state_counts_ahead_and_behind(clone):
    work, origin, seed = clone
    # local commit on main
    (work / "local.txt").write_text("local\n")
    _git(work, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(work, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "local")
    # advance origin
    (seed / "file.txt").write_text("v2\n")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "v2")
    _git(seed, "push", "-q", "origin", "main")

    assert git_helper.sync_state("main") == (1, 1)


def test_sync_state_raises_without_an_origin(repo):
    with pytest.raises(RuntimeError):
        git_helper.sync_state("main")


def test_merged_prs_parses_gh_json(monkeypatch):
    import subprocess as _subprocess

    def fake_run(cmd, **kw):
        assert cmd[:5] == ["gh", "pr", "list", "--state", "merged"]
        assert "--json" in cmd
        return _subprocess.CompletedProcess(
            cmd, 0, stdout='[{"number":16,"headRefName":"lime/bug-fix/54-x"}]', stderr="",
        )

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    result = git_helper.merged_prs()
    assert result == [{"number": 16, "headRefName": "lime/bug-fix/54-x"}]


def test_merged_prs_raises_when_gh_fails(monkeypatch):
    import subprocess as _subprocess

    def fake_run(cmd, **kw):
        return _subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        git_helper.merged_prs()
    assert "gh pr list" in str(exc.value)


def test_merged_prs_raises_when_gh_is_missing(monkeypatch):
    def fake_run(cmd, **kw):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        git_helper.merged_prs()
    assert "gh not found" in str(exc.value)


def test_pr_head_refs_lists_heads_across_every_state(monkeypatch):
    import subprocess as _subprocess

    def fake_run(cmd, **kw):
        assert cmd[:5] == ["gh", "pr", "list", "--state", "all"]
        assert "--limit" in cmd and "1000" in cmd
        return _subprocess.CompletedProcess(
            cmd, 0,
            stdout='[{"number":1,"headRefName":"lime/feature/72-x"},'
                   '{"number":2,"headRefName":"lime/install"}]',
            stderr="",
        )

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    result = git_helper.pr_head_refs()
    assert result == ["lime/feature/72-x", "lime/install"]


# --- commit_all with exclusions (ticket 122) ---


def _g(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


@pytest.fixture
def repo2(tmp_path, monkeypatch):
    """A tiny git repo with user.email/name configured."""
    root = tmp_path / "repo"
    root.mkdir()
    _g(root, "init", "-q", "-b", "main")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "config", "user.email", "t@t")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "config", "user.name", "t")
    (root / "readme.md").write_text("init\n")
    _g(root, "add", "-A")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    monkeypatch.chdir(root)
    return root


def test_commit_all_excludes_root_handoff_but_keeps_nested_handoff(repo2):
    root = repo2
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print('hello')\n")
    (root / "context_handoff").mkdir(exist_ok=True)
    (root / "context_handoff" / "alignment.md").write_text("alignment data\n" * 100)
    (root / "src" / "context_handoff").mkdir(parents=True, exist_ok=True)
    (root / "src" / "context_handoff" / "notes.md").write_text("nested handoff\n")
    _g(root, "add", root / "src" / "main.py")
    _g(root, "add", root / "src" / "context_handoff" / "notes.md")

    sha = git_helper.commit_all("add src/main.py with nested handoff", exclude=["context_handoff/"])
    assert sha
    # The commit should contain main.py and nested handoff, NOT root handoff
    tree = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    assert "src/main.py" in tree
    assert "src/context_handoff/notes.md" in tree
    assert "context_handoff/alignment.md" not in tree
    # The excluded file should still exist on disk
    assert (root / "context_handoff" / "alignment.md").exists()


def test_commit_all_honors_custom_exclusions_and_empty_override(repo2):
    root = repo2
    # Create files for exclusion patterns
    (root / "dist").mkdir()
    (root / "dist" / "bundle.js").write_text("bundle\n")
    (root / "logs").mkdir()
    (root / "logs" / "out.log").write_text("log\n")
    (root / "my file.txt").write_text("spacey\n")
    (root / "lib.py").write_text("def foo(): pass\n")
    _g(root, "add", "-A")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t",
       "commit", "-q", "-m", "staging pre-check")

    # Now make changes
    (root / "lib.py").write_text("def foo():\n    return 42\n")
    (root / "dist" / "bundle.js").write_text("bundle v2\n")
    (root / "logs" / "out.log").write_text("log v2\n")
    (root / "my file.txt").write_text("spacey v2\n")
    (root / "new.py").write_text("print('new')\n")

    sha = git_helper.commit_all("custom exclusions", exclude=["dist/", "logs/", "my file.txt"])
    assert sha
    # Check what changed in this commit (not full tree)
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", "HEAD~1", "HEAD"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    assert "lib.py" in diff  # lib.py change committed
    assert "new.py" in diff  # new file committed
    assert "dist/bundle.js" not in diff  # excluded
    assert "logs/out.log" not in diff  # excluded
    assert "my file.txt" not in diff  # excluded
    # Excluded files still modified on disk
    assert (root / "dist" / "bundle.js").read_text() == "bundle v2\n"

    # Explicit empty list includes everything
    (root / "another.py").write_text("another\n")
    (root / "context_handoff").mkdir(exist_ok=True)
    (root / "context_handoff" / "note.txt").write_text("note\n")
    (root / "a_file.txt").write_text("x\n")
    _g(root, "add", "another.py")  # pre-stage
    _g(root, "add", str(root / "context_handoff" / "note.txt"))

    sha3 = git_helper.commit_all("with empty exclude", exclude=[])
    assert sha3
    diff3 = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", "HEAD~1", "HEAD"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    assert "another.py" in diff3
    assert "context_handoff/note.txt" in diff3  # empty exclude includes it


def test_commit_all_unstages_excluded_changes_without_touching_worktree(repo2):
    root = repo2
    (root / "artefact.bin").write_bytes(b"\x00\x01\x02")
    (root / "lib.py").write_text("original\n")
    _g(root, "add", "-A")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")

    # Stage a new artefact and a tracked modification
    (root / "artefact.bin").write_bytes(b"\x03\x04\x05")  # modified
    (root / "lib.py").write_text("modified\n")  # modified
    (root / "out.tsv").write_text("data\n")  # new
    _g(root, "add", "artefact.bin", "lib.py", "out.tsv")

    # Also stage a deletion
    _g(root, "rm", "--cached", "readme.md")
    (root / "readme.md").unlink()

    # Commit with exclusion of artefact and out.tsv patterns
    sha = git_helper.commit_all("exclude staged", exclude=["artefact.bin", "out.tsv"])
    assert sha
    # Check what changed
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", "HEAD~1", "HEAD"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    assert "lib.py" in diff  # lib.py change committed
    # artefact.bin change was unstaged by exclusion before add, so not in new commit
    assert "artefact.bin" not in diff
    assert "out.tsv" not in diff  # new file was not committed
    # Working tree preserved
    assert (root / "lib.py").read_text() == "modified\n"
    assert (root / "artefact.bin").read_bytes() == b"\x03\x04\x05"
    assert (root / "out.tsv").read_text() == "data\n"
    # readme.md still deleted from worktree (but old version in HEAD)
    assert not (root / "readme.md").exists()


def test_commit_all_reports_nothing_when_only_excluded_changes_remain(repo2):
    root = repo2
    (root / "context_handoff").mkdir(exist_ok=True)
    (root / "context_handoff" / "plan.md").write_text("plan\n")
    _g(root, "add", root / "context_handoff" / "plan.md")

    with pytest.raises(RuntimeError, match="nothing to commit"):
        git_helper.commit_all("should fail", exclude=["context_handoff/"])
    # excluded file unchanged on disk
    assert (root / "context_handoff" / "plan.md").read_text() == "plan\n"


# --- excluded_paths ---


def test_excluded_paths_lists_matching_changes(repo2):
    root = repo2
    (root / "context_handoff").mkdir(exist_ok=True)
    (root / "context_handoff" / "plan.md").write_text("plan\n")
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "lib.py").write_text("code\n")
    _g(root, "add", "-A")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    _g(root, "add", root / "context_handoff" / "plan.md")  # stage plan.md

    paths = git_helper.excluded_paths(["context_handoff/"])
    assert "context_handoff/plan.md" in paths


# --- diff_stats (ticket 122) ---


def test_diff_stats_cover_branch_staged_unstaged_and_untracked_once(repo2):
    root = repo2
    base_commit = "HEAD"
    (root / "file_a.py").write_text("a\n" * 10)
    (root / "file_b.py").write_text("b\n")
    (root / "untracked.txt").write_text("new\n" * 5)
    _g(root, "add", "file_a.py", "file_b.py")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add files")
    # Now have a base commit before changes
    base = "HEAD"
    (root / "file_a.py").write_text("a\n" * 15 + "extra\n")
    _g(root, "add", "file_a.py")
    (root / "file_b.py").write_text("b\n" * 2)
    (root / "untracked.txt").write_text("new\n" * 7)
    (root / "new_file.md").write_text("hello\n")

    stats = git_helper.diff_stats(base, exclude_patterns=[])
    total = sum(s["added_lines"] for s in stats)
    # file_a: +6 lines, file_b: +1 line, untracked: +7 lines, new_file: +1 line = 15
    assert total == 15
    paths = [s["path"] for s in stats]
    assert "file_a.py" in paths
    assert "file_b.py" in paths
    assert "untracked.txt" in paths
    # Each file appears exactly once
    assert len(set(paths)) == len(paths)


def test_diff_stats_filter_excluded_and_ignored_paths_without_mutation(repo2):
    root = repo2
    (root / ".gitignore").write_text("*.log\n")
    _g(root, "add", "-A")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    base = "HEAD"
    (root / "big_output.txt").write_text("x\n" * 2000)
    (root / "debug.log").write_text("trace\n" * 100)
    (root / "context_handoff").mkdir(exist_ok=True)
    (root / "context_handoff" / "data.md").write_text("y\n" * 100)
    (root / "code.py").write_text("print(1)\n")

    stats = git_helper.diff_stats(base, exclude_patterns=["context_handoff/"])
    paths = [s["path"] for s in stats]
    assert "code.py" in paths
    assert "big_output.txt" in paths
    assert "context_handoff/data.md" not in paths  # excluded
    assert "debug.log" not in paths  # gitignored
    # All original files preserved
    assert (root / "readme.md").read_text() == "init\n"


def test_diff_stats_detect_binary_changes_but_not_binary_deletions(repo2):
    root = repo2
    (root / "text.txt").write_text("hello\n")
    (root / "binary.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00")
    _g(root, "add", "-A")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")

    # Delete binary.png: a binary deletion must not be flagged
    _g(root, "rm", "binary.png")

    # Create new binary (untracked)
    (root / "new_binary.bin").write_bytes(b"\x00\x01\x02\x03")
    # Add a file with spaces
    (root / "my binary.bin").write_bytes(b"\x00\x00\x00\x00")
    # Modify text
    (root / "text.txt").write_text("modified\n")
    _g(root, "add", "-A")

    stats = git_helper.diff_stats("HEAD", exclude_patterns=[])
    # binary.png is deleted — must not be a violation
    non_deleted_binaries = [s for s in stats if s["binary"] and not s.get("deleted")]
    assert all("binary.png" not in s["path"] for s in non_deleted_binaries), "deleted binary must not be a violation"
    # binary.png must be marked as deleted
    deleted = [s for s in stats if s.get("deleted")]
    assert any("binary.png" in s["path"] for s in deleted), "binary.png should be marked deleted"
    # New binaries must be flagged
    assert any("new_binary.bin" in s["path"] for s in non_deleted_binaries)
    assert any("my binary.bin" in s["path"] for s in non_deleted_binaries)
    # Text change is not binary
    text_paths = [s["path"] for s in stats if not s["binary"]]
    assert "text.txt" in text_paths


# --- uncounted_paths (ticket 126) ---


def test_uncounted_paths_match_branch_index_worktree_and_untracked(repo2):
    root = repo2
    (root / "tracked_mod.py").write_text("orig\n")
    (root / "staged_mod.py").write_text("orig\n")
    (root / "base_del.py").write_text("gone\n")
    _g(root, "add", "-A")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed")
    base = git_helper.rev("HEAD")  # retain the real base SHA before later commits

    # committed branch addition + committed modification
    (root / "committed_new.py").write_text("".join(f"c{i}\n" for i in range(20)))
    _g(root, "add", "committed_new.py")
    (root / "tracked_mod.py").write_text("orig\ncommitted\n")
    _g(root, "add", "tracked_mod.py")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "branch work")

    # staged addition + staged modification
    (root / "staged_new.txt").write_text("".join(f"s{i}\n" for i in range(30)))
    _g(root, "add", "staged_new.txt")
    (root / "staged_mod.py").write_text("orig\nstaged\n")
    _g(root, "add", "staged_mod.py")

    # unstaged modification
    (root / "tracked_mod.py").write_text("orig\ncommitted\nworktree\n")

    # eligible untracked file
    (root / "loose.txt").write_text("new\n")

    # base path deleted from index and worktree
    _g(root, "rm", "base_del.py")

    patterns = ["committed_new.py", "tracked_mod.py", "staged_new.txt",
                "staged_mod.py", "loose.txt", "base_del.py"]
    stats_before = git_helper.diff_stats(base, exclude_patterns=[])
    matched = git_helper.uncounted_paths(base, patterns)
    stats_after = git_helper.diff_stats(base, exclude_patterns=[])
    # classification is read-only: the eligible stats are byte-for-byte unchanged
    assert stats_after == stats_before
    assert set(patterns) <= matched

    by_path = {s["path"]: s for s in stats_before}
    assert by_path["base_del.py"]["deleted"] is True
    assert by_path["base_del.py"]["added_lines"] == 0
    assert by_path["committed_new.py"]["added_lines"] == 20
    assert by_path["staged_new.txt"]["added_lines"] == 30


def test_uncounted_paths_use_git_directory_glob_and_space_semantics(repo2):
    root = repo2
    (root / "gen" / "sub").mkdir(parents=True)
    (root / "gen" / "a.txt").write_text("a\n")
    (root / "gen" / "sub" / "b.txt").write_text("b\n")
    (root / "deep" / "dir").mkdir(parents=True)
    (root / "deep" / "dir" / "c.txt").write_text("c\n")
    (root / "my file.txt").write_text("space\n")
    base = git_helper.rev("HEAD")

    # a directory pattern includes every descendant
    assert {"gen/a.txt", "gen/sub/b.txt"} <= git_helper.uncounted_paths(base, ["gen/"])
    # Git globs cross slashes (`deep/*` reaches deep/dir/c.txt)
    assert git_helper.uncounted_paths(base, ["deep/*"]) == {"deep/dir/c.txt"}
    # a filename with spaces is preserved and matched literally
    assert git_helper.uncounted_paths(base, ["my file.txt"]) == {"my file.txt"}
    # multiple patterns matching the same file yield one unique path, not a multiplied count
    matched = git_helper.uncounted_paths(base, ["gen/", "gen/a.txt", "*.txt"])
    assert matched == {"gen/a.txt", "gen/sub/b.txt", "deep/dir/c.txt", "my file.txt"}
    # unmatched valid patterns are simply no matches
    assert git_helper.uncounted_paths(base, ["no-such-dir/"]) == set()


def test_uncounted_matching_preserves_exclude_and_ignore_precedence(repo2):
    root = repo2
    (root / ".gitignore").write_text("*.log\n")
    (root / "tracked_ignored.log").write_text("tracked\n")
    _g(root, "add", ".gitignore")
    _g(root, "add", "-f", "tracked_ignored.log")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    base = git_helper.rev("HEAD")

    # eligible uncounted content
    (root / "pnpm-lock.yaml").write_text("lock\n")
    (root / "gen" / "private").mkdir(parents=True)
    (root / "gen" / "public.txt").write_text("public\n")
    (root / "gen" / "private" / "secret.txt").write_text("secret\n")
    # ignored untracked match
    (root / "debug.log").write_text("ignored\n")
    # a tracked-but-ignored file that is a real change
    (root / "tracked_ignored.log").write_text("tracked\nmodified\n")

    exclude = ["gen/private/"]
    uncounted = ["gen/", "pnpm-lock.yaml", "*.log"]

    stats = git_helper.diff_stats(base, exclude_patterns=exclude)
    matched = git_helper.uncounted_paths(base, uncounted)

    # the matcher itself is exclude-agnostic, but the excluded path is not eligible
    assert "gen/private/secret.txt" in matched
    assert "gen/private/secret.txt" not in {s["path"] for s in stats}
    exempt = {s["path"] for s in stats} & matched
    assert "gen/private/secret.txt" not in exempt
    assert "gen/public.txt" in exempt
    assert "pnpm-lock.yaml" in exempt
    assert "tracked_ignored.log" in exempt  # tracked ignored is a real change
    # ignored untracked never matches and never counts
    assert "debug.log" not in matched
    assert "debug.log" not in exempt

    # commit with only exclude: eligible uncounted content ships, excluded/ignored do not
    sha = git_helper.commit_all("commit with only exclude", exclude=exclude)
    assert sha
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", "HEAD~1", "HEAD"],
        capture_output=True, text=True,
    ).stdout.splitlines()
    assert "gen/public.txt" in diff
    assert "pnpm-lock.yaml" in diff
    assert "gen/private/secret.txt" not in diff
    assert "debug.log" not in diff


def test_uncounted_paths_include_base_deletions(repo2):
    root = repo2
    (root / "staged_del.txt").write_text("content\n")
    (root / ".gitignore").write_text("*.tmp\n")
    (root / "ignored_base.tmp").write_text("tracked but ignored\n")
    _g(root, "add", "staged_del.txt", ".gitignore")
    _g(root, "add", "-f", "ignored_base.tmp")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    base = git_helper.rev("HEAD")

    # staged deletion of a base path
    _g(root, "rm", "staged_del.txt")
    # ignored-but-base-tracked deletion
    _g(root, "rm", "ignored_base.tmp")
    # a branch-added file subsequently deleted, absent from base
    (root / "branch_added.txt").write_text("temp\n")
    _g(root, "add", "branch_added.txt")
    _g(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "branch add")
    _g(root, "rm", "branch_added.txt")

    matched = git_helper.uncounted_paths(base, ["*.txt", "*.tmp"])
    assert "staged_del.txt" in matched  # --with-tree covers the index deletion
    assert "ignored_base.tmp" in matched  # tracked ignored deletion is still a base path
    assert "branch_added.txt" not in matched  # absent from base and index — no spurious entry


def test_uncounted_matching_is_batched_and_read_only(repo2, monkeypatch):
    root = repo2
    (root / "a.txt").write_text("a\n" * 5)
    (root / "b.txt").write_text("b\n" * 5)
    base = git_helper.rev("HEAD")

    calls = []
    real_run = subprocess.run

    def spy(args, *a, **k):
        calls.append(list(args) if isinstance(args, (list, tuple)) else args)
        return real_run(args, *a, **k)

    def snapshot():
        env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}

        def out(*args):
            return real_run(["git", "-C", str(root), *args], capture_output=True, text=True, env=env).stdout

        return {
            "head": out("rev-parse", "HEAD"),
            "index": (root / ".git" / "index").read_bytes(),
            "cached": out("diff", "--cached"),
            "unstaged": out("diff"),
            "files": {str(p.relative_to(root)): p.read_bytes()
                      for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts},
        }

    monkeypatch.setattr(subprocess, "run", spy)
    before = snapshot()
    matched = git_helper.uncounted_paths(base, ["a.txt", "b.txt", "*.txt"])
    assert matched == {"a.txt", "b.txt"}
    ls_calls = [c for c in calls if isinstance(c, list) and len(c) > 1 and c[1] == "ls-files"]
    assert len(ls_calls) == 1, "many patterns/files must batch into one invocation"
    assert not any(isinstance(c, list) and "diff" in c for c in calls), "no extra diff commands"

    # repeated stats/classification leave HEAD, index bytes, diffs and files untouched
    git_helper.diff_stats(base, exclude_patterns=[])
    git_helper.uncounted_paths(base, ["*.txt"])
    assert snapshot() == before

    # an empty pattern list never invokes git
    calls.clear()
    assert git_helper.uncounted_paths(base, []) == set()
    assert calls == []


def test_uncounted_matching_propagates_git_errors(repo2, monkeypatch):
    def failing(args, *a, **k):
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(RuntimeError, match="boom"):
        git_helper.uncounted_paths("HEAD", ["x"])
    monkeypatch.undo()
    # valid unmatched patterns succeed empty
    assert git_helper.uncounted_paths("HEAD", ["does-not-exist.txt"]) == set()


# --- lime deploy: temporary merged-tree worktree + base movement (ticket 141) ---


def _out(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _git_in(root, *args) -> None:
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", *args)


def _content(root: Path, name: str) -> str | None:
    path = root / name
    return path.read_text() if path.exists() else None


def _checkout_state(root: Path) -> dict:
    """HEAD/index/status/content/refs of a checkout — must be identical across the gate."""
    return {
        "head": _out(root, "rev-parse", "HEAD"),
        "trunk": _out(root, "rev-parse", "trunk"),
        "branch": _out(root, "branch", "--show-current"),
        "status": _out(root, "status", "--porcelain"),
        "staged": _out(root, "diff", "--cached", "--name-only"),
        "shared": _content(root, "shared.txt"),
    }


@pytest.fixture
def gate_repo(tmp_path):
    """A bare origin + a checkout on `trunk` + a pushed Run branch — the gate's real shape."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(origin))
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")
    (root / "shared.txt").write_text("base\n")
    (root / "default-only.txt").write_text("default\n")
    _git_in(root, "add", "-A")
    _git_in(root, "commit", "-q", "-m", "init")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "-u", "origin", "trunk")
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    _git_in(root, "checkout", "-q", "-b", "lime/feature/41-toy")
    (root / "branch-only.txt").write_text("branch\n")
    _git_in(root, "add", "-A")
    _git_in(root, "commit", "-q", "-m", "feature")
    _git(root, "push", "-q", "-u", "origin", "lime/feature/41-toy")
    _git(root, "checkout", "-q", "trunk")
    return root


def _tips(root: Path) -> tuple[str, str]:
    git_helper.fetch_origin(root)
    return (
        git_helper.resolve_remote_tip(root, "origin/trunk", label="default"),
        git_helper.resolve_remote_tip(root, "origin/lime/feature/41-toy", label="pr"),
    )


def test_merged_worktree_is_detached_and_contains_both_tips(gate_repo, tmp_path):
    root = gate_repo
    default_sha, pr_sha = _tips(root)
    before = _checkout_state(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with git_helper.merged_worktree(root, default_sha, pr_sha, scratch_dir=scratch) as tree:
        # both tips' files present in the throwaway tree
        assert (tree / "default-only.txt").read_text() == "default\n"
        assert (tree / "branch-only.txt").read_text() == "branch\n"
        # HEAD is detached at the default tip — no commit checked out
        assert _out(tree, "rev-parse", "HEAD") == default_sha
        # detached: symbolic-ref HEAD is not a branch
        detached = subprocess.run(
            ["git", "-C", str(tree), "symbolic-ref", "--quiet", "HEAD"],
            capture_output=True, text=True,
        )
        assert detached.returncode != 0
        # the merge is pending (MERGE_HEAD exists) and uncommitted
        assert _out(tree, "rev-parse", "--verify", "MERGE_HEAD")
        assert _out(tree, "log", "-1", "--format=%H") == default_sha
        # the original checkout and refs are untouched
        assert _checkout_state(root) == before
        temp_tree = tree

    assert not temp_tree.exists()
    assert list(scratch.iterdir()) == []
    assert _checkout_state(root) == before
    assert str(temp_tree) not in _out(root, "worktree", "list", "--porcelain")


@pytest.fixture
def conflict_repo(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(origin))
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")
    (root / "conflict me.txt").write_text("base\n")
    _git_in(root, "add", "-A")
    _git_in(root, "commit", "-q", "-m", "init")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "-u", "origin", "trunk")
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    _git_in(root, "checkout", "-q", "-b", "lime/feature/41-toy")
    (root / "conflict me.txt").write_text("branch\n")
    _git_in(root, "commit", "-qam", "branch change")
    _git(root, "push", "-q", "-u", "origin", "lime/feature/41-toy")
    _git_in(root, "checkout", "-q", "trunk")
    (root / "conflict me.txt").write_text("default\n")
    _git_in(root, "commit", "-qam", "default change")
    _git(root, "push", "-q", "origin", "trunk")
    return root


def test_merged_worktree_conflict_lists_files_and_cleans_up(conflict_repo, tmp_path):
    root = conflict_repo
    default_sha, pr_sha = _tips(root)
    before = _checkout_state(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(git_helper.MergeConflict) as exc:
        with git_helper.merged_worktree(root, default_sha, pr_sha, scratch_dir=scratch):
            raise AssertionError("a conflict must never reach the body")
    assert exc.value.paths == ["conflict me.txt"]
    assert "conflict me.txt" in str(exc.value)
    assert list(scratch.iterdir()) == []
    assert _checkout_state(root) == before


def test_merged_worktree_cleans_up_on_body_exception(gate_repo, tmp_path):
    root = gate_repo
    default_sha, pr_sha = _tips(root)
    before = _checkout_state(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(RuntimeError, match="boom"):
        with git_helper.merged_worktree(root, default_sha, pr_sha, scratch_dir=scratch) as tree:
            raise RuntimeError("boom")
    assert not tree.exists()
    assert list(scratch.iterdir()) == []
    assert _checkout_state(root) == before


def test_merged_worktree_partial_add_failure_cleans_its_temp_parent(gate_repo, tmp_path):
    root = gate_repo
    before = _checkout_state(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(RuntimeError, match="temporary merged worktree"):
        with git_helper.merged_worktree(root, "0" * 40, "0" * 40, scratch_dir=scratch):
            raise AssertionError("a failed add must never reach the body")
    assert list(scratch.iterdir()) == []
    assert _checkout_state(root) == before


def test_merged_worktree_reports_cleanup_failure(gate_repo, tmp_path, monkeypatch):
    root = gate_repo
    default_sha, pr_sha = _tips(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    real = git_helper._git_at

    def flaky(repo_root, *args):
        if args[:2] == ("worktree", "remove"):
            raise RuntimeError("worktree remove blocked")
        return real(repo_root, *args)

    monkeypatch.setattr(git_helper, "_git_at", flaky)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        with git_helper.merged_worktree(root, default_sha, pr_sha, scratch_dir=scratch):
            pass
    assert list(scratch.iterdir()) == []


def test_base_movement_counts_default_merges(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")  # a non-main default branch
    (root / "base.txt").write_text("base\n")
    _git_in(root, "add", "-A")
    _git_in(root, "commit", "-q", "-m", "base")
    base_sha = _out(root, "rev-parse", "HEAD")

    def commit(name, text, message):
        (root / name).write_text(text)
        _git_in(root, "add", "-A")
        _git_in(root, "commit", "-q", "-m", message)

    for n in (1, 2):  # each topic has multiple commits, merged with --no-ff
        _git_in(root, "checkout", "-q", "-b", f"topic{n}", "trunk")
        commit(f"t{n}.txt", f"t{n} a\n", f"topic{n} a")
        commit(f"t{n}.txt", f"t{n} b\n", f"topic{n} b")
        _git_in(root, "checkout", "-q", "trunk")
        _git_in(root, "merge", "--no-ff", "-q", "-m", f"Merge topic{n}", f"topic{n}")
        if n == 1:
            after_first = _out(root, "rev-parse", "trunk")  # just past the first merge
    tip = _out(root, "rev-parse", "trunk")

    # equal pin vs a tip that is the pin
    assert git_helper.base_movement(root, tip, tip).status == "equal"
    # behind: two first-parent merges on default since the birth base, not total commits
    moved = git_helper.base_movement(root, base_sha, tip)
    assert (moved.status, moved.merges) == ("behind", 2)
    # a pin just past the first merge is behind by exactly one merge
    assert git_helper.base_movement(root, after_first, tip).merges == 1

    # a diverged (non-ancestor) pin — a side commit never merged into trunk
    _git_in(root, "checkout", "-q", "-b", "side", base_sha)
    commit("side.txt", "side\n", "side work")
    side = _out(root, "rev-parse", "side")
    _git_in(root, "checkout", "-q", "trunk")
    assert git_helper.base_movement(root, side, tip).status == "diverged"

    # invalid pins are distinct from unmoved
    assert git_helper.base_movement(root, None, tip).status == "invalid"
    assert git_helper.base_movement(root, "f" * 40, tip).status == "invalid"
    # an unexpected Git failure raises rather than reading as unmoved
    with pytest.raises(RuntimeError):
        git_helper.base_movement(root, base_sha, "0" * 40)

    # a linear-behind history legitimately reports zero merges
    lin = tmp_path / "linear"
    lin.mkdir()
    _git(lin, "init", "-q", "-b", "trunk")
    (lin / "f.txt").write_text("1\n")
    _git_in(lin, "add", "-A")
    _git_in(lin, "commit", "-q", "-m", "one")
    first = _out(lin, "rev-parse", "HEAD")
    (lin / "f.txt").write_text("2\n")
    _git_in(lin, "commit", "-qam", "two")
    second = _out(lin, "rev-parse", "HEAD")
    linear = git_helper.base_movement(lin, first, second)
    assert (linear.status, linear.merges) == ("behind", 0)


# --- main_checkout_root (ticket 169) ---


def test_main_checkout_root_from_primary_and_linked_worktree(tmp_path, monkeypatch):
    """The primary checkout is named from any tree; repo_root still reports the
    worktree the cwd is in."""
    primary = tmp_path / "primary tree"  # spaces in a checkout path
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "trunk")
    (primary / "file.txt").write_text("v1\n")
    _git(primary, "add", "-A")
    _git(primary, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    nested = primary / "a" / "b"
    nested.mkdir(parents=True)
    linked = tmp_path / "linked worktree"  # spaces here too
    _git(primary, "worktree", "add", "-q", "-b", "ticket-169", str(linked))

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # explicit-root lookups never depend on cwd

    assert git_helper.main_checkout_root(primary).resolve() == primary.resolve()
    assert git_helper.main_checkout_root(linked).resolve() == primary.resolve()
    assert git_helper.main_checkout_root(nested).resolve() == primary.resolve()

    # repo_root is unchanged: the worktree the cwd sits in
    monkeypatch.chdir(linked)
    assert git_helper.repo_root() == linked.resolve()
    monkeypatch.chdir(primary)
    assert git_helper.repo_root() == primary.resolve()


def test_main_checkout_root_unavailable(tmp_path, monkeypatch):
    # a non-repo directory has no worktree list
    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_helper.main_checkout_root(plain) is None

    # command failure and OS failure are both "unavailable", never a fallback
    for error in (RuntimeError("git worktree list failed"), OSError("boom")):
        def boom(*a, _e=error, **k):
            raise _e

        monkeypatch.setattr(git_helper, "_git_at", boom)
        assert git_helper.main_checkout_root(plain) is None


@pytest.mark.parametrize(
    "output",
    [
        "",  # no records at all
        "\0worktree /linked\0HEAD x\0branch b\0\0",  # empty first record
        "HEAD abc\0\0worktree /linked\0HEAD x\0branch b\0\0",  # malformed first line
        "worktree \0\0worktree /linked\0HEAD x\0branch b\0\0",  # empty path
        "worktree /main\0bare\0\0worktree /linked\0HEAD x\0branch b\0\0",  # bare primary
    ],
    ids=["empty", "empty-record", "malformed", "empty-path", "bare"],
)
def test_main_checkout_root_malformed_or_bare_never_picks_a_later_worktree(
    tmp_path, monkeypatch, output
):
    monkeypatch.setattr(git_helper, "_git_at", lambda *a, **k: output)
    assert git_helper.main_checkout_root(tmp_path) is None


# --- open_pr resume + pr_number (ticket 216) ---


def test_open_pr_returns_the_branchs_open_pr_when_create_fails(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="",
                stderr='a pull request for branch "b" into branch "main" already exists',
            )
        assert cmd[:3] == ["gh", "pr", "view"]
        return subprocess.CompletedProcess(
            cmd, 0, stdout='{"state":"OPEN","url":"https://github.com/o/r/pull/7"}', stderr=""
        )

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    url = git_helper.open_pr(title="t", body="b", base="main")
    assert url == "https://github.com/o/r/pull/7"
    view = [c for c in calls if c[:3] == ["gh", "pr", "view"]][0]
    assert "--json" in view
    assert "state,url" in view


@pytest.mark.parametrize(
    "view_rc,view_out",
    [
        (0, '{"state":"CLOSED","url":"https://github.com/o/r/pull/7"}'),
        (1, ""),
        (0, "not json"),
    ],
    ids=["closed", "nonzero", "unparsable"],
)
def test_open_pr_still_raises_when_no_open_pr_exists(monkeypatch, view_rc, view_out):
    def fake_run(cmd, **kw):
        if cmd[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, view_rc, stdout=view_out, stderr="")

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        git_helper.open_pr(title="t", body="b", base="main")
    assert "gh pr create failed" in str(exc.value)


def test_open_pr_success_makes_one_gh_call(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout="https://github.com/o/r/pull/3\n", stderr=""
        )

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    assert git_helper.open_pr(title="t", body="b", base="main") == "https://github.com/o/r/pull/3"
    assert len(calls) == 1


def test_pr_number_reads_the_url_tail():
    assert git_helper.pr_number("https://github.com/o/r/pull/12") == 12
    assert git_helper.pr_number("https://github.com/o/r/pull/12/") == 12
    assert git_helper.pr_number("url") is None
    assert git_helper.pr_number("") is None
    assert git_helper.pr_number(None) is None
    assert git_helper.pr_number("https://github.com/o/r/issues/12") is None


# --- PR reads: cwd/timeout/mergedAt, CI verdict, merge-tree conflicts (ticket 221) ---


def test_pr_view_reads_selector_in_cwd_with_timeout_and_merged_at(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout='{"state":"OPEN","statusCheckRollup":[],"url":"u","mergedAt":null,'
                   '"headRefOid":"h"}',
            stderr="",
        )

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    cwd = Path("/tmp/project")
    data = git_helper.pr_view("42", cwd=cwd, timeout=20)
    assert data == {
        "state": "OPEN", "statusCheckRollup": [], "url": "u",
        "mergedAt": None, "headRefOid": "h",
    }
    cmd, kw = calls[0]
    assert cmd[:3] == ["gh", "pr", "view"]
    assert cmd[-2:] == ["--json", "state,statusCheckRollup,url,mergedAt,headRefOid"]
    assert kw["cwd"] == str(cwd)
    assert kw["timeout"] == 20


@pytest.mark.parametrize("behaviour", ["missing", "timeout", "badjson"])
def test_pr_view_turns_missing_gh_timeout_and_bad_json_into_runtime_error(
    monkeypatch, behaviour
):
    if behaviour == "missing":

        def fake_run(cmd, **kw):
            raise FileNotFoundError("gh not found")

    elif behaviour == "timeout":

        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(["gh"], 20)

    else:

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        git_helper.pr_view("main", timeout=20)


def test_fetch_origin_timeout_is_a_runtime_error(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="timed out"):
        git_helper.fetch_origin(tmp_path, timeout=30)


def test_merge_tree_conflicts_clean_and_conflicting(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    (tmp_path / "gitconfig").write_text("")

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    _git(root, "checkout", "-q", "-b", "x")
    (root / "a.txt").write_text("x\n")
    _git(root, "commit", "-q", "-am", "x edits a")
    x = _out(root, "rev-parse", "HEAD")

    _git(root, "checkout", "-q", "main")
    (root / "a.txt").write_text("main\n")
    _git(root, "commit", "-q", "-am", "main edits a")
    main = _out(root, "rev-parse", "HEAD")

    _git(root, "checkout", "-q", "-b", "y", "main")
    (root / "b.txt").write_text("b\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-q", "-m", "y adds b")
    y = _out(root, "rev-parse", "HEAD")

    assert git_helper.merge_tree_conflicts(root, main, x) == ["a.txt"]
    assert git_helper.merge_tree_conflicts(root, main, y) == []
    with pytest.raises(RuntimeError):
        git_helper.merge_tree_conflicts(root, main, "nope-not-a-rev")


def test_ci_status_lives_in_git_helper():
    assert git_helper.ci_status([]) == "green"
    assert git_helper.ci_status([{"conclusion": "FAILURE"}]) == "red"
    assert git_helper.ci_status([{"conclusion": ""}]) == "pending"


# --- ticket 275: docs-only merge primitives ---


def _three_way_repo(tmp_path, base: dict, branch: dict, default: dict):
    """Bare origin `trunk` + root + pushed `lime/feature/41-toy`; a `None` value deletes the file."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(origin))
    root = tmp_path / "root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")

    def apply(files: dict) -> None:
        for name, text in files.items():
            path = root / name
            if text is None:
                if path.exists():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)

    apply(base)
    _git_in(root, "add", "-A")
    _git_in(root, "commit", "-q", "-m", "base")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "-u", "origin", "trunk")
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    _git_in(root, "checkout", "-q", "-b", "lime/feature/41-toy")
    apply(branch)
    _git_in(root, "add", "-A")
    _git_in(root, "commit", "-q", "-m", "branch change")
    _git(root, "push", "-q", "-u", "origin", "lime/feature/41-toy")
    _git_in(root, "checkout", "-q", "trunk")
    apply(default)
    _git_in(root, "add", "-A")
    _git_in(root, "commit", "-q", "-m", "default change")
    _git(root, "push", "-q", "origin", "trunk")
    return root


def _entry(term: str) -> str:
    return f"- **{term}** — short text.\n"


def _glossary(*entries: str) -> str:
    return "# Glossary\n" + "".join(entries)


def test_merged_worktree_conflict_carries_diff3_base_section(tmp_path):
    root = _three_way_repo(
        tmp_path,
        base={"conflict me.txt": "base\n"},
        branch={"conflict me.txt": "branch\n"},
        default={"conflict me.txt": "default\n"},
    )
    default_sha, pr_sha = _tips(root)
    before = _checkout_state(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    captured: dict[str, str] = {}

    def resolver(path, text):
        captured[path] = text
        return None

    with pytest.raises(git_helper.MergeConflict) as exc:
        with git_helper.merged_worktree(
            root, default_sha, pr_sha, scratch_dir=scratch, resolve=resolver
        ):
            raise AssertionError("a refused conflict must never reach the body")
    assert exc.value.paths == ["conflict me.txt"]
    assert "\n||||||| " in captured["conflict me.txt"]
    assert list(scratch.iterdir()) == []
    assert _checkout_state(root) == before


def test_merged_worktree_resolver_stages_resolved_docs_and_yields(tmp_path):
    notch = _entry("Notch")
    workspace = _entry("Workspace")
    run_line = _entry("Run line")
    run_canvas = _entry("Run canvas")
    terminal = _entry("Terminal")
    root = _three_way_repo(
        tmp_path,
        base={"CONTEXT.md": _glossary(notch, workspace)},
        branch={"CONTEXT.md": _glossary(notch, terminal, workspace)},
        default={"CONTEXT.md": _glossary(notch, run_line, run_canvas, workspace)},
    )
    default_sha, pr_sha = _tips(root)
    before = _checkout_state(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    expected = _glossary(notch, run_line, run_canvas, terminal, workspace)

    with git_helper.merged_worktree(
        root, default_sha, pr_sha, scratch_dir=scratch,
        resolve=lambda p, t: docs_merge.resolve(t),
    ) as tree:
        assert (tree / "CONTEXT.md").read_text() == expected
        assert git_helper._conflict_paths(tree) == []
        assert "CONTEXT.md" in _out(tree, "diff", "--cached", "--name-only")
        assert _checkout_state(root) == before
    assert list(scratch.iterdir()) == []
    assert _checkout_state(root) == before


def test_merged_worktree_resolver_none_refuses_naming_every_path(tmp_path):
    notch = _entry("Notch")
    workspace = _entry("Workspace")
    root = _three_way_repo(
        tmp_path,
        base={"CONTEXT.md": _glossary(notch, workspace), "app.py": "base\n"},
        branch={"CONTEXT.md": _glossary(notch, _entry("Terminal"), workspace),
                "app.py": "branch\n"},
        default={"CONTEXT.md": _glossary(notch, _entry("Run line"), workspace),
                 "app.py": "default\n"},
    )
    default_sha, pr_sha = _tips(root)
    before = _checkout_state(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    def resolver(path, text):
        return None if path == "app.py" else docs_merge.resolve(text)

    with pytest.raises(git_helper.MergeConflict) as exc:
        with git_helper.merged_worktree(
            root, default_sha, pr_sha, scratch_dir=scratch, resolve=resolver
        ):
            raise AssertionError("refused conflicts must never reach the body")
    assert exc.value.paths == ["CONTEXT.md", "app.py"]
    assert list(scratch.iterdir()) == []
    assert _checkout_state(root) == before


def test_merged_worktree_resolver_unchanged_text_refuses(tmp_path):
    root = _three_way_repo(
        tmp_path,
        base={"CONTEXT.md": "base\n"},
        branch={"CONTEXT.md": "branch\n"},
        default={"CONTEXT.md": None},
    )
    default_sha, pr_sha = _tips(root)
    before = _checkout_state(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    with pytest.raises(git_helper.MergeConflict) as exc:
        with git_helper.merged_worktree(
            root, default_sha, pr_sha, scratch_dir=scratch,
            resolve=lambda p, t: t,
        ):
            raise AssertionError("a marker-less conflict must never read as resolved")
    assert exc.value.paths == ["CONTEXT.md"]
    assert list(scratch.iterdir()) == []
    assert _checkout_state(root) == before


def test_commit_merge_has_pr_then_default_parents_and_moves_no_ref(gate_repo):
    root = gate_repo
    default_sha, pr_sha = _tips(root)
    refs_before = _out(root, "for-each-ref")
    sha = ""

    with git_helper.merged_worktree(root, default_sha, pr_sha) as tree:
        oid = git_helper.staged_tree(tree)
        sha = git_helper.commit_merge(root, oid, pr_sha, default_sha, "m")
        parents = _out(root, "rev-list", "--parents", "-n1", sha).split()
        assert parents == [sha, pr_sha, default_sha]

    assert _out(root, "for-each-ref") == refs_before
    # the unreferenced object survives the throwaway worktree's removal
    assert _out(root, "cat-file", "-e", sha) == ""


def test_push_lease_moves_only_the_origin_branch(gate_repo):
    root = gate_repo
    default_sha, pr_sha = _tips(root)

    with git_helper.merged_worktree(root, default_sha, pr_sha) as tree:
        oid = git_helper.staged_tree(tree)
        merge_sha = git_helper.commit_merge(root, oid, pr_sha, default_sha, "m")

    git_helper.push_lease(root, merge_sha, "lime/feature/41-toy", pr_sha)

    remote = _out(root, "ls-remote", "origin", "refs/heads/lime/feature/41-toy").split()
    assert remote[0] == merge_sha
    # the local Run branch is untouched
    assert _out(root, "rev-parse", "refs/heads/lime/feature/41-toy") == pr_sha


def test_push_lease_refuses_when_origin_moved(gate_repo):
    root = gate_repo
    default_sha, pr_sha = _tips(root)

    with pytest.raises(RuntimeError):
        git_helper.push_lease(root, default_sha, "lime/feature/41-toy", default_sha)

    remote = _out(root, "ls-remote", "origin", "refs/heads/lime/feature/41-toy").split()
    assert remote[0] == pr_sha


def test_merge_pr_match_head_commit_argv(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(git_helper.subprocess, "run", fake_run)
    git_helper.merge_pr("b")
    git_helper.merge_pr("b", head_sha="deadbeef")

    assert calls[0] == ["gh", "pr", "merge", "b", "--merge"]
    assert calls[1] == ["git", "push", "origin", "--delete", "b"]
    assert calls[2] == ["gh", "pr", "merge", "b", "--merge", "--match-head-commit", "deadbeef"]
    # remote delete is unchanged in both shapes
    assert calls[3] == ["git", "push", "origin", "--delete", "b"]
