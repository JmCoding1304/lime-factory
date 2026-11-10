"""The feature chain (spec §7) — greenfield and front-end are feature tickets
that run this same file, not Chains of their own (CONTEXT.md: "only bug-fix
has its own Chain").

`engineer -> setup -> scout -> plan -> per Step N: build(N) -> verify [red ->
build(N, previous), <=3 attempts, then failed at N] -> review -> (revise -> review, <=2,
trust_violations always blocking) -> commit(code) -> changes ->
document -> commit(docs) -> push_pr` (ticket 28, 29). The scout gets the
bare ticket text and writes `scout_findings.md` into the handoff dir; the
planner reads it before planning (ticket 71). A scout that fails does not stop
the Run: its Phase stays `fail` in the trace and the planner gets a findings
file saying why (ticket 197). Greenfield is this same
chain plus a `Spec:` field and front-end plus a `Prototype:` field —
`tracker.prompt()` already threads both into the ask (ticket 31); the only
front-end-specific code here is the docs commit's conditional removal of a
consumed prototype route directory — it is retained while another open ticket in
the main checkout still names it or a descendant, and removed by the last close.
A `Workflow: bug-fix` ticket is handed straight to `bug_fix.py`
(cli.py's one `lime run` call site always calls `run()` here regardless of
Workflow, so the dispatch has to happen on entry); `bug_fix.py` re-enters
here with its own plan/review prompts via `prompts`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import BaseModel

from .. import budget, git_helper, limits, tracker
from ..data_types import BuildStep, ReviewOutput, ReviewStep
from . import phases

MAX_BUILD_ATTEMPTS = 3  # red -> retry, <=3 attempts per Step (spec §7)
MAX_REVISE_ATTEMPTS = 2  # review -> (revise -> review), <=2 cycles (spec §7)

SCOUT_LEAD = (
    "Brief the planner before it plans this ticket: for every file, module or subsystem the"
    " ticket names or touches, where it lives, what it does today, the conventions around it"
    " and the tests that cover it. Paths and one-line notes; never file contents."
    " For every table column, file or queue the ticket writes, list every existing writer and"
    " reader as path:line — admin, signals, jobs, API and tests included."
    " Report what the code does today as path:line facts; never whether the ticket is done or"
    " blocked, or how to plan it — the planner judges that."
)

_STEP_HEADING = re.compile(r"^## Step (\d+)\b.*$", re.MULTILINE)


class ChainPrompts(BaseModel):
    """A Chain's plan/review prompt variant (spec asset 10: "planner `prompt`
    += ...", "reviewer `prompt` += ..."). `bug_fix.py` is the only producer
    today; feature/greenfield/front-end use the ticket's own text for both."""

    plan: str
    review: str


def _steps(plan_md: str) -> list[str]:
    """Split plan.md on `## Step N` headings; each chunk is that step's full text."""
    matches = list(_STEP_HEADING.finditer(plan_md))
    if not matches:
        raise RuntimeError("plan.md has no `## Step N` sections — the planner failed its contract")
    return [
        plan_md[m.start() : (matches[i + 1].start() if i + 1 < len(matches) else len(plan_md))].strip()
        for i, m in enumerate(matches)
    ]


def _build_prompt(run, step_no: int, step_text: str, retry: bool) -> str:
    plan_path = run.context_handoff_dir / "plan.md"
    lead = (
        f"Step {step_no} still fails verification — read the failures in previous_envelope"
        f" and fix them. Full plan at {plan_path}."
        if retry
        else f"Read the plan at {plan_path} for context, then implement Step {step_no} below."
        " Work only on this step; do not implement other steps."
    )
    result = f"{lead}\n\n{step_text}"
    if step_no > 1 or retry:
        result += (
            f"\n\nThis is a fresh session: earlier work is already in the worktree, not in your memory."
            f" Do not redo it.\n\n{budget.git_facts(run.repo_root, 0)}"
        )
    # One static limits line per builder call, separate from the dynamic fresh-session git facts.
    return limits.append_builder(result, run.agent("builder"), run.cfg)


