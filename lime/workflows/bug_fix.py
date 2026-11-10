"""The bug-fix Chain — same steps as `feature.py`, run through it via
`ChainPrompts`. The plan addendum deviates from
specs/assets/10-phase-library-and-chains.md line 55 on purpose: that
line orders a failing-test step before the fix, and `verify` gates every
step on the full suite, so the red step burned all build attempts twice
(runs cf94034b, a60391c1; ticket 65). Review addendum still
string-for-string.
"""

from __future__ import annotations

from .. import tracker
from . import feature

PLAN_ADDENDUM = (
    "\n\nReproduce first. Find the root cause, not the symptom. One step: the regression"
    " test that proves the bug and the fix that makes it pass land together, so the step"
    " ends with the suite green. Never a separate failing-test step — verify runs the whole"
    " suite after every step, and a red step burns every build attempt. No other steps"
    " unless the fix forces them."
)

REVIEW_ADDENDUM = (
    "\n\nBlocking unless: a regression test exists and fails without the fix; the fix"
    " addresses the root cause named in plan.md."
)


def run(run, ticket: tracker.Ticket, base_branch: str) -> int:
    """Run the bug-fix Chain over one ticket — feature.py's phases, reproduce-first prompts.

    Delegation keeps the SAME Run object, so its pinned birth identity (`Run.base_sha`)
    is retained through `feature.run`; `base_branch` stays the PR target name only.
    """
    prompt = tracker.prompt(ticket)
    prompts = feature.ChainPrompts(plan=prompt + PLAN_ADDENDUM, review=prompt + REVIEW_ADDENDUM)
    return feature.run(run, ticket, base_branch, prompts=prompts)
