"""Per-Run sandbox: worktree + branch lifecycle (spec §8).

One worktree per Run, OUTSIDE the project at `~/.lime/worktrees/<repo>/<run-id>/`,
branched `lime/<workflow>/<NN>-<slug>` off `origin/<default>`'s current commit
(fetched), the ticket carried in as the branch's first commit.
Birth: branch check → worktree add → ticket commit → `commands.setup` as the first Phase (red =
Run `fail`, worktree and branch kept for `lime run --resume`; ratified §15.5).
Death: `commands.teardown` → worktree remove → branch delete (local, and any
copy `push_pr` left on origin) → the cwd-keyed Claude Code session dir under
`~/.claude/projects/`.

Local only, no container — this module is the single point a remote sandbox
would replace.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .utils import operator_env

WORKTREES = Path("~/.lime/worktrees").expanduser()
CC_PROJECTS = Path("~/.claude/projects").expanduser()


class WorktreeError(RuntimeError):
    """The sandbox could not be born (or torn down) as the spec demands."""


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo_root), *args],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def branch_name(workflow: str, ticket_stem: str) -> str:
    """`lime/<workflow>/<NN>-<slug>` — the stem is the ticket filename minus .md."""
    return f"lime/{workflow}/{ticket_stem}"


def path_for(repo_root: Path, run_id: str, base: Path | None = None) -> Path:
    return (base or WORKTREES) / Path(repo_root).name / run_id


def session_dir(worktree_path: Path) -> Path:
    """The cwd-keyed Claude Code session dir: the resolved path, `/` → `-` (07-guard-probe).

    `.resolve()` first — a relative `base=` or a macOS `/var`→`/private/var`
    alias would otherwise encode a different string than the one CC used as cwd.
    """
    return CC_PROJECTS / str(Path(worktree_path).resolve()).replace("/", "-")


def _has_origin(repo_root: Path) -> bool:
    """A repo with no origin (local-only, tests) has no remote copy to clean up."""
    return subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
    ).returncode == 0


def _branch_exists(repo_root: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet",
         f"refs/heads/{branch}"],
        capture_output=True,
    ).returncode == 0


def default_branch(repo_root: Path) -> str:
    """origin's HEAD when there is a remote, else the branch the repo sits on."""
    try:
        head = _git(repo_root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        return head.removeprefix("origin/")
    except WorktreeError:
        return _git(repo_root, "symbolic-ref", "--short", "HEAD")


def _discard(repo_root: Path, path: Path, branch: str) -> None:
    """Remove the worktree and delete the local branch. Nothing left behind."""
    if path and path.exists():
        try:
            _git(repo_root, "worktree", "remove", "--force", str(path))
        except WorktreeError:
            pass
    try:
        _git(repo_root, "branch", "-D", branch)
    except WorktreeError:
        pass


def create(
    repo_root: Path,
    run_id: str,
    branch: str,
    *,
    setup: str | None = None,
    env: dict[str, str] | None = None,
    base: Path | None = None,
    ticket: Path | None = None,
) -> Path:
    """Birth: branch check → worktree add → ticket commit → setup. Red = nothing left behind."""
    if _branch_exists(repo_root, branch):
        raise WorktreeError(
            f"branch {branch!r} already exists — a Run never reuses a branch (spec §8)")
    path = path_for(repo_root, run_id, base)
    if path.exists():
        raise WorktreeError(f"worktree {path} already exists")
    start = default_branch(repo_root)
    if _has_origin(repo_root):
        _git(repo_root, "fetch", "origin", start)
        start = f"origin/{start}"
    elif not _branch_exists(repo_root, start):
        start = f"origin/{start}"        # default branch known only through the remote
    _git(repo_root, "worktree", "add", "-b", branch, str(path), start)
    if ticket is not None:
        try:
            rel = Path(ticket).resolve().relative_to(Path(repo_root).resolve())
            dest = path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ticket, dest)
            if _git(path, "status", "--porcelain", "--", str(rel)):
                _git(path, "add", "--", str(rel))
                _git(path, "commit", "-q", "-m",
                     f"ticket: {Path(ticket).stem}\n\nLime-Run: {run_id}\nLime-Role: orchestrator")
        except (WorktreeError, OSError) as error:
            _discard(repo_root, path, branch)
            raise WorktreeError(f"could not carry the ticket into the Run's branch: {error}")
    if setup:
        result = subprocess.run(
            setup, shell=True, cwd=path, capture_output=True, text=True,
            env={**operator_env(), **(env or {})},
        )
        if result.returncode != 0:
            _discard(repo_root, path, branch)
            tail = (result.stderr or result.stdout).strip()[-2000:]
            raise WorktreeError(
                f"commands.setup exited {result.returncode} — no Run:\n{tail}")
    return path


def teardown(
    repo_root: Path,
    run_id: str,
    branch: str,
    *,
    command: str | None = None,
    env: dict[str, str] | None = None,
    base: Path | None = None,
) -> list[str]:
    """Death: `commands.teardown` → worktree remove → branch delete → CC session dir.

    Best-effort — a half-dead sandbox must still be removable — so problems are
    returned for the caller to report, never raised. Re-running it on an
    already torn-down Run reports nothing.
    """
    problems: list[str] = []
    path = path_for(repo_root, run_id, base)
    if command and path.exists():
        result = subprocess.run(
            command, shell=True, cwd=path, capture_output=True, text=True,
            env={**operator_env(), **(env or {})},
        )
        if result.returncode != 0:
            problems.append(
                f"commands.teardown exited {result.returncode}:"
                f" {(result.stderr or result.stdout).strip()[-500:]}")
    steps: list[list[str]] = [
        ["worktree", "remove", "--force", str(path)] if path.exists()
        else ["worktree", "prune"],
    ]
    if _branch_exists(repo_root, branch):
        steps.append(["branch", "-D", branch])
    for args in steps:
        try:
            _git(repo_root, *args)
        except WorktreeError as error:
            problems.append(str(error))
    # A Run that reached push_pr left the branch on origin too — a surviving copy
    # would reject the next Run of this ticket's push as non-fast-forward (§8:
    # a Run never reuses a branch, and a discarded Run's branch is dead).
    if branch and _has_origin(repo_root):
        probe = subprocess.run(
            ["git", "-C", str(repo_root), "ls-remote", "--exit-code", "origin",
             f"refs/heads/{branch}"],
            capture_output=True, text=True,
        )
        if probe.returncode == 0:
            try:
                _git(repo_root, "push", "origin", "--delete", branch)
            except WorktreeError as error:
                problems.append(str(error))
        elif probe.returncode != 2:  # 2 = origin answered: no such branch
            problems.append(
                f"couldn't ask origin whether branch {branch!r} was pushed:"
                f" {probe.stderr.strip()[-200:]} — delete it there yourself if it exists"
            )
    shutil.rmtree(session_dir(path), ignore_errors=True)
    return problems
