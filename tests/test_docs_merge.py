"""Tests for lime/docs_merge.py — every conflict is built by a real `git merge` in tmp_path."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from lime import docs_merge
from lime.docs_merge import resolve


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def _conflict(tmp_path, base, main, branch, *, name="CONTEXT.md", style="diff3"):
    """Commit `base`, then `branch` on a feature branch and `main` on a work branch, then merge feature into work."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / name).write_text(base)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / name).write_text(branch)
    _git(repo, "commit", "-qam", "branch")
    _git(repo, "checkout", "-q", "work")
    (repo / name).write_text(main)
    _git(repo, "commit", "-qam", "main")
    r = _git(repo, "-c", f"merge.conflictStyle={style}", "merge", "-q", "feature")
    assert r.returncode != 0
    return (repo / name).read_text()


def _entry(term):
    return f"- **{term}** — short text.\n"


def _glossary(*entries):
    return "# Glossary\n" + "".join(entries)


def test_e3590b3_empty_base_keeps_main_then_branch(tmp_path):
    focus = _entry("Focus")
    workspace = _entry("Workspace")
    ticket = _entry("Ticket close")
    live = _entry("Live Focus")
    base = _glossary(focus, workspace)
    main = _glossary(focus, ticket, workspace)
    branch = _glossary(focus, live, workspace)
    text = _conflict(tmp_path, base, main, branch)
    assert "\n||||||| " in text
    assert resolve(text) == _glossary(focus, ticket, live, workspace)


def test_263_empty_base_two_main_lines_then_branch_line(tmp_path):
    notch = _entry("Notch")
    workspace = _entry("Workspace")
    run_line = _entry("Run line")
    run_canvas = _entry("Run canvas")
    terminal = _entry("Terminal")
    base = _glossary(notch, workspace)
    main = _glossary(notch, run_line, run_canvas, workspace)
    branch = _glossary(notch, terminal, workspace)
    text = _conflict(tmp_path, base, main, branch)
    assert "\n||||||| " in text
    assert resolve(text) == _glossary(notch, run_line, run_canvas, terminal, workspace)


def test_dc3ecd8_branch_rewrote_main_added_after(tmp_path):
    dashboard = _entry("Dashboard")
    dashboard_branch = _entry("Dashboard′")
    home = _entry("Home")
    last = _entry("Last visit")
    file_api = _entry("File API")
    trace = _entry("Trace")
    base = _glossary(dashboard, trace)
    main = _glossary(dashboard, file_api, trace)
    branch = _glossary(dashboard_branch, home, last, trace)
    text = _conflict(tmp_path, base, main, branch)
    assert "\n||||||| " in text
    assert resolve(text) == _glossary(dashboard_branch, home, last, file_api, trace)


def test_bbd177b_context_main_added_before_branch_rewrote(tmp_path):
    ledger = _entry("Ledger")
    reservation = _entry("Reservation")
    reservation_branch = _entry("Reservation′")
    adr = _entry("ADR number")
    where = _entry("Where paths")
    live_overlap = _entry("Live overlap")
    guard = _entry("Overlap guard")
    held = _entry("Held")
    run = _entry("Run")
    base = _glossary(ledger, reservation, run)
    main = _glossary(ledger, where, live_overlap, guard, held, reservation, run)
    branch = _glossary(ledger, reservation_branch, adr, run)
    text = _conflict(tmp_path, base, main, branch)
    assert "\n||||||| " in text
    assert resolve(text) == _glossary(
        ledger, reservation_branch, adr, where, live_overlap, guard, held, run
    )


def test_bbd177b_agents_both_rewrote_returns_none(tmp_path):
    numbering = (
        "- `lime/numbering.py` — `next_number()` (git as ledger) + `reserve()` (CAS ref on origin)\n"
    )
    numbering_main = numbering[:-1] + "; `lime/overlap.py` — `live_overlap()` (guards a launch)\n"
    numbering_branch = (
        "- `lime/numbering.py` — `next_adr_number()` (git as ledger) + `reserve_adr()` (CAS ref on origin)\n"
    )
    other = "- something else\n"
    base = "## Layout\n" + numbering + other
    main = "## Layout\n" + numbering_main + other
    branch = "## Layout\n" + numbering_branch + other
    text = _conflict(tmp_path, base, main, branch, name="AGENTS.md")
    assert resolve(text) is None


def test_two_hunks_around_one_kept_line_both_resolve(tmp_path):
    a, m, r, n, z = (_entry(t) for t in ("A", "M", "R", "N", "Z"))
    b, c = _entry("B"), _entry("C")
    base = _glossary(a, r, z)
    main = _glossary(a, m, r, n, z)
    branch = _glossary(a, b, r, c, z)
    text = _conflict(tmp_path, base, main, branch)
    assert text.count("\n||||||| ") == 2
    assert resolve(text) == _glossary(a, m, b, r, n, c, z)


