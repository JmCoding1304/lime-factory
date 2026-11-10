"""Ticket parsing: one header, kind by field presence (spec §10).

Minimal for the tracer-bullet Run (ticket 28) — enough to read a `feature`/
`bug-fix` ticket, thread its four-line prompt into the chain, and refuse one
whose `Blocked by:` files still exist. Ticket 31 extends this for the other
Workflows' header quirks (`Spec:`/`Prototype:` handling already lives here
since `feature.py` needs it too).
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

SECTIONS = ("Ask", "Where", "Done means", "Out of scope")


class Ticket(BaseModel):
    path: str
    number: int
    slug: str  # filename stem — branch names and PR titles key off this
    title: str
    workflow: str | None = None
    type: str | None = None  # Type: — wayfinder tickets (grilling|research|prototype|task)
    status: str = "open"
    blocked_by: list[str] = Field(default_factory=list)
    spec: str | None = None
    prototype: str | None = None
    ask: str = ""
    where: str = ""
    done_means: str = ""
    out_of_scope: str = ""


def _field(lines: list[str], key: str) -> str | None:
    prefix = key + ":"
    return next(
        (line[len(prefix) :].strip() for line in lines if line.startswith(prefix)),
        None,
    )


def _section(lines: list[str], heading: str) -> str:
    marker = f"## {heading}"
    if marker not in lines:
        return ""
    start = lines.index(marker) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


def parse(text: str, path: str | Path) -> Ticket:
    """Parse ticket text as if it were the file at `path`.

    Same section/header parsing `load` has always used, split out so a Git blob
    can be read through the identical code path without writing a temp file.
    """
    path = Path(path)
    lines = text.splitlines()
    title_line = next((line for line in lines if line.startswith("# ")), f"# {path.stem}")
    title = re.sub(r"^#\s*\d+\s*—\s*", "", title_line).strip()
    number_match = re.match(r"(\d+)", path.stem)
    blocked_raw = _field(lines, "Blocked by") or ""
    return Ticket(
        path=str(path),
        number=int(number_match.group(1)) if number_match else 0,
        slug=path.stem,
        title=title,
        workflow=_field(lines, "Workflow"),
        type=_field(lines, "Type"),
        status=_field(lines, "Status") or "open",
        blocked_by=[b.strip() for b in blocked_raw.split(",") if b.strip()],
        spec=_field(lines, "Spec"),
        prototype=_field(lines, "Prototype"),
        ask=_section(lines, "Ask"),
        where=_section(lines, "Where"),
        done_means=_section(lines, "Done means"),
        out_of_scope=_section(lines, "Out of scope"),
    )


def load(path: str | Path) -> Ticket:
    """Parse one ticket file. Raises FileNotFoundError if it does not exist."""
    path = Path(path)
    return parse(path.read_text(), path)


def find(root: str | Path, arg: str) -> Path | None:
    """Resolve a `lime run` ticket argument (a number, or a full stem) to its file."""
    tickets_dir = Path(root) / ".lime" / "tickets"
    direct = tickets_dir / f"{arg}.md"
    if direct.exists():
        return direct
    matches = sorted(tickets_dir.glob(f"{arg}-*.md"))
    if not matches and is_number(arg):  # `1` finds `01-x.md`: a number ignores zero padding
        matches = by_number(root, arg)
    return matches[0] if matches else None


def is_number(arg: str) -> bool:
    """A bare ticket number: ASCII digits only (`str.isdigit` also passes `²`, which `int` rejects)."""
    return re.fullmatch(r"[0-9]+", arg) is not None


def ambiguous(root: str | Path, arg: str) -> list[Path]:
    """Every ticket file a bare number matches, when more than one does (`1-b.md`, `01-a.md`);
    otherwise `[]`. A full stem names one file and is never ambiguous."""
    matches = by_number(root, arg) if is_number(arg) else []
    return matches if len(matches) > 1 else []


def stem_matches(stem: str, arg: str) -> bool:
    """Whether a ticket stem answers a `lime run` argument: its stem or stem prefix, or its number."""
    if stem == arg or stem.startswith(f"{arg}-"):
        return True
    number = re.match(r"[0-9]+", stem)
    return is_number(arg) and number is not None and int(number.group(0)) == int(arg)


def blocking(ticket: Ticket, root: str | Path = Path(".")) -> list[str]:
    """`Blocked by:` files that still exist — delete-on-close means existing = unresolved."""
    return [b for b in ticket.blocked_by if (Path(root) / b).exists()]


def set_header_status(text: str, value: str) -> str | None:
    """`text` with its first header `Status:` line (above the first `## ` heading)
    replaced by `Status: <value>`; every other character kept. None when the header
    has no `Status:` line."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("## "):
            return None
        if line.startswith("Status:"):
            ending = line[len(line.splitlines()[0]):]
            lines[i] = f"Status: {value}{ending}"
            return "".join(lines)
    return None


