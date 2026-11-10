"""lime/overlap.py — launch/batch overlap detection against unmerged origin Runs (ticket 160)."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from lime import cli, overlap, tracer, tracker

from overlap_helpers import (
    Origin,
    close_local_ticket,
    git,
    ticket_text,
    unpushed_run,
)


def _candidate(where: str, *, blocked_by: str | None = None) -> tracker.Ticket:
    return tracker.parse(
        ticket_text(160, where, blocked_by=blocked_by), "160-candidate.md"
    )


# --- exact / directory-prefix / boundary conflicts ---


def test_live_overlap_reports_an_exact_path_conflict(tmp_path):
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/70-alpha", 70, "`src/a.py`")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        "lime/feature/70-alpha": ["src/a.py"]
    }


def test_live_overlap_directory_prefix_when_the_sibling_names_the_directory(tmp_path):
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/70-alpha", 70, "`src/`")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        "lime/feature/70-alpha": ["src/a.py"]
    }


def test_live_overlap_directory_prefix_when_the_candidate_names_the_directory(tmp_path):
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/70-alpha", 70, "`src/a.py`")
    assert overlap.live_overlap(origin.root, _candidate("`src/`")) == {
        "lime/feature/70-alpha": ["src/"]
    }


def test_live_overlap_ignores_segment_boundaries_and_extensions(tmp_path):
    origin = Origin(tmp_path)
    origin.publish_run(
        "lime/feature/70-alpha", 70, "- `src/ab.py`\n- `foo.py.bak`\n- `src/a`\n"
    )
    candidate = _candidate("- `src/a.py`\n- `foo.py`\n- `src/ab`\n")
    assert overlap.live_overlap(origin.root, candidate) == {}


def test_live_overlap_reports_every_conflicting_branch(tmp_path):
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/70-alpha", 70, "`src/a.py`")
    origin.publish_run("lime/feature/71-beta", 71, "`src/`")
    origin.publish_run("lime/feature/72-gamma", 72, "`unrelated.py`")
    result = overlap.live_overlap(origin.root, _candidate("- `src/a.py`\n- `other.py`\n"))
    assert result == {
        "lime/feature/70-alpha": ["src/a.py"],
        "lime/feature/71-beta": ["src/a.py"],
    }


def test_live_overlap_reads_only_the_branch_own_ticket(tmp_path):
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/70-alpha", 70, "`src/a.py`")
    # 71 inherits 70's overlapping ticket in its tree but owns a disjoint one.
    origin.publish_run(
        "lime/feature/71-beta", 71, "`other.py`", base="lime/feature/70-alpha"
    )
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        "lime/feature/70-alpha": ["src/a.py"]
    }


def test_live_overlap_matches_the_complete_number_boundary(tmp_path):
    origin = Origin(tmp_path)
    # Branch 35 owns no `35-*.md`; only an inherited-looking `350-*.md` overlaps.
    origin.publish(
        "lime/feature/35-alpha",
        {".lime/tickets/350-beta.md": ticket_text(350, "`src/a.py`")},
    )
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}
    # The real three-digit branch still matches its own ticket.
    origin.publish_run("lime/feature/350-beta", 350, "`src/a.py`")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        "lime/feature/350-beta": ["src/a.py"]
    }


# --- waiver, merged tips, cache-free fetch/prune ---


def test_live_overlap_exempts_a_named_blocker(tmp_path):
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/70-alpha", 70, "`src/a.py`")
    candidate = _candidate("`src/a.py`", blocked_by=".lime/tickets/70-alpha.md")
    assert overlap.live_overlap(origin.root, candidate) == {}


@pytest.mark.parametrize("named", ["70-alpha.md", "70-alpha", "./.lime/tickets/70-alpha.md"])
def test_live_overlap_exemption_accepts_path_basename_and_stem(tmp_path, named):
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/70-alpha", 70, "`src/a.py`")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`", blocked_by=named)) == {}


def test_live_overlap_exemption_is_not_a_substring_match(tmp_path):
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/71-beta", 71, "`src/a.py`")
    candidate = _candidate("`src/a.py`", blocked_by=".lime/tickets/70-alpha.md")
    assert overlap.live_overlap(origin.root, candidate) == {
        "lime/feature/71-beta": ["src/a.py"]
    }


def test_live_overlap_excludes_a_branch_merged_into_the_default(tmp_path):
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/70-alpha", 70, "`src/a.py`")
    origin.merge_into_default("lime/feature/70-alpha", delete=False)
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


def test_live_overlap_discovers_a_newly_published_branch(tmp_path):
    origin = Origin(tmp_path)
    origin.fetch()  # root knows no lime branches yet
    origin.publish_run("lime/feature/70-alpha", 70, "`src/a.py`")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        "lime/feature/70-alpha": ["src/a.py"]
    }


def test_live_overlap_prunes_a_deleted_branch(tmp_path):
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/70-alpha", 70, "`src/a.py`")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) != {}
    origin.delete_branch("lime/feature/70-alpha")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


# --- no-op and failure boundaries ---


def test_live_overlap_returns_empty_without_an_origin(tmp_path):
    root = tmp_path / "local"
    root.mkdir()
    git(root, "init", "-q", "-b", "trunk")
    assert overlap.live_overlap(root, _candidate("`src/a.py`")) == {}


def test_live_overlap_returns_empty_without_touching_origin_when_no_paths(tmp_path):
    origin = Origin(tmp_path)
    git(origin.root, "remote", "set-url", "origin", str(tmp_path / "nope.git"))
    assert overlap.live_overlap(origin.root, _candidate("(repo root)")) == {}


def test_live_overlap_ignores_non_ticket_and_ticketless_branches(tmp_path):
    origin = Origin(tmp_path)
    origin.publish("lime/feature/70-alpha", {"src/a.py": "x\n"})  # no ticket
    origin.publish(
        "lime/feature/noslug",
        {".lime/tickets/70-alpha.md": ticket_text(70, "`src/a.py`")},  # no leading number
    )
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


def test_live_overlap_raises_on_a_fetch_failure(tmp_path):
    origin = Origin(tmp_path)
    git(origin.root, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    with pytest.raises(overlap.OverlapError):
        overlap.live_overlap(origin.root, _candidate("`src/a.py`"))


# --- project-local unpushed Runs (ticket 167) ---


def _run_key(run) -> str:
    return f"Run {run.run_id} ({run.branch})"


def _patch_row(root, run_id: str, **fields) -> None:
    con = tracer.connect(root / ".lime" / "runs" / "lime.db")
    try:
        sets = ", ".join(f"{key}=?" for key in fields)
        con.execute(f"UPDATE runs SET {sets} WHERE run_id=?", (*fields.values(), run_id))
        con.commit()
    finally:
        con.close()


def test_live_overlap_sees_unpushed_run(tmp_path):
    origin = Origin(tmp_path)
    run = unpushed_run(origin.root, 70, "`src/a.py`", run_id="deadbeef", process="dead")
    candidate = _candidate("- `src/a.py`\n- `other.py`\n")
    expected = {_run_key(run): ["src/a.py"]}
    # no remote Run ref exists; the local row alone holds the path
    assert overlap.live_overlap(origin.root, candidate) == expected
    # the branch's own carried ticket is authoritative: edit/remove the root file
    run.ticket.write_text("# 70 — edited\n")
    assert overlap.live_overlap(origin.root, candidate) == expected
    run.ticket.unlink()
    assert overlap.live_overlap(origin.root, candidate) == expected
    # repeated discovery recomputes from current state; nothing new is written
    runs_dir = origin.root / ".lime" / "runs"
    before = sorted(p.name for p in runs_dir.iterdir())
    assert overlap.live_overlap(origin.root, candidate) == expected
    assert sorted(p.name for p in runs_dir.iterdir()) == before


def test_live_overlap_unpushed_directory_prefix_hit(tmp_path):
    origin = Origin(tmp_path)
    run = unpushed_run(origin.root, 70, "`src/`", process="dead")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        _run_key(run): ["src/a.py"]
    }


ELIGIBLE_STATUSES = [
    status for status in tracer.RUN_TRANSITIONS if status not in ("deployed", "discarded")
]


@pytest.mark.parametrize("status", ELIGIBLE_STATUSES)
def test_live_overlap_unpushed_statuses(tmp_path, status):
    origin = Origin(tmp_path)
    run = unpushed_run(origin.root, 70, "`src/a.py`", status=status, process="dead")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        _run_key(run): ["src/a.py"]
    }


def test_live_overlap_unpushed_archived_still_holds(tmp_path):
    origin = Origin(tmp_path)
    run = unpushed_run(
        origin.root, 70, "`src/a.py`", status="fail", process="dead", archived=True
    )
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        _run_key(run): ["src/a.py"]
    }


@pytest.mark.parametrize("status", ["discarded", ["success", "deployed"]])
def test_live_overlap_unpushed_released_statuses(tmp_path, status):
    origin = Origin(tmp_path)
    unpushed_run(origin.root, 70, "`src/a.py`", status=status, process="dead")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


def _seed_birth(origin, number=70, where="`src/a.py`"):
    text = ticket_text(number, where)
    origin.commit_push({f".lime/tickets/{number}-toy.md": text})


def test_live_overlap_unpushed_birth_live_holds(tmp_path):
    origin = Origin(tmp_path)
    _seed_birth(origin)
    run = unpushed_run(origin.root, 70, "`src/a.py`", process="alive")
    # birth HEAD is already an ancestor of origin/trunk, but the Run is live
    assert run.base_sha == git(origin.root, "rev-parse", "origin/trunk")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        _run_key(run): ["src/a.py"]
    }


def test_live_overlap_unpushed_birth_window_holds_without_process(tmp_path):
    origin = Origin(tmp_path)
    _seed_birth(origin)
    run = unpushed_run(origin.root, 70, "`src/a.py`", status="running", process=None)
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        _run_key(run): ["src/a.py"]
    }


def test_live_overlap_unpushed_merged_stopped_releases(tmp_path):
    origin = Origin(tmp_path)
    run = unpushed_run(origin.root, 70, "`src/a.py`", status="fail", process="dead")
    origin.push_branch(run.branch)
    origin.merge_into_default(run.branch)
    origin.fetch()
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


def test_live_overlap_unpushed_stopped_unmerged_holds(tmp_path):
    origin = Origin(tmp_path)
    run = unpushed_run(origin.root, 70, "`src/a.py`", status="fail", process="dead")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        _run_key(run): ["src/a.py"]
    }


@pytest.mark.parametrize(
    "named",
    [
        ".lime/tickets/70-toy.md",
        "./.lime/tickets/70-toy.md",
        "70-toy.md",
        "70-toy",
    ],
)
def test_live_overlap_unpushed_exemptions(tmp_path, named):
    origin = Origin(tmp_path)
    unpushed_run(origin.root, 70, "`src/a.py`", process="dead")
    candidate = _candidate("`src/a.py`", blocked_by=named)
    assert overlap.live_overlap(origin.root, candidate) == {}


@pytest.mark.parametrize(
    "named",
    [
        ".lime/tickets/700-toy.md",
        "70-toy-x",
        ".lime/tickets/71-toy.md",
        "lime/feature/70-toy",
    ],
)
def test_live_overlap_unpushed_non_exemptions(tmp_path, named):
    origin = Origin(tmp_path)
    run = unpushed_run(origin.root, 70, "`src/a.py`", process="dead")
    candidate = _candidate("`src/a.py`", blocked_by=named)
    assert overlap.live_overlap(origin.root, candidate) == {_run_key(run): ["src/a.py"]}


def test_live_overlap_unpushed_path_boundaries(tmp_path):
    origin = Origin(tmp_path)
    unpushed_run(origin.root, 70, "- `src/ab.py`\n- `foo.py.bak`\n", process="dead")
    candidate = _candidate("- `src/a.py`\n- `foo.py`\n")
    assert overlap.live_overlap(origin.root, candidate) == {}


@pytest.mark.parametrize(
    "mutation",
    ["branch-null", "ticket-null", "ref-missing", "ticket-outside", "ticket-missing"],
)
def test_live_overlap_skips_unreadable_local_rows(tmp_path, mutation):
    origin = Origin(tmp_path)
    run = unpushed_run(origin.root, 70, "`src/a.py`", run_id="bad", process="dead")
    if mutation == "branch-null":
        _patch_row(origin.root, "bad", branch=None)
    elif mutation == "ticket-null":
        _patch_row(origin.root, "bad", ticket=None)
    elif mutation == "ref-missing":
        _patch_row(origin.root, "bad", branch="lime/feature/999-ghost")
    elif mutation == "ticket-outside":
        _patch_row(origin.root, "bad", ticket=str(tmp_path / "outside.md"))
    else:  # ticket-missing: names a ticket the branch never carried
        _patch_row(origin.root, "bad", ticket=str(origin.root / ".lime" / "tickets" / "71-nope.md"))
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


def test_live_overlap_skips_an_inherited_ticket(tmp_path):
    # Branch 70-other exists and carries `70-other.md`; the row claims `70-toy`, whose
    # canonical path is absent, so the branch-local read is skipped (no hold, no raise).
    origin = Origin(tmp_path)
    run = unpushed_run(
        origin.root, 70, "`src/a.py`", ticket_name="70-other.md",
        branch="lime/feature/70-other", run_id="bad", process="dead",
    )
    _patch_row(origin.root, "bad", ticket=str(origin.root / ".lime" / "tickets" / "70-toy.md"))
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


def test_live_overlap_skips_a_forced_local_read_failure(tmp_path, monkeypatch):
    origin = Origin(tmp_path)
    unpushed_run(origin.root, 70, "`src/a.py`", run_id="bad", process="dead")

    def boom(*args, **kwargs):
        raise overlap.OverlapError("local read failed")

    monkeypatch.setattr(overlap, "_show", boom)
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


def test_live_overlap_reports_valid_among_invalid_rows(tmp_path):
    origin = Origin(tmp_path)
    good = unpushed_run(origin.root, 70, "`src/a.py`", run_id="good", process="dead")
    unpushed_run(origin.root, 71, "`src/a.py`", run_id="bad", process="dead")
    _patch_row(origin.root, "bad", branch=None)
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        _run_key(good): ["src/a.py"]
    }


def test_live_overlap_unreadable_local_row_keeps_remote_conflict(tmp_path):
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/70-remote", 70, "`src/a.py`")
    unpushed_run(origin.root, 71, "`src/a.py`", run_id="bad", process="dead")
    _patch_row(origin.root, "bad", branch=None)
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        "lime/feature/70-remote": ["src/a.py"]
    }


def test_live_overlap_ignores_another_projects_rows(tmp_path, monkeypatch):
    origin = Origin(tmp_path)
    other = Origin(tmp_path / "other")
    unpushed_run(other.root, 70, "`src/a.py`", process="dead")
    monkeypatch.setenv("LIME_RUNS_DIR", str(other.root / ".lime" / "runs"))
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


def test_live_overlap_absent_db_is_not_created(tmp_path):
    origin = Origin(tmp_path)
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}
    assert not (origin.root / ".lime" / "runs" / "lime.db").exists()


def test_live_overlap_no_origin_still_reads_local_rows(tmp_path):
    root = tmp_path / "local"
    root.mkdir()
    git(root, "init", "-q", "-b", "trunk")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "app.py").write_text("v1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    run = unpushed_run(root, 70, "`src/a.py`", process="dead")
    assert overlap.live_overlap(root, _candidate("`src/a.py`")) == {
        _run_key(run): ["src/a.py"]
    }
    assert overlap.live_overlap(root, _candidate("`unrelated.py`")) == {}


def test_live_overlap_local_and_remote_same_branch(tmp_path):
    origin = Origin(tmp_path)
    run = unpushed_run(origin.root, 70, "`src/a.py`", run_id="deadbeef", process="dead")
    stem = run.branch.split("/")[-1]
    origin.publish(
        run.branch,
        {f".lime/tickets/{stem}.md": ticket_text(70, "- `src/a.py`\n- `src/b.py`\n")},
    )
    # one Run-labelled entry, candidate-order union of both tips' hits
    assert overlap.live_overlap(
        origin.root, _candidate("- `src/a.py`\n- `src/b.py`\n")
    ) == {_run_key(run): ["src/a.py", "src/b.py"]}


def test_live_overlap_remote_only_keeps_branch_key(tmp_path):
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/70-alpha", 70, "`src/a.py`")
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        "lime/feature/70-alpha": ["src/a.py"]
    }


def test_live_overlap_trace_read_failure(tmp_path):
    origin = Origin(tmp_path)
    runs = origin.root / ".lime" / "runs"
    runs.mkdir(parents=True)
    (runs / "lime.db").write_bytes(b"this is not a sqlite database")
    with pytest.raises(overlap.OverlapError):
        overlap.live_overlap(origin.root, _candidate("`src/a.py`"))


# --- a finished Run's deleted ticket keeps its hold (ticket 273) ---


def _fail_git(monkeypatch, verb):
    real = overlap._run

    def flaky(root, *args, **kw):
        if args and args[0] == verb:
            return subprocess.CompletedProcess(["git", *args], 128, "", f"fatal: {verb} boom")
        return real(root, *args, **kw)

    monkeypatch.setattr(overlap, "_run", flaky)


def test_live_overlap_remote_holds_after_the_tip_deleted_its_ticket(tmp_path):
    origin = Origin(tmp_path)
    branch = "lime/feature/70-alpha"
    origin.publish_run(branch, 70, "- `src/a.py`\n- `src/b.py`\n")
    origin.close_ticket(branch)
    origin.fetch()
    assert "70-alpha" not in git(
        origin.root,
        "ls-tree",
        "-r",
        "--name-only",
        f"origin/{branch}",
        "--",
        ".lime/tickets/",
    )
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        branch: ["src/a.py"]
    }


def test_live_overlap_reads_the_last_carried_ticket_version(tmp_path):
    origin = Origin(tmp_path)
    branch = "lime/feature/70-alpha"
    origin.publish_run(branch, 70, "`src/old.py`")
    origin.publish(
        branch,
        {".lime/tickets/70-alpha.md": ticket_text(70, "`src/a.py`", title="70-alpha")},
        base=branch,
    )
    origin.close_ticket(branch)
    # the newest carried version names `src/a.py`, not the older `src/old.py`
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        branch: ["src/a.py"]
    }
    assert overlap.live_overlap(origin.root, _candidate("`src/old.py`")) == {}


@pytest.mark.parametrize("variant", ["other-stem", "default-only"])
def test_live_overlap_deleted_ticket_never_carried_holds_nothing(tmp_path, variant):
    origin = Origin(tmp_path)
    branch = "lime/feature/70-alpha"
    if variant == "other-stem":
        # the branch carried only a same-number file with another slug
        origin.publish(branch, {".lime/tickets/70-other.md": ticket_text(70, "`src/a.py`")})
        origin.close_ticket(branch, stem="70-other")
    else:
        # the only commits holding the file are reachable from origin/trunk
        origin.commit_push({".lime/tickets/70-alpha.md": ticket_text(70, "`src/a.py`")})
        origin.close_ticket(branch, base=origin.default)
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


def test_live_overlap_deleted_ticket_releases_on_merge(tmp_path):
    origin = Origin(tmp_path)
    branch = "lime/feature/70-alpha"
    origin.publish_run(branch, 70, "`src/a.py`")
    origin.close_ticket(branch)
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        branch: ["src/a.py"]
    }
    origin.merge_into_default(branch, delete=False)
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


@pytest.mark.parametrize("verb", ["rev-list", "cat-file"])
def test_live_overlap_remote_history_read_failure_raises(tmp_path, monkeypatch, verb):
    origin = Origin(tmp_path)
    branch = "lime/feature/70-alpha"
    origin.publish_run(branch, 70, "`src/a.py`")
    origin.close_ticket(branch)
    _fail_git(monkeypatch, verb)
    with pytest.raises(overlap.OverlapError):
        overlap.live_overlap(origin.root, _candidate("`src/a.py`"))


def test_live_overlap_fetch_failure_names_the_git_error(tmp_path, monkeypatch):
    origin = Origin(tmp_path)
    _fail_git(monkeypatch, "fetch")
    # The batch's streak lines quote this text, so it must carry git's own stderr.
    with pytest.raises(overlap.OverlapError) as excinfo:
        overlap.live_overlap(origin.root, _candidate("`src/a.py`"))
    assert str(excinfo.value) == "git fetch --prune origin failed: fatal: fetch boom"


@pytest.mark.parametrize("status", ELIGIBLE_STATUSES)
def test_live_overlap_local_row_holds_after_its_tip_deleted_its_ticket(tmp_path, status):
    origin = Origin(tmp_path)
    run = unpushed_run(origin.root, 70, "`src/a.py`", status=status, process="dead")
    close_local_ticket(run)
    run.ticket.unlink()
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        _run_key(run): ["src/a.py"]
    }


def test_live_overlap_finished_run_on_both_sources_keeps_one_hold(tmp_path):
    # the 265 shape: commit_docs deleted the ticket, then push_pr published the branch
    origin = Origin(tmp_path)
    run = unpushed_run(
        origin.root, 70, "`src/a.py`", run_id="deadbeef", status="success", process="dead"
    )
    close_local_ticket(run)
    origin.push_branch(run.branch)
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        _run_key(run): ["src/a.py"]
    }


def test_live_overlap_no_origin_local_row_holds_after_its_tip_deleted_its_ticket(tmp_path):
    root = tmp_path / "local"
    root.mkdir()
    git(root, "init", "-q", "-b", "trunk")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "app.py").write_text("v1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    run = unpushed_run(root, 70, "`src/a.py`", process="dead")
    close_local_ticket(run)
    assert overlap.live_overlap(root, _candidate("`src/a.py`")) == {
        _run_key(run): ["src/a.py"]
    }
    assert overlap.live_overlap(root, _candidate("`unrelated.py`")) == {}


def test_live_overlap_local_deleted_ticket_releases_on_merge(tmp_path):
    origin = Origin(tmp_path)
    run = unpushed_run(origin.root, 70, "`src/a.py`", status="fail", process="dead")
    close_local_ticket(run)
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        _run_key(run): ["src/a.py"]
    }
    origin.push_branch(run.branch)
    origin.merge_into_default(run.branch)
    origin.fetch()
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


def test_live_overlap_runs_rm_releases_a_finished_runs_hold(tmp_path, monkeypatch):
    origin = Origin(tmp_path)
    run = unpushed_run(
        origin.root, 70, "`src/a.py`", run_id="deadbeef", status="success", process="dead"
    )
    close_local_ticket(run)
    origin.push_branch(run.branch)
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) != {}
    monkeypatch.chdir(origin.root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.worktree, "CC_PROJECTS", tmp_path / "cc")
    monkeypatch.setattr(cli.config, "load", lambda root=Path("."): {"commands": {}})
    assert cli.main(["runs", "rm", "deadbeef"]) == 0
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {}


def test_live_overlap_local_history_read_failure_skips_the_row_alone(tmp_path, monkeypatch):
    origin = Origin(tmp_path)
    good = unpushed_run(origin.root, 70, "`src/a.py`", run_id="good", process="dead")
    bad = unpushed_run(origin.root, 71, "`src/a.py`", run_id="bad", process="dead")
    close_local_ticket(bad)
    _fail_git(monkeypatch, "rev-list")
    # `good`'s tip still carries its ticket (no history read); `bad`'s failed read is local
    assert overlap.live_overlap(origin.root, _candidate("`src/a.py`")) == {
        _run_key(good): ["src/a.py"]
    }