def test_one_unresolvable_hunk_refuses_the_whole_file(tmp_path):
    a = _entry("A")
    l1, l2, l3 = _entry("L1"), _entry("L2"), _entry("L3")
    base = _glossary(a, l1, l2, l3, "- **Conflict** — base text.\n", _entry("Tail"))
    main = _glossary(
        a, _entry("Main insert"), l1, l2, l3, "- **Conflict** — main text.\n", _entry("Tail")
    )
    branch = _glossary(
        a, _entry("Branch insert"), l1, l2, l3, "- **Conflict** — branch text.\n", _entry("Tail")
    )
    text = _conflict(tmp_path, base, main, branch)
    assert resolve(text) is None


def test_no_base_section_returns_none(tmp_path):
    focus = _entry("Focus")
    workspace = _entry("Workspace")
    base = _glossary(focus, workspace)
    main = _glossary(focus, _entry("Ticket close"), workspace)
    branch = _glossary(focus, _entry("Live Focus"), workspace)
    text = _conflict(tmp_path, base, main, branch)
    assert "\n||||||| " in text
    lines = text.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("|||||||"))
    end = next(i for i, line in enumerate(lines) if line.rstrip("\n") == "=======")
    del lines[start:end]
    assert "|||||||" not in "".join(lines)
    assert resolve("".join(lines)) is None


@pytest.mark.parametrize("action", ["drop_close", "drop_sep", "drop_open", "dup_base"])
def test_malformed_markers_return_none(tmp_path, action):
    focus = _entry("Focus")
    workspace = _entry("Workspace")
    base = _glossary(focus, workspace)
    main = _glossary(focus, _entry("Ticket close"), workspace)
    branch = _glossary(focus, _entry("Live Focus"), workspace)
    text = _conflict(tmp_path, base, main, branch)
    lines = text.splitlines(keepends=True)
    mutated = []
    for line in lines:
        if action == "drop_open" and line.startswith("<<<<<<<"):
            continue
        if action == "drop_sep" and line.rstrip("\n") == "=======":
            continue
        if action == "drop_close" and line.startswith(">>>>>>>"):
            continue
        mutated.append(line)
        if action == "dup_base" and line.startswith("|||||||"):
            mutated.append(line)
    assert resolve("".join(mutated)) is None


def test_duplicate_term_inside_a_hunk_returns_none(tmp_path):
    notch = _entry("Notch")
    workspace = _entry("Workspace")
    base = _glossary(notch, workspace)
    main = _glossary(notch, "- **Run line** — main text.\n", workspace)
    branch = _glossary(notch, "- **Run line** — branch text.\n", workspace)
    text = _conflict(tmp_path, base, main, branch)
    assert resolve(text) is None


def test_hunk_term_duplicating_a_term_outside_the_hunk_returns_none(tmp_path):
    notch = _entry("Notch")
    workspace = _entry("Workspace")
    base = _glossary(notch, workspace)
    main = _glossary(notch, _entry("Run line"), workspace)
    branch = _glossary(notch, "- **Workspace** — added by branch.\n", workspace)
    text = _conflict(tmp_path, base, main, branch)
    assert resolve(text) is None


def test_preexisting_duplicate_outside_hunks_returns_none(tmp_path):
    old_a = "- **Old** — a\n"
    old_b = "- **Old** — b\n"
    focus = _entry("Focus")
    workspace = _entry("Workspace")
    ticket = _entry("Ticket close")
    live = _entry("Live Focus")
    base = _glossary(old_a, old_b, focus, workspace)
    main = _glossary(old_a, old_b, focus, ticket, workspace)
    branch = _glossary(old_a, old_b, focus, live, workspace)
    text = _conflict(tmp_path, base, main, branch)
    assert resolve(text) is None


def test_text_without_hunks_is_returned_unchanged():
    text = _glossary(_entry("Focus"), _entry("Workspace"))
    assert resolve(text) == text


def test_resolve_output_has_no_markers_and_is_stable(tmp_path):
    dashboard = _entry("Dashboard")
    dashboard_branch = _entry("Dashboard′")
    home = _entry("Home")
    last = _entry("Last visit")
    file_api = _entry("File API")
    trace = _entry("Trace")
    base = _glossary(dashboard, trace)
    main = _glossary(dashboard, file_api, trace)
    branch = _glossary(dashboard_branch, home, last, trace)
    text = _conflict(tmp_path, base, main, branch)
    out = resolve(text)
    assert out is not None
    assert "<<<<<<<" not in out and "|||||||" not in out and ">>>>>>>" not in out
    assert resolve(out) == out


def test_docs_merge_reads_no_files_and_runs_no_git():
    tree = ast.parse(Path(docs_merge.__file__).read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    assert modules <= {"__future__", "collections", "config"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = ast.unparse(node.func)
            assert func != "open"
            assert not func.endswith(
                ("read_text", "read_bytes", "run", "Popen", "check_output")
            )
    # Behavioural half: cases 1-14 call `resolve` on a str only.
