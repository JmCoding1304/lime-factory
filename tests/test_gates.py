"""Validation gates: verify the envelope's CLAIMS, never guesses (spec §8)."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from lime import gates, git_helper
from lime.data_types import (
    BuildOutput,
    DesignOutput,
    DesignVariant,
    DocumentOutput,
    ResearchOutput,
    ResearchSource,
    ReviewFinding,
    ReviewOutput,
)


def run_at(repo_root) -> SimpleNamespace:
    return SimpleNamespace(repo_root=repo_root)


def pinned_run_at(repo_root, base_sha) -> SimpleNamespace:
    """A Run carrying the launch-time base_sha pin (ADR 0024)."""
    return SimpleNamespace(repo_root=repo_root, base_sha=base_sha)


# --- artifacts_exist ---


def test_artifacts_exist_passes_for_a_real_file_and_fails_for_a_missing_one(tmp_path):
    (tmp_path / "a.md").write_text("hi")
    envelope = BuildOutput(status="success", artifacts=["a.md", "missing.md"])
    report = gates.artifacts_exist(envelope, run_at(tmp_path))
    assert not report.passed
    assert report.violations == ["missing.md: declared artifact does not exist"]


def test_artifacts_exist_resolves_relative_to_repo_root(tmp_path):
    (tmp_path / "a.md").write_text("hi")
    envelope = BuildOutput(status="success", artifacts=["a.md"])
    report = gates.artifacts_exist(envelope, run_at(tmp_path))
    assert report.passed


# --- diff_matches_claims ---


def test_diff_matches_claims_fails_when_a_claimed_file_does_not_exist(tmp_path):
    (tmp_path / "a.py").write_text("x")
    envelope = BuildOutput(status="success", changed_files=["a.py", "ghost.py"])
    report = gates.diff_matches_claims(envelope, run_at(tmp_path))
    assert report.violations == ["ghost.py: claimed changed file does not exist"]


def _git(*args: str, cwd: Path) -> str:
    """A checked, local-only git call — no remote, no auth."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture
def claims_repo(tmp_path, monkeypatch):
    """A throwaway repo with one commit holding gone.py and keep.py."""
    monkeypatch.chdir(tmp_path)
    _git("init", "-b", "claims-test", cwd=tmp_path)
    _git("config", "user.name", "claims-test", cwd=tmp_path)
    _git("config", "user.email", "claims-test@example.invalid", cwd=tmp_path)
    (tmp_path / "gone.py").write_text("gone\n")
    (tmp_path / "keep.py").write_text("keep\n")
    _git("add", "gone.py", "keep.py", cwd=tmp_path)
    _git("commit", "-m", "seed", cwd=tmp_path)
    return tmp_path, _git("rev-parse", "HEAD", cwd=tmp_path)


@pytest.mark.parametrize("kind", ["unstaged", "staged", "committed"])
@pytest.mark.parametrize("claim_form", ["relative", "absolute"])
def test_diff_matches_claims_accepts_deletion_since_pinned_base(
    claims_repo, kind, claim_form
):
    repo, base_sha = claims_repo
    (repo / "gone.py").unlink()
    if kind == "staged":
        _git("add", "-A", cwd=repo)
    elif kind == "committed":
        _git("add", "-A", cwd=repo)
        _git("commit", "-m", "delete gone.py", cwd=repo)
        # A later commit keeps HEAD and HEAD~1 from standing in for the pin.
        (repo / "keep.py").write_text("keep changed\n")
        _git("commit", "-am", "change keep.py", cwd=repo)
    claim = "gone.py" if claim_form == "relative" else str(repo / "gone.py")
    envelope = BuildOutput(status="success", changed_files=[claim])
    report = gates.diff_matches_claims(envelope, pinned_run_at(repo, base_sha))
    assert report.passed, report.violations
    check = {c.item: c for c in report.checks}[claim]
    assert check.ok
    assert base_sha in check.note
    assert "gone.py" in git_helper.diff_files(base_sha)


def test_diff_matches_claims_rejects_missing_path_outside_pinned_diff(tmp_path, monkeypatch):
    calls = []

    def fake_diff(base):
        calls.append(base)
        return ["gone.py"]

    monkeypatch.setattr(gates.git_helper, "diff_files", fake_diff)
    envelope = BuildOutput(status="success", changed_files=["gone.py", "ghost.py"])
    report = gates.diff_matches_claims(envelope, pinned_run_at(tmp_path, "pin123"))
    assert report.violations == ["ghost.py: claimed changed file does not exist"]
    assert calls == ["pin123"]


