"""Low-level git operations for code phases, plus delivery (push + `gh pr create`, spec §10).
All low-level logic lives in lime modules."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def current_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD")


def is_repo() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"], capture_output=True, text=True
    )
    return result.returncode == 0


def repo_root() -> Path:
    """Absolute root of the codebase — where agents are spawned to work.

    The git toplevel when there is one, else the process cwd (a chain runs fine
    in a non-git dir; only a commit phase requires a repo). Always absolute, so
    it is safe to hand to a subprocess regardless of where the chain was
    launched from.
    """
    if is_repo():
        return Path(_git("rev-parse", "--show-toplevel")).resolve()
    return Path.cwd().resolve()


def main_checkout_root(root: Path) -> Path | None:
    """The primary checkout's root, read-only, or None when it cannot be identified.

    Git's `worktree list --porcelain` names the primary worktree first, independent
    of which linked worktree invokes it. Parses NUL-delimited records so checkout
    paths keep their spaces, and uses only the first record — never cwd, the linked
    worktree, or a later record. Returns None for a command/OS failure, an
    absent/malformed first record, or a bare primary repository. Read-only: needs
    neither a remote nor authentication.
    """
    try:
        out = _git_at(root, "worktree", "list", "--porcelain", "-z")
    except (OSError, RuntimeError):
        return None
    # A record is its attribute lines up to the first empty segment (the record
    # separator). Only the first record identifies the primary worktree.
    record: list[str] = []
    for part in out.split("\0"):
        if part == "":
            break
        record.append(part)
    if not record or not record[0].startswith("worktree "):
        return None
    raw = record[0][len("worktree "):]
    if not raw:
        return None
    if any(line == "bare" for line in record):
        return None
    path = Path(raw)
    if not path.is_absolute():
        return None
    return path


def _make_pathspecs(exclude_patterns: list[str]) -> tuple[list[str], list[str]]:
    """Convert root-relative exclude patterns to (positive, negative) Git pathspec lists.

    Positive: ["."] (whole root).
    Negative: [":(exclude)<pattern>" for pattern in exclude_patterns] for `git add`.
    """
    positive = ["."]
    negative = [f":(exclude){p}" for p in exclude_patterns]
    return positive, negative


def _unstaged_staged_matches(exclude_patterns: list[str]) -> None:
    """Unstage already-staged changes that match exclude patterns.

    Uses `git restore --staged --source=HEAD -- <path>` via `git diff --cached`
    to enumerate staged paths matching each pattern, then unstages each one.
    Preserves working-tree contents and unrelated staged changes.
    """
    for pattern in exclude_patterns:
        try:
            staged = _git("diff", "--cached", "-z", "--name-only", "--", pattern)
            for p in staged.split("\0"):
                if p:
                    _git("restore", "--staged", "--source=HEAD", "--", p)
        except RuntimeError:
            pass


def excluded_paths(exclude_patterns: list[str]) -> list[str]:
    """Enumerate actual changed/untracked paths that match exclude patterns.

    Uses the same Git pathspec semantics as the commit exclusion logic.
    Returns sorted individual file paths (including staged/deleted matches),
    using NUL-delimited Git output for correct handling of spaces/tabs.
    """
    if not exclude_patterns:
        return []
    matched: set[str] = set()
    for pattern in exclude_patterns:
        # Staged/indexed changes matching the pattern
        try:
            idx_out = _git("ls-files", "-z", "--cached", "--", pattern)
            matched.update(p for p in idx_out.split("\0") if p)
        except RuntimeError:
            pass
        # Untracked non-ignored files matching the pattern
        try:
            untracked_out = _git("ls-files", "-z", "--others", "--exclude-standard", "--", pattern)
            matched.update(p for p in untracked_out.split("\0") if p)
        except RuntimeError:
            pass
        # Modified/deleted in working tree matching the pattern
        try:
            modified_out = _git("diff", "-z", "--name-only", "--", pattern)
            matched.update(p for p in modified_out.split("\0") if p)
        except RuntimeError:
            pass
    return sorted(matched)


def uncounted_paths(base: str, patterns: list[str]) -> set[str]:
    """Root-relative paths matching the `commit.uncounted` patterns, for accounting exemption.

    One read-only `git ls-files -z --cached --others --exclude-standard
    --with-tree=<base> -- <patterns>` run, with every pattern passed as its own
    argv element (nothing shell-expanded). `--with-tree` adds base paths removed
    from the index without writing the real index, so base deletions are covered;
    committed additions, staged/unstaged changes and non-ignored untracked files
    are covered too. Returns a set of paths (whitespace preserved, duplicates
    collapsed). Empty `patterns` returns an empty set without invoking Git.

    May include unchanged tracked matches — only intersecting with the collected
    eligible stats grants an exemption. Raises RuntimeError on any git failure.
    """
    if not patterns:
        return set()
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
         f"--with-tree={base}", "--", *patterns],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git ls-files --with-tree={base} failed: {result.stderr.strip()}"
        )
    return {p for p in result.stdout.split("\0") if p}


def commit_all(message: str, exclude: list[str] | None = None) -> str:
    """Stage the working tree and commit it. Returns the new short sha.

    When `exclude` is None (no-argument call), defaults to ["context_handoff/"].
    Pass explicit `[]` to stage everything.

    Excluded paths are first unstaged via `git restore --staged --source=HEAD`
    (index-only, preserving working tree contents), then excluded from `git add -A`
    via pathspec exclusions. "Nothing to commit" is determined from the staged diff
    after exclusions, not from whole-tree status.
    """
    if exclude is None:
        exclude = ["context_handoff/"]

    if not is_repo():
        raise RuntimeError(
            "not a git repository — a commit phase needs one. Run `git init` in the"
            " repo root (and make a first commit) before running a chain that commits."
        )

    # Unstage any already-staged changes that match exclude patterns
    if exclude:
        _unstaged_staged_matches(exclude)

    # Stage everything except excluded paths
    if exclude:
        positive, negative = _make_pathspecs(exclude)
        _git("add", "-A", "--", *positive, *negative)
    else:
        _git("add", "-A")

    # Check staged diff (not whole-tree status) for emptiness
    staged_diff = ""
    try:
        staged_diff = _git("diff", "--cached", "--name-only")
    except RuntimeError:
        pass
    if not staged_diff:
        raise RuntimeError("nothing to commit — the preceding phases changed no files")

    _git("commit", "-m", message)
    return _git("rev-parse", "--short", "HEAD")


def changed_files() -> list[str]:
    out = _git("status", "--porcelain")
    return [line[3:] for line in out.splitlines() if line]


# --- diff plumbing (composed into a ChangeSet by changes.py) ---


def ref_exists(ref: str) -> bool:
    """True when `ref` resolves to a commit. Never raises — this is a question."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def rev(ref: str = "HEAD") -> str:
    return _git("rev-parse", ref)