def by_number(root: str | Path, arg: str) -> list[Path]:
    """Ticket files under `<root>/.lime/tickets/*.md` whose stem's leading digits
    equal `arg`'s leading digits as integers, sorted. `[]` when `arg` has no leading
    digits or nothing matches."""
    match = re.match(r"\d+", arg)
    if match is None:
        return []
    number = int(match.group(0))
    tickets_dir = Path(root) / ".lime" / "tickets"
    matches = []
    for path in tickets_dir.glob("*.md"):
        stem_match = re.match(r"\d+", path.stem)
        if stem_match is not None and int(stem_match.group(0)) == number:
            matches.append(path)
    return sorted(matches)


# --- Where paths (ticket 160): lexical path tokens for overlap detection ---

# The first single-backtick run on a line: `` `token` ``. Later backtick tokens on
# the same line are ignored, and a line without backticks contributes nothing.
_FIRST_BACKTICK = re.compile(r"`([^`]+)`")


def _leading_dot_slash(path: str) -> str:
    """Strip leading `./` segments and a trailing directory slash — comparison only."""
    while path.startswith("./"):
        path = path[2:]
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


def normalize_path(path: str) -> str:
    """`where_paths`' comparison form: no leading `./`, no trailing directory slash.

    Purely lexical — never touches the filesystem or resolves against the cwd, so a
    new or remote-only path normalizes exactly like an existing one.
    """
    return _leading_dot_slash(path.strip())


def _looks_like_path(token: str) -> bool:
    """A repository-relative file/directory-looking token (never a symbol or prose).

    Has a slash, or is a filename with an extension (dotfiles included). Rejects
    whitespace/prose, comma-separated lists, signatures/parentheses and bare
    identifiers such as `_run_ticket`. Existence is deliberately not required.
    """
    if not token or any(c.isspace() for c in token):
        return False
    if "," in token or "(" in token or ")" in token:
        return False
    if "/" in token:
        return True
    if token.startswith(".") and len(token) > 1:
        return True
    return "." in token and not token.endswith(".")


def _paths_in_where(where: str) -> list[str]:
    """The path tokens a `## Where` body names, one per line via its first backtick token.

    Encounter order, deduplicated on `normalize_path` (first spelling kept). Lexical
    only: no glob expansion, table/row interpretation, filesystem check, or inference
    from Ask/Done means.
    """
    seen: set[str] = set()
    result: list[str] = []
    for line in where.splitlines():
        match = _FIRST_BACKTICK.search(line)
        if match is None:
            continue
        token = match.group(1).strip()
        if not _looks_like_path(token):
            continue
        key = normalize_path(token)
        if key in seen:
            continue
        seen.add(key)
        result.append(token)
    return result


def where_paths(ticket: Ticket) -> list[str]:
    """The path tokens a `## Where` names, one per line via its first backtick token.

    Encounter order, deduplicated on `normalize_path` (first spelling kept). Lexical
    only: no glob expansion, table/row interpretation, filesystem check, or inference
    from Ask/Done means.
    """
    return _paths_in_where(ticket.where)


# The `Ticket: <slug> — ` header line `prompt()` opens with, anchored at the start
# of the text so a later `Ask` line quoting a ticket header cannot match.
_PROMPT_TICKET = re.compile(r"Ticket: (\S+) — ")


def prompt_scope(text: str) -> tuple[list[str], str | None]:
    """Read back what `prompt()` writes (ticket 246): its `## Where` paths and own ticket file.

    Returns `(paths, own_ticket)`: the Where tokens via `_paths_in_where`, and
    `.lime/tickets/<slug>.md` when `text` opens with a `Ticket: <slug> — ` line, else
    `None`. Text that is not a ticket prompt gives `([], None)`.
    """
    paths = _paths_in_where(_section(text.splitlines(), "Where"))
    match = _PROMPT_TICKET.match(text)
    own_ticket = f".lime/tickets/{match.group(1)}.md" if match else None
    return paths, own_ticket


