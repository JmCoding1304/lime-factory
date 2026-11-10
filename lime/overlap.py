"""Live overlap detection at launch: a candidate's `## Where` vs unmerged Runs.

Two read-only sources feed one conflict map:

- published `refs/remotes/origin/lime/**` Run branches not merged into the fetched
  origin default branch, and
- this explicit project's own `.lime/runs/lime.db` Run rows that are not yet
  `deployed`/`discarded`, whose named branch is a local `refs/heads/...` ref.

The trace row names the Run id, so local conflicts are keyed
`Run <run_id> (<branch>)`; remote-only conflicts keep the bare
`lime/<workflow>/<stem>` branch key they always had. Discovery never reads
`LIME_RUNS_DIR`, another clone, or another project's DB, and never writes: an
absent DB is empty and is never created, and no port/lifecycle/trace writer is
touched. There is no global atomic simultaneous-launch lock — checks recompute
from current state with no cache — so a single launch can still race another
process; the guard is best-effort, not admission control.

Every remote operation runs through list-form `git -C <root>` subprocesses (no
shell interpolation) and every remote Git failure raises `OverlapError` — a
remote that cannot be fetched/enumerated/read is never silently treated as
"nothing overlaps". A single local row's unreadable branch/ticket is skipped
(contributes no hold), but a DB that cannot be read at all raises `OverlapError`.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from . import tracker, tracer, worktree
from .tracker import Ticket


class OverlapError(RuntimeError):
    """Overlap discovery against an existing origin or trace DB could not complete.

    Distinct from "no origin / no rows" (which returns `{}` without side effects):
    the caller reports this and refuses to launch rather than assuming permission.
    """


def _run(
    root: Path, *args: str, input: str | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, input=input
    )


def _git(root: Path, *args: str) -> str:
    result = _run(root, *args)
    if result.returncode != 0:
        raise OverlapError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _has_origin(root: Path) -> bool:
    """`git remote get-url origin` rc == 0 — never raises."""
    return _run(root, "remote", "get-url", "origin").returncode == 0


def _leading_number(stem: str) -> int | None:
    digits = ""
    for char in stem:
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else None


def _tip_ticket_path(root: Path, ref: str, number: int, stem: str) -> str | None:
    """The branch tip's own `.<num>-*.md` ticket path, or None.

    `git show ref:path` does not expand `<NN>-*.md`, so list the ticket tree and
    match basenames: prefer the exact branch stem, else the first full-number
    match (boundary-safe: `35-` never matches `350-`).
    """
    listing = _git(root, "ls-tree", "-r", "--name-only", ref, "--", ".lime/tickets/")
    paths = [p for p in listing.splitlines() if p]
    exact = f".lime/tickets/{stem}.md"
    if exact in paths:
        return exact
    prefix = f"{number}-"
    for path in paths:
        name = Path(path).name
        if name.startswith(prefix) and name.endswith(".md"):
            return path
    return None


def _last_carried(root: Path, ref: str, stem: str, base: str | None) -> str | None:
    """Newest commit on `ref`, not reachable from `base`, holding `.lime/tickets/<stem>.md`.

    `commit_docs` deletes the ticket before `push_pr`, so a finished, unmerged Run's
    tip carries none (ticket 273). Its last carried version still names the Run's
    Where paths, so read backwards through the branch's own history to find it.
    `--topo-order` guarantees a child is listed before its parent even when test
    commits share a timestamp; `--` separates the refs from the path-less batch-check
    stdin. Matches the exact path only, so a same-number file with another slug never
    counts and a branch that never carried this file returns None.
    """
    path = f".lime/tickets/{stem}.md"
    exclude = [f"^{base}"] if base is not None else []
    commits = _git(root, "rev-list", "--topo-order", ref, *exclude, "--").split()
    if not commits:
        return None
    result = _run(
        root,
        "cat-file",
        "--batch-check",
        input="".join(f"{c}:{path}\n" for c in commits),
    )
    if result.returncode != 0:
        raise OverlapError(f"git cat-file --batch-check failed: {result.stderr.strip()}")
    for commit, line in zip(commits, result.stdout.splitlines()):
        fields = line.split()
        if len(fields) == 3 and fields[1] == "blob":
            return commit
    return None


def _carried_ticket(
    root: Path, ref: str, number: int, stem: str, base: str | None
) -> tuple[str, str] | None:
    """The ticket the branch `ref` carries as `(rev, path)`, or None.

    The tip keeps its exact-then-prefix precedence (`_tip_ticket_path`); the history
    read (`_last_carried`) runs only when the tip has no ticket, so a finished Run
    whose tip already deleted it still holds on its last carried version.
    """
    tip = _tip_ticket_path(root, ref, number, stem)
    if tip is not None:
        return ref, tip
    commit = _last_carried(root, ref, stem, base)
    return None if commit is None else (commit, f".lime/tickets/{stem}.md")


def _show(root: Path, ref: str, path: str) -> str:
    result = _run(root, "show", f"{ref}:{path}")
    if result.returncode != 0:
        raise OverlapError(
            f"git show {ref}:{path} failed: {result.stderr.strip()}"
        )
    return result.stdout


def _normalize_ticket_ref(value: str) -> str:
    value = value.strip()
    while value.startswith("./"):
        value = value[2:]
    return value


def _exempt(candidate: Ticket, sibling_path: str, stem: str) -> bool:
    """True when `Blocked by:` explicitly names this sibling's resolved ticket.

    Exact lexical equality against the path, basename or stem — an arbitrary
    substring or another workflow's name never exempts a branch.
    """
    named = {
        _normalize_ticket_ref(sibling_path),
        Path(sibling_path).name,
        stem,
    }
    return any(_normalize_ticket_ref(b) in named for b in candidate.blocked_by)


def _conflict(left: str, right: str) -> bool:
    """Equality, or one normalized path starting with the other plus `/`.

    `src/a` never matches `src/ab`; `foo.py` never matches `foo.py.bak`. Works in
    both directions even when neither file exists locally.
    """
    a, b = tracker.normalize_path(left), tracker.normalize_path(right)
    if a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def _intersections(candidate_paths: list[str], sibling_paths: list[str]) -> list[str]:
    hits: list[str] = []
    for candidate in candidate_paths:
        if any(_conflict(candidate, sibling) for sibling in sibling_paths):
            if candidate not in hits:
                hits.append(candidate)
    return hits


def conflicts_with(candidate: Ticket, sibling: Ticket, sibling_path: str, stem: str) -> list[str]:
    """Candidate Where paths intersecting `sibling`'s, unless `Blocked by:` exempts it.

    One shared ticket-to-ticket conflict rule (the same `_exempt`/`_intersections`
    primitives local, remote and batch-active checks use), so a held launch and the
    in-memory active-child guard can never drift.
    """
    if _exempt(candidate, sibling_path, stem):
        return []
    return _intersections(tracker.where_paths(candidate), tracker.where_paths(sibling))


# --- remote source: published origin/lime/** branches ---


def _remote_siblings(root: Path, base: str) -> dict[str, tuple[Path, str, Ticket]]:
    """Unmerged origin lime branches -> (sibling_path, stem, parsed ticket)."""
    listing = _git(
        root,
        "for-each-ref",
        f"--no-merged={base}",
        "--format=%(refname)",
        "refs/remotes/origin/lime/",
    )
    prefix = "refs/remotes/origin/"
    siblings: dict[str, tuple[Path, str, Ticket]] = {}
    for refname in (line for line in listing.splitlines() if line):
        key = refname.removeprefix(prefix)
        parts = key.split("/")
        if len(parts) != 3 or parts[0] != "lime":
            continue
        stem = parts[2]
        number = _leading_number(stem)
        if number is None:
            continue
        found = _carried_ticket(root, refname, number, stem, base)
        if found is None:
            continue
        rev, sibling_path = found
        sibling = tracker.parse(_show(root, rev, sibling_path), sibling_path)
        if not tracker.where_paths(sibling):
            continue
        siblings[key] = (Path(sibling_path), stem, sibling)
    return siblings


# --- local source: this project's own trace DB rows ---


def _local_ticket_stem(root: Path, ticket: str | None) -> str | None:
    """Row ticket identity -> repo-relative ticket stem, or None when unusable.

    An absolute row path is normal; a project-contained `.lime/tickets/<stem>.md`
    normalizes to its stem. A path outside the project (or not a ticket file) yields
    None, so a row can never make discovery read an arbitrary external file.
    """
    if not ticket:
        return None
    raw = Path(ticket)
    candidate = raw if raw.is_absolute() else (root / raw)
    try:
        rel = candidate.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    if len(rel.parts) != 3 or rel.parts[0] != ".lime" or rel.parts[1] != "tickets":
        return None
    if rel.suffix != ".md":
        return None
    return rel.stem


def _read_local_rows(root: Path) -> list[tuple[sqlite3.Row, sqlite3.Row | None]]:
    """Eligible `kind='run'` rows plus each Run's latest process row.

    Reads only this explicit project's `.lime/runs/lime.db`, read-only; an absent
    DB is empty and is never created. A real read failure raises `OverlapError`
    (never hidden under the per-row skip rule). `deployed`/`discarded` release the
    path; every other status — including a stopped red one — still holds.
    """
    db_path = root / ".lime" / "runs" / "lime.db"
    if not db_path.exists():
        return []
    con = None
    try:
        con = tracer.connect(db_path, readonly=True)
        rows = con.execute(
            "SELECT run_id, status, ticket, branch FROM runs"
            " WHERE kind='run' AND status NOT IN ('deployed', 'discarded')"
        ).fetchall()
        procs = con.execute(
            "SELECT run_id, pid, ended_at FROM processes WHERE kind='run' ORDER BY id"
        ).fetchall()
    except sqlite3.Error as error:
        raise OverlapError(f"could not read trace db {db_path}: {error}") from error
    finally:
        if con is not None:
            con.close()
    latest: dict[str, sqlite3.Row] = {p["run_id"]: p for p in procs}
    return [(row, latest.get(row["run_id"])) for row in rows]


def _run_is_live(status: str, proc: sqlite3.Row | None) -> bool:
    """A live Run holds even if its birth HEAD is already merged.

    Live = the latest process is open and its pid is alive, or a `running` row that
    has not registered a process yet (the birth window, mirroring `_held_ports`).
    A dead/missing pid or a red status is *not* release — only merge/removal is.
    """
    if proc is None:
        return status == "running"
    return proc["ended_at"] is None and tracer.pid_alive(proc["pid"])


def _local_siblings(
    root: Path, base: str | None
) -> dict[str, tuple[str, Path, str, Ticket]]:
    """Local Run branch -> (Run-labelled key, sibling_path, stem, parsed ticket).

    Each eligible row resolves strictly to a local `refs/heads/<branch>` ref and
    reads its *own* carried ticket — at its tip, else the last version the branch
    carried (`commit_docs` deletes it before push) — matched against the
    branch-local stem derived from the row's ticket identity. A row whose
    branch/ticket is null, unreadable, missing, or not ticket-shaped is skipped
    alone. Stopped rows release only when their actual branch tip is an ancestor of
    the fetched origin default.
    """
    siblings: dict[str, tuple[str, Path, str, Ticket]] = {}
    for row, proc in _read_local_rows(root):
        branch = row["branch"]
        stem = _local_ticket_stem(root, row["ticket"])
        if not branch or stem is None:
            continue
        number = _leading_number(stem)
        if number is None:
            continue
        ref = f"refs/heads/{branch}"
        if _run(root, "rev-parse", "--verify", "--quiet", ref).returncode != 0:
            continue
        if base is not None and not _run_is_live(row["status"], proc):
            # The current branch tip, not `base_sha` (a birth pin). 0 merged (release),
            # 1 unmerged (hold), anything else is an unreadable local-row probe (skip).
            rc = _run(root, "merge-base", "--is-ancestor", ref, base).returncode
            if rc != 1:
                continue
        try:
            found = _carried_ticket(root, ref, number, stem, base)
            if found is None or Path(found[1]).stem != stem:
                continue
            rev, sibling_path = found
            sibling = tracker.parse(_show(root, rev, sibling_path), sibling_path)
        except OverlapError:
            continue
        if not tracker.where_paths(sibling):
            continue
        siblings[branch] = (f"Run {row['run_id']} ({branch})", Path(sibling_path), stem, sibling)
    return siblings


def live_overlap(root: str | Path, ticket: Ticket) -> dict[str, list[str]]:
    """Unmerged Runs whose Where paths intersect `ticket`'s, from two sources.

    Returns `{}` (no side effects, no fetch) when the candidate parses no paths.
    With an origin, refreshes `--prune`, enumerates `refs/remotes/origin/lime/**`
    not merged into `origin/<default>`, and reads each branch's own ticket — at its
    tip, else the last version the branch carried (`commit_docs` deletes it before
    push), so a finished, unmerged Run keeps holding until merge or `lime runs rm`;
    without an origin, remote inference is skipped but this project's local rows
    are still inspected. Local rows come from `.lime/runs/lime.db` (kind `run`,
    status outside `deployed`/`discarded`), resolve their branch locally and read
    the branch's own carried ticket. A branch hitting both sources collapses into
    one `Run <run_id> (<branch>)` entry (candidate-order union of paths); a
    remote-only branch keeps its bare `lime/<workflow>/<stem>` key. The map is
    sorted; no checkout/merge/rebase/push and no long-lived cache.
    """
    candidate_paths = tracker.where_paths(ticket)
    if not candidate_paths:
        return {}
    root = Path(root)
    remote: dict[str, tuple[Path, str, Ticket]] = {}
    base: str | None = None
    if _has_origin(root):
        _git(root, "fetch", "--prune", "origin")
        try:
            base_branch = worktree.default_branch(root)
        except worktree.WorktreeError as error:
            raise OverlapError(str(error)) from error
        base = f"origin/{base_branch}"
        remote = _remote_siblings(root, base)
    local = _local_siblings(root, base)

    sibling_paths_by_branch: dict[str, list[str]] = {}
    meta: dict[str, tuple[str, Path, str]] = {}
    for branch, (sibling_path, stem, sibling) in remote.items():
        sibling_paths_by_branch.setdefault(branch, []).extend(tracker.where_paths(sibling))
        meta[branch] = (branch, sibling_path, stem)
    for branch, (label, sibling_path, stem, sibling) in local.items():
        # A local row present for the same branch wins the label (Run id) while the
        # two sources' path hits union, so a remote-only hit is never lost.
        sibling_paths_by_branch.setdefault(branch, []).extend(tracker.where_paths(sibling))
        meta[branch] = (label, sibling_path, stem)

    result: dict[str, list[str]] = {}
    for branch, (label, sibling_path, stem) in meta.items():
        if _exempt(ticket, str(sibling_path), stem):
            continue
        hits = _intersections(candidate_paths, sibling_paths_by_branch[branch])
        if hits:
            result[label] = hits
    return dict(sorted(result.items()))