def short_sha(ref: str = "HEAD") -> str:
    return _git("rev-parse", "--short", ref)


def merge_base(ref: str, other: str = "HEAD") -> str:
    """The commit where `ref` and `other` diverged — the honest base of a branch.

    On the base branch itself this returns HEAD, which makes the diff exactly
    "what is not committed yet". Off it, the diff is the whole branch plus the
    working tree. One command covers both cases, so no chain has to branch on it.

    Legacy/standalone callers only: a pinned Run never derives its base from this
    (see `resolve_diff_base`).
    """
    return _git("merge-base", ref, other)


class InvalidRunBase(RuntimeError):
    """A Run's pinned birth base no longer describes a valid, linear ancestry.

    The Run's work is preserved but cannot be measured or resumed against the pin:
    the branch was rebased or merged past the commit the Run was born from. This is
    terminal — the caller stops, reports the pin, and tells the engineer to start a
    fresh Run. It is deliberately NOT an ordinary inspection error.
    """


def _base_message(base_sha: str) -> str:
    return (
        f"pinned Run base {base_sha} is missing, unresolvable, or no longer an ancestor"
        f" of HEAD — branch rebased/merged past its base — start a fresh Run."
    )


def _merge_after_pin_message(base_sha: str) -> str:
    return (
        f"a merge commit was introduced in {base_sha}..HEAD — branch rebased/merged"
        f" past its base — start a fresh Run (pinned base {base_sha})."
    )


