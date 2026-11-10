"""Ticket parsing: one header, kind by field presence (spec §10)."""

import subprocess
from pathlib import Path

import pytest

from lime import tracker
from lime.tracker import Ticket

TICKET = """# 41 — Toy Ticket

Workflow: feature
Status: open
Blocked by: .lime/tickets/99-ghost.md
Spec: .lime/specs/foundation.md

## Ask
Do the small thing.

## Where
lime/toy.py

## Done means
It works.

## Out of scope
Everything else.
"""


def write_ticket(tmp_path: Path, text: str = TICKET, name: str = "41-toy-ticket.md") -> Path:
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    path = tickets / name
    path.write_text(text)
    return path


def test_load_parses_header_fields_and_number(tmp_path):
    path = write_ticket(tmp_path)
    ticket = tracker.load(path)
    assert ticket.number == 41
    assert ticket.slug == "41-toy-ticket"
    assert ticket.title == "Toy Ticket"
    assert ticket.workflow == "feature"
    assert ticket.status == "open"
    assert ticket.spec == ".lime/specs/foundation.md"
    assert ticket.blocked_by == [".lime/tickets/99-ghost.md"]


def test_load_parses_the_four_body_sections(tmp_path):
    path = write_ticket(tmp_path)
    ticket = tracker.load(path)
    assert ticket.ask == "Do the small thing."
    assert ticket.where == "lime/toy.py"
    assert ticket.done_means == "It works."
    assert ticket.out_of_scope == "Everything else."


def test_load_handles_missing_optional_fields(tmp_path):
    text = "# 42 — Bare Ticket\n\nWorkflow: feature\nStatus: open\n\n## Ask\nX\n\n## Where\nY\n\n## Done means\nZ\n\n## Out of scope\nnone\n"
    path = write_ticket(tmp_path, text, "42-bare-ticket.md")
    ticket = tracker.load(path)
    assert ticket.blocked_by == []
    assert ticket.spec is None
    assert ticket.prototype is None


def test_blocking_is_empty_when_the_blocker_file_is_already_gone(tmp_path):
    path = write_ticket(tmp_path)  # blocked by 99-ghost.md, which was never written
    ticket = tracker.load(path)
    assert tracker.blocking(ticket, tmp_path) == []


def test_blocking_returns_the_blocker_while_its_file_still_exists(tmp_path):
    path = write_ticket(tmp_path)
    ticket = tracker.load(path)
    (tmp_path / ".lime" / "tickets" / "99-ghost.md").write_text("# 99 — Ghost\n")
    assert tracker.blocking(ticket, tmp_path) == [".lime/tickets/99-ghost.md"]


def test_find_resolves_a_bare_number_to_its_ticket_file(tmp_path):
    write_ticket(tmp_path)
    assert tracker.find(tmp_path, "41") == tmp_path / ".lime" / "tickets" / "41-toy-ticket.md"


def test_find_resolves_a_full_stem_directly(tmp_path):
    write_ticket(tmp_path)
    assert tracker.find(tmp_path, "41-toy-ticket") == tmp_path / ".lime" / "tickets" / "41-toy-ticket.md"


def test_find_returns_none_for_no_match(tmp_path):
    write_ticket(tmp_path)
    assert tracker.find(tmp_path, "99") is None


def test_find_matches_a_number_whatever_its_zero_padding(tmp_path):
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "01-x.md").write_text("# 1 — x\n")
    (tickets / "10-y.md").write_text("# 10 — y\n")
    assert tracker.find(tmp_path, "1") == tickets / "01-x.md"
    assert tracker.find(tmp_path, "001") == tickets / "01-x.md"
    assert tracker.find(tmp_path, "01-x") == tickets / "01-x.md"
    assert tracker.find(tmp_path, "10") == tickets / "10-y.md"
    assert tracker.find(tmp_path, "1-x") is None


def test_ambiguous_names_every_file_a_bare_number_matches(tmp_path):
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    for name in ("1-b.md", "01-a.md", "10-c.md"):
        (tickets / name).write_text("")
    assert [p.name for p in tracker.ambiguous(tmp_path, "1")] == ["01-a.md", "1-b.md"]
    assert tracker.ambiguous(tmp_path, "10") == []
    assert tracker.ambiguous(tmp_path, "1-b") == []  # a full stem names one file


def test_a_non_ascii_digit_is_not_a_ticket_number(tmp_path):
    write_ticket(tmp_path)
    assert not tracker.stem_matches("2-x", "\u00b2")  # superscript two: isdigit() but not 0-9
    assert tracker.find(tmp_path, "\u00b2") is None
    assert tracker.ambiguous(tmp_path, "\u00b2") == []


