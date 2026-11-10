"""Validation gates: verify the envelope's CLAIMS, never guesses.

A gate is `gate(envelope, run) -> GateReport` — one check per item it looked at.
Violations are derived from the failed checks and sent back to the SAME agent
session as a correction. Every check is recorded either way, so a green gate
says WHAT it verified instead of only that it passed.

Gates check what is mechanically checkable; plan quality is a reviewer's job.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import config, git_helper
from .data_types import EnvelopeBase, GateReport
from .utils import operator_env

TAIL_CHARS = 1000  # command output kept as evidence on a failure

# Plan-step headings use the same numbering/title syntax as feature.py's
# `_STEP_HEADING`; duplicated here so gates never imports a workflow (circular).
_STEP_HEADING = re.compile(r"^## Step (\d+)\b.*$", re.MULTILINE)
_H2_HEADING = re.compile(r"^## .*$", re.MULTILINE)
# A lowercase literal label at the very start of a line with a non-whitespace value
# on that same line. `[ \t]*` (not `\s*`) keeps a bare label from swallowing the
# newline and looking populated.
_FAILURE_LINE = re.compile(r"^failure:[ \t]*(\S.*)$", re.MULTILINE)
_WRITERS_LINE = re.compile(r"^writers:[ \t]*(\S.*)$", re.MULTILINE)


def _resolve(path: str, run) -> Path:
    """Agents report paths relative to where they worked — the repo root."""
    p = Path(path)
    return p if p.is_absolute() else Path(run.repo_root) / p


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


def artifacts_exist(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = _resolve(a, run)
        report.check(
            a,
            p.exists(),
            f"exists, {_size(p)}" if p.exists() else "declared artifact does not exist",
        )
    return report


def diff_matches_claims(envelope: EnvelopeBase, run) -> GateReport:
    """Every file claimed changed must exist on disk, or be a measured change.

    A Run pinned to a launch `base_sha` (ADR 0024) may honestly claim a path it
    deleted: a missing claim also passes when it appears in
    `git_helper.diff_files(base_sha)` — the exact pin, never HEAD, a branch, or a
    recomputed merge base. The diff is measured lazily, at most once per
    invocation, and only for a missing claim with a truthy pin; no pin means
    today's existence-only behaviour, and a helper failure propagates rather
    than granting an unmeasured pass. Nothing is cached across invocations.
    """
    report = GateReport()
    base_sha = getattr(run, "base_sha", None)
    measured: set[Path] | None = None
    for f in getattr(envelope, "changed_files", []):
        p = _resolve(f, run)
        if p.exists():
            report.check(f, True, f"exists, {_size(p)}")
            continue
        if base_sha and measured is None:
            measured = {_resolve(path, run) for path in git_helper.diff_files(base_sha)}
        if base_sha and p in measured:
            report.check(f, True, f"present in diff against pinned base {base_sha}")
            continue
        report.check(f, False, "claimed changed file does not exist")
    return report


def changed_docs_exist(envelope: EnvelopeBase, run) -> GateReport:
    """Every doc the documenter claims to have changed must exist on disk."""
    report = GateReport()
    for f in getattr(envelope, "changed_docs", []):
        p = _resolve(f, run)
        report.check(
            f,
            p.exists(),
            f"exists, {_size(p)}" if p.exists() else "claimed changed doc does not exist",
        )
    return report


def _show_at_base(repo_root, base_sha: str, path: str) -> str:
    """The file's bytes at `base_sha`, or `""` when it did not exist there."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{base_sha}:{path}"],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def glossary_entries_capped(envelope: EnvelopeBase, run) -> GateReport:
    """New or changed CONTEXT.md entries over the cap fail; an entry identical to
    one at the pinned base is never measured."""
    report = GateReport()
    p = Path(run.repo_root) / "CONTEXT.md"
    if not p.exists():
        return report.check("CONTEXT.md", True, "absent — nothing to measure")
    base_sha = getattr(run, "base_sha", None)
    if not base_sha:
        return report.check("CONTEXT.md", True, "no pinned base — not measured")
    base = {text for _, text in config.glossary_entries(
        _show_at_base(run.repo_root, base_sha, "CONTEXT.md"))}
    measured = False
    for term, text in config.glossary_entries(p.read_text(encoding="utf-8")):
        if text in base:
            continue
        measured = True
        n = len(text)
        ok = n <= config.CONTEXT_ENTRY_LIMIT
        report.check(
            f"CONTEXT.md **{term}**",
            ok,
            f"{n} characters"
            if ok
            else f"{n} characters — over the {config.CONTEXT_ENTRY_LIMIT}-character cap; "
            "rewrite it as one or two sentences on what the term is",
        )
    if not measured:
        report.check("CONTEXT.md", True, "no new or changed entries")
    return report


