"""The step library (spec §7): one function per Phase, kind = engineer | agent | code.

`engineer -> setup -> scout -> plan -> build/verify -> review/revise -> commit(code) ->
changes -> document -> commit(docs) -> push_pr` (ticket 28, 29). The four
Workflows all run this same library — `feature.py`/`bug_fix.py` (ticket 31)
supply the per-Workflow prompt strings and a `route_dir` to delete.
`lime prototype` calls `scout()` too.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from .. import budget, changes as change_capture
from .. import config, diff_accounting, gates, git_helper, limits, numbering, quality, tracker
from ..data_types import (
    AgentCall,
    BuildOutput,
    BuildStep,
    ChangeCapture,
    ChangesOutput,
    DiffCapContributor,
    DocumentOutput,
    PhaseParams,
    PlanOutput,
    PlanProvenance,
    PLAN_PROVENANCE_VERSION,
    ReviewOutput,
    ReviewStep,
    ScoutOutput,
    VerifyCheck,
    VerifyOutput,
)
from ..utils import operator_env

GATE_RETRIES = 1  # bounded same-session corrections for an agent's own claims
REVIEW_HUNK_CONTEXT = 30  # lines of context around each hunk — judge a change without the whole file (ticket 83)
REVIEW_PROMPT_TOKENS = budget.WARN  # ceiling for the reviewer's whole first prompt, estimated at bytes/2 — turn one must not start in the warn zone (ticket 83)

_MISS = object()  # sentinel: this phase has no prior success to replay


def _validate_pin(run, base: str | None = None) -> str | None:
    """Fail-closed preflight for a pinned Run before replay, quality or a push.

    Read-only. Returns the validated pin, or None for a legacy Run (no pin). An
    invalid pin raises `InvalidRunBase`, which must propagate to the chain boundary
    — never be downgraded to a cache miss or an ordinary inspection error.
    """
    base_sha = getattr(run, "base_sha", None)
    if not base_sha:
        return None
    return git_helper.validate_run_base(base_sha)


def _replayed(run, name: str, output_type=None):
    """The prior pass's result for phase `name`, or `_MISS` to run it for real.

    On `lime run --resume`, phases that already succeeded are not re-run (spec §9:
    restart the failed Phase, not the chain) — their original trace row stands and
    their recorded envelope is rehydrated for the phases downstream. A success
    whose envelope was never recorded (older trace dbs) falls through to a real
    re-run — correct either way, just not free."""
    entry = run.replay.get(name)
    if entry is None:
        return _MISS
    if output_type is not None and not entry["payload_json"]:
        return _MISS
    run.console.note(f"{name}: already succeeded — replayed from the trace (resume)")
    run.tracer.event(
        run.run_id, "log", phase_id=entry["phase_id"], name=name, payload={"replayed": True}
    )
    if output_type is None:
        return None
    return output_type.model_validate_json(entry["payload_json"])


def _attempt_name(prefix: str, attempt: int, step_no: int | None = None) -> str:
    """`build_1`, `build_1_retry1`, … or `review`, `review_retry1`, … (no step number)."""
    base = f"{prefix}_{step_no}" if step_no is not None else prefix
    return base if attempt == 1 else f"{base}_retry{attempt - 1}"


def engineer(run, prompt: str) -> None:
    """Record the ask — the phase IS the record."""
    if _replayed(run, "engineer") is not _MISS:
        return
    with run.phase(
        PhaseParams(
            name="engineer",
            kind="engineer",
            stage="Prepare",
            owner=run.engineer,
            description="Record the request that starts this Run.",
        )
    ) as ph:
        ph.log(prompt=prompt)


def setup(run) -> None:
    """`commands.setup` once, inside the Run's trace; missing = skipped, red = Run fails."""
    if _replayed(run, "setup") is not _MISS:
        return
    command = (run.commands or {}).get("setup")
    with run.phase(
        PhaseParams(
            name="setup",
            kind="code",
            stage="Prepare",
            owner="git",
            description="Run commands.setup once for this Run's worktree.",
        )
    ) as ph:
        if not command:
            ph.log(skipped="commands.setup is unset")
            return
        result = subprocess.run(
            command,
            shell=True,
            cwd=run.repo_root,
            env=operator_env(),
            capture_output=True,
            text=True,
        )
        ph.log(command=command, returncode=result.returncode)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip()[-2000:]
            raise RuntimeError(f"commands.setup exited {result.returncode}: {tail}")


PLANNER_BRIEF_DESCRIPTION = "Brief the planner on where the ticket's code, conventions and tests live."

SCOUT_UNFINISHED = (
    "# Scout did not finish\n\n"
    "The scout stopped before delivering a brief, so there is no brief for this ticket.\n\n"
    "Reason: {reason}\n\n"
    "Plan from the ticket and your own reads (the no-brief path).\n"
)


def _brief_unfinished_scout(run, error: Exception) -> None:
    """Overwrite `scout_findings.md` with the no-brief notice when a non-fatal scout fails.

    The only code writer of `scout_findings.md`: a successful scout writes it itself, and
    this fallback replaces whatever a failed scout left behind (ticket 197).
    """
    reason = str(error).strip()[:1000] or type(error).__name__
    (run.context_handoff_dir / "scout_findings.md").write_text(SCOUT_UNFINISHED.format(reason=reason))
    run.console.note(f"scout did not finish — the planner plans without a brief: {reason}")


