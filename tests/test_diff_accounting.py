"""Diff accounting against a pinned Run base (ticket 144).

The regression fixture is a real local origin + clone. Local `main` is left at an
older commit while origin is advanced (4929 inherited text lines + 8 binary files)
via disposable side-branch ref plumbing — main is never checked out or pushed. A Run
is born with `worktree.create(..., ticket=...)`, and its birth HEAD is pinned. The
old branch-based measurement charges the Run for origin's inherited work; the pinned
measurement sees only the Run's own 2217 lines.

No network: every remote is a `tmp_path` local origin.
"""

import subprocess
from types import SimpleNamespace

import pytest

from lime import diff_accounting, git_helper, worktree

INHERITED_LINES = 4929
COMMITTED_LINES = 1000
DIRTY_LINES = 800
UNTRACKED_LINES = 417
OWN_LINES = COMMITTED_LINES + DIRTY_LINES + UNTRACKED_LINES  # 2217
LEGACY_LINES = INHERITED_LINES + OWN_LINES  # 7146
INHERITED_BINARIES = 8


def git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(cwd, message: str) -> None:
    git(cwd, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message)


@pytest.fixture
def pinned_run(tmp_path, monkeypatch):
    """A born Run with inherited origin work already behind it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(tmp_path, "init", "-q", "-b", "main", str(origin))
    (origin / "app.py").write_text("base\n")
    git(origin, "add", "-A")
    commit(origin, "init")
    a_sha = git(origin, "rev-parse", "HEAD")

    project = tmp_path / "project"
    git(tmp_path, "clone", "-q", str(origin), str(project))
    git(project, "config", "user.email", "t@t")
    git(project, "config", "user.name", "t")
    git(project, "switch", "-q", "-c", "other")  # never sit on main in the fixture

    # Advance origin/main: commit on a disposable side branch, then move the ref.
    # Main is neither checked out nor pushed.
    git(origin, "switch", "-q", "-c", "inherit")
    (origin / "inherited.txt").write_text(
        "".join(f"inherited {i}\n" for i in range(INHERITED_LINES))
    )
    for i in range(INHERITED_BINARIES):
        (origin / f"img{i}.png").write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00" + bytes([i]) * 32
        )
    git(origin, "add", "-A")
    commit(origin, "inherit: 4929 text lines + 8 binaries")
    b_sha = git(origin, "rev-parse", "HEAD")
    git(origin, "update-ref", "refs/heads/main", b_sha)

    # An untracked, empty ticket so the carried ticket commit adds 0 counted lines.
    tickets = project / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    ticket = tickets / "41-empty.md"
    ticket.write_text("")

    worktrees = tmp_path / "worktrees"
    path = worktree.create(
        project, "run-1", "lime/feature/41-empty", ticket=ticket, base=worktrees
    )
    birth = git(path, "rev-parse", "HEAD")
    return SimpleNamespace(
        origin=origin, project=project, path=path, birth=birth, a_sha=a_sha, b_sha=b_sha
    )


def make_run(base_sha, cap: int = 100000) -> SimpleNamespace:
    """The minimal Run surface `evaluate()` reads: cfg + the optional pin."""
    return SimpleNamespace(base_sha=base_sha, cfg={"commit": {"max_added_lines": cap}})


def add_own_work(path, *, committed=COMMITTED_LINES, dirty=DIRTY_LINES,
                 untracked=UNTRACKED_LINES, binary: bool = False):
    if committed:
        (path / "work_committed.py").write_text(
            "".join(f"c{i}\n" for i in range(committed))
        )
        git(path, "add", "work_committed.py")
        commit(path, "own committed work")
    if dirty:
        (path / "app.py").write_text("".join(f"a{i}\n" for i in range(dirty)))
    if untracked:
        (path / "work_untracked.py").write_text(
            "".join(f"u{i}\n" for i in range(untracked))
        )
    if binary:
        (path / "own.bin").write_bytes(b"\x00\x01\x02\x00\xff")


# --- the real-Git regression ---


def test_pinned_base_ignores_stale_main_text_and_inherited_binaries(pinned_run, monkeypatch):
    f = pinned_run
    add_own_work(f.path)
    monkeypatch.chdir(f.path)

    # Legacy control: the old branch-based measurement charges the Run for origin's
    # inherited text and binaries.
    legacy = diff_accounting.evaluate(make_run(None), "main")
    assert legacy.counted_total == LEGACY_LINES == 7146
    assert len(legacy.binaries) == INHERITED_BINARIES
    assert legacy.requested_base == "main"
    assert legacy.merge_base_sha == f.a_sha  # ordinary merge-base against stale main

    # Pinned mode resolves through the pin and never calls the ordinary merge base.
    def _no_merge_base(*args, **kwargs):
        raise AssertionError("pinned mode must not call git_helper.merge_base")

    monkeypatch.setattr(git_helper, "merge_base", _no_merge_base)
    measurement = diff_accounting.evaluate(make_run(f.birth), "main")

    assert measurement.counted_total == OWN_LINES == 2217
    assert measurement.binaries == []
    assert measurement.requested_base == f.birth
    assert measurement.merge_base_sha == f.birth
    assert measurement.failures == []
    assert not any(c.path == "inherited.txt" for c in measurement.counted_files)
    assert not any(c.path.startswith("img") for c in measurement.counted_files)

    # Fetching/creating the Run left local main exactly where it was.
    assert git(f.project, "rev-parse", "main") == f.a_sha


def test_pinned_own_work_is_measured_never_failed(pinned_run, monkeypatch):
    f = pinned_run
    (f.path / "own_big.py").write_text("".join(f"o{i}\n" for i in range(2217)))
    monkeypatch.chdir(f.path)
    measurement = diff_accounting.evaluate(make_run(f.birth, cap=2000))

    assert measurement.counted_total == 2217
    assert measurement.max_added_lines == 2000
    assert measurement.requested_base == f.birth
    assert measurement.merge_base_sha == f.birth
    assert measurement.failures == []


def test_pinned_own_work_is_measured_under_a_tighter_cap(pinned_run, monkeypatch):
    f = pinned_run
    add_own_work(f.path, committed=1000, dirty=1000, untracked=379)  # 2379
    monkeypatch.chdir(f.path)
    measurement = diff_accounting.evaluate(make_run(f.birth, cap=1500))

    assert measurement.counted_total == 2379
    assert measurement.max_added_lines == 1500
    assert measurement.merge_base_sha == f.birth
    assert measurement.failures == []


def test_pinned_own_new_binary_still_fails_the_binary_gate(pinned_run, monkeypatch):
    f = pinned_run
    add_own_work(f.path, committed=0, dirty=0, untracked=0, binary=True)
    monkeypatch.chdir(f.path)
    measurement = diff_accounting.evaluate(make_run(f.birth))

    assert measurement.binaries == ["own.bin"]
    assert any("binary" in failure for failure in measurement.failures)
    # inherited binaries still never appear
    assert not any(name.startswith("img") for name in measurement.binaries)


# --- read-only pinned-base guard tests (small, linear repos) ---


@pytest.fixture
def guard_repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("v1\n")
    git(tmp_path, "add", "-A")
    commit(tmp_path, "c1")
    initial = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "app.py").write_text("v2\n")
    git(tmp_path, "add", "-A")
    commit(tmp_path, "c2")
    head = git(tmp_path, "rev-parse", "HEAD")
    monkeypatch.chdir(tmp_path)
    return SimpleNamespace(repo=tmp_path, initial=initial, head=head)


def _no_git_measurement(monkeypatch):
    """Any attempt to read stats must fail loudly — a rejected pin measures nothing."""

    def _boom(*args, **kwargs):
        raise AssertionError("no stats may be emitted or queried after a rejected pin")

    monkeypatch.setattr(git_helper, "diff_stats", _boom)
    monkeypatch.setattr(git_helper, "uncounted_paths", _boom)


def _repo_state(repo):
    return (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        git(repo, "ls-files", "-s"),
        git(repo, "status", "--porcelain"),
    )


def test_missing_pinned_commit_fails_loudly_and_read_only(guard_repo, monkeypatch):
    before = _repo_state(guard_repo.repo)
    _no_git_measurement(monkeypatch)
    with pytest.raises(git_helper.InvalidRunBase, match="rebased/merged past its base"):
        diff_accounting.evaluate(make_run("0" * 40))
    assert _repo_state(guard_repo.repo) == before


def test_divergent_recreated_history_is_rejected(guard_repo, monkeypatch):
    # A recreated ticket history: a side branch diverges from the pin. Model it with a
    # safe side branch — never a destructive reset.
    git(guard_repo.repo, "switch", "-q", "-c", "recreated", guard_repo.initial)
    (guard_repo.repo / "recreated.py").write_text("r\n")
    git(guard_repo.repo, "add", "recreated.py")
    commit(guard_repo.repo, "recreated ticket history")
    recreated = git(guard_repo.repo, "rev-parse", "HEAD")
    git(guard_repo.repo, "switch", "-q", "main")

    before = _repo_state(guard_repo.repo)
    _no_git_measurement(monkeypatch)
    with pytest.raises(git_helper.InvalidRunBase) as caught:
        diff_accounting.evaluate(make_run(recreated))
    assert recreated in str(caught.value)
    assert _repo_state(guard_repo.repo) == before


def test_merge_after_pin_is_rejected_even_though_pin_stays_an_ancestor(guard_repo, monkeypatch):
    git(guard_repo.repo, "switch", "-q", "-c", "side", guard_repo.head)
    (guard_repo.repo / "side.py").write_text("s\n")
    git(guard_repo.repo, "add", "side.py")
    commit(guard_repo.repo, "side work")
    git(guard_repo.repo, "switch", "-q", "main")
    git(
        guard_repo.repo, "-c", "user.email=t@t", "-c", "user.name=t",
        "merge", "--no-ff", "-m", "merge side", "side",
    )
    # the pin is still an ancestor of HEAD — only the merge itself indicts the history
    assert git(guard_repo.repo, "merge-base", "--is-ancestor", guard_repo.initial, "HEAD") == ""

    before = _repo_state(guard_repo.repo)
    _no_git_measurement(monkeypatch)
    with pytest.raises(git_helper.InvalidRunBase, match="rebased/merged past its base"):
        diff_accounting.evaluate(make_run(guard_repo.initial))
    assert _repo_state(guard_repo.repo) == before


def test_normal_linear_work_past_the_pin_is_measured(guard_repo, monkeypatch):
    measurement = diff_accounting.evaluate(make_run(guard_repo.initial))
    assert measurement.merge_base_sha == guard_repo.initial
    assert measurement.inspection_error == ""
    assert measurement.counted_total >= 1


def test_head_equal_to_pin_is_allowed(guard_repo):
    measurement = diff_accounting.evaluate(make_run(guard_repo.head))
    assert measurement.counted_total == 0
    assert measurement.binaries == []


def test_merges_before_the_pin_are_fine(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("v1\n")
    git(tmp_path, "add", "-A")
    commit(tmp_path, "c1")
    git(tmp_path, "switch", "-q", "-c", "side")
    (tmp_path / "side.py").write_text("s\n")
    git(tmp_path, "add", "side.py")
    commit(tmp_path, "side")
    git(tmp_path, "switch", "-q", "main")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t",
        "merge", "--no-ff", "-m", "merge side", "side")
    (tmp_path / "app.py").write_text("v2\n")
    git(tmp_path, "add", "-A")
    commit(tmp_path, "c2")
    monkeypatch.chdir(tmp_path)

    pin = git(tmp_path, "rev-parse", "HEAD")
    measurement = diff_accounting.evaluate(make_run(pin))
    assert measurement.merge_base_sha == pin
    assert measurement.failures == []