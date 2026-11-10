"""Tests for lime/numbering.py — ticket numbers derived from git (origin branches,
PRs, ticket commits, reservations) plus .lime/tickets/."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from lime import git_helper, numbering, worktree

RESERVATION_NS = numbering.RESERVATION_NS
_STEM = numbering._STEM
_BRANCH = numbering._BRANCH
_SUBJECT = numbering._SUBJECT
_RESERVATION = numbering._RESERVATION


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def clone(tmp_path):
    """Bare origin + seed commit pushed as main + a clone of it.

    Returns (project, origin) so tests can push branches to origin,
    create reservations, etc.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "file.txt").write_text("v1\n")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    _git(seed, "symbolic-ref", "refs/remotes/origin/HEAD",
         "refs/remotes/origin/main")

    project = tmp_path / "project"
    _git(tmp_path, "clone", "-q", str(origin), str(project))
    _git(project, "config", "user.email", "t@t")
    _git(project, "config", "user.name", "t")
    return project, origin


# --- pure helpers ---


class TestNumbers:
    def test_numbers_reads_each_naming_shape(self):
        assert numbering.numbers(
            ["lime/feature/72-x", "refs/heads/lime/bug-fix/7-y", "lime/install"],
            _BRANCH,
        ) == {72, 7}

        assert numbering.numbers(
            ["ticket: 63-z", "fix: thing"], _SUBJECT
        ) == {63}

        assert numbering.numbers(
            [f"{RESERVATION_NS}/80"], _RESERVATION
        ) == {80}

        assert numbering.numbers(
            ["41-toy", "notes"], _STEM
        ) == {41}


class TestLocalNumbers:
    def test_local_numbers_reads_ticket_stems(self, tmp_path):
        tickets_dir = tmp_path / ".lime" / "tickets"
        tickets_dir.mkdir(parents=True)
        (tickets_dir / "67-a.md").write_text("a")
        (tickets_dir / "73-b.md").write_text("b")
        (tickets_dir / "README.md").write_text("readme")
        assert numbering.local_numbers(tmp_path) == {67, 73}

    def test_local_numbers_returns_empty_set_when_dir_missing(self, tmp_path):
        assert numbering.local_numbers(tmp_path) == set()


class TestCommitNumbers:
    def test_commit_numbers_sees_ticket_commits_on_any_branch(self, clone):
        project, origin = clone
        # ticket: 58-a on main
        (project / "58-a.txt").write_text("a")
        _git(project, "add", "-A")
        _git(project, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "ticket: 58-a")
        _git(project, "push", "-q", "origin", "main")

        # ticket: 60-b on an unmerged side branch
        _git(project, "checkout", "-q", "-b", "lime/feature/60-b")
        (project / "60-b.txt").write_text("b")
        _git(project, "add", "-A")
        _git(project, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "ticket: 60-b")
        _git(project, "push", "-q", "origin", "lime/feature/60-b")
        _git(project, "checkout", "-q", "main")

        assert numbering.commit_numbers(project) == {58, 60}


class TestOriginNumbers:
    def test_origin_numbers_reads_branch_heads_and_reservations(self, clone, monkeypatch):
        project, origin = clone
        # Push a branch head with a number
        _git(project, "checkout", "-q", "-b", "lime/feature/70-x")
        (project / "70.txt").write_text("70")
        _git(project, "add", "-A")
        _git(project, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "ticket: 70-x")
        _git(project, "push", "-q", "origin", "lime/feature/70-x")
        _git(project, "checkout", "-q", "main")

        # Push a branch that does NOT match _BRANCH pattern (no number)
        _git(project, "checkout", "-q", "-b", "lime/install")
        (project / "install.txt").write_text("install")
        _git(project, "add", "-A")
        _git(project, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "install")
        _git(project, "push", "-q", "origin", "lime/install")
        _git(project, "checkout", "-q", "main")

        # Reserve a number via reserve()
        numbering.reserve(project, 71, "71-r", "run-r")

        assert numbering.origin_numbers(project) == {70, 71}