def validate_run_base(base_sha: str, *, repo_root: Path | None = None) -> str:
    """Validate that `base_sha` is a usable pinned birth base for the current HEAD.

    Read-only. The pin itself must resolve to a commit and be an ancestor of HEAD
    (`git merge-base --is-ancestor <pin> HEAD` by exit status — never an ordinary
    merge-base result) and no merge commit may have been introduced between the pin
    and HEAD. HEAD == pin and ordinary linear commits past the pin are allowed, as
    are merges that were already in history at/before the pin. Returns the pin.

    Raises `InvalidRunBase` with a loud message on any violation — there is no
    fallback to default/HEAD/HEAD~1 or a recomputed merge base. `repo_root` lets a
    caller validate a preserved worktree before mutating resume state.
    """
    if not base_sha:
        raise InvalidRunBase(_base_message(str(base_sha)))

    def run(*args: str) -> subprocess.CompletedProcess:
        cmd = ["git"]
        if repo_root is not None:
            cmd += ["-C", str(repo_root)]
        return subprocess.run([*cmd, *args], capture_output=True, text=True)

    exists = run("rev-parse", "--verify", "--quiet", f"{base_sha}^{{commit}}")
    if exists.returncode != 0 or not exists.stdout.strip():
        raise InvalidRunBase(_base_message(base_sha))

    ancestor = run("merge-base", "--is-ancestor", base_sha, "HEAD")
    if ancestor.returncode != 0:
        raise InvalidRunBase(_base_message(base_sha))

    merges = run("rev-list", "--merges", f"{base_sha}..HEAD")
    if merges.returncode != 0:
        raise InvalidRunBase(_base_message(base_sha))
    if merges.stdout.strip():
        raise InvalidRunBase(_merge_after_pin_message(base_sha))

    return base_sha


def resolve_diff_base(
    requested_base: str | None,
    base_sha: str | None = None,
    *,
    repo_root: Path | None = None,
) -> str:
    """The exact commit a diff should be measured against.

    A pinned Run (`base_sha` set) validates and returns its pin — the pin itself is
    the comparison commit, never a recomputed merge base. A legacy caller (no pin)
    keeps the ordinary `merge-base(requested_base, HEAD)` behavior. Pinned mode is
    decided by an explicit `base_sha`, never inferred from a ref that looks like a
    SHA.
    """
    if base_sha:
        return validate_run_base(base_sha, repo_root=repo_root)
    if requested_base is None:
        requested_base = "HEAD"
    return merge_base(requested_base, "HEAD")


def is_dirty() -> bool:
    return bool(_git("status", "--porcelain"))


def status_short() -> str:
    """`git status --short` — one of the two facts a budget continuation's fresh Session gets (spec §9)."""
    return _git("status", "--short")


def untracked_files() -> list[str]:
    out = _git("ls-files", "--others", "--exclude-standard")
    return [line for line in out.splitlines() if line]