def test_stem_matches_number_regardless_of_padding():
    assert tracker.stem_matches("01-x", "1")
    assert tracker.stem_matches("1-x", "01")
    assert tracker.stem_matches("01-x", "01-x")
    assert tracker.stem_matches("41-toy", "41")
    assert not tracker.stem_matches("10-y", "1")
    assert not tracker.stem_matches("01-x", "02")
    assert not tracker.stem_matches("01-x", "01-y")


# --- set_header_status / by_number (ticket 236) ---


def test_set_header_status_rewrites_only_the_header_line():
    text = (
        "# 9 — X\r\nType: task\r\nStatus: open (owner)\r\n\r\n## Ask\r\nStatus: open\r\nbody"
    )
    expected = (
        "# 9 — X\r\nType: task\r\nStatus: closed\r\n\r\n## Ask\r\nStatus: open\r\nbody"
    )
    result = tracker.set_header_status(text, "closed")
    assert result == expected
    assert result.endswith("Status: open\r\nbody")  # body line, CRLF and no final newline survive
    assert not result.endswith("body\n")
    reopened = tracker.set_header_status(result, "open")
    assert "Status: open\r\n" in reopened
    assert reopened == (
        "# 9 — X\r\nType: task\r\nStatus: open\r\n\r\n## Ask\r\nStatus: open\r\nbody"
    )


def test_set_header_status_is_idempotent():
    text = "# 9 — X\nStatus: closed\n\n## Ask\nbody"
    assert tracker.set_header_status(text, "closed") == text


def test_set_header_status_none_without_a_header_status():
    assert tracker.set_header_status("# 9 — X\nType: task\n\n## Ask\nStatus: open\n", "closed") is None
    assert tracker.set_header_status("", "closed") is None
    assert tracker.set_header_status("# 9 — X\nType: task\n", "closed") is None


def test_by_number_matches_leading_digits(tmp_path):
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    toy = tickets / "41-toy.md"
    toy.write_text("# 41 — Toy\n")
    (tickets / "410-big.md").write_text("# 410 — Big\n")
    (tickets / "notes.md").write_text("notes\n")

    assert tracker.by_number(tmp_path, "41") == [toy]
    assert tracker.by_number(tmp_path, "041") == [toy]
    assert tracker.by_number(tmp_path, "41-toy") == [toy]
    assert tracker.by_number(tmp_path, "4") == []
    assert tracker.by_number(tmp_path, "99") == []
    assert tracker.by_number(tmp_path, "x") == []
    assert tracker.by_number(tmp_path / "missing", "41") == []


def test_load_parses_the_type_field(tmp_path):
    text = "# 50 — Wayfinder\n\nType: task\nStatus: open\n\n## Ask\nX\n\n## Where\nY\n\n## Done means\nZ\n\n## Out of scope\nnone\n"
    path = write_ticket(tmp_path, text, "50-wayfinder.md")
    ticket = tracker.load(path)
    assert ticket.type == "task"
    assert ticket.workflow is None


def test_prompt_composes_the_four_line_body_plus_spec(tmp_path):
    path = write_ticket(tmp_path)
    ticket = tracker.load(path)
    text = tracker.prompt(ticket)
    assert "## Ask" in text and "Do the small thing." in text
    assert "## Done means" in text and "It works." in text
    assert "Spec: .lime/specs/foundation.md" in text


# --- the four Workflows, assembled off ticket headers alone (spec §7, ticket 31) ---

BUG_FIX_TICKET = (
    "# 42 — Greet Crashes\n\nWorkflow: bug-fix\nStatus: open\n\n"
    "## Ask\ngreet('') raises.\n\n## Where\napp.py\n\n"
    "## Done means\nRegression test passes.\n\n## Out of scope\nNothing.\n"
)

GREENFIELD_TICKET = (
    "# 43 — Bootstrap The Stack\n\nWorkflow: feature\nStatus: open\n"
    "Spec: .lime/specs/toy.md\n\n"
    "## Ask\nScaffold the stack the spec names.\n\n## Where\n(repo root)\n\n"
    "## Done means\ncommands.test runs green.\n\n## Out of scope\nNothing.\n"
)

FRONTEND_TICKET = (
    "# 44 — Wire The Locked Prototype\n\nWorkflow: feature\nStatus: open\n"
    "Prototype: app/proto-toy\n\n"
    "## Ask\nPort the locked proto-toy variant into the stack.\n\n## Where\napp/\n\n"
    "## Done means\ncommands.test runs green.\n\n## Out of scope\nNothing.\n"
)


