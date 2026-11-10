"""Local bare-origin fixtures for the launch overlap guard (ticket 160).

Everything stays in pytest temporary directories with a local Git identity and a
default branch named `trunk`; nothing is pushed or checked out on main/master.
Reusable from both `test_overlap.py` and `test_cli.py`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

from lime import tracer, worktree

DEFAULT = "trunk"


def git(cwd: Path | str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result.stdout.strip()


def ticket_text(number: int, where: str, *, title: str = "Toy", blocked_by: str | None = None,
                workflow: str = "feature") -> str:
    header = f"# {number} — {title}\n\nWorkflow: {workflow}\nStatus: open\n"
    if blocked_by:
        header += f"Blocked by: {blocked_by}\n"
    return (
        header
        + f"\n## Ask\nDo the thing.\n\n## Where\n{where}\n\n"
        + "## Done means\nIt works.\n\n## Out of scope\nNothing.\n"
    )


class Origin:
    """A bare origin (default `trunk`) plus a `root` clone that tracks it."""

    def __init__(self, tmp_path: Path, default: str = DEFAULT):
        self.default = default
        self.tmp_path = tmp_path
        self.origin = tmp_path / "origin.git"
        self.origin.parent.mkdir(parents=True, exist_ok=True)
        git(self.origin.parent, "init", "-q", "--bare", "-b", default, str(self.origin))
        self.root = tmp_path / "root"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", default)
        git(self.root, "config", "user.email", "t@t")
        git(self.root, "config", "user.name", "t")
        (self.root / "app.py").write_text("v1\n")
        tickets = self.root / ".lime" / "tickets"
        tickets.mkdir(parents=True)
        (tickets / "1-base.md").write_text(ticket_text(1, "`README.md`"))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        git(self.root, "remote", "add", "origin", str(self.origin))
        git(self.root, "push", "-q", "-u", "origin", default)
        git(self.root, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{default}")

    def _scratch(self) -> Path:
        scratch = self.tmp_path / f"scratch-{uuid.uuid4().hex[:8]}"
        git(self.tmp_path, "clone", "-q", str(self.origin), str(scratch))
        git(scratch, "config", "user.email", "t@t")
        git(scratch, "config", "user.name", "t")
        return scratch

    def publish(self, branch: str, files: dict[str, str], *, base: str | None = None) -> None:
        """Commit `files` ({rel path: text}) on `branch` off `base` (default trunk) and push."""
        scratch = self._scratch()
        try:
            git(scratch, "checkout", "-q", "-b", branch, f"origin/{base or self.default}")
            for rel, text in files.items():
                dest = scratch / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text)
            git(scratch, "add", "-A")
            git(scratch, "commit", "-q", "-m", f"ticket: {Path(branch).name}")
            git(scratch, "push", "-q", "origin", branch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def publish_run(self, branch: str, number: int, where: str, *, base: str | None = None,
                    **kwargs) -> None:
        """Publish a `lime/<workflow>/<NN>-<slug>` branch carrying its own ticket."""
        stem = Path(branch).name
        kwargs.setdefault("title", stem)
        self.publish(
            branch,
            {f".lime/tickets/{stem}.md": ticket_text(number, where, **kwargs)},
            base=base,
        )

    def delete_branch(self, branch: str) -> None:
        git(self.root, "push", "-q", "origin", "--delete", branch)

    def close_ticket(self, branch: str, *, stem: str | None = None, base: str | None = None) -> None:
        """`commit_docs` on a published branch; `base` starts a fresh branch whose only
        commit is the deletion."""
        name = stem or Path(branch).name
        scratch = self._scratch()
        try:
            git(scratch, "checkout", "-q", "-b", branch, f"origin/{base or branch}")
            git(scratch, "rm", "-q", f".lime/tickets/{name}.md")
            git(scratch, "commit", "-q", "-m", f"docs: close {name}")
            git(scratch, "push", "-q", "origin", branch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def merge_into_default(self, branch: str, *, delete: bool = True) -> None:
        scratch = self._scratch()
        try:
            git(scratch, "checkout", "-q", self.default)
            git(scratch, "merge", "--no-ff", "-q", "-m", f"Merge {branch}", f"origin/{branch}")
            git(scratch, "push", "-q", "origin", self.default)
            if delete:
                git(scratch, "push", "-q", "origin", "--delete", branch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def push_branch(self, branch: str) -> None:
        """Push root's *local* branch to origin (no clone, no checkout)."""
        git(self.root, "push", "-q", "origin", branch)

    def commit_push(self, files: dict[str, str], *, message: str = "commit") -> None:
        """Commit `files` onto the local default branch and push it to origin.

        Seeds origin/trunk with a ticket so a later Run branch off it can share an
        identical birth HEAD (the pre-existing-identical-ticket case).
        """
        for rel, text in files.items():
            dest = self.root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)
        git(self.root, "push", "-q", "origin", self.default)

    def fetch(self) -> None:
        git(self.root, "fetch", "--prune", "origin")

    def write_ticket(self, name: str, text: str) -> Path:
        path = self.root / ".lime" / "tickets" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