def has_origin() -> bool:
    """True when the repo has a remote named 'origin'. Never raises."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True
    )
    return result.returncode == 0


def unpushed_commits(branch: str, path: str) -> list[str]:
    """Short shas of commits on local `branch` touching `path` that are not on `origin/<branch>`.

    The one state a Run must refuse — the ticket would get a second hash on the
    branch and `main` could never fast-forward past the local one (ticket 59).
    Returns [] when there is no origin or the branch does not exist locally.
    """
    if not has_origin() or not ref_exists(branch):
        return []
    _git("fetch", "origin", branch)
    out = _git("log", "--format=%h", f"origin/{branch}..{branch}", "--", path)
    return [line for line in out.splitlines() if line]


def uncommitted_state(path: str) -> str | None:
    """None when `path` is committed and clean; 'untracked' or 'modified' otherwise.

    Scoped to the one path — serves `lime deploy` to tell a closed ticket's leftover
    untracked copy from a tracked file.
    """
    out = _git("status", "--porcelain", "--", path)
    if not out:
        return None
    return "untracked" if out.startswith("??") else "modified"


def diff_files(base: str) -> list[str]:
    """Tracked files that differ between `base` and the working tree."""
    out = _git("diff", "--name-only", base)
    return [line for line in out.splitlines() if line]


def diff_stat(base: str) -> str:
    return _git("diff", "--stat", base)


def diff_counts(base: str) -> tuple[int, int]:
    """(insertions, deletions) across the diff. Binary files count as neither."""
    insertions = deletions = 0
    for line in _git("diff", "--numstat", base).splitlines():
        added, removed, *_ = line.split("\t")
        if added.isdigit():
            insertions += int(added)
        if removed.isdigit():
            deletions += int(removed)
    return insertions, deletions


def diff_text(base: str) -> str:
    return _git("diff", base)


def _make_shared_pathspecs(exclude_patterns: list[str]) -> list[str]:
    """Build pathspec list for filter commands: positive ["."] plus negative exclusion patterns."""
    if not exclude_patterns:
        return []
    ps = ["."]
    ps.extend(f":(exclude){p}" for p in exclude_patterns)
    return ps


def diff_stats(
    base: str,
    exclude_patterns: list[str] | None = None,
) -> list[dict]:
    """Per-file diff statistics: each entry is a dict with path, added_lines, and binary flag.

    Read-only with respect to index, HEAD, and worktree. Measures eligible tracked changes
    against the resolved base including committed branch changes and staged/unstaged changes,
    then includes eligible non-ignored untracked files exactly once.

    Uses Git numstat (NUL-delimited, no external diff/textconv) and binary markers.
    --no-renames makes rename accounting conservative and avoids compound rename paths.

    Returns a list of dicts with keys: path, added_lines (int), binary (bool), deleted (bool).
    Raises RuntimeError on git command failures.
    """
    if exclude_patterns is None:
        exclude_patterns = []

    pathspecs = _make_shared_pathspecs(exclude_patterns)
    results: list[dict] = []
    seen: set[str] = set()

    # 1) Tracked changes: git diff --no-renames --numstat -z <base> -- <pathspecs>
    tracked_args = ["diff", "--no-renames", "--numstat", "-z", base, "--"]
    if pathspecs:
        tracked_args.extend(pathspecs)
    else:
        tracked_args.append(".")

    # Also run name-status to detect deletions (D status) — numstat alone cannot
    # distinguish a deleted binary from a modified binary.
    status_args = ["diff", "--no-renames", "--name-status", "-z", base, "--"]
    if pathspecs:
        status_args.extend(pathspecs)
    else:
        status_args.append(".")
    status_out = _git(*status_args)
    deletion_paths: set[str] = set()
    if status_out:
        parts = status_out.split("\0")
        i = 0
        while i < len(parts):
            status = parts[i]
            i += 1
            if not status:
                continue
            # -z format: <status>\0<path>\0 (one NUL segment each)
            if i < len(parts) and parts[i]:
                path = parts[i].strip()
                if status.startswith("D"):
                    deletion_paths.add(path)
            i += 1

    tracked_out = _git(*tracked_args)
    if tracked_out:
        # NUL-delimited: format is <added>\t<deleted>\t<path>\0
        parts = tracked_out.split("\0")
        i = 0
        while i < len(parts):
            line = parts[i]
            if not line:
                i += 1
                continue
            tabs = line.split("\t")
            if len(tabs) >= 3:
                added_str, deleted_str, path = tabs[0], tabs[1], tabs[2]
                path = path.strip()
                if not path or path in seen:
                    i += 1
                    continue
                seen.add(path)
                is_binary = False
                added = 0
                deleted = path in deletion_paths
                if added_str == "-" and deleted_str == "-":
                    is_binary = True
                elif added_str.isdigit():
                    added = int(added_str)
                result_entry = {"path": path, "added_lines": added, "binary": is_binary, "deleted": deleted}
                results.append(result_entry)
            i += 1

    # 2) Untracked non-ignored files: ls-files --others --exclude-standard
    untracked_args = ["ls-files", "-z", "--others", "--exclude-standard", "--"]
    if pathspecs:
        untracked_args.extend(pathspecs)
    else:
        untracked_args.append(".")
    untracked_out = _git(*untracked_args)
    untracked_paths = [p for p in untracked_out.split("\0") if p and p not in seen]

    for path in untracked_paths:
        seen.add(path)
        # Per-file git diff --no-index --numstat -z /dev/null <path>
        proc = subprocess.run(
            ["git", "diff", "--no-index", "--numstat", "-z", "/dev/null", path],
            capture_output=True, text=True,
        )
        # exit 0 = no diff (empty file), exit 1 = differences found, anything else = error
        if proc.returncode not in (0, 1):
            raise RuntimeError(
                f"git diff --no-index --numstat -z /dev/null {path} failed: {proc.stderr.strip()}"
            )
        added = 0
        is_binary = False
        if proc.stdout:
            parts = proc.stdout.split("\0")
            if parts and "\t" in parts[0]:
                tabs = parts[0].split("\t")
                if len(tabs) >= 3:
                    added_str, deleted_str = tabs[0], tabs[1]
                    if added_str == "-" and deleted_str == "-":
                        is_binary = True
                    elif added_str.isdigit():
                        added = int(added_str)
        results.append({"path": path, "added_lines": added, "binary": is_binary, "deleted": False})

    return results


def diff_hunks(base: str, *, context: int) -> list[tuple[str, str]]:
    """[(path, hunks)] for every file that differs from `base`: one `git diff -U<context> <base> -- <path>`
    per tracked path from diff_files(), then one whole-file `--no-index` hunk per untracked file (they are
    absent from `git diff` by construction — the builder's new files, ticket 62). Per path so the reviewer
    prompt can fit them file by file (ticket 83). Files whose diff text is empty (mode-only) are skipped."""
    result = []
    for path in diff_files(base):
        text = _git("diff", f"-U{context}", base, "--", path)
        if text:
            result.append((path, text))
    for path in untracked_files():
        proc = subprocess.run(
            ["git", "diff", "--no-index", "--", "/dev/null", path],
            capture_output=True, text=True,
        )
        if proc.returncode not in (0, 1):
            raise RuntimeError(
                f"git diff --no-index /dev/null {path} failed: {proc.stderr.strip()}"
            )
        text = proc.stdout.strip()
        if text:
            result.append((path, text))
    return result


# --- delivery: push + PR (composed by phases.push_pr, spec §10) ---


def push(branch: str) -> None:
    """Push the branch to origin, setting the upstream tracking ref."""
    _git("push", "-u", "origin", branch)


def open_pr(title: str, body: str, base: str) -> str:
    """`gh pr create` from the current branch; returns the PR URL.

    Requires `gh auth status` green (spec §5 — `lime check` probes it). A branch
    that already has an OPEN PR (a resumed Run that opened it before an interrupt)
    gets that PR back instead of an error.
    """
    result = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body, "--base", base],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        existing = _open_pr_url()
        if existing:
            return existing
        raise RuntimeError(f"gh pr create failed: {result.stderr.strip()}")
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _open_pr_url() -> str | None:
    """The current branch's OPEN PR URL from `gh pr view`, or None.

    Returns None on a non-zero exit or unparsable JSON — judged by exit status,
    never by stderr text.
    """
    result = subprocess.run(
        ["gh", "pr", "view", "--json", "state,url"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    return data["url"] if data.get("state") == "OPEN" else None


def pr_number(url: str | None) -> int | None:
    """The PR number in a PR URL (`.../pull/<n>`), or None when `url` names no PR."""
    head, _, tail = (url or "").rstrip("/").rpartition("/")
    return int(tail) if head.endswith("/pull") and tail.isdigit() else None


# --- lime deploy: PR state + CI, merge (spec §10 (a)-(b)) ---

CI_RED = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
CI_PENDING = {"", "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "STALE", "REQUESTED", "WAITING"}


def ci_status(checks: list[dict]) -> str:
    """green | pending | red, from `gh pr view`'s statusCheckRollup.

    Empty rollup (no CI configured on the repo) counts as green — nothing there to block on.
    """
    if not checks:
        return "green"
    conclusions = [(c.get("conclusion") or c.get("state") or "").upper() for c in checks]
    if any(c in CI_RED for c in conclusions):
        return "red"
    if any(c in CI_PENDING for c in conclusions):
        return "pending"
    return "green"


def pr_view(branch: str, *, cwd: Path | None = None, timeout: float | None = None) -> dict:
    """`gh pr view <branch>` — state + CI rollup + url + mergedAt + headRefOid.

    The selector may be a branch, a PR number or a PR URL (`gh pr view <url>` addresses
    the exact PR whatever the cwd). `cwd` runs the call in an explicit repo (never the
    process cwd), `timeout` bounds it. Missing gh, a timeout and unparsable JSON all
    surface as RuntimeError so every caller's existing `except RuntimeError` handles them.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", branch, "--json",
             "state,statusCheckRollup,url,mergedAt,headRefOid"],
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise RuntimeError("gh not found") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"gh pr view timed out after {timeout}s") from error
    if result.returncode != 0:
        raise RuntimeError(f"gh pr view failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError("gh pr view: unparsable output") from error


def merge_pr(branch: str, head_sha: str | None = None) -> None:
    """`gh pr merge --merge`, then delete just the remote branch (spec §10 (b) — local
    branch removal is (c)'s job, once the worktree checked out on it is torn down).

    When `head_sha` is set the merge is pinned with `--match-head-commit`, so a PR whose
    head moved after the check refuses instead of merging an untested commit. Without it
    the argv is unchanged.

    `gh`'s own `--delete-branch` flag deletes local too, which fails outright while a
    Run's worktree still has this branch checked out elsewhere — so the two are kept apart.
    """
    argv = ["gh", "pr", "merge", branch, "--merge"]
    if head_sha is not None:
        argv += ["--match-head-commit", head_sha]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh pr merge failed: {result.stderr.strip()}")
    result = subprocess.run(
        ["git", "push", "origin", "--delete", branch], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"git push --delete {branch} (remote branch) failed: {result.stderr.strip()}")


def fast_forward(branch: str) -> str:
    """Fetch origin and fast-forward the local `branch` to `origin/<branch>`.

    `gh pr merge` merges on the server, so without this the local default branch —
    and the working tree the engineer's dev server serves — stays pre-merge (ticket 45).

    Two shapes, because git only merges into the branch that is checked out:
      - `branch` is checked out -> `git merge --ff-only origin/<branch>`, which moves
        the ref *and* the working tree, which is the whole point.
      - `branch` is not checked out -> `git fetch origin <branch>:<branch>`, which git
        itself refuses unless it is a fast-forward and leaves HEAD alone.

    Returns the short sha the local branch ends on. Raises RuntimeError when git
    refuses (diverged local branch, dirty tree in the way, no origin) — nothing is
    ever reset, stashed, forced, or checked out.
    """
    if current_branch() == branch:
        _git("fetch", "origin", branch)
        _git("merge", "--ff-only", f"origin/{branch}")
    else:
        _git("fetch", "origin", f"{branch}:{branch}")
    return short_sha(branch)


# --- lime deploy: reconcile + --check (ticket 61) ---


def _pr_list(state: str, limit: int) -> list[dict]:
    """`gh pr list --state <state> --limit <limit> --json number,headRefName`.

    Raises RuntimeError when gh is missing, unauthenticated or offline — callers
    downgrade that to "check skipped", never to a failed deploy.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", state, "--limit", str(limit),
             "--json", "number,headRefName"],
            capture_output=True, text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError("gh not found") from error
    if result.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def merged_prs() -> list[dict]:
    """`gh pr list --state merged` — number + head branch of every merged PR.

    The head branch (`lime/<workflow>/<ticket stem>`) is the only join key between a
    merged PR and a ticket file: Runs before ticket 216 carry no `pr_number` (never
    backfilled), so the head branch stays the join key here.
    Raises RuntimeError when gh is missing, unauthenticated or offline — callers
    downgrade that to "check skipped", never to a failed deploy.
    """
    return _pr_list("merged", 200)


def pr_head_refs() -> list[str]:
    """Head ref names of every open, merged, and closed-unmerged PR.

    A closed-unmerged PR's head ref is the only place its ticket number survives
    once the branch is deleted (ticket 73).
    """
    return [pr["headRefName"] for pr in _pr_list("all", 1000)]


def sync_state(branch: str) -> tuple[int, int]:
    """(ahead, behind) of local `branch` vs `origin/<branch>` after a fetch — read-only.

    Fetch updates the remote-tracking ref only; the local branch, HEAD and the working
    tree are never touched (`lime deploy --check`'s contract). Raises RuntimeError
    when there is no origin or the fetch fails.
    """
    _git("fetch", "origin", branch)
    out = _git("rev-list", "--left-right", "--count", f"{branch}...origin/{branch}")
    ahead, behind = out.split()
    return int(ahead), int(behind)


# --- lime deploy: temporary merged-tree gate + base movement (ticket 141) ---


def _git_at(repo_root: Path, *args: str, timeout: float | None = None) -> str:
    """Run git in an explicit repo root (never the process cwd), raising on failure.

    `timeout` bounds the call; a timeout surfaces as RuntimeError like any other failure.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args], capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"git {args[0]} timed out after {timeout}s") from error
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def fetch_origin(repo_root: Path, *, timeout: float | None = None) -> None:
    """`git fetch origin` — refresh the remote-tracking refs the gate reads.

    Nothing but the remote-tracking refs moves. Raises RuntimeError when there is no
    origin or the fetch fails, so the caller refuses rather than reading stale refs.
    """
    _git_at(repo_root, "fetch", "origin", timeout=timeout)