def test_diff_matches_claims_existing_file_needs_no_diff(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x")

    def boom(base):
        raise AssertionError("no diff should be measured when every claim exists")

    monkeypatch.setattr(gates.git_helper, "diff_files", boom)
    envelope = BuildOutput(status="success", changed_files=["a.py"])
    report = gates.diff_matches_claims(envelope, pinned_run_at(tmp_path, "pin123"))
    assert report.passed
    assert report.checks[0].note == "exists, 1B"

    empty = BuildOutput(status="success", changed_files=[])
    empty_report = gates.diff_matches_claims(empty, pinned_run_at(tmp_path, "pin123"))
    assert empty_report.passed
    assert empty_report.checks == []


@pytest.mark.parametrize("pin", ["absent", None, ""])
def test_diff_matches_claims_without_pin_preserves_existence_checks(tmp_path, monkeypatch, pin):
    (tmp_path / "a.py").write_text("x")

    def boom(base):
        raise AssertionError("no diff may be inspected without a pin")

    monkeypatch.setattr(gates.git_helper, "diff_files", boom)
    run = run_at(tmp_path)
    if pin != "absent":
        run.base_sha = pin
    envelope = BuildOutput(status="success", changed_files=["a.py", "ghost.py"])
    report = gates.diff_matches_claims(envelope, run)
    assert report.violations == ["ghost.py: claimed changed file does not exist"]


def test_diff_matches_claims_does_not_reuse_previous_measurement(tmp_path, monkeypatch):
    answers = iter([["gone.py"], []])
    calls = []

    def fake_diff(base):
        calls.append(base)
        return next(answers)

    monkeypatch.setattr(gates.git_helper, "diff_files", fake_diff)
    envelope = BuildOutput(status="success", changed_files=["gone.py"])
    run = pinned_run_at(tmp_path, "pin123")
    assert gates.diff_matches_claims(envelope, run).passed
    assert gates.diff_matches_claims(envelope, run).violations == [
        "gone.py: claimed changed file does not exist"
    ]
    assert calls == ["pin123", "pin123"]


def test_diff_matches_claims_propagates_diff_inspection_failure(tmp_path, monkeypatch):
    def boom(base):
        raise RuntimeError(f"git diff --name-only {base} failed")

    monkeypatch.setattr(gates.git_helper, "diff_files", boom)
    envelope = BuildOutput(status="success", changed_files=["gone.py"])
    with pytest.raises(RuntimeError):
        gates.diff_matches_claims(envelope, pinned_run_at(tmp_path, "pin123"))


# --- changed_docs_exist ---


def test_changed_docs_exist_fails_when_a_claimed_doc_does_not_exist(tmp_path):
    (tmp_path / "AGENTS.md").write_text("x")
    envelope = DocumentOutput(status="success", changed_docs=["AGENTS.md", "ghost.md"])
    report = gates.changed_docs_exist(envelope, run_at(tmp_path))
    assert report.violations == ["ghost.md: claimed changed doc does not exist"]


def test_changed_docs_exist_passes_when_nothing_was_claimed_changed(tmp_path):
    envelope = DocumentOutput(status="success", changed_docs=[])
    report = gates.changed_docs_exist(envelope, run_at(tmp_path))
    assert report.passed


# --- sources_cited ---


def test_sources_cited_fails_when_no_brief_artifact_was_declared():
    envelope = ResearchOutput(
        status="success", artifacts=[], sources=[ResearchSource(url="https://example.com", note="ok")]
    )
    report = gates.sources_cited(envelope, run_at("."))
    assert not report.passed
    assert "brief: no brief artifact declared" in report.violations


def test_sources_cited_fails_when_no_source_was_reported():
    envelope = ResearchOutput(status="success", artifacts=["research-x.md"], sources=[])
    report = gates.sources_cited(envelope, run_at("."))
    assert not report.passed
    assert report.violations == ["sources: no sources reported"]


def test_sources_cited_fails_when_a_source_has_no_url():
    envelope = ResearchOutput(
        status="success",
        artifacts=["research-x.md"],
        sources=[ResearchSource(url="https://example.com", note="ok"), ResearchSource(url="", note="oops")],
    )
    report = gates.sources_cited(envelope, run_at("."))
    assert not report.passed
    assert any("source has no url" in v for v in report.violations)


def test_sources_cited_passes_with_a_brief_and_a_url_on_every_source():
    envelope = ResearchOutput(
        status="success",
        artifacts=["research-x.md"],
        sources=[ResearchSource(url="https://example.com", note="settled it")],
    )
    report = gates.sources_cited(envelope, run_at("."))
    assert report.passed


# --- verdict_consistent ---


def test_verdict_consistent_rejects_approval_with_blocking_items():
    envelope = ReviewOutput(status="success", approved=True, blocking=["still broken"])
    report = gates.verdict_consistent(envelope, run_at("."))
    assert not report.passed
    assert any("blocking" in v for v in report.violations)


def test_verdict_consistent_rejects_approval_with_unmet_findings():
    envelope = ReviewOutput(
        status="success",
        approved=True,
        findings=[ReviewFinding(requirement="does X", met=False, evidence="missing")],
    )
    report = gates.verdict_consistent(envelope, run_at("."))
    assert not report.passed


def test_verdict_consistent_rejects_approval_with_trust_violations():
    envelope = ReviewOutput(status="success", approved=True, trust_violations=["a.py:1 — .env read"])
    report = gates.verdict_consistent(envelope, run_at("."))
    assert not report.passed
    assert any("trust_violations" in v for v in report.violations)


def test_verdict_consistent_rejects_a_rejection_that_names_no_problem():
    envelope = ReviewOutput(status="success", approved=False)
    report = gates.verdict_consistent(envelope, run_at("."))
    assert not report.passed
    assert any("no blocking item" in v for v in report.violations)


def test_verdict_consistent_passes_a_clean_approval():
    envelope = ReviewOutput(
        status="success",
        approved=True,
        findings=[ReviewFinding(requirement="does X", met=True, evidence="a.py:1")],
    )
    report = gates.verdict_consistent(envelope, run_at("."))
    assert report.passed


def test_verdict_consistent_passes_a_supported_rejection():
    envelope = ReviewOutput(status="success", approved=False, blocking=["missing tests"])
    report = gates.verdict_consistent(envelope, run_at("."))
    assert report.passed


# --- route_dir_in_fence / variant_count / notes_locked (ticket 38) ---


def test_route_dir_in_fence_passes_a_matching_route_and_url_that_exists(tmp_path):
    (tmp_path / "app" / "proto-hero").mkdir(parents=True)
    gate = gates.route_dir_in_fence("hero")
    envelope = DesignOutput(status="success", route_dir="app/proto-hero/", url_path="/proto-hero")
    report = gate(envelope, run_at(tmp_path))
    assert report.passed


def test_route_dir_in_fence_fails_a_route_for_a_different_slug():
    gate = gates.route_dir_in_fence("hero")
    envelope = DesignOutput(status="success", route_dir="app/proto-other/", url_path="/proto-hero")
    report = gate(envelope, run_at("."))
    assert not report.passed
    assert any("route_dir" in v for v in report.violations)


def test_route_dir_in_fence_fails_a_mismatched_url_path(tmp_path):
    (tmp_path / "app" / "proto-hero").mkdir(parents=True)
    gate = gates.route_dir_in_fence("hero")
    envelope = DesignOutput(status="success", route_dir="app/proto-hero/", url_path="/proto-hero-x")
    report = gate(envelope, run_at(tmp_path))
    assert not report.passed
    assert any("url_path" in v for v in report.violations)


def test_route_dir_in_fence_fails_when_nothing_was_declared():
    gate = gates.route_dir_in_fence("hero")
    report = gate(DesignOutput(status="success"), run_at("."))
    assert not report.passed


def test_route_dir_in_fence_fails_a_correctly_named_route_that_does_not_exist_on_disk(tmp_path):
    """A JSON-only claim — the designer wrote nothing at all — must not pass the fence
    just because the name and URL happen to line up (17-designer-role.md)."""
    gate = gates.route_dir_in_fence("hero")
    envelope = DesignOutput(status="success", route_dir="app/proto-hero/", url_path="/proto-hero")
    report = gate(envelope, run_at(tmp_path))
    assert not report.passed
    assert any("does not exist on disk" in v for v in report.violations)


def test_variant_count_passes_two_or_three():
    envelope = DesignOutput(
        status="success",
        variants=[DesignVariant(id="a"), DesignVariant(id="b")],
    )
    assert gates.variant_count(envelope, run_at(".")).passed


def test_variant_count_fails_a_single_variant():
    envelope = DesignOutput(status="success", variants=[DesignVariant(id="a")])
    report = gates.variant_count(envelope, run_at("."))
    assert not report.passed
    assert "1 variant(s)" in report.violations[0]


def test_variant_count_fails_more_than_three():
    envelope = DesignOutput(
        status="success",
        variants=[DesignVariant(id=x) for x in "abcd"],
    )
    assert not gates.variant_count(envelope, run_at(".")).passed


def test_notes_locked_fails_when_no_notes_path_declared():
    report = gates.notes_locked(DesignOutput(status="success"), run_at("."))
    assert not report.passed
    assert report.violations == ["notes_path: no notes_path declared"]


def test_notes_locked_fails_when_the_declared_file_does_not_exist(tmp_path):
    envelope = DesignOutput(status="success", notes_path="NOTES.md")
    report = gates.notes_locked(envelope, run_at(tmp_path))
    assert not report.passed
    assert "does not exist" in report.violations[0]


def test_notes_locked_fails_when_a_section_is_missing(tmp_path):
    (tmp_path / "NOTES.md").write_text("## fixed\nx\n## free\ny\n")
    envelope = DesignOutput(status="success", notes_path="NOTES.md")
    report = gates.notes_locked(envelope, run_at(tmp_path))
    assert not report.passed
    assert report.violations == ["open: missing"]


def test_notes_locked_passes_all_three_sections_present(tmp_path):
    (tmp_path / "NOTES.md").write_text("## Fixed\nx\n## Free\ny\n## Open\nz\n")
    envelope = DesignOutput(status="success", notes_path="NOTES.md")
    report = gates.notes_locked(envelope, run_at(tmp_path))
    assert report.passed


def test_notes_locked_ignores_incidental_prose_containing_the_section_words(tmp_path):
    """A bare substring check would let "unfixed" and "opened" count as sections —
    only an actual `## <section>` heading should."""
    (tmp_path / "NOTES.md").write_text(
        "The layout is still unfixed and the shop is opened.\n## Free\ncopy\n"
    )
    envelope = DesignOutput(status="success", notes_path="NOTES.md")
    report = gates.notes_locked(envelope, run_at(tmp_path))
    assert not report.passed
    assert sorted(report.violations) == ["fixed: missing", "open: missing"]


# --- tests_pass ---


def test_tests_pass_runs_at_the_repo_root_and_reports_exit_status(tmp_path):
    gate = gates.tests_pass("exit 0")
    report = gate(BuildOutput(status="success"), run_at(tmp_path))
    assert report.passed


def test_tests_pass_captures_output_on_failure(tmp_path):
    gate = gates.tests_pass("echo boom && exit 1")
    report = gate(BuildOutput(status="success"), run_at(tmp_path))
    assert not report.passed
    assert "boom" in report.violations[0]


# --- verdict_consistent: no layout (ticket 192) ---


def test_verdict_consistent_records_no_layout_check():
    envelope = ReviewOutput(
        status="success", approved=True,
        findings=[ReviewFinding(requirement="does X", met=True, evidence="a.py:1")],
    )
    report = gates.verdict_consistent(envelope, run_at("."))
    assert report.passed
    assert not any("layout" in c.item for c in report.checks)


# --- plan_has_failure_lines ---

VALID_ONE_STEP = "## Repo facts\nnone\n\n## Step 1\nDo it.\nfailure: pure\nwriters: none\n"
VALID_MULTI_STEP = (
    "## Repo facts\nnone\n\n"
    "## Step 1\nFirst.\nfailure: pure\nwriters: none\n\n"
    "## Step 2 — add the thing\nSecond.\n"
    "failure: a red build retries then fails\nwriters: migrated\n"
)


def handoff_run(handoff_dir) -> SimpleNamespace:
    return SimpleNamespace(repo_root=str(handoff_dir), context_handoff_dir=handoff_dir)


def write_plan(handoff_dir, text: str) -> SimpleNamespace:
    (handoff_dir / "plan.md").write_text(text)
    return handoff_run(handoff_dir)


def plan_report(handoff_dir, text: str, *, artifacts=None):
    run = write_plan(handoff_dir, text)
    return gates.plan_has_failure_lines(
        BuildOutput(status="success", artifacts=artifacts or []), run
    )


@pytest.mark.parametrize("plan", [VALID_ONE_STEP, VALID_MULTI_STEP])
def test_plan_has_failure_lines_passes_valid_plans(tmp_path, plan):
    assert plan_report(tmp_path, plan).passed


def test_plan_has_failure_lines_flags_a_missing_failure_line(tmp_path):
    report = plan_report(tmp_path, "## Repo facts\nnone\n\n## Step 1\nDo it.\nwriters: none\n")
    assert not report.passed
    assert report.violations == [
        "step 1 failure: step 1 is missing a `failure:` line with a non-whitespace value"
    ]


def test_plan_has_failure_lines_flags_a_missing_writers_line(tmp_path):
    report = plan_report(tmp_path, "## Repo facts\nnone\n\n## Step 1\nDo it.\nfailure: pure\n")
    assert not report.passed
    assert report.violations == [
        "step 1 writers: step 1 is missing a `writers:` line with a non-whitespace value"
    ]


def test_plan_has_failure_lines_flags_both_missing(tmp_path):
    report = plan_report(tmp_path, "## Repo facts\nnone\n\n## Step 1\nDo it.\n")
    assert not report.passed
    assert [v.split(":", 1)[0] for v in report.violations] == [
        "step 1 failure",
        "step 1 writers",
    ]


def test_plan_has_failure_lines_flags_only_the_second_step(tmp_path):
    plan = (
        "## Repo facts\nnone\n\n"
        "## Step 1\nFirst.\nfailure: pure\nwriters: none\n\n"
        "## Step 2\nSecond.\nfailure: pure\n"
    )
    report = plan_report(tmp_path, plan)
    assert not report.passed
    assert report.violations == [
        "step 2 writers: step 2 is missing a `writers:` line with a non-whitespace value"
    ]


def test_plan_has_failure_lines_ignores_labels_outside_the_step(tmp_path):
    """A `failure:`/`writers:` in `## Repo facts` or a later `## Notes` section is
    not the step's own line."""
    plan = (
        "## Repo facts\nfailure: pure\nwriters: none\n\n"
        "## Step 1\nDo it.\n\n"
        "## Notes\nfailure: pure\nwriters: none\n"
    )
    report = plan_report(tmp_path, plan)
    assert not report.passed
    assert [v.split(":", 1)[0] for v in report.violations] == [
        "step 1 failure",
        "step 1 writers",
    ]


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_plan_has_failure_lines_rejects_an_empty_value(tmp_path, value):
    plan = f"## Repo facts\nnone\n\n## Step 1\nDo it.\nfailure:{value}\nwriters: none\n"
    report = plan_report(tmp_path, plan)
    assert not report.passed
    assert report.violations == [
        "step 1 failure: step 1 is missing a `failure:` line with a non-whitespace value"
    ]


def test_plan_has_failure_lines_accepts_an_assumed_outcome(tmp_path):
    plan = (
        "## Repo facts\nnone\n\n"
        "## Step 1\nDo it.\n"
        "failure: assumed — a duplicate delivery is ignored\nwriters: none\n"
    )
    report = plan_report(tmp_path, plan)
    assert report.passed
    assert report.violations == []


def test_plan_has_failure_lines_assumed_passes_while_an_empty_value_still_fails(tmp_path):
    plan = (
        "## Repo facts\nnone\n\n"
        "## Step 1\nFirst.\nfailure: assumed — the newest write wins\nwriters: none\n\n"
        "## Step 2\nSecond.\nfailure:\nwriters: none\n"
    )
    report = plan_report(tmp_path, plan)
    assert not report.passed
    assert report.violations == [
        "step 2 failure: step 2 is missing a `failure:` line with a non-whitespace value"
    ]


def test_plan_has_failure_lines_does_not_consume_the_next_line(tmp_path):
    """A bare `failure:` must not borrow the following line's text as its value."""
    plan = "## Repo facts\nnone\n\n## Step 1\nDo it.\nfailure:\npure\nwriters: none\n"
    report = plan_report(tmp_path, plan)
    assert not report.passed
    assert report.violations[0].startswith("step 1 failure")


@pytest.mark.parametrize("text", ["", "   \n\n", "## Repo facts\nnone\n"])
def test_plan_has_failure_lines_fails_without_step_sections(tmp_path, text):
    report = plan_report(tmp_path, text)
    assert not report.passed
    assert "no `## Step N` sections" in report.violations[0]


def test_plan_has_failure_lines_requires_the_canonical_plan_not_any_artifact(tmp_path):
    (tmp_path / "other.md").write_text(VALID_ONE_STEP)  # a valid plan under another name
    run = handoff_run(tmp_path)
    report = gates.plan_has_failure_lines(
        BuildOutput(status="success", artifacts=["other.md"]), run
    )
    assert not report.passed
    assert "plan.md" in report.violations[0]
    assert "cannot read" in report.violations[0]


@pytest.mark.parametrize("error", [OSError("boom"), UnicodeError("bad bytes")])
def test_plan_has_failure_lines_handles_a_read_error(tmp_path, monkeypatch, error):
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(VALID_ONE_STEP)

    def boom(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(Path, "read_text", boom)
    report = gates.plan_has_failure_lines(BuildOutput(status="success"), handoff_run(tmp_path))
    assert not report.passed
    assert "cannot read" in report.violations[0]


def test_plan_has_failure_lines_is_read_only_and_rechecks_current_bytes(tmp_path):
    """Repeat validation: identical reports and untouched bytes/dir. A rewritten
    plan is re-judged from its current bytes — no stale pass carries."""
    run = write_plan(tmp_path, VALID_ONE_STEP)
    before = sorted(p.name for p in tmp_path.iterdir())
    bytes_before = (tmp_path / "plan.md").read_bytes()

    first = gates.plan_has_failure_lines(BuildOutput(status="success"), run)
    second = gates.plan_has_failure_lines(BuildOutput(status="success"), run)
    assert first.passed and second.passed
    assert first.model_dump() == second.model_dump()
    assert (tmp_path / "plan.md").read_bytes() == bytes_before
    assert sorted(p.name for p in tmp_path.iterdir()) == before

    (tmp_path / "plan.md").write_text("## Repo facts\nnone\n\n## Step 1\nDo it.\n")
    third = gates.plan_has_failure_lines(BuildOutput(status="success"), run)
    assert not third.passed


# --- plan_assumptions ---


def test_plan_assumptions_lists_assumed_steps_in_step_order():
    plan = (
        "## Repo facts\nnone\n\n"
        "## Step 1\nDo it.\nfailure: pure\nwriters: none\n\n"
        "## Step 2 — x\nDo it.\nfailure: assumed — retry once\nwriters: none\n\n"
        "## Step 3\nDo it.\nfailure: assumed — keep the old row\nwriters: none\n"
    )
    assert gates.plan_assumptions(plan) == [(2, "retry once"), (3, "keep the old row")]


@pytest.mark.parametrize(
    ("value", "outcome"),
    [
        ("assumed - a", "a"),
        ("assumed: a", "a"),
        ("assumed – a", "a"),
        ("assumed", "assumed"),
    ],
)
def test_plan_assumptions_strips_the_separator_or_keeps_a_bare_value(value, outcome):
    plan = f"## Step 1\nDo it.\nfailure: {value}\nwriters: none\n"
    assert gates.plan_assumptions(plan) == [(1, outcome)]


def test_plan_assumptions_ignores_non_step_sections_and_other_values():
    plan = (
        "## Repo facts\nnone\nfailure: assumed — x\n\n"
        "## Step 1\nDo it.\nfailure: pure\nwriters: none\n\n"
        "## Step 2\nDo it.\nfailure: a red build retries then fails\nwriters: none\n\n"
        "## Notes\nfailure: assumed — x\n"
    )
    assert gates.plan_assumptions(plan) == []


def test_plan_assumptions_empty_for_no_steps():
    assert gates.plan_assumptions("") == []


# --- glossary_entries_capped ---


def _entry(term: str, n: int) -> str:
    """A `- **Term** …` bullet line of exactly `n` characters."""
    prefix = f"- **{term}** "
    return prefix + "x" * (n - len(prefix))


def _glossary_repo(tmp_path):
    """A repo whose seed commit holds a 400-char Legacy entry and a short Small."""
    _git("init", "-b", "glossary-test", cwd=tmp_path)
    _git("config", "user.name", "glossary-test", cwd=tmp_path)
    _git("config", "user.email", "glossary-test@example.invalid", cwd=tmp_path)
    (tmp_path / "CONTEXT.md").write_text(_entry("Legacy", 400) + "\n" + _entry("Small", 40) + "\n")
    _git("add", "CONTEXT.md", cwd=tmp_path)
    _git("commit", "-m", "seed", cwd=tmp_path)
    return tmp_path, _git("rev-parse", "HEAD", cwd=tmp_path)


def _envelope() -> DocumentOutput:
    return DocumentOutput(status="success", changed_docs=[])


def test_glossary_gate_fails_a_new_long_entry_naming_term_and_length(tmp_path):
    repo, base_sha = _glossary_repo(tmp_path)
    with (repo / "CONTEXT.md").open("a", encoding="utf-8") as f:
        f.write(_entry("Fresh", 400) + "\n")
    report = gates.glossary_entries_capped(_envelope(), pinned_run_at(repo, base_sha))
    assert len(report.violations) == 1
    assert "**Fresh**" in report.violations[0]
    assert "400" in report.violations[0]


def test_glossary_gate_fails_a_changed_entry_over_the_cap(tmp_path):
    repo, base_sha = _glossary_repo(tmp_path)
    (repo / "CONTEXT.md").write_text(_entry("Legacy", 400) + "\n" + _entry("Small", 400) + "\n")
    report = gates.glossary_entries_capped(_envelope(), pinned_run_at(repo, base_sha))
    assert len(report.violations) == 1
    assert "Small" in report.violations[0]


def test_glossary_gate_never_measures_an_entry_identical_at_base(tmp_path):
    repo, base_sha = _glossary_repo(tmp_path)
    with (repo / "CONTEXT.md").open("a", encoding="utf-8") as f:
        f.write(_entry("Added", 30) + "\n")
    report = gates.glossary_entries_capped(_envelope(), pinned_run_at(repo, base_sha))
    assert report.passed, report.violations


def test_glossary_gate_350_passes_351_fails(tmp_path):
    repo, base_sha = _glossary_repo(tmp_path)
    base_text = (repo / "CONTEXT.md").read_text()
    (repo / "CONTEXT.md").write_text(base_text + _entry("Edge", 350) + "\n")
    assert gates.glossary_entries_capped(_envelope(), pinned_run_at(repo, base_sha)).passed
    (repo / "CONTEXT.md").write_text(base_text + _entry("Edge", 351) + "\n")
    report = gates.glossary_entries_capped(_envelope(), pinned_run_at(repo, base_sha))
    assert len(report.violations) == 1
    assert "**Edge**" in report.violations[0]


def test_glossary_gate_without_a_pin_measures_nothing(tmp_path):
    (tmp_path / "CONTEXT.md").write_text(_entry("Long", 400) + "\n")
    report = gates.glossary_entries_capped(_envelope(), run_at(tmp_path))
    assert report.passed, report.violations


def test_glossary_gate_passes_without_a_context_md(tmp_path):
    report = gates.glossary_entries_capped(_envelope(), run_at(tmp_path))
    assert report.passed, report.violations


def test_glossary_gate_measures_all_entries_when_base_had_no_context_md(tmp_path):
    _git("init", "-b", "glossary-test", cwd=tmp_path)
    _git("config", "user.name", "glossary-test", cwd=tmp_path)
    _git("config", "user.email", "glossary-test@example.invalid", cwd=tmp_path)
    (tmp_path / "seed.txt").write_text("seed\n")
    _git("add", "seed.txt", cwd=tmp_path)
    _git("commit", "-m", "seed", cwd=tmp_path)
    base_sha = _git("rev-parse", "HEAD", cwd=tmp_path)
    (tmp_path / "CONTEXT.md").write_text(_entry("Later", 400) + "\n")
    report = gates.glossary_entries_capped(_envelope(), pinned_run_at(tmp_path, base_sha))
    assert not report.passed
    assert "**Later**" in report.violations[0]


def test_glossary_gate_never_writes_context_md(tmp_path):
    repo, base_sha = _glossary_repo(tmp_path)
    with (repo / "CONTEXT.md").open("a", encoding="utf-8") as f:
        f.write(_entry("Fresh", 400) + "\n")
    before = (repo / "CONTEXT.md").read_bytes()
    gates.glossary_entries_capped(_envelope(), pinned_run_at(repo, base_sha))
    assert (repo / "CONTEXT.md").read_bytes() == before