def outside_where(changed: list[str], where: list[str], own_ticket: str | None = None) -> list[str]:
    """`changed` entries that sit outside the ticket's `Where` (ticket 246).

    Kept in `changed` order and original spelling, deduplicated on `normalize_path`.
    A changed path is outside when its normalized form is neither equal to
    `normalize_path(own_ticket)`, nor equal to, or starting with `k + "/"` for, any
    `k = normalize_path(w)` for `w` in `where`. Purely lexical: `pkgextra/x.py` is not
    under `pkg/`.
    """
    own = normalize_path(own_ticket) if own_ticket else None
    keys = [normalize_path(w) for w in where]
    seen: set[str] = set()
    result: list[str] = []
    for path in changed:
        key = normalize_path(path)
        if key in seen:
            continue
        seen.add(key)
        if own is not None and key == own:
            continue
        if any(key == k or key.startswith(k + "/") for k in keys):
            continue
        result.append(path)
    return result


def where_failures(ticket: Ticket, root: Path) -> list[str]:
    """Read-only `## Where` and `Prototype:` diagnostics, shared by launch and check.

    Each Where line names exactly one path via its first backtick token (see
    `where_paths`). Three faults are reported without changing extraction:

    * zero paths — the section names none, so overlap detection sees nothing;
    * a hidden occurrence — a later backticked token on the same line that no one
      reads. A later token is hidden iff it contains no whitespace and either
      contains `/` or names an existing file under `root`. Symbols, prose, and
      whitespace-bearing tokens pass; a directory-only basename does not; and
    * a missing prototype — a nonempty `Prototype:` with no path at
      `root / ticket.prototype`. Existence (file or directory) is checked against
      the filesystem, never Git tracking.

    `root` is explicit so the basename-file and prototype checks never depend on
    process cwd. Faults are not deduplicated (paths are, but faults are one per
    occurrence).
    """
    root = Path(root)
    failures: list[str] = []
    if not where_paths(ticket):
        failures.append(
            f"{ticket.path}: Where parses zero paths — use one backticked path per line"
        )
    for line_no, line in enumerate(ticket.where.splitlines(), start=1):
        # Ignore exactly the first match — the designated path slot — regardless of
        # whether it actually looks like a path.
        for match in list(_FIRST_BACKTICK.finditer(line))[1:]:
            token = match.group(1)
            if any(c.isspace() for c in token):
                continue
            if "/" in token or (root / token).is_file():
                failures.append(
                    f"{ticket.path}: Where line {line_no}: {token!r} is a second"
                    f" path on {line!r} — use one backticked path per line"
                )
    if ticket.prototype and not (root / ticket.prototype).exists():
        failures.append(
            f"{ticket.path}: Prototype {ticket.prototype!r} is missing under"
            f" {root} — create the file/directory or fix the ticket's `Prototype:` path"
        )
    return failures


def prompt(ticket: Ticket) -> str:
    """The ticket's four-line body, threaded whole into engineer/plan (spec §7's table)."""
    parts = [f"Ticket: {ticket.slug} — {ticket.title}", "", "## Ask", ticket.ask]
    if ticket.where:
        parts += ["", "## Where", ticket.where]
    parts += ["", "## Done means", ticket.done_means]
    if ticket.out_of_scope:
        parts += ["", "## Out of scope", ticket.out_of_scope]
    if ticket.spec:
        parts += ["", f"Spec: {ticket.spec}"]
    if ticket.prototype:
        parts += ["", f"Prototype: {ticket.prototype}"]
        proto = Path(ticket.prototype)
        if proto.is_file():
            notes = proto.parent / "NOTES.md"
            if notes.is_file():
                parts.append(
                    f"Read {notes} first (fixed · free · open), then {ticket.prototype}"
                    " itself; port it into the project's stack, never copy."
                )
            else:
                parts.append(
                    f"Read {ticket.prototype} itself; port it into the project's stack, never copy."
                )
        else:
            parts.append(
                f"Read {ticket.prototype}/NOTES.md first (fixed · free · open), then the route"
                " itself; port it into the project's stack, never copy."
            )
    return "\n".join(parts)