def scout(run, prompt: str, *, description: str = PLANNER_BRIEF_DESCRIPTION, fatal: bool = True) -> ScoutOutput | None:
    """The scout (read-only): writes `<context_handoff_dir>/scout_findings.md` for whoever
    runs next — the planner in the feature/bug-fix chains (ticket 71), the designer in
    `lime prototype`. One constructor for both; only the prompt and description differ.

    `fatal=False` is the feature/bug-fix Chain (ticket 197): any scout failure (reported
    fail/blocked, gate miss, ceiling, raised error) fails only the scout Phase, which stays
    `fail` in the trace. `scout_findings.md` then says the scout did not finish and why, and
    the planner takes its no-brief path. `lime scout` and the prototype keep the default and
    still raise."""
    if (hit := _replayed(run, "scout", ScoutOutput)) is not _MISS:
        return hit
    params = PhaseParams(
        name="scout", kind="agent", stage="Prepare", owner="scout", description=description,
        retries=GATE_RETRIES, fatal=fatal,
    )
    try:
        with run.phase(params) as ph:
            return ph.call(AgentCall(output_type=ScoutOutput, prompt=prompt, gates=[gates.artifacts_exist]))
    except Exception as error:
        if fatal:
            raise
        _brief_unfinished_scout(run, error)
        return None


def plan(run, prompt: str, *, ticket_sha256: str = "") -> PlanOutput:
    """The planner (Fable): `plan.md` = `## Repo facts` + one `## Step N` per step.

    The raw input is augmented with the planner-limits line — the builder's resolved
    clock/token ceilings plus the Run's added-lines sizing budget — before replay, the
    planner call and provenance, so the planner sizes against the numbers the builder
    will run under. A replay-eligible plan is only replayed when the ticket bytes, the
    exact augmented prompt and the produced `plan.md` still match the recorded
    `PlanProvenance`. On a miss the cached plan and every dependent success are
    invalidated (and stale agent sessions cleared) before a fresh planner runs — a
    direct feature caller cannot silently accept a changed prompt/plan, nor continue an
    old plan from cached `build_N` rows.
    """
    prompt = limits.append_planner(prompt, run.agent("builder"), run.cfg)
    entry = run.replay.get("plan")
    if entry is not None:
        if _plan_replayable(run, entry, prompt, ticket_sha256):
            return _replayed(run, "plan", PlanOutput)
        reason = "ticket/prompt/limits/plan provenance changed"
        run.console.note(f"plan: cached plan is stale ({reason}) — re-planning")
        run.tracer.event(
            run.run_id, "log", phase_id=entry["phase_id"], name="plan_cache_miss",
            payload={"reason": reason},
        )
        dropped = run.tracer.invalidate_from(
            run.run_id, "plan", reason=f"stale plan: {reason}"
        )
        run.replay = run.tracer.replay_map(run.run_id)  # traversal must not reuse the stale suffix
        # Stale runtime sessions belong to the old plan — clear the map so planner and
        # builder sessions cannot continue it (same convention as CLI invalidation).
        run._agent_map_path.unlink(missing_ok=True)
        run.agent_map = {}
        if dropped:
            run.console.note(f"invalidated stale plan dependents: {', '.join(dropped)}")
    with run.phase(
        PhaseParams(
            name="plan",
            kind="agent",
            stage="Prepare",
            owner="planner",
            description="Turn the request into plan.md — every step sized to one Session.",
            retries=GATE_RETRIES,
        )
    ) as ph:
        envelope = ph.call(
            AgentCall(
                output_type=PlanOutput,
                prompt=prompt,
                gates=[gates.artifacts_exist, gates.plan_has_failure_lines],
            )
        )
        _record_plan_provenance(run, ph.phase.phase_id, prompt, ticket_sha256)
        return envelope


def load_plan_provenance(
    tracer, run_id: str, phase_id: str
) -> PlanProvenance | None:
    """The one reader of `plan_provenance` events, used by cli resume and plan replay.

    Returns the latest recorded provenance for the phase, or None when there is no
    row or its payload cannot be parsed."""
    row = tracer.con.execute(
        "SELECT payload_json FROM events WHERE run_id=? AND phase_id=?"
        " AND type='handoff' AND name='plan_provenance' ORDER BY event_id DESC LIMIT 1",
        (run_id, phase_id),
    ).fetchone()
    if row is None:
        return None
    try:
        return PlanProvenance.model_validate_json(row["payload_json"])
    except Exception:
        return None