def sources_cited(envelope: EnvelopeBase, run) -> GateReport:
    """A research call that declares no brief, or a brief with no URL-sourced
    claim, is not a brief (researcher system.md; spec §14)."""
    report = GateReport()
    report.check(
        "brief", bool(envelope.artifacts),
        "declared" if envelope.artifacts else "no brief artifact declared",
    )
    sources = getattr(envelope, "sources", [])
    report.check(
        "sources", bool(sources), f"{len(sources)} source(s)" if sources else "no sources reported"
    )
    for i, source in enumerate(sources):
        report.check(source.url or f"source {i}", bool(source.url), "" if source.url else "source has no url")
    return report


def _plan_step_sections(text: str) -> list[tuple[int, str]]:
    """`(step number, that step's own section)` for every `## Step N` heading.

    A step's section ends at the next level-two heading of any kind, so a label in
    a later unrelated `## ...` section (or in `## Repo facts`, before any step)
    cannot satisfy an earlier step's requirement.
    """
    headings = list(_H2_HEADING.finditer(text))
    sections: list[tuple[int, str]] = []
    for m in _STEP_HEADING.finditer(text):
        end = len(text)
        for h in headings:
            if h.start() > m.start():
                end = h.start()
                break
        sections.append((int(m.group(1)), text[m.start():end]))
    return sections


def plan_assumptions(text: str) -> list[tuple[int, str]]:
    """`(step number, outcome)` for every `## Step N` whose `failure:` value starts
    with `assumed`, in Step order: the planner's unstated product decisions, shown
    in the PR body so the owner can rule on them (ticket 187).

    The outcome is the value after `assumed`, without its leading separator
    (`—`, `–`, `-`, `:`) or whitespace. A bare `failure: assumed` keeps the whole
    value. Same Step scoping and `failure:` line as `plan_has_failure_lines`."""
    found: list[tuple[int, str]] = []
    for step_no, section in _plan_step_sections(text):
        m = _FAILURE_LINE.search(section)
        if m is None:
            continue
        value = m.group(1).strip()
        if not value.startswith("assumed"):
            continue
        outcome = value[len("assumed"):].lstrip(" \t—–-:").strip()
        found.append((step_no, outcome or value))
    return found


def plan_has_failure_lines(envelope: EnvelopeBase, run) -> GateReport:
    """Every `## Step N` in the canonical `plan.md` carries a `failure:` and a
    `writers:` line, each a line value with a non-whitespace value.

    Reads `run.context_handoff_dir / "plan.md"` — the same file the workflow
    consumes — never `envelope.artifacts`, so an unrelated artifact or an empty
    artifact list cannot evade validation. Read-only and deterministic: repeated
    calls over unchanged bytes return identical reports; a rewritten file is
    re-judged from its current bytes, so a stale earlier pass never carries.
    """
    report = GateReport()
    path = Path(run.context_handoff_dir) / "plan.md"
    try:
        text = path.read_text()
    except (OSError, UnicodeError) as e:
        return report.check("plan.md", False, f"cannot read {path}: {e}")
    sections = _plan_step_sections(text)
    if not sections:
        return report.check("plan.md", False, f"{path} has no `## Step N` sections")
    for step_no, section in sections:
        for field, pattern in (("failure", _FAILURE_LINE), ("writers", _WRITERS_LINE)):
            found = pattern.search(section) is not None
            report.check(
                f"step {step_no} {field}",
                found,
                f"`{field}:` present"
                if found
                else f"step {step_no} is missing a `{field}:` line with a non-whitespace value",
            )
    return report