def resolve_remote_tip(repo_root: Path, ref: str, *, label: str) -> str:
    """The commit SHA `ref` names after a fetch, or a useful RuntimeError.

    A missing remote/default/PR ref must never fall through to merging, so this
    raises rather than returning a fallback (ticket 141).
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet",
         f"{ref}^{{commit}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"cannot resolve {label} {ref!r} — does the branch exist on origin?")
    return result.stdout.strip()


class MergeConflict(RuntimeError):
    """The throwaway merged-tree worktree could not combine PR head onto the base tip.

    Carries the unmerged paths so the refusal can name them instead of guessing.
    """

    def __init__(self, paths: list[str]):
        self.paths = paths
        super().__init__("merge conflict in: " + ", ".join(paths))


def _conflict_paths(tree: Path) -> list[str]:
    """Unmerged (conflicted) paths in `tree`, NUL-delimited so spaces are safe."""
    result = subprocess.run(
        ["git", "-C", str(tree), "diff", "--name-only", "--diff-filter=U", "-z"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [p for p in result.stdout.split("\0") if p]


def _resolve_conflicts(
    tree: Path, paths: list[str], resolve: Callable[[str, str], str | None]
) -> bool:
    """Resolve every conflicted path in the throwaway `tree`; True when nothing is left unmerged.

    Computes every resolution first, then writes — a single refusal leaves the tree as it
    was. A path is refused when it is a symlink or not a regular file, when it is not
    strict UTF-8, when `resolve(path, text)` returns None, or when the result equals the
    input text (a marker-less modify/delete or binary conflict must never read as
    "resolved"). Only after every path passes are the resolutions written, `git add`ed and
    the unmerged set re-checked.
    """
    planned: list[tuple[Path, bytes]] = []
    for path in paths:
        full = tree / path
        if full.is_symlink() or not full.is_file():
            return False
        try:
            text = full.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return False
        result = resolve(path, text)
        if result is None or result == text:
            return False
        planned.append((full, result.encode("utf-8")))
    for full, data in planned:
        full.write_bytes(data)
    _git_at(tree, "add", "--", *paths)
    return not _conflict_paths(tree)


@contextlib.contextmanager
def merged_worktree(
    repo_root: Path,
    base_sha: str,
    pr_sha: str,
    *,
    scratch_dir: Path | None = None,
    resolve: Callable[[str, str], str | None] | None = None,
):
    """A detached throwaway worktree at `base_sha` with `pr_sha` merged in, uncommitted.

    Yields the worktree path. The merge is `-c merge.conflictStyle=diff3 --no-commit --no-ff`,
    so a fast-forward cannot slip a commit in and nothing lands in any real checkout. Only
    the throwaway path is ever written: the Run checkout, the default checkout and every ref
    are left untouched — no merge/rebase into the Run, no checkout switch, no merge commit.

    On a conflicted merge, `resolve(path, text)` may stage a resolution: with `resolve` set
    and `_resolve_conflicts` succeeding, the tree yields with the resolution staged;
    otherwise `MergeConflict` is raised naming every conflicted path.

    Creation, merge, use, conflicts, exceptions and interruption all attempt cleanup:
    the registered worktree is removed with `worktree remove --force`, its temporary
    parent with Python's `shutil.rmtree` (never Run teardown, broad pruning, reset or a
    shell recursive delete). A cleanup failure is raised so the caller refuses before
    merging. A partial `worktree add` failure still removes its own temporary parent.
    """
    parent = Path(
        tempfile.mkdtemp(prefix="lime-deploy-", dir=str(scratch_dir) if scratch_dir else None)
    )
    tree = parent / "tree"
    registered = False
    try:
        try:
            _git_at(repo_root, "worktree", "add", "--detach", str(tree), base_sha)
            registered = True
        except RuntimeError as error:
            raise RuntimeError(f"could not create the temporary merged worktree: {error}") from error
        merge = subprocess.run(
            ["git", "-C", str(tree), "-c", "merge.conflictStyle=diff3",
             "merge", "--no-commit", "--no-ff", pr_sha],
            capture_output=True, text=True,
        )
        if merge.returncode != 0:
            conflicts = _conflict_paths(tree)
            if conflicts:
                if resolve is None or not _resolve_conflicts(tree, conflicts, resolve):
                    raise MergeConflict(conflicts)
            else:
                raise RuntimeError(
                    f"could not merge the PR head {pr_sha} onto {base_sha}: {merge.stderr.strip()}"
                )
        yield tree
    finally:
        errors: list[str] = []
        if registered:
            try:
                _git_at(repo_root, "worktree", "remove", "--force", str(tree))
            except RuntimeError as error:
                errors.append(str(error))
        try:
            shutil.rmtree(parent)
        except OSError as error:
            errors.append(f"could not remove {parent}: {error}")
        if errors:
            message = "deploy gate cleanup failed: " + "; ".join(errors)
            original = sys.exc_info()[1]
            if original is not None:
                raise RuntimeError(f"{message} (after: {original})") from original
            raise RuntimeError(message)


def staged_tree(tree: Path) -> str:
    """`git write-tree` in the throwaway `tree` — the exact index setup/test will run on.

    Writes only an unreferenced tree object; no ref, HEAD or checkout moves.
    """
    return _git_at(tree, "write-tree")


def commit_merge(
    repo_root: Path,
    tree_oid: str,
    first_parent: str,
    second_parent: str,
    message: str,
) -> str:
    """`git commit-tree` with the PR head first, the default tip second — an unreferenced object.

    Returns the new commit SHA. Nothing but the object is written: no ref, HEAD or checkout
    moves, so a re-run rebuilds the commit from scratch.
    """
    return _git_at(
        repo_root, "commit-tree", tree_oid,
        "-p", first_parent, "-p", second_parent, "-m", message,
    )


def push_lease(repo_root: Path, sha: str, branch: str, expected: str) -> None:
    """Force-push `sha` onto `refs/heads/<branch>` on origin, leased on `expected`.

    Uses `--force-with-lease=refs/heads/<branch>:<expected>`, so the push is refused when
    the remote branch is not exactly `expected` — nobody else's commit is overwritten. No
    local branch is written. Judged by exit status; a refusal raises RuntimeError.
    """
    _git_at(
        repo_root, "push",
        f"--force-with-lease=refs/heads/{branch}:{expected}",
        "origin", f"{sha}:refs/heads/{branch}",
    )


@dataclass(frozen=True)
class BaseMovement:
    """Read-only relationship between a Run's pinned base and the fetched default tip.

    status: "equal" (pin is the tip), "behind" (pin is an ancestor of the tip, with
    `merges` first-parent merge commits on default since the pin), "diverged" (pin
    resolves but is not an ancestor) or "invalid" (empty/unresolvable pin).
    """

    status: str
    merges: int = 0
    detail: str = ""


def merge_tree_conflicts(repo_root: Path, base: str, head: str) -> list[str]:
    """Unmerged paths from merging `head` onto `base` via `git merge-tree`, read-only.

    `merge-tree --write-tree --name-only --no-messages` exits 0 with a clean tree, 1 with
    conflicts, anything else with an error. Only unreferenced objects are written — no ref,
    no checkout and no working tree is touched — and the outcome is judged by exit status,
    never by scanning stderr. A clean merge returns `[]`; a conflict returns the non-empty
    path lines after the first (tree OID) line, stopping at the first blank line.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "merge-tree", "--write-tree", "--name-only",
         "--no-messages", base, head],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return []
    if result.returncode != 1:
        raise RuntimeError(f"git merge-tree failed: {result.stderr.strip()}")
    lines = result.stdout.splitlines()
    if not lines or not lines[0].strip():
        # exit 1 without the tree OID line is a bad rev / ref, not a conflict
        raise RuntimeError(f"git merge-tree failed: {result.stderr.strip()}")
    paths: list[str] = []
    for line in lines[1:]:
        if not line.strip():
            break
        paths.append(line)
    return paths


