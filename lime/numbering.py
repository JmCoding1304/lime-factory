""".lime/tickets/ cannot number tickets (delete-on-close empties it → 41/57/60/63
reissued); git is the ledger.  `next_number` = max over every source + 1;
`reserve` uses git's create-if-absent push so two Runs racing on one number
cannot both win.

ADRs use a second, independent ledger: `refs/lime/adr/<NNNN>` (padded), next
number = 1 + max over local `docs/adr/*.md`, the origin default branch tree and
the reservation refs.  A ticket ref of the same number never collides with an
ADR ref — the namespaces are separate.  Ticket reservations are owned by the
ticket stem (the same stem may re-reserve); ADR reservations are owned by the
Run ID (a Run re-launched for the same ticket returns its own number).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from . import git_helper, worktree

RESERVATION_NS = "refs/lime/tickets"
ADR_RESERVATION_NS = "refs/lime/adr"

_STEM = re.compile(r"^(\d+)")                                       # tracker.load's rule: leading digits of NN-slug
_BRANCH = re.compile(r"^(?:refs/heads/)?lime/[^/]+/(\d+)")         # lime/<workflow>/NN-slug (worktree.branch_name)
_SUBJECT = re.compile(r"^ticket: (\d+)")                             # worktree.create's ticket commit
_RESERVATION = re.compile(rf"^{RESERVATION_NS}/(\d+)$")
_ADR_RESERVATION = re.compile(rf"^{ADR_RESERVATION_NS}/(\d+)$")
# A reservation commit's subject, ticket or ADR (ticket: the holder is the stem;
# adr: the holder is the Run ID).  Legacy ticket subjects stay recognized.
_HOLDER = re.compile(r"^lime: (?:ticket|adr) (\S+) reserved")


class NumberingError(RuntimeError):
    """A ticket-number operation could not complete — the number is taken, the remote
    refused, or git plumbing failed.  Callers print this to stderr and return 1."""


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise NumberingError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _has_origin(root: Path) -> bool:
    """`git remote get-url origin` rc == 0 (same as worktree._has_origin)."""
    return subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
    ).returncode == 0


def _ref_for(namespace: str, number: int) -> str:
    """The ledger ref name for `number` in `namespace`.

    ADR refs are zero-padded to a minimum of four digits (never truncated past
    9999); ticket refs stay unpadded — their historical name is part of the ledger.
    """
    if namespace == ADR_RESERVATION_NS:
        return f"{namespace}/{number:04d}"
    return f"{namespace}/{number}"


def numbers(names: Iterable[str], pattern: re.Pattern) -> set[int]:
    """Pure: extract matching groups from each name as an int.

    Non-matching names are silently skipped.
    """
    result: set[int] = set()
    for name in names:
        m = pattern.search(name)
        if m:
            try:
                result.add(int(m.group(1)))
            except ValueError:
                pass
    return result


def local_numbers(root: Path) -> set[int]:
    """Stems of (.lime/tickets).glob("*.md") through _STEM; missing dir → set()."""
    tickets_dir = root / ".lime" / "tickets"
    if not tickets_dir.is_dir():
        return set()
    return numbers((p.stem for p in tickets_dir.glob("*.md")), _STEM)


def commit_numbers(root: Path) -> set[int]:
    """ticket-stem numbers from `git log --all --format=%s --grep=^ticket:`."""
    try:
        out = _git(root, "log", "--all", "--format=%s", "--grep=^ticket: ")
    except NumberingError:
        # A repo with no commits or no matching commits exits 0 with empty output;
        # an actually broken repo is a different story (caller chooses to absorb or raise).
        return set()
    if not out.strip():
        return set()
    return numbers(out.splitlines(), _SUBJECT)


def origin_numbers(root: Path) -> set[int]:
    """Numbers from origin branch heads and reservation refs in one ls-remote call.

    Returns empty set when there is no origin or ls-remote fails (the repo may be
    offline — caller chooses to treat that as no numbers rather than raise).
    """
    if not _has_origin(root):
        return set()
    result = subprocess.run(
        ["git", "-C", str(root), "ls-remote", "origin",
         "refs/heads/lime/*", f"{RESERVATION_NS}/*"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return set()
    refnames = []
    for line in result.stdout.splitlines():
        if "\t" in line:
            refnames.append(line.split("\t", 1)[1])
    return numbers(refnames, _BRANCH) | numbers(refnames, _RESERVATION)


def pr_numbers() -> set[int]:
    """Numbers from all-state PR head refs via git_helper.pr_head_refs().

    RuntimeError propagates (gh is a factory requirement — `lime check` probes it;
    a silently low number is worse than a red command).
    """
    return numbers(git_helper.pr_head_refs(), _BRANCH)


def next_number(root: Path) -> int:
    """The next free ticket number: max over every source + 1.

    Reads local tickets, ticket commits on every branch, origin branches and
    reservations, and PR head refs (when there is an origin).  A fresh repo
    with no tickets and no origin returns 1.
    """
    seen = local_numbers(root) | commit_numbers(root)
    if _has_origin(root):
        seen |= origin_numbers(root) | pr_numbers()
    return max(seen, default=0) + 1


def _remote_oid(root: Path, ref: str) -> str | None:
    """OID of `ref` on origin, or None when origin answers 'no such ref'.

    Raises NumberingError when origin cannot be asked at all.
    """
    probe = subprocess.run(
        ["git", "-C", str(root), "ls-remote", "--exit-code", "origin", ref],
        capture_output=True, text=True,
    )
    if probe.returncode == 0:
        line = probe.stdout.strip()
        return line.split("\t", 1)[0] if "\t" in line else line.split()[0]
    if probe.returncode == 2:
        return None
    raise NumberingError(
        f"couldn't ask origin whether {ref} exists: {probe.stderr.strip()[-200:]}"
    )


def _reserved_on_origin(root: Path, ref: str) -> bool:
    """True when `ref` exists on origin; False when origin answers 'no such ref'.

    Raises NumberingError when origin cannot be asked at all.
    """
    return _remote_oid(root, ref) is not None


def _holder(root: Path, ref: str) -> str:
    """The stem (ticket) or Run ID (ADR) that holds `ref` on origin, or 'another Run'.

    Reads the fetched commit by the OID captured from `ls-remote`, never a shared
    mutable `FETCH_HEAD` another Git command could have moved — safe when several
    requests inspect refs concurrently.  Fetching downloads objects only; it never
    creates a persistent local `refs/lime/*` ref.
    """
    oid = _remote_oid(root, ref)
    if oid is None:
        return "another Run"
    _git(root, "fetch", "origin", ref)
    subject = _git(root, "log", "-1", "--format=%s", oid)
    m = _HOLDER.match(subject)
    if m:
        return m.group(1)
    return "another Run"


def reserve(root: Path, number: int, stem: str, run_id: str, *,
            namespace: str = RESERVATION_NS) -> str | None:
    """CAS-create `<namespace>/<number>` on origin, pointing at a Run-unique commit.

    `namespace` defaults to the ticket ledger; pass `ADR_RESERVATION_NS` for ADRs,
    whose ref is zero-padded to four digits and whose `stem` is the Run ID (so the
    owner is the Run, not the ticket).  The ref is never deleted — it is the ledger
    `next_number`/`next_adr_number` reads.

    Returns the ref string on success.  Returns None when there is no origin or
    the number is ≤ 0 (a stem with no leading digits).

    The same `stem` re-reserving (a Run re-launched after a discard) is allowed and
    leaves the ref's object unchanged; a different holder on the same number is
    refused, naming the ref and its holder.
    """
    if number <= 0 or not _has_origin(root):
        return None
    ref = _ref_for(namespace, number)
    kind = "adr" if namespace == ADR_RESERVATION_NS else "ticket"

    if _reserved_on_origin(root, ref):
        holder = _holder(root, ref)
        if holder == stem:
            return ref
        raise NumberingError(
            f"{kind} number {number} is already taken on origin: {ref} is held by"
            f" {holder} — number this {kind} with `lime {kind} next`"
        )

    parent = f"origin/{worktree.default_branch(root)}"
    commit = _git(root, "commit-tree", f"{parent}^{{tree}}", "-p", parent, "-m",
                  f"lime: {kind} {stem} reserved\n\nLime-Run: {run_id}\nLime-Role: orchestrator")
    push = subprocess.run(
        ["git", "-C", str(root), "push", "origin", f"{commit}:{ref}"],
        capture_output=True, text=True,
    )
    if push.returncode != 0:
        raise NumberingError(
            f"{kind} number {number} was taken on origin while this Run started: {ref}"
            f" — number this {kind} with `lime {kind} next`"
            f" ({push.stderr.strip()[-200:]})"
        )
    return ref


# --- ADRs: a second ledger, owned by the Run ---------------------------------


def _local_adr_numbers(root: Path) -> set[int]:
    """Leading numeric stems of direct `docs/adr/*.md` files; missing dir → set()."""
    adr_dir = root / "docs" / "adr"
    if not adr_dir.is_dir():
        return set()
    return numbers((p.stem for p in adr_dir.glob("*.md")), _STEM)


def _origin_adr_tree_numbers(root: Path) -> set[int]:
    """Direct Markdown entries under `docs/adr/` in the origin default branch tree.

    Read with `git ls-tree` against the remote-tracking ref — no checkout.  A tree
    with no `docs/adr/` directory yields no entries (and no failure).
    """
    branch = worktree.default_branch(root)
    try:
        out = _git(root, "ls-tree", f"origin/{branch}", "docs/adr/")
    except NumberingError:
        return set()
    stems: list[str] = []
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        fields = meta.split()
        if len(fields) < 2 or fields[1] != "blob":
            continue
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".md"):
            stems.append(name[:-3])
    return numbers(stems, _STEM)


def _adr_ledger(root: Path) -> list[tuple[str, int]]:
    """The `refs/lime/adr/*` refs on origin as (ref, number), oldest first by remote order.

    Raises NumberingError when origin cannot be asked at all — a configured but
    unreachable origin is not 'no origin'.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "ls-remote", "origin", f"{ADR_RESERVATION_NS}/*"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise NumberingError(
            f"couldn't list {ADR_RESERVATION_NS} on origin: {result.stderr.strip()[-200:]}"
        )
    entries: list[tuple[str, int]] = []
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        _oid, ref = line.split("\t", 1)
        m = _ADR_RESERVATION.match(ref)
        if m:
            entries.append((ref, int(m.group(1))))
    return entries


