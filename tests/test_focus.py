"""Tests for lime/focus.py — the owner's goal list (ticket 219)."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from lime import cli, focus, home

REPO_ROOT = Path(__file__).resolve().parent.parent

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=timezone(timedelta(hours=-3)))

SPEC_ROWS = [
    "- [ ] f1 · Ship event_site event page · due 2026-11-06 · event_site",
    "- [ ] f2 · Lock bridge-home spec · due 2026-11-07 · limefactory",
    "- [ ] f3 · Ratify shop-api seam tickets · limefactory",
    "- [x] f4 · C1 dashboard localhost-only · due 2026-11-03 · limefactory",
]

BODY = "\n".join(
    [
        "- [ ] f1 · A · due 2026-11-04",
        "- [ ] f2 · B · due 2026-11-06",
        "- [ ] f10 · J · due 2026-11-06",
        "- [ ] f3 · C · due 2026-11-05",
        "- [ ] f4 · D · due 2026-11-08",
        "- [ ] f5 · E · due 2026-11-09",
        "- [ ] f6 · F",
        "- [x] f7 · G · due 2026-11-01",
    ]
) + "\n"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def log(repo):
    return _git(repo, "log", "--format=%s").stdout.splitlines()


@pytest.fixture
def env(tmp_path, monkeypatch):
    home_root = tmp_path / "home"
    home_root.mkdir()
    monkeypatch.setenv("HOME", str(home_root))
    monkeypatch.delenv("LIME_AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (tmp_path / "gitconfig").write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "t")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "t@t")
    monkeypatch.setattr(home, "_now", lambda: T0)
    projects = tmp_path / "projects"
    projects.write_text("# registry\n\n/work/event_site\n/work/limefactory/\n")
    monkeypatch.setenv("LIME_PROJECTS", str(projects))
    return SimpleNamespace(tmp=tmp_path, home=home_root / ".lime" / "dashboard")


def make_home(env, body=""):
    h = env.home
    h.mkdir(parents=True, exist_ok=True)
    _git(h, "init", "-q", "-b", "main")
    (h / ".gitignore").write_text(home.GITIGNORE_TEXT)
    (h / "focus.md").write_text(home.FOCUS_HEADER + body)
    _git(h, "add", "-A")
    _git(h, "commit", "-q", "-m", "dashboard init")
    remote = env.tmp / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )
    _git(h, "remote", "add", "origin", str(remote))
    _git(h, "push", "-q", "-u", "origin", "main")
    return h


def test_parse_reads_rows_and_skips_other_lines():
    text = (
        home.FOCUS_HEADER
        + "\n"
        + "note: buy milk\n"
        + "\n".join(SPEC_ROWS)
        + "\n- [X] f7 · E\n"
    )
    rows = focus.parse(text)
    assert [(r.n, r.title, r.due, r.project, r.done) for r in rows] == [
        (1, "Ship event_site event page", "2026-11-06", "event_site", False),
        (2, "Lock bridge-home spec", "2026-11-07", "limefactory", False),
        (3, "Ratify shop-api seam tickets", None, "limefactory", False),
        (4, "C1 dashboard localhost-only", "2026-11-03", "limefactory", True),
        (7, "E", None, None, True),
    ]


def test_format_row_round_trips_the_spec_rows():
    for line in SPEC_ROWS:
        assert focus.format_row(focus.parse_line(line)) == line


def test_list_json_groups_and_sorts_by_due_then_id():
    rows = focus.list_rows(BODY, T0.date())
    assert set(rows[0]) == {"id", "title", "due", "project", "done", "group", "overdue"}
    assert rows == [
        {"id": "f1", "title": "A", "due": "2026-11-04", "project": None, "done": False, "group": "today", "overdue": True},
        {"id": "f3", "title": "C", "due": "2026-11-05", "project": None, "done": False, "group": "today", "overdue": False},
        {"id": "f2", "title": "B", "due": "2026-11-06", "project": None, "done": False, "group": "this_week", "overdue": False},
        {"id": "f10", "title": "J", "due": "2026-11-06", "project": None, "done": False, "group": "this_week", "overdue": False},
        {"id": "f4", "title": "D", "due": "2026-11-08", "project": None, "done": False, "group": "this_week", "overdue": False},
        {"id": "f5", "title": "E", "due": "2026-11-09", "project": None, "done": False, "group": "upcoming", "overdue": False},
        {"id": "f6", "title": "F", "due": None, "project": None, "done": False, "group": "upcoming", "overdue": False},
        {"id": "f7", "title": "G", "due": "2026-11-01", "project": None, "done": True, "group": "completed", "overdue": False},
    ]


def test_list_text_prints_group_titles_and_flags_overdue(env, capsys):
    make_home(env, BODY)
    assert cli.main(["focus", "list"]) == 0
    assert capsys.readouterr().out == (
        "Today\n"
        "  f1 · A · due 2026-11-04 · overdue\n"
        "  f3 · C · due 2026-11-05\n"
        "This week\n"
        "  f2 · B · due 2026-11-06\n"
        "  f10 · J · due 2026-11-06\n"
        "  f4 · D · due 2026-11-08\n"
        "Upcoming\n"
        "  f5 · E · due 2026-11-09\n"
        "  f6 · F\n"
        "Completed\n"
        "  f7 · G · due 2026-11-01\n"
    )


def test_list_without_home_repo_prints_no_rows_and_exits_0(env, capsys):
    assert cli.main(["focus", "list"]) == 0
    assert capsys.readouterr().out == ""
    assert cli.main(["focus", "list", "--json"]) == 0
    assert capsys.readouterr().out == "[]\n"
    assert not env.home.exists()


def test_list_is_never_refused_and_never_writes(env, capsys, monkeypatch):
    h = make_home(env, "- [ ] f1 · A · due 2026-11-04\n")
    focus_bytes = (h / "focus.md").read_bytes()
    log = _git(h, "log", "--format=%H").stdout
    monkeypatch.setenv("LIME_AGENT_RUNTIME", "pi")
    assert cli.main(["focus", "list", "--json"]) == 0
    assert '"f1"' in capsys.readouterr().out
    assert (h / "focus.md").read_bytes() == focus_bytes
    assert _git(h, "status", "--porcelain").stdout == ""
    assert _git(h, "log", "--format=%H").stdout == log


# --- add / done / edit: writes and commits -------------------------------


def test_add_appends_next_id_commits_and_pushes(env, capsys):
    h = make_home(env, "- [ ] f1 · A\n- [ ] f3 · C\n")
    assert cli.main(
        ["focus", "add", "Ship it", "--due", "2026-11-07", "--project", "event_site"]
    ) == 0
    assert capsys.readouterr().out == "f4 added · committed · pushed\n"
    assert (h / "focus.md").read_text().splitlines()[-1] == (
        "- [ ] f4 · Ship it · due 2026-11-07 · event_site"
    )
    assert log(h)[0] == "focus: add f4 Ship it"
    assert _git(env.tmp / "remote.git", "rev-parse", "main").stdout.strip() == (
        _git(h, "rev-parse", "HEAD").stdout.strip()
    )


def test_add_to_an_empty_list_is_f1_without_optional_fields(env, capsys):
    h = make_home(env)
    assert cli.main(["focus", "add", "Title"]) == 0
    assert capsys.readouterr().out == "f1 added · committed · pushed\n"
    assert (h / "focus.md").read_text().splitlines()[-1] == "- [ ] f1 · Title"


def test_add_commits_a_hand_edit_first_and_counts_its_id(env, capsys):
    h = make_home(env)
    (h / "focus.md").write_text((h / "focus.md").read_text() + "- [ ] f5 · hand\n")
    assert cli.main(["focus", "add", "New"]) == 0
    assert (h / "focus.md").read_text().splitlines()[-1] == "- [ ] f6 · New"
    assert log(h) == ["focus: add f6 New", "focus: hand edit", "dashboard init"]


def test_done_rewrites_only_that_row_in_one_commit(env, capsys):
    body = "note: buy milk\n- [ ] f1 · A\n- [ ] f2 · B\n- [ ] f3 · C\n"
    h = make_home(env, body)
    before = (h / "focus.md").read_text()
    before_log = log(h)
    assert cli.main(["focus", "done", "f2"]) == 0
    assert capsys.readouterr().out == "f2 done · committed · pushed\n"
    after = (h / "focus.md").read_text()
    assert after == before.replace("- [ ] f2 · B", "- [x] f2 · B")
    assert log(h) == ["focus: done f2", *before_log]


def test_edit_changes_fields_and_reopen_unchecks(env, capsys):
    h = make_home(env, "- [x] f4 · Old · due 2026-11-06 · limefactory\n")
    assert cli.main(
        ["focus", "edit", "f4", "--title", "New", "--due", "2026-11-10",
         "--project", "limefactory", "--reopen"]
    ) == 0
    assert capsys.readouterr().out == "f4 edited · committed · pushed\n"
    assert (h / "focus.md").read_text().splitlines()[-1] == (
        "- [ ] f4 · New · due 2026-11-10 · limefactory"
    )
    assert log(h)[0] == "focus: edit f4"
    assert cli.main(["focus", "edit", "f4", "--due", ""]) == 0
    capsys.readouterr()
    assert (h / "focus.md").read_text().splitlines()[-1] == "- [ ] f4 · New · limefactory"


def test_done_on_a_done_row_is_unchanged_and_writes_nothing(env, capsys):
    h = make_home(env, "- [x] f4 · Done\n")
    before = log(h)
    assert cli.main(["focus", "done", "f4"]) == 0
    assert capsys.readouterr().out == "f4 unchanged\n"
    assert log(h) == before
    assert _git(h, "status", "--porcelain").stdout == ""


# --- add / done / edit: refusals with no write ---------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["focus", "add", "a · b"],
        ["focus", "add", "Title · "],
        ["focus", "add", ""],
        ["focus", "add", "x", "--due", "2026-13-01"],
        ["focus", "add", "x", "--due", "11/06/2026"],
        ["focus", "add", "x", "--due", "2026-02-30"],
        ["focus", "add", "x", "--project", "nope"],
        ["focus", "edit", "f1", "--title", "a · b"],
        ["focus", "edit", "f1", "--title", " a · "],
        ["focus", "edit", "f1", "--due", "2026-13-01"],
        ["focus", "edit", "f1", "--project", "nope"],
    ],
)
def test_add_and_edit_refuse_bad_input_with_no_write(env, capsys, argv):
    h = make_home(env, "- [ ] f1 · A\n")
    before = (h / "focus.md").read_bytes()
    before_log = log(h)
    assert cli.main(argv) == 2
    assert (h / "focus.md").read_bytes() == before
    assert log(h) == before_log
    assert not (h / "push-error").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["focus", "done", "f99"],
        ["focus", "edit", "f99", "--title", "x"],
        ["focus", "done", "x1"],
    ],
)
def test_unknown_id_exits_nonzero_with_no_write(env, capsys, argv):
    h = make_home(env, "- [ ] f1 · A\n")
    (h / "focus.md").write_text((h / "focus.md").read_text() + "- [ ] f9 · hand\n")
    before_log = log(h)
    assert cli.main(argv) != 0
    assert log(h) == before_log
    assert argv[2] in capsys.readouterr().err


def test_writes_without_a_home_repo_name_dash_init(env, capsys):
    assert cli.main(["focus", "add", "x"]) == 1
    assert "run lime dash init" in capsys.readouterr().err
    assert not env.home.exists()


# --- tripwire (behavioural) ----------------------------------------------


@pytest.mark.parametrize(
    "argv,message",
    [
        (["focus", "add", "Row"], "focus: add f2 Row"),
        (["focus", "done", "f1"], "focus: done f1"),
        (["focus", "edit", "f1", "--title", "Renamed"], "focus: edit f1"),
    ],
)
def test_owner_only_tripwire_in_a_subprocess(env, argv, message):
    h = make_home(env, "- [ ] f1 · A\n")
    before = (h / "focus.md").read_bytes()
    before_log = log(h)
    refused = subprocess.run(
        [sys.executable, "-m", "lime", *argv],
        env={**os.environ, "LIME_AGENT_RUNTIME": "pi"},
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert refused.returncode == 2
    assert "lime focus: owner-only; refused inside a Role session" in refused.stderr
    assert (h / "focus.md").read_bytes() == before
    assert log(h) == before_log

    allowed = subprocess.run(
        [sys.executable, "-m", "lime", *argv],
        env={k: v for k, v in os.environ.items() if k != "LIME_AGENT_RUNTIME"},
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert allowed.returncode == 0, allowed.stderr
    assert log(h)[0] == message


# --- push failure --------------------------------------------------------


def test_failed_push_keeps_the_commit_and_the_next_write_pushes_both(env, capsys):
    h = make_home(env)
    _git(h, "remote", "set-url", "origin", str(env.tmp / "missing.git"))
    assert cli.main(["focus", "add", "A"]) == 0
    out = capsys.readouterr()
    assert out.out == "f1 added · committed\n"
    lines = [l for l in out.err.splitlines() if l.startswith("dashboard: push failed (")]
    assert len(lines) == 1
    assert lines[0].endswith("· 1 commits not pushed")
    assert (h / "push-error").exists()
    assert log(h)[0] == "focus: add f1 A"

    _git(h, "remote", "set-url", "origin", str(env.tmp / "remote.git"))
    assert cli.main(["focus", "add", "B"]) == 0
    out2 = capsys.readouterr()
    assert out2.out.endswith("· pushed\n")
    assert not (h / "push-error").exists()
    assert _git(env.tmp / "remote.git", "rev-list", "--count", "main").stdout.strip() == "3"


# --- writer guard (AST) --------------------------------------------------


def focus_md_refs(source):
    """The enclosing function names (None at module level) that name `focus.md`
    or `FOCUS_FILE`, skipping module, class and function docstrings."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            first = body[0] if body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
    refs = set()

    def visit(node, fn):
        if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            fn = node.name
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and (node.value == "focus.md" or node.value.endswith("/focus.md"))
        ):
            refs.add(fn)
        if isinstance(node, ast.Name) and node.id == "FOCUS_FILE":
            refs.add(fn)
        if isinstance(node, ast.Attribute) and node.attr == "FOCUS_FILE":
            refs.add(fn)
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, None)
    return refs