def base_movement(repo_root: Path, base_sha: str | None, default_sha: str) -> BaseMovement:
    """The relationship of a pinned Run base to the fetched default tip — read-only.

    `merge-base --is-ancestor` is judged by exit status (0 ancestor, 1 not). For a
    behind pin, `merges` counts **first-parent merge commits on default** since the
    pin — never total branch commits — so a behind linear history legitimately reports
    zero. Diverged/unrelated histories are distinct from behind. An unexpected Git
    exit or rev-list failure raises rather than masquerading as unmoved.
    """
    if not base_sha:
        return BaseMovement("invalid", 0, "no base recorded")
    resolved = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet",
         f"{base_sha}^{{commit}}"],
        capture_output=True, text=True,
    )
    tip = resolved.stdout.strip()
    if resolved.returncode != 0 or not tip:
        return BaseMovement("invalid", 0, f"base {base_sha} does not resolve")
    if tip == default_sha:
        return BaseMovement("equal")
    ancestor = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", base_sha, default_sha],
        capture_output=True, text=True,
    )
    if ancestor.returncode == 0:
        count = subprocess.run(
            ["git", "-C", str(repo_root), "rev-list", "--first-parent", "--merges",
             "--count", f"{base_sha}..{default_sha}"],
            capture_output=True, text=True,
        )
        if count.returncode != 0:
            raise RuntimeError(f"git rev-list failed: {count.stderr.strip()}")
        return BaseMovement("behind", int(count.stdout.strip() or "0"))
    if ancestor.returncode == 1:
        return BaseMovement("diverged")
    raise RuntimeError(f"git merge-base --is-ancestor failed: {ancestor.stderr.strip()}")
