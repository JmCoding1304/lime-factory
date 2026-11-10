# Diff size is measured and logged, never a stop

Status: proposed

[ADR 0017](0017-diff-gate-before-agent-review.md) made `commit.max_added_lines` a hard ceiling, and [ADR 0019](0019-measured-diff-cap-overflow-stops-the-chain.md) made an overflow stop the Chain and preserve the Run as `needs_rescope`. [ADR 0023](0023-early-diff-checks-and-provenance-gated-resume.md) added an early check between build steps and an 80% advisory to the builder. In 367 Runs the overflow stop fired 8 times. Each time the Run was let through (the cap was raised, the overflow accepted, or the measurement was wrong), except one ticket that was split. The cap drifted from 1500 to 3000, 4000 and then 5000 to fit the work. A launched Run must finish without a human, and a stop that is always overridden only adds a human step.

1. **`commit.max_added_lines` is a sizing budget.** Verify measures the counted added lines, logs them against the budget, and never fails or stops a Run for size. A Run over the budget goes on through review, docs and PR like any other Run. Non-deleted binary files still fail verify, and `commit.exclude` and `commit.uncounted` are still honored.

2. **Size is decided before the build, not during it.** The planner receives the budget and sizes each Step against it, proposing a `Blocked by:` split when the work cannot fit. Ticket cutting is sized against the same resolved limits. Neither produces a numerical fail gate.

3. **The builder never sees the number.** Its limits line carries only its Session time and token ceilings. The early diff check between build steps and the 80% advisory are deleted, because the advisory nudged the builder to squeeze code, which is what [ADR 0019](0019-measured-diff-cap-overflow-stops-the-chain.md) feared.

4. **This supersedes ADR 0019 and ADR 0023 items 2 and 3.** A measured overflow no longer closes a Run as `needs_rescope`. The status remains only so Runs already preserved in it still load. Its flags and recovery decision ([ADR 0021](0021-event-bound-recovery-of-a-preserved-overflow.md)) were removed by [ADR 0062](0062-delete-needs-rescope-keep-legacy-runs-readable.md).

We considered,

- **Keep the stop and raise the cap again.** Rejected: the cap has already moved from 1500 to 5000, and each raise only moves the stop further out.
- **Keep the 80% advisory as a builder nudge.** Rejected: it pushes the builder to compress code the ticket needs, and it never changed a Run's outcome.
- **Gate the plan on a planner size estimate.** Rejected: an estimate is advice. The owner chose to measure and never stop.

Trade-offs accepted: a Run can open a PR larger than its budget, and nothing stops it on the way. The budget shapes how tickets are cut and how steps are planned, and the engineer reviews the PR. Oversize shows only in the verify log until a later change puts it in the PR body and `lime runs`.