def test_bug_fix_workflow_parses_and_carries_no_spec_or_prototype(tmp_path):
    path = write_ticket(tmp_path, BUG_FIX_TICKET, "42-bug.md")
    ticket = tracker.load(path)
    assert ticket.workflow == "bug-fix"
    assert ticket.spec is None
    assert ticket.prototype is None


def test_greenfield_ticket_prompt_carries_the_spec_field(tmp_path):
    path = write_ticket(tmp_path, GREENFIELD_TICKET, "43-bootstrap.md")
    ticket = tracker.load(path)
    assert ticket.workflow == "feature"
    assert ticket.spec == ".lime/specs/toy.md"
    text = tracker.prompt(ticket)
    assert "Spec: .lime/specs/toy.md" in text
    assert "commands.test runs green" in text  # bootstrap ticket's Done means (§15.3)


def test_frontend_ticket_prompt_carries_the_prototype_field_and_notes_guidance(tmp_path):
    path = write_ticket(tmp_path, FRONTEND_TICKET, "44-frontend.md")
    ticket = tracker.load(path)
    assert ticket.workflow == "feature"
    assert ticket.prototype == "app/proto-toy"
    text = tracker.prompt(ticket)
    assert "Prototype: app/proto-toy" in text
    assert "app/proto-toy/NOTES.md" in text
    assert "then the route" in text


def test_file_prototype_prompt_reads_parent_notes_then_file(tmp_path, monkeypatch):
    """A `Prototype:` naming a file points the planner at the sibling NOTES.md, then
    the file itself — never `<file>/NOTES.md`."""
    proto = tmp_path / "prototypes" / "home" / "opus-3.html"
    proto.parent.mkdir(parents=True)
    proto.write_text("<html>\n")
    (proto.parent / "NOTES.md").write_text("fixed: layout\n")
    monkeypatch.chdir(tmp_path)
    ticket = Ticket(
        path="x.md", number=1, slug="1-x", title="X",
        prototype="prototypes/home/opus-3.html",
    )
    text = tracker.prompt(ticket)
    assert "Prototype: prototypes/home/opus-3.html" in text
    assert (
        "Read prototypes/home/NOTES.md first (fixed · free · open), then"
        " prototypes/home/opus-3.html itself; port it into the project's stack, never copy."
        in text
    )
    assert "opus-3.html/NOTES.md" not in text


def test_file_prototype_prompt_without_notes_reads_only_file(tmp_path, monkeypatch):
    """Without a usable sibling NOTES.md the brief points at the file alone; a
    NOTES.md that is a directory is not offered as a file."""
    proto = tmp_path / "prototypes" / "home" / "opus-3.html"
    proto.parent.mkdir(parents=True)
    proto.write_text("<html>\n")
    monkeypatch.chdir(tmp_path)
    ticket = Ticket(
        path="x.md", number=1, slug="1-x", title="X",
        prototype="prototypes/home/opus-3.html",
    )
    text = tracker.prompt(ticket)
    assert "Prototype: prototypes/home/opus-3.html" in text
    assert "prototypes/home/opus-3.html" in text
    assert "NOTES.md" not in text

    (proto.parent / "NOTES.md").mkdir()  # unusable: a directory, not a notes file
    text_with_dir_notes = tracker.prompt(ticket)
    assert "NOTES.md" not in text_with_dir_notes
    assert "prototypes/home/opus-3.html" in text_with_dir_notes


def test_directory_prototype_prompt_wording_is_unchanged(tmp_path, monkeypatch):
    """A real prototype directory keeps the existing folder guidance byte-for-byte."""
    (tmp_path / "app" / "proto-toy").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    ticket = Ticket(
        path="x.md", number=1, slug="1-x", title="X", prototype="app/proto-toy",
    )
    text = tracker.prompt(ticket)
    assert "Prototype: app/proto-toy" in text
    assert (
        "Read app/proto-toy/NOTES.md first (fixed · free · open), then the route"
        " itself; port it into the project's stack, never copy."
        in text
    )


def test_prompt_omits_spec_and_prototype_lines_when_neither_is_set(tmp_path):
    text = (
        "# 45 — Bare\n\nWorkflow: feature\nStatus: open\n\n"
        "## Ask\nX\n\n## Where\nY\n\n## Done means\nZ\n\n## Out of scope\nnone\n"
    )
    path = write_ticket(tmp_path, text, "45-bare.md")
    ticket = tracker.load(path)
    body = tracker.prompt(ticket)
    assert "Spec:" not in body
    assert "Prototype:" not in body