def verdict_consistent(envelope: EnvelopeBase, run) -> GateReport:
    """A review's verdict must agree with the findings it just wrote down.

    Nothing here judges the code — that is the reviewer's job. This checks the
    envelope against itself: an approval that ships blocking items, unmet
    requirements, or trust violations (§6: never approved while non-empty), or
    a rejection that names no problem, is a claim the harness can refute
    without reading a line of the diff.
    """
    report = GateReport()
    approved = bool(getattr(envelope, "approved", False))
    blocking = list(getattr(envelope, "blocking", []))
    unmet = [f.requirement for f in getattr(envelope, "findings", []) if not f.met]
    trust = list(getattr(envelope, "trust_violations", []))

    report.check(
        "approved vs blocking",
        not (approved and blocking),
        "no blocking items"
        if not blocking
        else f"{len(blocking)} blocking item(s) while approved=true"
        if approved
        else f"{len(blocking)} blocking item(s), not approved",
    )
    report.check(
        "approved vs findings",
        not (approved and unmet),
        "every requirement met"
        if not unmet
        else f"{len(unmet)} unmet requirement(s) while approved=true"
        if approved
        else f"{len(unmet)} unmet requirement(s), not approved",
    )
    report.check(
        "approved vs trust_violations",
        not (approved and trust),
        "no trust violations"
        if not trust
        else f"{len(trust)} trust violation(s) while approved=true"
        if approved
        else f"{len(trust)} trust violation(s), not approved",
    )
    report.check(
        "rejection names a problem",
        approved or bool(blocking or unmet or trust),
        "verdict is supported"
        if approved or blocking or unmet or trust
        else "approved=false but no blocking item, unmet requirement or trust violation was given",
    )
    return report


def route_dir_in_fence(slug: str):
    """Gate factory: the declared route is named right, lives inside `proto-<slug>/` — not
    just anywhere the designer's `writes:` glob would technically allow — AND actually
    exists on disk (17-designer-role.md: "`route_dir` exists inside the fence")."""
    name = f"proto-{slug}"

    def gate(envelope: EnvelopeBase, run) -> GateReport:
        report = GateReport()
        route_dir = getattr(envelope, "route_dir", "")
        named_right = bool(route_dir) and Path(route_dir.rstrip("/")).name == name
        report.check("route_dir", named_right, route_dir or "no route_dir declared")
        if named_right:
            p = _resolve(route_dir, run)
            report.check(
                "route_dir exists", p.is_dir(),
                "exists" if p.is_dir() else f"{route_dir} does not exist on disk",
            )
        url_path = getattr(envelope, "url_path", "")
        report.check(
            "url_path", url_path == f"/{name}", url_path or "no url_path declared"
        )
        return report

    gate.__name__ = f"route_dir_in_fence({slug})"
    return gate


def variant_count(envelope: EnvelopeBase, run) -> GateReport:
    """2-3 substantively different variants, never one or a wall of options (17-designer-role.md)."""
    variants = getattr(envelope, "variants", [])
    report = GateReport()
    report.check("variants", 2 <= len(variants) <= 3, f"{len(variants)} variant(s) declared")
    return report


def notes_locked(envelope: EnvelopeBase, run) -> GateReport:
    """`--lock` must produce NOTES.md with all three sections: fixed, free, open.

    Looks for a `## <section>` heading (`"## {s}" not in text`), not a bare
    substring, which would let incidental prose ("unfixed", "opened") count as
    the section.
    """
    report = GateReport()
    notes_path = getattr(envelope, "notes_path", "")
    if not notes_path:
        return report.check("notes_path", False, "no notes_path declared")
    p = _resolve(notes_path, run)
    if not p.exists():
        return report.check("notes_path", False, "declared NOTES.md does not exist")
    text = p.read_text().lower()
    for section in ("fixed", "free", "open"):
        found = f"## {section}" in text
        report.check(section, found, "present" if found else "missing")
    return report


def tests_pass(command: str):
    """Gate factory: the given shell command must exit 0 (run at the repo root)."""

    def gate(envelope: EnvelopeBase, run) -> GateReport:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=run.repo_root,
            env=operator_env(),
        )
        ok = result.returncode == 0
        note = f"exit {result.returncode}"
        if not ok:
            note += "\n" + (result.stdout + result.stderr)[-TAIL_CHARS:]
        return GateReport().check(command, ok, note)

    gate.__name__ = f"tests_pass({command})"
    return gate