def test_only_focus_py_writes_focus_md():
    package = Path(home.__file__).parent
    found = set()
    for path in package.rglob("*.py"):
        if path.name == "focus.py":
            continue
        rel = path.relative_to(package).as_posix()
        for fn in focus_md_refs(path.read_text()):
            found.add((rel, fn))
    assert found <= {("home.py", "init_command")}, (
        "focus.md belongs to lime/focus.py; readers should use focus.load/focus.parse: "
        f"{found}"
    )


def test_focus_md_guard_flags_a_stray_writer():
    assert focus_md_refs(
        'def f(h):\n    (h / "focus.md").write_text("- [ ] f1 · x\\n")\n'
    ) == {"f"}


def test_summarize_is_not_owner_only_and_never_touches_focus_md(env, monkeypatch, capsys):
    make_home(env, "- [ ] f1 · A\n")
    monkeypatch.setenv("LIME_AGENT_RUNTIME", "pi")
    empty = env.tmp / "empty_projects"
    empty.write_text("")
    monkeypatch.setenv("LIME_PROJECTS", str(empty))

    focus_file = env.home / "focus.md"
    before = focus_file.read_bytes()
    log_before = log(env.home)

    assert cli.main(["focus", "summarize"]) == 0
    err = capsys.readouterr().err
    assert "owner-only" not in err
    assert focus_file.read_bytes() == before
    assert log(env.home) == log_before