def _file_sha256(path: Path) -> str | None:
    """The sha256 hexdigest of the file's bytes, or None on OSError."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _plan_sha(run) -> str | None:
    return _file_sha256(run.context_handoff_dir / "plan.md")


def stale_plan_reason(
    tracer,
    run_id: str,
    phase_id: str,
    *,
    ticket_sha256: str,
    prompt: str,
    plan_path: Path,
) -> str | None:
    """The single staleness decision: cli prints the reason, and `_plan_replayable`
    is "no reason".

    Fail closed, in this exact order: missing/legacy/malformed provenance, an
    unsupported provenance version, a changed ticket, a changed augmented planning
    prompt (ticket and resolved builder limits, ADR 0025), a missing plan.md or
    changed plan.md bytes. None means the cached plan may be replayed."""
    prov = load_plan_provenance(tracer, run_id, phase_id)
    if prov is None:
        return "the cached plan has no provenance (recorded before provenance existed)"
    if prov.version != PLAN_PROVENANCE_VERSION:
        return f"the cached plan's provenance version {prov.version} is unsupported"
    if prov.ticket_sha256 != ticket_sha256:
        return "the ticket changed since it was planned"
    if prov.prompt != prompt:
        return "the planning inputs or resolved limits changed since it was planned"
    current = _file_sha256(plan_path)
    if current is None:
        return "the cached plan.md is missing"
    if prov.plan_sha256 != current:
        return "the cached plan.md changed since it was planned"
    return None


def _plan_replayable(run, entry: dict, prompt: str, ticket_sha256: str) -> bool:
    """Complete, supported provenance that still matches the current ticket, prompt
    and plan.md bytes. Missing/legacy/malformed evidence is never replayable."""
    if not entry.get("payload_json"):
        return False
    return stale_plan_reason(
        run.tracer,
        run.run_id,
        entry["phase_id"],
        ticket_sha256=ticket_sha256,
        prompt=prompt,
        plan_path=run.context_handoff_dir / "plan.md",
    ) is None


def _record_plan_provenance(run, phase_id: str, prompt: str, ticket_sha256: str) -> None:
    prov = PlanProvenance(
        ticket_sha256=ticket_sha256,
        prompt=prompt,
        plan_sha256=_plan_sha(run) or "",
    )
    # Recorded as phase evidence, not an envelope: replay_map() picks the latest
    # envelope per phase, and a provenance envelope would shadow the PlanOutput.
    run.tracer.event(
        run.run_id, "handoff", phase_id=phase_id, name="plan_provenance",
        payload=prov.model_dump(),
    )


def build(run, step: BuildStep) -> BuildOutput:
    """The builder, one Step at a time. `step.attempt` > 1 names the retry after a red verify."""
    name = _attempt_name("build", step.attempt, step.step_no)
    if (hit := _replayed(run, name, BuildOutput)) is not _MISS:
        return hit
    description = f"Implement plan.md Step {step.step_no}."
    if step.attempt > 1:
        description = f"Fix Step {step.step_no} after a failing verify (attempt {step.attempt})."
    with run.phase(
        PhaseParams(name=name, kind="agent", stage="Build", owner="builder", description=description, retries=GATE_RETRIES)
    ) as ph:
        return ph.call(
            AgentCall(
                output_type=BuildOutput,
                prompt=step.prompt,
                previous=step.previous,
                gates=[gates.diff_matches_claims],
            )
        )


def verify(run, step_no: int, *, attempt: int = 1, base: str = "HEAD") -> VerifyOutput:
    """One code Phase over `commands.test -> lint -> typecheck -> build`, absent keys skipped.

    The quality commands run first; then a NEW measurement is taken after them, so
    growth they generate is included. The measured size is logged (`total_added`
    against `max_added_lines`) and never fails verify (ticket 193).

    On resume the Run's latest verify (the one the next review or commit relies on)
    re-runs against the current tree. Every earlier verify replays as recorded, passed
    or failed, with or without `--rereview` (its recorded envelope steers traversal
    only). A failed re-run invalidates every stale logical dependent — from this
    verify's first row onward, so a prior passing resume's newer row cannot preserve
    an older review or commit — and the chain continues exactly as a live failed
    verify; a pass keeps later successes replayable.

    `base` defaults to "HEAD" for standalone callers; feature.run passes its diff base
    (the Run's pin when pinned, else its base branch).
    """
    _validate_pin(run, base)  # before any replay or quality command
    name = _attempt_name("verify", attempt, step_no)
    stale = run.replay.get(name)
    if stale is not None and stale["payload_json"] and name != run.tracer.latest_verify_name(run.run_id):
        # an earlier verify — replayed as recorded, passed or failed
        return _replayed(run, name, VerifyOutput)
    if stale is not None:
        run.console.note(
            f"{name}: the Run's latest verify — re-running against the current tree (resume)"
        )

    with run.phase(
        PhaseParams(
            name=name,
            kind="code",
            stage="Build",
            owner="git",
            description=f"Run the quality commands for Step {step_no}.",
        )
    ) as ph:
        # 1) Quality checks first
        result = quality.run_quality(run)
        quality_failures = list(result.failures)
        quality_passed = result.passed

        # 2) A NEW measurement after quality, so generated outputs are included
        measurement = diff_accounting.evaluate(run, base)
        try:
            head_sha = measurement.head_sha or git_helper.rev("HEAD")
        except RuntimeError:
            head_sha = ""
        gate_failures = list(measurement.failures)
        gate_passed = not gate_failures

        # Log structured phase evidence (keys kept for existing consumers)
        try:
            ph.log(
                base=base,
                merge_base=(
                    git_helper.short_sha(measurement.merge_base_sha)
                    if measurement.merge_base_sha else "unknown"
                ),
                total_added=measurement.counted_total,
                max_added_lines=measurement.max_added_lines,
                binary_paths=measurement.binaries,
                uncounted_files=[c.model_dump() for c in measurement.uncounted_files],
                uncounted_patterns=measurement.uncounted,
                top_contributors=[c.model_dump() for c in measurement.top_contributors],
                omitted_contributors=measurement.omitted_contributors,
                quality_passed=quality_passed,
                gate_passed=gate_passed,
            )
        except Exception:
            pass

        # Combine gate and quality failures
        all_failures = quality_failures + gate_failures
        combined_passed = quality_passed and gate_passed

        # Build a proper summary message
        parts = []
        if not quality_passed:
            parts.append(f"{len(quality_failures)} quality check(s) failed")
        if not gate_passed:
            parts.append(f"diff gate failed ({len(gate_failures)} issue(s))")
        summary = (
            f"verify step {step_no}: {'all passed' if combined_passed else '; '.join(parts)}"
        )

        if combined_passed:
            notes = ""
        else:
            notes = "Fix every failure below."

        envelope = VerifyOutput(
            status="success" if combined_passed else "fail",
            summary=summary,
            artifacts=result.artifacts,
            notes_for_next_agent=notes,
            passed=combined_passed,
            failures=all_failures,
            head_sha=head_sha,
            checks=[
                VerifyCheck(**c.model_dump(include=set(VerifyCheck.model_fields)))
                for c in result.checks
            ],
        )
        run.tracer.record_envelope(
            run.run_id, ph.phase.phase_id, agent="git",
            output_type="VerifyOutput", payload_json=envelope.model_dump_json(),
            valid=True, attempt=attempt,
        )
        if stale is not None and not envelope.passed:
            # A failed re-run is a live failed verify: every stale logical dependent
            # must drop, including ones that precede a newer passing re-run row. The
            # threshold is this verify's FIRST row — its chain position — not the
            # latest replay entry's seq: a prior passing resume appended a higher-seq
            # row after the review/commit rows it preserved, so that seq would leave
            # them replayable. The re-run row itself is still running, so it stays.
            threshold = run.tracer.first_phase_seq(run.run_id, name)
            dropped = run.tracer.invalidate_from(
                run.run_id, name,
                reason="latest verify failed on resume",
                seq=threshold if threshold is not None else stale.get("seq"),
            )
            run.replay = run.tracer.replay_map(run.run_id)
            if dropped:
                run.console.note(f"invalidated stale verify dependents: {', '.join(dropped)}")
        ph.log(passed=envelope.passed, failures=len(envelope.failures))
        return envelope


def _verify_envelope(run) -> VerifyOutput | None:
    """Latest replay-eligible VerifyOutput envelope for this run, or None.

    Invalidated verify evidence is ignored so a stale pass cannot be fed back to a
    reviewer after a re-verify."""
    payload = run.tracer.latest_envelope(run.run_id, "VerifyOutput")
    if payload is None:
        return None
    return VerifyOutput.model_validate_json(payload)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: bytes/2 rounded up — deliberately ~2× the tokenizer's real count (ticket 83)."""
    return (len(text.encode()) + 1) // 2


def _fit_hunks(hunks: list[tuple[str, str]], room: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Drop the largest hunks first until the rest fit in `room` estimated tokens (ticket 83).
    Returns (inline, omitted) — inline keeps diff order, omitted is in drop order (largest first)."""
    inline = list(hunks)
    omitted: list[tuple[str, str]] = []
    while inline and sum(_estimate_tokens(t) for _, t in inline) > room:
        biggest = max(inline, key=lambda h: len(h[1].encode()))
        inline.remove(biggest)
        omitted.append(biggest)
    return inline, omitted


def _omitted_line(path: str, text: str, base: str) -> str:
    """One-line summary for an omitted hunk: additions, deletions, bytes, and how to retrieve it (ticket 83)."""
    adds = dels = 0
    for line in text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            adds += 1
        elif line.startswith("-") and not line.startswith("---"):
            dels += 1
    return (
        f"- {path}: +{adds} -{dels}, {len(text.encode()):,} bytes of hunks omitted —"
        f" run `git diff -U{REVIEW_HUNK_CONTEXT} {base} -- {path}` (or read the file when it is new)"
    )


def _spine_checks(env: VerifyOutput) -> str:
    """The Spine-recorded quality commands for the reviewer's `## Tests` (ticket 184)."""
    head = env.head_sha[:12] if env.head_sha else "unavailable"
    lines = [
        "Recorded by the Spine (the factory ran each command itself on this tree; not the"
        f" builder's report) — verified HEAD {head}, uncommitted changes shown in ## Diff included:"
    ]
    for c in env.checks:
        lines.append(f"- {c.name} `{c.command}`: exit {c.returncode}, {c.duration_seconds:.1f}s")
    return "\n".join(lines)


def _review_context(run, step: ReviewStep) -> str:
    """The full context block appended to the reviewer's prompt (ticket 62, 83, 246)."""
    base_commit = change_capture.resolve_base(
        step.base, base_sha=getattr(run, "base_sha", None)
    ).commit
    hunks = git_helper.diff_hunks(base_commit, context=REVIEW_HUNK_CONTEXT)
    tracked = git_helper.diff_files(base_commit)
    untracked = git_helper.untracked_files()

    # Changed files section (names only, no file contents)
    if tracked or untracked:
        changed_lines = [f"- {f}" for f in tracked] + [f"- {f} (new)" for f in untracked]
        changed_files_section = "\n".join(changed_lines)
    else:
        changed_files_section = "(none)"

    # Outside Where section (ticket 246): changed paths the ticket's Where does not cover
    where, own_ticket = tracker.prompt_scope(step.prompt)
    if not where:
        outside_section = "Where: none parsed"
    else:
        outside = tracker.outside_where(tracked + untracked, where, own_ticket)
        outside_section = "\n".join(f"- {f}" for f in outside) or "none"

    # Tests section
    verify_env = _verify_envelope(run)
    if verify_env is None:
        tests_section = "passed: false\nno verify recorded"
    else:
        passed_str = "true" if verify_env.passed else "false"
        if verify_env.failures:
            failures_str = "\n".join(verify_env.failures)
        else:
            failures_str = "none"
        tests_section = f"passed: {passed_str}\n{failures_str}"
        if verify_env.checks:
            tests_section += "\n" + _spine_checks(verify_env)

    # Build a render of the sections to estimate room
    def render(diff_display: str) -> str:
        return (
            f"## Checklist\n{step.checklist}\n\n"
            f"## Diff\n{diff_display}\n\n"
            f"## Changed files\n{changed_files_section}\n\n"
            f"## Outside Where\n{outside_section}\n\n"
            f"## Tests\n{tests_section}\n\n"
        )

    room = REVIEW_PROMPT_TOKENS - _estimate_tokens(step.prompt + render(""))
    inline, omitted = _fit_hunks(hunks, room)

    diff_display = "\n".join(text for _, text in inline)
    if not hunks:
        diff_display = "(no changes)"
    elif omitted:
        if diff_display:
            diff_display += "\n\n"
        diff_display += (
            f"Hunks omitted to keep the prompt under ~{REVIEW_PROMPT_TOKENS:,} estimated tokens, largest first:\n"
            + "\n".join(_omitted_line(path, text, base_commit) for path, text in omitted)
        )

    return render(diff_display)


def review(run, step: ReviewStep) -> ReviewOutput:
    """The reviewer (Grok): gets the diff, changed files and verify result
    in the prompt and judges. No turn cap — the token budget is the only ceiling (ticket 72)."""
    name = _attempt_name("review", step.attempt)
    _validate_pin(run, step.base)  # before a replayed review can bypass the guard
    if (hit := _replayed(run, name, ReviewOutput)) is not _MISS:
        return hit
    description = (
        "Confirm the built code matches the plan and the ticket."
        if step.attempt == 1
        else f"Re-review after a revise (attempt {step.attempt})."
    )
    with run.phase(
        PhaseParams(name=name, kind="agent", stage="Review", owner="reviewer", description=description, retries=GATE_RETRIES)
    ) as ph:
        prompt = f"{step.prompt}\n\n{_review_context(run, step)}"
        gates_list = [gates.verdict_consistent]
        return ph.call(
            AgentCall(
                output_type=ReviewOutput, prompt=prompt, previous=step.previous,
                gates=gates_list,
            )
        )


def revise(run, review_output: ReviewOutput, *, attempt: int) -> BuildOutput:
    """The builder, fixing what the reviewer flagged (spec §7's revise loop, ≤2)."""
    name = f"revise_{attempt}"
    if (hit := _replayed(run, name, BuildOutput)) is not _MISS:
        return hit
    with run.phase(
        PhaseParams(
            name=name,
            kind="agent",
            stage="Review",
            owner="builder",
            description=f"Fix the reviewer's findings (revise attempt {attempt}).",
            retries=GATE_RETRIES,
        )
    ) as ph:
        unmet = [f"{f.requirement} — {f.evidence}" for f in review_output.findings if not f.met]
        items = review_output.blocking + review_output.trust_violations + unmet
        base_prompt = (
            "The reviewer found problems with your previous work. Fix every item"
            " below, then re-emit your Report JSON.\n\n" + "\n".join(f"- {item}" for item in items)
        )
        prompt = (
            f"{base_prompt}\n\nThis is a fresh session: earlier work is already in the worktree,"
            f" not in your memory. Do not redo it.\n\n{budget.git_facts(run.repo_root, 0)}"
        )
        prompt = limits.append_builder(prompt, run.agent("builder"), run.cfg)
        return ph.call(
            AgentCall(
                output_type=BuildOutput, prompt=prompt, previous=review_output, gates=[gates.diff_matches_claims]
            )
        )


def commit(run, message: str) -> str:
    """Stage and commit the working tree — the code commit (spec §10, ≤2 per Run)."""
    if _replayed(run, "commit_code") is not _MISS:
        return ""  # the commit is already on the branch; no caller consumes the sha
    with run.phase(
        PhaseParams(
            name="commit_code",
            kind="code",
            stage="Review",
            owner="git",
            description="Commit the working tree as the Run's code commit.",
        )
    ) as ph:
        # Resolve exclusions from config
        exclude_patterns, _, _ = config.resolve_commit_settings(run.cfg)
        # Enumerate excluded paths BEFORE unstaging/committing
        excluded_paths_list = git_helper.excluded_paths(exclude_patterns)
        ph.log(excluded=excluded_paths_list, exclude_patterns=exclude_patterns)
        trailers = f"\n\nLime-Run: {run.run_id}\nLime-Role: builder"
        sha = git_helper.commit_all(message + trailers, exclude=exclude_patterns)
        ph.log(sha=sha)
        return sha


def _ticket_header_fields(text: str) -> dict[str, str]:
    """`Status:`/`Prototype:` header values before the first `## ` section heading.

    Parsing stops at the first section heading, so a body example can never be
    read as a ticket's declared holder.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("## "):
            break
        for key in ("Status", "Prototype"):
            if line.startswith(key + ":"):
                fields[key] = line[len(key) + 1 :].strip()
    return fields


def _directory_prototype_holders(
    run, path: Path, ticket_path, tickets_dir: Path | None
) -> tuple[list[str], str | None]:
    """Open tickets in the authoritative store naming `path` or a descendant.

    Returns `(holder_ticket_paths, keep_reason)`. A `None` keep_reason means the
    store was enumerated and read completely, so the caller may delete the
    directory; any other value is a fail-closed reason to retain it. Read-only
    against the store — never edits a ticket, prototype, or commit there.

    Matching is by normalized path components in the primary-checkout namespace,
    equality or descendant only; a merely prefixed sibling or a parent-only
    reference does not match. The Run's own ticket is excluded by filename before
    it is read, since its linked-worktree and primary absolute paths differ.
    """
    if tickets_dir is None:
        return [], (
            "no authoritative main-checkout ticket source is available"
            " — kept conservatively"
        )
    run_root = os.path.normpath(os.path.abspath(str(run.repo_root)))
    route_abs = os.path.normpath(os.path.abspath(str(path)))
    rel = os.path.relpath(route_abs, run_root)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return [], (
            f"prototype {route_abs} cannot be mapped inside the Run root {run_root}"
            " — kept conservatively"
        )
    primary_root = os.path.normpath(os.path.abspath(str(tickets_dir.parent.parent)))
    target = os.path.normpath(os.path.join(primary_root, rel))
    own_name = Path(ticket_path).name
    holders: list[str] = []
    try:
        entries = list(tickets_dir.iterdir())
        for entry in entries:
            if not entry.is_file() or entry.suffix != ".md":
                continue
            if entry.name == own_name:
                continue
            fields = _ticket_header_fields(entry.read_text())
            if (fields.get("Status") or "").strip() != "open":
                continue
            proto = (fields.get("Prototype") or "").strip()
            if not proto:
                continue
            candidate = os.path.normpath(os.path.join(primary_root, proto))
            if candidate == target or candidate.startswith(target + os.sep):
                holders.append(str(entry))
    except (OSError, UnicodeError) as error:
        return sorted(holders), (
            "the main-checkout ticket source could not be read completely"
            f" ({error.__class__.__name__}: {error}) — kept conservatively"
        )
    return sorted(holders), None


def commit_docs(
    run,
    message: str,
    ticket_path,
    route_dir: Path | str | None = None,
    *,
    tickets_dir: Path | None = None,
) -> str:
    """Delete the ticket and commit the docs change — delete-on-close (spec §10).

    Merging the PR is what closes the ticket: by the time it lands, the
    ticket file is already gone. `route_dir` is a front-end ticket's
    `Prototype:` path. A `Prototype:` that names a *file* is kept for the engineer
    to read after close. A *directory* is removed only when the authoritative
    main-checkout ticket store (`tickets_dir`) was read completely and no other
    open ticket there names the directory or a descendant; when that store is
    unavailable or unreadable, or an open ticket still holds the path, the
    directory is retained and logged. `tickets_dir=None` means the store is
    unknown — never an empty ticket list and never permission to fall back to the
    Run worktree.
    """
    if _replayed(run, "commit_docs") is not _MISS:
        return ""  # the commit is already on the branch; no caller consumes the sha
    with run.phase(
        PhaseParams(
            name="commit_docs",
            kind="code",
            stage="Ship",
            owner="git",
            description="Delete the ticket (and a consumed prototype route) and commit the docs change.",
        )
    ) as ph:
        Path(ticket_path).unlink(missing_ok=True)
        deleted = [str(ticket_path)]
        path = Path(route_dir) if route_dir is not None else None
        if path is not None and path.is_dir():
            holders, keep_reason = _directory_prototype_holders(
                run, path, ticket_path, tickets_dir
            )
            if keep_reason is not None:
                payload: dict = {"kept": [str(path)], "reason": keep_reason}
                if holders:
                    payload["holding_tickets"] = holders
                ph.log(**payload)
            elif holders:
                ph.log(
                    kept=[str(path)],
                    reason="another open ticket still names this prototype",
                    holding_tickets=holders,
                )
            else:
                shutil.rmtree(path)
                deleted.append(str(path))
        elif path is not None and path.is_file():
            # A file prototype is not dead weight — keep it and say so.
            ph.log(kept=[str(path)], reason="Prototype is a file; kept at close")
        # Resolve exclusions from config (same as code commit to avoid re-adding leftovers)
        exclude_patterns, _, _ = config.resolve_commit_settings(run.cfg)
        # Enumerate excluded paths BEFORE unstaging/committing
        excluded_paths_list = git_helper.excluded_paths(exclude_patterns)
        ph.log(excluded=excluded_paths_list, exclude_patterns=exclude_patterns)
        trailers = f"\n\nLime-Run: {run.run_id}\nLime-Role: documenter"
        sha = git_helper.commit_all(message + trailers, exclude=exclude_patterns)
        ph.log(sha=sha, deleted=deleted)
        return sha


def changes(run, base: str) -> ChangesOutput:
    """Capture the diff since `base` for the documenter — deterministic, not a judgement call."""
    _validate_pin(run, base)  # before a replayed changes artifact can bypass the guard
    if (hit := _replayed(run, "changes", ChangesOutput)) is not _MISS:
        return hit
    with run.phase(
        PhaseParams(
            name="changes",
            kind="code",
            stage="Review",
            owner="git",
            description="Capture the diff since the base branch for the documenter.",
        )
    ) as ph:
        changeset = change_capture.capture(run, ChangeCapture(base=base))
        envelope = change_capture.as_envelope(changeset)
        run.tracer.record_envelope(
            run.run_id, ph.phase.phase_id, agent="git",
            output_type="ChangesOutput", payload_json=envelope.model_dump_json(),
            valid=True, attempt=1,
        )
        ph.log(files=len(envelope.changed_files), insertions=envelope.insertions, deletions=envelope.deletions)
        return envelope


def document(run, prompt: str, previous: ChangesOutput) -> DocumentOutput:
    """The documenter: AGENTS.md/CONTEXT.md/ADRs from the diff, or nothing at all.

    The factory reserves the ADR number before the agent runs and appends it, and
    the review path when a review is recorded (the Spine writes `review.md` from
    the latest `ReviewOutput` first) — the documenter never chooses its own number.
    Reservation happens even when the agent writes no ADR (gaps are allowed) and is
    skipped on a successful replay (the prior pass already reserved).  A reservation
    failure prevents the agent call and fails the phase normally.
    """
    if (hit := _replayed(run, "document", DocumentOutput)) is not _MISS:
        return hit
    with run.phase(
        PhaseParams(
            name="document",
            kind="agent",
            stage="Ship",
            owner="documenter",
            description="Update the docs the diff made false, or report nothing needed changing.",
            retries=GATE_RETRIES,
        )
    ) as ph:
        number = numbering.reserve_adr(Path(run.repo_root), run.run_id)
        review_path = _write_review(run)
        prompt = f"{prompt}\nADR number: {number:04d} (reserved — use it or write no ADR)"
        if review_path is not None:
            prompt += f"\nReview: {review_path}"
        return ph.call(
            AgentCall(output_type=DocumentOutput, prompt=prompt, previous=previous, gates=[gates.changed_docs_exist, gates.glossary_entries_capped])
        )


def _phase_table(run) -> str:
    """`role/model/tokens/cost/gate` per phase — everything joined from the trace db.

    Sourced from the phases table, not the in-memory list, so a resumed Run's
    PR still shows the phases its first pass completed. Successes only (the
    call's own push_pr phase is still `running`, a prior pass's failed attempt
    is noise), latest success per name."""
    latest: dict[str, dict] = {}
    for row in run.tracer.con.execute(
        "SELECT phase_id, seq, name, owner FROM phases WHERE run_id=? AND status='success'"
        " ORDER BY seq",
        (run.run_id,),
    ):
        latest[row["name"]] = row
    rows = []
    for p in sorted(latest.values(), key=lambda r: r["seq"]):
        session = run.tracer.con.execute(
            "SELECT model FROM sessions WHERE phase_id=? ORDER BY created_at LIMIT 1", (p["phase_id"],)
        ).fetchone()
        spend = run.tracer.con.execute(
            "SELECT sum(tokens) AS tokens, sum(json_extract(payload_json, '$.cost')) AS cost"
            " FROM events WHERE phase_id=? AND type='turn'",
            (p["phase_id"],),
        ).fetchone()
        gate = run.tracer.con.execute(
            "SELECT passed FROM gate_results WHERE phase_id=? ORDER BY id DESC LIMIT 1", (p["phase_id"],)
        ).fetchone()
        model = session["model"] if session else "-"
        tokens = (spend["tokens"] if spend else 0) or 0
        cost = (spend["cost"] if spend else 0.0) or 0.0
        gate_cell = "-" if gate is None else ("pass" if gate["passed"] else "FAIL")
        rows.append(f"| {p['name']} | {p['owner'] or ''} | {model} | {tokens} | ${cost:.4f} | {gate_cell} |")
    return "\n".join(rows)


def _last_verify(run) -> str:
    """The most recent `commands.test` run — `quality._run`'s own tool_call event."""
    row = run.tracer.con.execute(
        "SELECT payload_json FROM events"
        " WHERE run_id = ? AND type = 'tool_call' AND name = 'quality:test'"
        " ORDER BY event_id DESC LIMIT 1",
        (run.run_id,),
    ).fetchone()
    if row is None:
        return "(no test command ran)"
    payload = json.loads(row["payload_json"] or "{}")
    verdict = "passed" if payload.get("passed") else f"failed (exit {payload.get('returncode')})"
    return f"test: {verdict}"


def _write_review(run) -> Path | None:
    """Write the Run's latest recorded ReviewOutput to `<context_handoff_dir>/review.md`.

    The only writer of `review.md` (ticket 232): the reviewer delivers its Report and the
    Spine renders it for the documenter. Returns the path, or None (nothing written) when no
    review is recorded."""
    payload_json = run.tracer.latest_envelope(run.run_id, "ReviewOutput")
    if payload_json is None:
        return None
    payload = json.loads(payload_json)
    lines = ["# Review", ""]
    lines.append(f"approved: {'true' if payload.get('approved') else 'false'}")
    lines.append(f"summary: {payload.get('summary', '')}")
    lines.append("")
    lines.append("## Findings")
    findings = payload.get("findings", [])
    if findings:
        for finding in findings:
            met = bool(finding.get("met"))
            mark = "x" if met else " "
            verdict = "met" if met else "unmet"
            lines.append(
                f"- [{mark}] {finding.get('requirement', '')} — {verdict} — {finding.get('evidence', '')}"
            )
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Blocking")
    blocking = payload.get("blocking", [])
    if blocking:
        lines.extend(f"- {item}" for item in blocking)
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Trust violations")
    trust_violations = payload.get("trust_violations", [])
    if trust_violations:
        lines.extend(f"- {item}" for item in trust_violations)
    else:
        lines.append("(none)")
    lines.append("")
    lines.append("## Notes for next agent")
    lines.append(payload.get("notes_for_next_agent", "") or "(none)")
    path = run.context_handoff_dir / "review.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def _last_review(run) -> str:
    payload_json = run.tracer.latest_envelope(run.run_id, "ReviewOutput")
    if payload_json is None:
        return "(no review recorded)"
    payload = json.loads(payload_json)
    verdict = "approved" if payload.get("approved") else "not approved"
    lines = [f"{verdict} — {payload.get('summary', '')}"]
    for finding in payload.get("findings", []):
        mark = "x" if finding.get("met") else " "
        lines.append(f"- [{mark}] {finding.get('requirement', '')} — {finding.get('evidence', '')}")
    return "\n".join(lines)


def last_size(con, run_id: str) -> tuple[int, int] | None:
    """`(counted added lines, budget)` from the Run's newest verify measurement —
    verify's `log` row carrying `total_added` (ticket 195). None when no verify
    measured the Run (old, research, prototype Runs) or the row is unreadable;
    never raises, so neither the PR body nor `lime runs` can fail over it."""
    try:
        row = con.execute(
            "SELECT payload_json FROM events WHERE run_id=? AND type='log'"
            " AND name GLOB 'verify_[0-9]*' AND payload_json LIKE '%\"total_added\"%'"
            " ORDER BY event_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        added, cap = payload["total_added"], payload["max_added_lines"]
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (added, cap)):
        return None
    return added, cap


def _size_line(run) -> str:
    """`Size: N added lines / budget B (+X%)` plus a newline, or "" when unmeasured.
    A budget of 0 or less gets no percentage."""
    size = last_size(run.tracer.con, run.run_id)
    if size is None:
        return ""
    added, cap = size
    pct = f" ({round((added - cap) * 100 / cap):+d}%)" if cap > 0 else ""
    return f"Size: {added} added lines / budget {cap}{pct}\n"


def _assumptions(run) -> str:
    """`## Assumptions`: one `Step N: <outcome>` line per plan Step whose `failure:`
    starts with `assumed` (ticket 187). Returns "" when no Step is assumed, or
    when plan.md is missing or unreadable, so the PR body stays as before and
    the push never fails."""
    handoff = getattr(run, "context_handoff_dir", None)
    if handoff is None:
        return ""
    path = Path(handoff) / "plan.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    lines = [f"Step {n}: {outcome}" for n, outcome in gates.plan_assumptions(text)]
    return "## Assumptions\n" + "\n".join(lines) + "\n\n" if lines else ""


def _pr_body(run, ticket, base: str) -> str:
    """The full one-screen PR body (spec §10): phase table, last test result, review
    verdict, the plan's assumed Steps (`## Assumptions`, only when there are any),
    diff --stat, the last verify's `Size:` line (only when measured), Trace line —
    sourced from the trace db and the branch itself.

    A pinned Run takes its diff stat from the validated pin; a legacy Run keeps the
    branch-based stat."""
    base_sha = getattr(run, "base_sha", None)
    stat_base = git_helper.validate_run_base(base_sha) if base_sha else base
    return (
        f"## Ask\n{ticket.ask}\n\n"
        f"## Done means\n{ticket.done_means}\n\n"
        f"{_assumptions(run)}"
        "### Phases\n"
        "| name | role | model | tokens | cost | gate |\n|---|---|---|---|---|---|\n"
        f"{_phase_table(run)}\n\n"
        f"### Last test result\n{_last_verify(run)}\n\n"
        f"### Review\n{_last_review(run)}\n\n"
        f"### Diff\n{_size_line(run)}```\n{git_helper.diff_stat(stat_base)}\n```\n\n"
        f"Trace: `lime trace {run.run_id}`\n"
    )


def push_pr(run, ticket, base: str) -> str:
    """Push the branch and open the PR from the current branch onto `base` (the
    default branch NAME — PR target, never the diff identity).

    Validation and the PR body happen before the push, so an invalid pinned history
    can never cause a network side effect."""
    body = _pr_body(run, ticket, base)  # validates the pin and builds the stat first
    with run.phase(
        PhaseParams(
            name="push_pr",
            kind="code",
            stage="Ship",
            owner="git",
            description="Push the branch and open the PR with the full SQLite-sourced body.",
        )
    ) as ph:
        branch = git_helper.current_branch()
        git_helper.push(branch)
        url = git_helper.open_pr(
            title=f"{ticket.slug}: {ticket.title}", body=body, base=base
        )
        try:
            run.tracer.set_pr(run.run_id, git_helper.pr_number(url), url or None)
        except (sqlite3.Error, OSError):
            pass  # a ledger fact for `lime status`; a failed write never fails the delivery
        ph.log(url=url)
        return url