def _ticket_sha256(ticket: tracker.Ticket) -> str:
    """SHA-256 over the authoritative original ticket file bytes (ticket 132)."""
    try:
        return hashlib.sha256(Path(ticket.path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _invalidate_for_replan(run, step_no: int) -> None:
    """A step that burned every build attempt indicts the plan, not just the step.

    The phases still read `success` — verify's command ran, the builder returned an
    envelope — so `--resume` would replay the whole chain from cache and do no work
    (ticket 49). Record the invalidation instead: the plan phase and everything after
    it stop being replayable, so a resume re-plans against the edited ticket. The
    cached plan.md goes with them — disk and sqlite must not disagree.
    """
    reason = f"step {step_no} exhausted {MAX_BUILD_ATTEMPTS} build attempts"
    dropped = run.tracer.invalidate_from(run.run_id, "plan", reason=reason)
    (run.context_handoff_dir / "plan.md").unlink(missing_ok=True)
    if dropped:
        run.console.note(f"invalidated for re-plan: {', '.join(dropped)}")


def run(run, ticket: tracker.Ticket, base_branch: str, prompts: ChainPrompts | None = None) -> int:
    """Run the feature chain over one ticket. Returns the process exit code.

    `prompts` is `bug_fix.py`'s plan/review variant; a bare `Workflow: bug-fix`
    ticket (prompts is None) is dispatched to `bug_fix.run()` instead, which
    calls back in here with its own `ChainPrompts`.

    The chain body is wrapped so an `InvalidRunBase` — an invalid pinned birth base,
    which can arise inside a Phase but also outside one (a guard before replay or
    push) — stops the Run here: an error is traced, the reason is printed, and the
    Run finishes `fail` with its worktree/branch/plan/content preserved. No
    retry/revise/replan, commit, document, push or PR follows.
    """
    if prompts is None and ticket.workflow == "bug-fix":
        from . import bug_fix  # deferred: bug_fix imports this module at load time

        return bug_fix.run(run, ticket, base_branch)
    try:
        return _run_chain(run, ticket, base_branch, prompts)
    except git_helper.InvalidRunBase as error:
        run.tracer.event(
            run.run_id, "error", name="invalid_run_base",
            payload={"reason": str(error), "base_sha": getattr(run, "base_sha", None)},
        )
        run.console.note(str(error))
        run.close_run(ok=False)
        return run.finish(accepted=False, reason=str(error))


def _run_chain(
    run, ticket: tracker.Ticket, base_branch: str, prompts: ChainPrompts | None = None
) -> int:
    """The feature chain body — see `run()` for the boundary guarantee."""
    prompt = tracker.prompt(ticket)
    plan_prompt = prompts.plan if prompts else prompt
    review_prompt = prompts.review if prompts else prompt
    # The diff identity: a pinned Run measures against its immutable birth base;
    # a legacy Run keeps the base branch. `base_branch` stays the PR target name.
    diff_base = getattr(run, "base_sha", None) or base_branch

    def _review(previous, attempt):
        return phases.review(run, ReviewStep(prompt=review_prompt, checklist=ticket.done_means,
                                             base=diff_base, previous=previous, attempt=attempt))

    phases.engineer(run, prompt)
    phases.setup(run)

    phases.scout(run, f"{SCOUT_LEAD}\n\n{prompt}", fatal=False)

    phases.plan(run, plan_prompt, ticket_sha256=_ticket_sha256(ticket))
    plan_path = run.context_handoff_dir / "plan.md"
    if not plan_path.exists():
        raise RuntimeError("planner reported success but wrote no plan.md")
    steps = _steps(plan_path.read_text())

    last_build = None
    for step_no, step_text in enumerate(steps, start=1):
        previous = None
        for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
            build_prompt = _build_prompt(
                run, step_no, step_text, retry=attempt > 1
            )
            last_build = phases.build(
                run, BuildStep(step_no=step_no, prompt=build_prompt, previous=previous, attempt=attempt)
            )
            verify_result = phases.verify(run, step_no, attempt=attempt, base=diff_base)
            if verify_result.passed:
                break
            previous = verify_result
        else:
            _invalidate_for_replan(run, step_no)
            return run.finish(
                accepted=False,
                reason=f"step {step_no} still failed verify after {MAX_BUILD_ATTEMPTS} attempts",
            )

    review_output = _review(last_build, 1)
    last_change = last_build  # the most recent BuildOutput — a revise supersedes the step build
    revise_attempt = 0
    while not review_output.approved or review_output.trust_violations:
        revise_attempt += 1
        if revise_attempt > MAX_REVISE_ATTEMPTS:
            reason = (
                f"trust violation(s) still present after {MAX_REVISE_ATTEMPTS} revise attempt(s): "
                + "; ".join(review_output.trust_violations)
                if review_output.trust_violations
                else f"review still not approved after {MAX_REVISE_ATTEMPTS} revise attempt(s): "
                + "; ".join(review_output.blocking)
            )
            return run.finish(accepted=False, reason=reason)
        last_change = phases.revise(
            run, review_output, attempt=revise_attempt
        )

        # Re-verify after revise before re-review (ticket 122)
        verify_result = phases.verify(
            run, step_no, attempt=MAX_BUILD_ATTEMPTS + revise_attempt, base=diff_base,
        )
        if not verify_result.passed:
            # Feed verify failures as blocking items to the revise loop
            review_output = ReviewOutput(
                status="fail",
                summary="re-verify after revise failed",
                approved=False,
                blocking=[f"Diff gate / quality: {f}" for f in verify_result.failures],
                trust_violations=[],
                artifacts=[],
                notes_for_next_agent="",
                findings=[],
            )
            continue  # continue the revise loop to address verify failures

        review_output = _review(last_change, revise_attempt + 1)

    message = (last_change.commit_message if last_change else "") or f"{ticket.slug}: {ticket.title}"
    phases.commit(run, message)

    change_envelope = phases.changes(run, diff_base)
    document_output = phases.document(run, prompt, change_envelope)
    docs_message = document_output.commit_message or f"lime({run.run_id}): close ticket {ticket.number}"
    ticket_path = run.repo_root / ".lime" / "tickets" / Path(ticket.path).name
    route_dir = (run.repo_root / ticket.prototype) if ticket.prototype else None
    # A prototype directory is shared with the main checkout, whose ticket store is
    # authoritative: another open ticket there may still name it. `None` means that
    # store is unknown — never a fallback to this worktree. No prototype, no lookup.
    tickets_dir = None
    if route_dir is not None:
        primary_root = git_helper.main_checkout_root(Path(run.repo_root))
        if primary_root is not None:
            tickets_dir = primary_root / ".lime" / "tickets"
    phases.commit_docs(run, docs_message, ticket_path, route_dir, tickets_dir=tickets_dir)

    phases.push_pr(run, ticket, base_branch)
    return run.finish(accepted=True)
