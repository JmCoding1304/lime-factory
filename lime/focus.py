"""Ticket 219 — the owner's goal list in the home repo.

Spec: specs/assets/bridge-home-focus.md (read "bridge" as dashboard/dash). Rows
live in `focus.md`; groups are derived at read time, never stored. This module
is the parser and the `lime focus` command. Writes (add/done/edit) are owner-only.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from lime import home

FOCUS_FILE = "focus.md"
SEP = " · "
GROUPS = ("today", "this_week", "upcoming", "completed")
GROUP_TITLES = {
    "today": "Today",
    "this_week": "This week",
    "upcoming": "Upcoming",
    "completed": "Completed",
}

ROW_RE = re.compile(r"^- \[([ xX])\] f(\d+) · (.*)$")
DUE_RE = re.compile(r"due (\d{4}-\d{2}-\d{2})")


@dataclass
class Row:
    n: int
    title: str
    due: str | None
    project: str | None
    done: bool


def parse_line(line: str) -> Row | None:
    """One row of `focus.md`, or None for the header, blanks and hand notes."""
    m = ROW_RE.match(line)
    if m is None:
        return None
    done = m.group(1) in ("x", "X")
    parts = m.group(3).split(SEP)
    title = parts[0]
    due = None
    project = None
    for segment in parts[1:]:
        dm = DUE_RE.fullmatch(segment)
        if dm is not None:
            try:
                date.fromisoformat(dm.group(1))
            except ValueError:
                project = segment
                continue
            due = dm.group(1)
        else:
            project = segment
    return Row(n=int(m.group(2)), title=title, due=due, project=project, done=done)


def format_row(row: Row) -> str:
    """The canonical `focus.md` line for a row; no trailing newline."""
    box = "x" if row.done else " "
    out = f"- [{box}] f{row.n} · {row.title}"
    if row.due:
        out += f" · due {row.due}"
    if row.project:
        out += f" · {row.project}"
    return out


def parse(text: str) -> list[Row]:
    """Every parseable row, in file order."""
    rows = []
    for line in text.splitlines():
        row = parse_line(line)
        if row is not None:
            rows.append(row)
    return rows


def group_of(row: Row, today: date) -> str:
    """The row's group: completed · today · this_week · upcoming (ISO week)."""
    if row.done:
        return "completed"
    if not row.due:
        return "upcoming"
    due = date.fromisoformat(row.due)
    if due <= today:
        return "today"
    end_of_week = today + timedelta(days=6 - today.weekday())
    if due <= end_of_week:
        return "this_week"
    return "upcoming"


def list_rows(text: str, today: date) -> list[dict]:
    """The `--json` rows, sorted by group, then due, then id (undated last)."""
    rows = []
    for row in parse(text):
        group = group_of(row, today)
        overdue = not row.done and row.due is not None and date.fromisoformat(row.due) < today
        rows.append(
            {
                "id": f"f{row.n}",
                "title": row.title,
                "due": row.due,
                "project": row.project,
                "done": row.done,
                "group": group,
                "overdue": overdue,
            }
        )
    rows.sort(key=lambda r: (GROUPS.index(r["group"]), r["due"] or "9999-12-31", int(r["id"][1:])))
    return rows


def load() -> list[dict]:
    """Read rows from the home repo. A missing home dir or `focus.md` gives [].
    Read-only: never writes, never takes the lock."""
    path = home.home_dir() / FOCUS_FILE
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except OSError:
        return []
    return list_rows(text, home._now().date())