def close_local_ticket(run) -> None:
    """`commit_docs` on an unpushed Run's branch (deletes the carried ticket); never pushes."""
    git(run.worktree, "rm", "-q", f".lime/tickets/{run.ticket.name}")
    git(run.worktree, "commit", "-q", "-m", f"docs: close {run.ticket.stem}")


def _birth_run(
    root: Path,
    ticket_path: Path,
    *,
    run_id: str,
    branch: str | None,
    status: str | list[str] | None,
    process: str | None,
    worktree_base: Path,
    archived: bool,
    workflow: str,
) -> SimpleNamespace:
    """A real local Run branch carrying `ticket_path`'s ticket + its project DB row.

    Never pushes the branch. `process`: None (no row), "alive" (this process),
    "dead" (registered then closed). `status` may be one value or a transition list.
    """
    root = Path(root)
    stem = ticket_path.stem
    branch = branch or f"lime/{workflow}/{stem}"
    wt = worktree.create(root, run_id, branch, base=worktree_base, ticket=ticket_path)
    base_sha = git(wt, "rev-parse", "HEAD")
    tr = tracer.Tracer(root / ".lime" / "runs")
    try:
        tr.start_run(
            run_id,
            workflow=workflow,
            request="toy",
            ticket=str(ticket_path),
            branch=branch,
            worktree=str(wt),
            base_sha=base_sha,
        )
        if process == "alive":
            tr.start_process(run_id, kind="run", name="run", pid=os.getpid(), command="toy")
        elif process == "dead":
            dead_pid = 2**30 - 1
            tr.start_process(run_id, kind="run", name="run", pid=dead_pid, command="toy")
            tr.end_process(run_id, dead_pid)
        for value in ([status] if isinstance(status, str) else (status or [])):
            if value == "running":
                continue  # start_run already inserts 'running'
            tr.set_run_status(run_id, value)
        if archived:
            tr.con.execute("UPDATE runs SET archived=1 WHERE run_id=?", (run_id,))
            tr.con.commit()
    finally:
        tr.con.close()
    return SimpleNamespace(run_id=run_id, branch=branch, ticket=Path(ticket_path), worktree=wt, base_sha=base_sha)


def unpushed_run(
    root: Path,
    number: int,
    where: str,
    *,
    ticket_name: str | None = None,
    run_id: str | None = None,
    status: str | list[str] | None = None,
    branch: str | None = None,
    process: str | None = None,
    blocked_by: str | None = None,
    workflow: str = "feature",
    archived: bool = False,
    title: str = "Toy",
) -> SimpleNamespace:
    """Write a root ticket, birth its local Run branch/worktree, and register the row.

    All artifacts stay under the pytest tmp tree; `trunk` is the default; no setup/
    teardown command and no agent runs. The branch carries its own ticket, so the
    root file may be edited/removed afterward to prove branch-local reads.
    """
    root = Path(root)
    stem = ticket_name or f"{number}-toy.md"
    tickets = root / ".lime" / "tickets"
    tickets.mkdir(parents=True, exist_ok=True)
    ticket_path = tickets / stem
    ticket_path.write_text(
        ticket_text(number, where, title=title, blocked_by=blocked_by, workflow=workflow)
    )
    return _birth_run(
        root,
        ticket_path,
        run_id=run_id or f"run{number:04d}",
        branch=branch,
        status=status,
        process=process,
        worktree_base=root.parent / "worktrees",
        archived=archived,
        workflow=workflow,
    )