# --- where_paths (ticket 160): the first backticked path per Where line ---


def _where_ticket(where: str) -> Ticket:
    return tracker.parse(
        "# 41 — Toy\n\nWorkflow: feature\nStatus: open\n\n## Ask\nx\n\n"
        f"## Where\n{where}\n\n## Done means\ny\n",
        "41-toy.md",
    )


def test_where_paths_first_backtick_per_line():
    ticket = _where_ticket(
        "- `lime/tracker.py` — parsing, and `lime/cli.py` later\n"
        "- `src/` — a directory\n"
        "lime/bare.py — no backticks, so it contributes nothing\n"
    )
    # Only each line's FIRST backtick token; bare prose never contributes.
    assert tracker.where_paths(ticket) == ["lime/tracker.py", "src/"]


def test_where_paths_drops_symbols_lists_and_prose():
    ticket = _where_ticket(
        "- `_run_ticket` — a symbol\n"
        "- `do_thing(x, y)` — a signature\n"
        "- `app.py, cli.py` — a comma list\n"
        "- `some prose here` — words\n"
        "- `lime/tracker.py:30,72` — a comma list\n"
        "- `app.py` — a real root filename\n"
        "- `cli.py` — another real root filename\n"
    )
    assert tracker.where_paths(ticket) == ["app.py", "cli.py"]


def test_where_paths_empty_and_unquoted():
    assert tracker.where_paths(_where_ticket("")) == []
    assert tracker.where_paths(_where_ticket("app.py\nlime/tracker.py\n")) == []
    assert tracker.where_paths(_where_ticket("(repo root)")) == []


def test_where_paths_deduplicates_without_requiring_files(tmp_path):
    ticket = _where_ticket(
        "- `src/`\n"
        "- `src`\n"
        "- `./app.py`\n"
        "- `app.py`\n"
        "- `.gitignore`\n"
        "- `lime/new/subsystem.py`\n"
    )
    # Directories, root filenames, dotfiles and new nested files all count; the
    # `./`/trailing-slash spellings collapse, keeping the first spelling seen.
    assert tracker.where_paths(ticket) == [
        "src/", "./app.py", ".gitignore", "lime/new/subsystem.py",
    ]
    assert not any((tmp_path / p).exists() for p in ("src", "app.py", ".gitignore", "lime"))


def test_parse_matches_load_on_the_same_text(tmp_path):
    path = write_ticket(tmp_path)
    assert tracker.parse(path.read_text(), path) == tracker.load(path)


# --- where_failures: zero-path and hidden-token diagnostics ---


def _no_where_ticket() -> Ticket:
    return tracker.parse(
        "# 41 — Toy\n\nWorkflow: feature\nStatus: open\n\n## Ask\nx\n\n## Done means\ny\n",
        "41-toy.md",
    )


@pytest.mark.parametrize(
    "where",
    [
        "",  # empty section body
        "app.py\nlime/tracker.py\n",  # bare (unquoted) paths
        "_run_ticket\n",  # a first-token-only symbol
    ],
)
def test_where_failures_zero_paths(where, tmp_path):
    failures = tracker.where_failures(_where_ticket(where), tmp_path)
    assert len(failures) == 1
    assert "41-toy.md" in failures[0]
    assert "use one backticked path per line" in failures[0]


def test_where_failures_zero_paths_when_section_is_missing(tmp_path):
    failures = tracker.where_failures(_no_where_ticket(), tmp_path)
    assert len(failures) == 1
    assert "41-toy.md" in failures[0]
    assert "use one backticked path per line" in failures[0]


def test_where_failures_reports_each_hidden_occurrence(tmp_path):
    ticket = _where_ticket(
        "- `lime/a.py` then `lime/b.py` and `lime/c.py`\n"
        "- `src/x.py` `src/y.py`\n"
        "- `lime/b.py`\n"
        "`a.py` `src/y.py` `src/y.py`\n"
    )
    failures = tracker.where_failures(ticket, tmp_path)
    assert len(failures) == 5
    assert "Where line 1" in failures[0] and "lime/b.py" in failures[0]
    assert "Where line 1" in failures[1] and "lime/c.py" in failures[1]
    assert "Where line 2" in failures[2] and "src/y.py" in failures[2]
    assert "Where line 4" in failures[3] and "src/y.py" in failures[3]
    assert "Where line 4" in failures[4] and "src/y.py" in failures[4]
    assert all("use one backticked path per line" in f for f in failures)