def command(args, registry: Path) -> int:
    """Dispatch `lime focus <subcommand>`: list (read-only) or add/done/edit."""
    sub = args.focus_command
    if sub == "list":
        rows = load()
        if args.json:
            print(json.dumps(rows))
            return 0
        for group in GROUPS:
            group_rows = [r for r in rows if r["group"] == group]
            if not group_rows:
                continue
            print(GROUP_TITLES[group])
            for r in group_rows:
                line = f"  {r['id']} · {r['title']}"
                if r["due"]:
                    line += f" · due {r['due']}"
                if r["project"]:
                    line += f" · {r['project']}"
                if r["overdue"]:
                    line += " · overdue"
                print(line)
        return 0
    if sub not in ("add", "done", "edit"):
        return 0

    # The tripwire runs first: before any argument check, before any file access.
    if home.owner_only("lime focus"):
        return 2

    # Validate every given field before touching the repo; exit 2, write nothing.
    title = due = project = None
    new_title = None
    if sub == "add":
        title = _validate_title(args.title)
        if title is None:
            return 2
        due = args.due
        if due is not None and not _validate_due(due):
            return 2
        project = args.project
        if project is not None and not _validate_project(project, registry):
            return 2
    else:  # done/edit share the id
        if sub == "edit":
            if args.title is not None:
                new_title = _validate_title(args.title)
                if new_title is None:
                    return 2
            if args.due and not _validate_due(args.due):
                return 2
            if args.project and not _validate_project(args.project, registry):
                return 2

    h = home.home_dir()
    if not home._has_commit(h):
        print(f"lime focus: no home repo at {h} — run lime dash init", file=sys.stderr)
        return 1

    with home.locked(h):
        path = h / FOCUS_FILE
        text = path.read_text() if path.exists() else home.FOCUS_HEADER
        if sub == "add":
            n = max((row.n for row in parse(text)), default=0) + 1
            if text and not text.endswith("\n"):
                text += "\n"
            new_text = text + format_row(Row(n, title, due, project, False)) + "\n"
            message = f"focus: add f{n} {title}"
            verb = "added"
        else:
            idm = re.fullmatch(r"f(\d+)", args.id)
            wanted = int(idm.group(1)) if idm else None
            lines = text.splitlines(keepends=True)
            idx = None
            old = None
            for i, line in enumerate(lines):
                row = parse_line(line.rstrip("\r\n"))
                if row is not None and wanted is not None and row.n == wanted:
                    idx, old = i, row
                    break
            if idx is None:
                print(f"lime focus: no row {args.id}", file=sys.stderr)
                return 1
            n = old.n
            new_row = Row(old.n, old.title, old.due, old.project, old.done)
            if sub == "done":
                new_row.done = True
                message = f"focus: done f{n}"
                verb = "done"
            else:
                if new_title is not None:
                    new_row.title = new_title
                if args.due is not None:
                    new_row.due = args.due or None
                if args.project is not None:
                    new_row.project = args.project or None
                if args.reopen:
                    new_row.done = False
                message = f"focus: edit f{n}"
                verb = "edited"
            old_line = lines[idx]
            if old_line.endswith("\r\n"):
                ending = "\r\n"
            elif old_line.endswith("\n"):
                ending = "\n"
            else:
                ending = ""
            new_line = format_row(new_row) + ending
            if new_line == old_line:
                print(f"f{n} unchanged")
                return 0
            lines[idx] = new_line
            new_text = "".join(lines)

        pushed = home.write(h, message, lambda p: (p / FOCUS_FILE).write_text(new_text))

    if pushed:
        print(f"f{n} {verb} · committed · pushed")
    else:
        print(f"f{n} {verb} · committed")
    return 0


def _registered_projects(registry: Path) -> set[str]:
    """The registered project names: `Path(line).name` over the registry's
    non-blank, non-`#` lines. A missing registry means no names."""
    try:
        lines = registry.read_text().splitlines()
    except OSError:
        return set()
    names = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        names.add(Path(stripped).name)
    return names


def _validate_title(title: str) -> str | None:
    """A stripped title, or None (after printing the refusal) when unusable."""
    if SEP in title or "\n" in title:
        print('lime focus: a title cannot be empty or contain " · "', file=sys.stderr)
        return None
    title = title.strip()
    if not title:
        print('lime focus: a title cannot be empty or contain " · "', file=sys.stderr)
        return None
    return title


def _validate_due(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        print(f'lime focus: bad date "{value}" — use YYYY-MM-DD', file=sys.stderr)
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        print(f'lime focus: bad date "{value}" — use YYYY-MM-DD', file=sys.stderr)
        return False
    return True


def _validate_project(value: str, registry: Path) -> bool:
    if value not in _registered_projects(registry):
        print(
            f'lime focus: project "{value}" is not registered in {registry}',
            file=sys.stderr,
        )
        return False
    return True