class TestNextNumber:
    def test_next_number_is_max_plus_one_across_every_source_and_survives_an_empty_tickets_dir(
        self, clone, monkeypatch
    ):
        project, origin = clone
        # Push an origin branch (70)
        _git(project, "checkout", "-q", "-b", "lime/feature/70-x")
        (project / "70.txt").write_text("70")
        _git(project, "add", "-A")
        _git(project, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "ticket: 70-x")
        _git(project, "push", "-q", "origin", "lime/feature/70-x")
        _git(project, "checkout", "-q", "main")

        # Reserve 71
        numbering.reserve(project, 71, "71-r", "run-r")

        # Mock PR head refs (72)
        monkeypatch.setattr(numbering.git_helper, "pr_head_refs",
                            lambda: ["lime/bug-fix/72-w", "lime/install"])

        # Commit 58 on main
        (project / "58.txt").write_text("58")
        _git(project, "add", "-A")
        _git(project, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "ticket: 58-a")

        # Local file 67
        tickets_dir = project / ".lime" / "tickets"
        tickets_dir.mkdir(parents=True)
        (tickets_dir / "67-a.md").write_text("a")

        # max(58, 67, 70, 71, 72) = 72 + 1 = 73
        assert numbering.next_number(project) == 73

        # Clear the tickets dir — still 73
        for f in tickets_dir.iterdir():
            f.unlink()
        assert numbering.next_number(project) == 73

    def test_next_number_is_one_in_a_fresh_repo_and_never_calls_gh_without_an_origin(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "repo"
        root.mkdir()
        _git(tmp_path, "init", "-q", "-b", "main", str(root))
        (root / "f.txt").write_text("content\n")
        _git(root, "add", "-A")
        _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "init")
        # No origin configured
        monkeypatch.setattr(numbering.git_helper, "pr_head_refs",
                            lambda: (_ for _ in ()).throw(AssertionError("should not be called")))
        assert numbering.next_number(root) == 1

    def test_next_number_raises_when_gh_fails(self, clone, monkeypatch):
        project, origin = clone
        monkeypatch.setattr(
            numbering.git_helper, "pr_head_refs",
            lambda: (_ for _ in ()).throw(RuntimeError("gh pr list failed")),
        )
        with pytest.raises(RuntimeError, match="gh pr list failed"):
            numbering.next_number(project)