def test_where_failures_hidden_after_nonpath_first_token(tmp_path):
    ticket = _where_ticket("- `_run_ticket` — see `src/new.py`\n")
    failures = tracker.where_failures(ticket, tmp_path)
    assert len(failures) == 2
    assert "zero paths" in failures[0]
    assert "Where line 1" in failures[1]
    assert "src/new.py" in failures[1]


def test_where_failures_existing_root_files_only(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README").write_text("readme\n")
    (repo / "commands.setup").write_text("setup\n")
    (repo / "mydir").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # explicit root, not cwd, decides existence
    ticket = _where_ticket(
        "- `lime/real.py`\n"
        "- `lime/real.py` then `README` and `commands.setup`\n"
        "- `lime/real.py` then `missing.py` and `nope` and `mydir`\n"
    )
    failures = tracker.where_failures(ticket, repo)
    assert len(failures) == 2
    assert "README" in failures[0]
    assert "commands.setup" in failures[1]
    assert all("missing.py" not in f and "nope" not in f and "mydir" not in f for f in failures)


def test_where_failures_allows_symbols_and_whitespace(tmp_path):
    ticket = _where_ticket(
        "- `lime/real.py`\n"
        "- `lime/real.py` then `_cmd_run` and `commands.setup`\n"
        "- `lime/real.py` then `lime_<run_id>`\n"
        "- `lime/real.py` then `some prose here`\n"
        "- `lime/real.py` then `\tlime/a.py\t`\n"
        "- `lime/real.py` then ` lime/a.py `\n"
    )
    assert tracker.where_failures(ticket, tmp_path) == []


def test_where_failures_one_path_per_line(tmp_path):
    ticket = _where_ticket(
        "- `src/`\n"
        "- `src`\n"
        "- `./app.py`\n"
        "- `app.py`\n"
        "- `lime/new.py` then `_run_ticket`\n"
    )
    assert tracker.where_failures(ticket, tmp_path) == []
    assert tracker.where_paths(ticket) == ["src/", "./app.py", "lime/new.py"]


# --- where_failures: missing Prototype (ticket 168) ---


def _proto_ticket(prototype: str, where: str = "`app.py`") -> Ticket:
    return tracker.parse(
        "# 41 — Toy\n\nWorkflow: feature\nStatus: open\n"
        f"Prototype: {prototype}\n\n## Ask\nx\n\n## Where\n{where}\n\n## Done means\ny\n",
        "41-toy.md",
    )


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


def test_where_failures_missing_prototype_names_ticket_and_path(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "protos").mkdir(parents=True)
    (elsewhere / "protos" / "x.css").write_text("c\n")
    monkeypatch.chdir(elsewhere)  # explicit root, not cwd, decides existence
    failures = tracker.where_failures(_proto_ticket("protos/x.css"), repo)
    assert len(failures) == 1
    assert "41-toy.md" in failures[0]
    assert "protos/x.css" in failures[0]
    assert "Prototype" in failures[0]


@pytest.mark.parametrize("kind", ["file", "dir"])
@pytest.mark.parametrize("tracked", [False, True])
def test_where_failures_accepts_existing_prototype(tmp_path, monkeypatch, kind, tracked):
    """A file or directory prototype exists independent of cwd or Git tracking."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    proto = repo / "protos" / "x"
    if kind == "file":
        proto.parent.mkdir(parents=True)
        proto.write_text("c\n")
    else:
        proto.mkdir(parents=True)
        (proto / "child.txt").write_text("c\n")
    if tracked:
        _git(repo, "add", "-A")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert tracker.where_failures(_proto_ticket("protos/x"), repo) == []


def test_where_failures_accepts_existing_prototype_without_git(tmp_path, monkeypatch):
    """Existence works with no Git metadata at all."""
    repo = tmp_path / "repo"
    (repo / "protos").mkdir(parents=True)
    (repo / "protos" / "x.css").write_text("c\n")
    monkeypatch.chdir(repo)
    assert tracker.where_failures(_proto_ticket("protos/x.css"), repo) == []


def test_where_failures_accumulates_prototype_and_where_faults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ticket = _proto_ticket("gone/x.css", where="_foo")
    failures = tracker.where_failures(ticket, tmp_path)
    assert len(failures) == 2
    assert "zero paths" in failures[0]
    assert "Prototype" in failures[1]
    assert "gone/x.css" in failures[1]
