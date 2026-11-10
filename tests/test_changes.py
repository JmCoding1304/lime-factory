"""Deterministic change capture: what was built, straight from git (spec §7)."""

import subprocess
from types import SimpleNamespace

import pytest

from lime import changes, git_helper
from lime.data_types import ChangeCapture


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "app.py").write_text("v1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run_at(repo_root) -> SimpleNamespace:
    handoff = repo_root / "handoff"
    handoff.mkdir(exist_ok=True)
    return SimpleNamespace(context_handoff_dir=handoff)


# --- resolve_base ---


def test_resolve_base_raises_without_a_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # tmp_path itself is not a git repo
    with pytest.raises(RuntimeError, match="not a git repository"):
        changes.resolve_base("main")


def test_resolve_base_raises_for_a_missing_ref(repo):
    with pytest.raises(RuntimeError, match="does not exist"):
        changes.resolve_base("no-such-branch")


def test_resolve_base_on_clean_main_with_no_parent_says_so(repo):
    base = changes.resolve_base("main")
    assert "no parent commit" in base.reason


def test_resolve_base_on_clean_main_with_a_parent_falls_back_to_the_last_commit(repo):
    (repo / "app.py").write_text("v2\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "second")
    base = changes.resolve_base("main")
    assert "last commit" in base.reason


def test_resolve_base_on_dirty_main_diffs_the_working_tree(repo):
    (repo / "app.py").write_text("v2\n")
    base = changes.resolve_base("main")
    assert "uncommitted working tree" in base.reason


def test_resolve_base_off_branch_diffs_the_whole_branch(repo):
    git(repo, "switch", "-q", "-c", "feature")
    (repo / "app.py").write_text("v2\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "feature work")
    base = changes.resolve_base("main")
    assert "ahead of" in base.reason


# --- capture / as_envelope ---


def test_capture_writes_the_diff_and_returns_a_change_set(repo):
    git(repo, "switch", "-q", "-c", "feature")
    (repo / "app.py").write_text("v2\n")
    (repo / "new.py").write_text("brand new\n")
    git(repo, "add", "app.py")  # new.py stays untracked

    change = changes.capture(run_at(repo), ChangeCapture(base="main"))

    assert change.files == ["app.py"]
    assert change.untracked == ["new.py"]
    assert change.insertions >= 1
    assert not change.empty
    diff_text = (repo / "handoff" / "changes.diff").read_text()
    assert diff_text == open(change.diff_path).read()
    assert "new.py" in diff_text


def test_as_envelope_summarizes_the_change_set(repo):
    (repo / "app.py").write_text("v2\n")
    change = changes.capture(run_at(repo), ChangeCapture(base="main"))
    envelope = changes.as_envelope(change, notes="ready for review")
    assert envelope.status == "success"
    assert envelope.notes_for_next_agent == "ready for review"
    assert envelope.diff_path == change.diff_path
    assert "app.py" in envelope.changed_files


def test_capture_truncates_a_long_diff(repo):
    (repo / "app.py").write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
    change = changes.capture(run_at(repo), ChangeCapture(base="main", max_diff_lines=5))
    assert change.truncated
    assert "truncated at 5 lines" in open(change.diff_path).read()


def test_base_ref_label_shortens_a_pinned_sha(repo):
    sha = git_helper.rev("HEAD")
    base = changes.resolve_base(sha)
    assert base.label == sha[:7]


# --- pinned mode (ticket 144) ---


def run_pinned(repo_root, base_sha) -> SimpleNamespace:
    # handoff lives OUTSIDE the repo so its own artifacts never pollute the diff
    handoff = repo_root.parent / f"{repo_root.name}-handoff"
    handoff.mkdir(exist_ok=True)
    return SimpleNamespace(context_handoff_dir=handoff, base_sha=base_sha)


def test_resolve_base_pinned_records_the_pin_and_a_truthful_reason(repo):
    sha = git_helper.rev("HEAD")
    base = changes.resolve_base("main", base_sha=sha)
    assert base.ref == sha
    assert base.commit == sha
    assert base.label == sha[:7]
    assert "pinned at launch" in base.reason


def test_pinned_clean_head_equal_to_base_is_an_empty_change_not_head1(repo):
    # advance so HEAD~1 exists and the legacy clean-tree fallback would fire
    (repo / "app.py").write_text("v2\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "second")
    sha = git_helper.rev("HEAD")

    pinned = changes.capture(run_pinned(repo, sha), ChangeCapture(base="main"))
    assert pinned.base.commit == sha  # the pin itself, never HEAD~1
    assert pinned.empty


def test_pinned_capture_excludes_inherited_paths(repo):
    (repo / "inherited.py").write_text("".join(f"i{i}\n" for i in range(40)))
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "inherited")
    pin = git_helper.rev("HEAD")

    (repo / "own.py").write_text("o\n")
    git(repo, "add", "own.py")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "own")
    (repo / "dirty.py").write_text("d\n")

    pinned = changes.capture(run_pinned(repo, pin), ChangeCapture(base="main"))
    assert "inherited.py" not in pinned.files
    assert "own.py" in pinned.files
    assert "dirty.py" in pinned.untracked


def test_invalid_pin_fails_loudly(repo):
    with pytest.raises(git_helper.InvalidRunBase, match="rebased/merged past its base"):
        changes.resolve_base("main", base_sha="0" * 40)