class TestReserve:
    def test_reserve_creates_the_ref_on_origin_and_no_local_ref(self, clone, monkeypatch):
        project, origin = clone
        monkeypatch.setattr(numbering.git_helper, "pr_head_refs", lambda: [])

        ref = numbering.reserve(project, 73, "73-foo", "run-a")
        assert ref == f"{RESERVATION_NS}/73"

        ls = _git(project, "ls-remote", "origin", f"{RESERVATION_NS}/73")
        assert ls, "ref should exist on origin"

        local_refs = subprocess.run(
            ["git", "-C", str(project), "for-each-ref", "refs/lime"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert not local_refs, "no local ref should exist"

        assert numbering.next_number(project) == 74

    def test_reserve_refuses_a_number_held_by_another_ticket(self, clone):
        project, origin = clone
        numbering.reserve(project, 73, "73-foo", "run-a")
        with pytest.raises(numbering.NumberingError) as exc:
            numbering.reserve(project, 73, "73-bar", "run-b")
        msg = str(exc.value)
        assert f"{RESERVATION_NS}/73" in msg
        assert "73-foo" in msg

    def test_reserve_allows_the_same_ticket_again(self, clone):
        project, origin = clone

        ref1 = numbering.reserve(project, 73, "73-foo", "run-a")
        assert ref1 == f"{RESERVATION_NS}/73"

        sha_before = _git(project, "ls-remote", "origin", f"{RESERVATION_NS}/73").split("\t")[0]

        ref2 = numbering.reserve(project, 73, "73-foo", "run-b")
        assert ref2 == f"{RESERVATION_NS}/73"

        sha_after = _git(project, "ls-remote", "origin", f"{RESERVATION_NS}/73").split("\t")[0]
        assert sha_after == sha_before, "ref should be unchanged"

    def test_reserve_refuses_when_the_ref_appears_between_probe_and_push(self, clone, monkeypatch):
        project, origin = clone
        # First reserve succeeds
        numbering.reserve(project, 73, "73-foo", "run-a")

        # Now make _reserved_on_origin return False to simulate a race,
        # then the push itself will fail (CAS rejects it)
        monkeypatch.setattr(numbering, "_reserved_on_origin", lambda root, ref: False)

        with pytest.raises(numbering.NumberingError) as exc:
            numbering.reserve(project, 73, "73-bar", "run-b")
        msg = str(exc.value)
        assert f"{RESERVATION_NS}/73" in msg

    def test_reserve_is_a_noop_without_an_origin_or_a_number(self, tmp_path):
        # No origin repo
        root = tmp_path / "noorigin"
        root.mkdir()
        _git(tmp_path, "init", "-q", "-b", "main", str(root))
        (root / "f.txt").write_text("content\n")
        _git(root, "add", "-A")
        _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "init")
        assert numbering.reserve(root, 73, "73-foo", "run-a") is None

    def test_reserve_noop_with_number_zero(self, clone):
        project, origin = clone
        # Reserve with number=0 returns None, nothing on origin
        assert numbering.reserve(project, 0, "0-foo", "run-a") is None
        ls = subprocess.run(
            ["git", "-C", str(project), "ls-remote", "origin", f"{RESERVATION_NS}/0"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert not ls

    def test_reserved_on_origin_raises_when_origin_cannot_be_asked(self, clone):
        project, origin = clone
        _git(project, "remote", "set-url", "origin", "/nonexistent")
        with pytest.raises(numbering.NumberingError) as exc:
            numbering._reserved_on_origin(project, f"{RESERVATION_NS}/73")
        assert "couldn't ask origin" in str(exc.value)


# --- ADR ledger: a second namespace, owned by the Run -------------------------

ADR_NS = numbering.ADR_RESERVATION_NS


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message)


def _clone(origin: Path, dest: Path) -> Path:
    _git(dest.parent, "clone", "-q", str(origin), str(dest))
    _git(dest, "config", "user.email", "t@t")
    _git(dest, "config", "user.name", "t")
    return dest


@pytest.fixture
def trunk_clone(tmp_path):
    """Bare origin on the non-protected `trunk` + a clone. Returns (project, origin, seed)."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(origin))

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "trunk")
    (seed / "file.txt").write_text("v1\n")
    _commit_all(seed, "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "trunk")
    _git(seed, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")

    project = _clone(origin, tmp_path / "project")
    return project, origin, seed


class TestIndependentNamespace:
    def test_reserve_accepts_an_independent_namespace(self, trunk_clone):
        project, origin, _seed = trunk_clone
        ticket_ref = numbering.reserve(project, 5, "5-foo", "run-a")
        assert ticket_ref == f"{RESERVATION_NS}/5"
        adr_ref = numbering.reserve(
            project, 5, "run-b", "run-b", namespace=ADR_NS
        )
        assert adr_ref == f"{ADR_NS}/0005"
        assert _git(project, "ls-remote", "origin", ticket_ref)
        assert _git(project, "ls-remote", "origin", adr_ref)


class TestNextAdrNumber:
    @pytest.mark.parametrize("setup,expected", [
        ("missing", 1),
        ("empty", 1),
        ("files", 8),
    ])
    def test_next_adr_number_without_origin(self, tmp_path, monkeypatch, setup, expected):
        root = tmp_path / "repo"
        root.mkdir()
        _git(tmp_path, "init", "-q", "-b", "trunk", str(root))
        (root / "f.txt").write_text("x\n")
        _commit_all(root, "init")
        adr = root / "docs" / "adr"
        if setup == "empty":
            adr.mkdir(parents=True)
        elif setup == "files":
            adr.mkdir(parents=True)
            (adr / "0003-a.md").write_text("a")
            (adr / "0007-b.md").write_text("b")
            (adr / "README.md").write_text("readme")
            (adr / "notes.txt").write_text("n")
            (adr / "sub").mkdir()
            (adr / "sub" / "0009-nested.md").write_text("n")

        monkeypatch.setattr(
            numbering, "pr_numbers",
            lambda: (_ for _ in ()).throw(AssertionError("no PR/gh call for ADRs")),
        )
        assert numbering.next_adr_number(root) == expected
        refs = subprocess.run(
            ["git", "-C", str(root), "for-each-ref", "refs/lime"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert not refs, "no local ledger ref should be written"

    @pytest.mark.parametrize("highest", ["local", "origin_tree", "ref"])
    def test_next_adr_number_max_across_sources(self, trunk_clone, highest):
        values = {"local": 4, "origin_tree": 9, "ref": 6}
        values[highest] = 20
        project, origin, seed = trunk_clone

        # An origin-only ADR file in the default branch tree, not in the working tree.
        (seed / "docs" / "adr").mkdir(parents=True)
        (seed / "docs" / "adr" / f"{values['origin_tree']:04d}-origin.md").write_text("o")
        _commit_all(seed, "origin adr")
        _git(seed, "push", "-q", "origin", "trunk")
        _git(project, "fetch", "-q", "origin")

        adr = project / "docs" / "adr"
        adr.mkdir(parents=True)
        (adr / f"{values['local']:04d}-local.md").write_text("l")
        assert not any(p.name.endswith("-origin.md") for p in adr.glob("*.md"))

        numbering.reserve(
            project, values["ref"], f"run-{values['ref']}", f"run-{values['ref']}",
            namespace=ADR_NS,
        )
        # A higher ticket ref is a different namespace and must not be counted.
        numbering.reserve(project, 99, "99-ticket", "run-t", namespace=RESERVATION_NS)

        assert worktree.default_branch(project) == "trunk"
        assert numbering.next_adr_number(project) == max(values.values()) + 1

    def test_next_adr_number_origin_without_adr_directory(self, trunk_clone):
        project, origin, _seed = trunk_clone
        assert numbering.next_adr_number(project) == 1


class TestReserveAdr:
    def test_reserve_adr_creates_padded_ref_and_is_idempotent(self, tmp_path, trunk_clone):
        project, origin, _seed = trunk_clone
        number = numbering.reserve_adr(project, "run-a")
        assert isinstance(number, int) and number == 1
        ref = f"{ADR_NS}/0001"
        assert _git(project, "ls-remote", "origin", ref)
        sha_before = _git(project, "ls-remote", "origin", ref).split("\t")[0]
        assert numbering._holder(project, ref) == "run-a"

        local_refs = subprocess.run(
            ["git", "-C", str(project), "for-each-ref", "refs/lime"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert not local_refs, "no persistent local ledger ref"

        assert numbering.reserve_adr(project, "run-a") == 1
        assert _git(project, "ls-remote", "origin", ref).split("\t")[0] == sha_before

        # Another clone, same Run, after a higher reservation exists: durable identity,
        # not recalculation or process-local caching.
        other = _clone(origin, tmp_path / "other")
        assert numbering.reserve_adr(other, "run-b") == 2
        assert numbering.reserve_adr(other, "run-a") == 1

    def test_reserve_adr_concurrent_runs_get_distinct_numbers(self, tmp_path, trunk_clone, monkeypatch):
        import threading

        project, origin, _seed = trunk_clone
        other = _clone(origin, tmp_path / "other")

        barrier = threading.Barrier(2)
        real_reserve = numbering.reserve
        lock = threading.Lock()
        seen: set[int] = set()

        def gated_reserve(*args, **kwargs):
            tid = threading.get_ident()
            with lock:
                first = tid not in seen
                seen.add(tid)
            if first:
                barrier.wait(timeout=15)  # only the first attempt waits; retry never does
            return real_reserve(*args, **kwargs)

        monkeypatch.setattr(numbering, "reserve", gated_reserve)

        results: dict[str, int] = {}
        errors: dict[str, Exception] = {}

        def worker(name: str, root: Path) -> None:
            try:
                results[name] = numbering.reserve_adr(root, name)
            except Exception as error:  # noqa: BLE001 - surfaced by the assertions below
                errors[name] = error

        threads = [
            threading.Thread(target=worker, args=("run-a", project)),
            threading.Thread(target=worker, args=("run-b", other)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, errors
        assert results["run-a"] != results["run-b"]
        assert set(results.values()) == {1, 2}
        for run, num in results.items():
            ref = f"{ADR_NS}/{num:04d}"
            assert _git(project, "ls-remote", "origin", ref)
            assert numbering._holder(project, ref) == run
        assert numbering.reserve_adr(project, "run-a") == results["run-a"]
        assert numbering.reserve_adr(other, "run-b") == results["run-b"]

    def test_reserve_adr_without_origin_is_unreserved(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        _git(tmp_path, "init", "-q", "-b", "trunk", str(root))
        (root / "f.txt").write_text("x\n")
        _commit_all(root, "init")
        (root / "docs" / "adr").mkdir(parents=True)
        (root / "docs" / "adr" / "0007-a.md").write_text("a")

        assert numbering.reserve_adr(root, "run-a") == 8
        refs = subprocess.run(
            ["git", "-C", str(root), "for-each-ref", "refs/lime"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert not refs, "no ref written without an origin"

    def test_reserve_adr_does_not_retry_transport_errors(self, trunk_clone):
        project, origin, _seed = trunk_clone
        _git(project, "remote", "set-url", "origin", str(origin.parent / "nonexistent.git"))
        with pytest.raises(numbering.NumberingError):
            numbering.reserve_adr(project, "run-a")

    def test_reserve_adr_does_not_retry_a_rejected_push(self, trunk_clone):
        project, origin, _seed = trunk_clone
        hook = origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        with pytest.raises(numbering.NumberingError):
            numbering.reserve_adr(project, "run-a")
        # no ref was claimed, and no success was reported
        assert not _git(project, "ls-remote", "origin", f"{ADR_NS}/*")