def _origin_adr_ref_numbers(root: Path) -> set[int]:
    """Numbers from origin's `refs/lime/adr/*` ledger refs (empty when origin is unreadable)."""
    try:
        return {number for _ref, number in _adr_ledger(root)}
    except NumberingError:
        return set()


def next_adr_number(root: Path) -> int:
    """The next free ADR number: 1 + max over local files, origin tree and ledger refs.

    Ticket refs, ticket commits and GitHub PRs are not ADR sources — the ADR ledger
    is separate.  With no origin only local `docs/adr/*.md` count; a fresh directory
    (or none) yields 1.  Never calls `pr_numbers`/gh.
    """
    seen = _local_adr_numbers(root)
    if _has_origin(root):
        seen |= _origin_adr_tree_numbers(root) | _origin_adr_ref_numbers(root)
    return max(seen, default=0) + 1


def _owned_adr_number(root: Path, run_id: str) -> int | None:
    """The ADR number this Run already holds on origin, or None.

    Ledger order is not assumed — every reservation ref is inspected by its fetched
    object ID so a resume in another clone finds the Run's own number.
    """
    for ref, number in _adr_ledger(root):
        if _holder(root, ref) == run_id:
            return number
    return None


def reserve_adr(root: Path, run_id: str) -> int:
    """Reserve and return this Run's ADR number from the `refs/lime/adr` ledger.

    No origin: return the local next number without creating any ref or cache — this
    fallback is intentionally unreserved and not globally idempotent if files change.

    With origin: return the number this Run already owns (durable across clones and
    later reservations); otherwise CAS-create the next number through `reserve` with
    Run-unique reservation commits.  A genuine collision (another Run claimed the
    number) recalculates and retries; a failed transport, missing parent object or
    rejection with no competing ref propagates `NumberingError` rather than looping.
    """
    if not _has_origin(root):
        return next_adr_number(root)

    owned = _owned_adr_number(root, run_id)  # deadline: resume in any clone
    if owned is not None:
        return owned

    while True:
        number = next_adr_number(root)
        ref = _ref_for(ADR_RESERVATION_NS, number)
        try:
            reserve(root, number, run_id, run_id, namespace=ADR_RESERVATION_NS)
            return number
        except NumberingError:
            # A competing ref that now exists is a real collision: the same Run
            # means someone else's push was ours (return it), another Run means
            # recalculate and retry.  No ref means transport/rejection — propagate.
            if not _reserved_on_origin(root, ref):
                raise
            owned = _owned_adr_number(root, run_id)
            if owned is not None:
                return owned