# A `--rereview` replays earlier verifies as recorded

Status: proposed

Since the reviewer became `openai-codex/gpt-6-astra` (2026-10-10), it refuses a `## Tests` that says only `passed: true` as unproven. Acmeweb Runs `29ec72fe` (ticket 60) and `e031d997` (ticket 56) spent both revise attempts asking for command-specific results while every verify had exited 0. Every phase of those Runs had succeeded, so [ADR 0023](0023-early-diff-checks-and-provenance-gated-resume.md) made `--resume` refuse them, and `--replan` would re-plan and rebuild finished work. The fix has two parts: the verify now carries each quality command's result, and an explicit `--rereview` reopens the review with a fresh verify. The second part needs one exception to ADR 0023.

1. **`--rereview` is an explicit, recorded engineer decision.** `lime run --resume RUN_ID --rereview` records the decision first, then invalidates from the last live verify before the first live review and clears the agent map. Scout, plan and builds replay; verify re-measures the current tree; review and revise run again with the full revise budget. It is refused without `--resume`, alongside `--replan`, `--rescope-event` or `--recovery-decision`, on a `needs_rescope` Run, on a Run that never reached review or already committed its code, and on a changed ticket or plan (that is `--replan`).

2. **Verifies recorded before the decision replay as recorded.** A verify whose seq precedes the latest `--rereview` decision replays, passed or failed, without the provenance check that ADR 0023 item 4 requires. This is the one exception to that item's rule that a pass without matching provenance is never reused. Without it, an earlier verify whose tree has since moved on would miss its provenance, be invalidated, and take its dependent builds with it, which is the rebuild the engineer asked to avoid. Its result steers traversal only; the review reads the fresh verify.
   *Superseded by [ADR 0057](0057-resume-reruns-only-the-latest-verify.md) item 2: every earlier verify replays as recorded, with or without `--rereview`, and no provenance check exists.*

3. **Command evidence travels on the verify.** Each `VerifyOutput` records every quality command's name, command, exit code, passed and duration. The reviewer's `## Tests` lists one line per command with the verified HEAD, marked as recorded by the Spine. A legacy envelope without those lines renders as before.

We considered,

- **`--replan` for a stuck review.** Rejected: the plan was not wrong, and re-planning re-runs the finished builds.
- **Apply the provenance check to earlier verifies too.** Rejected: the tree has moved past them, so every build after the first verify would be invalidated and rebuilt.
- **Re-review on the same verify with no evidence change.** Rejected: the reviewer blocked on the missing command results, so the same verify would draw the same block.

Trade-offs accepted: a verify before the rereview point is trusted without a provenance check, and the trace marks it as replayed from that history. Builds before the point are not re-measured until the fresh verify runs, which happens before the review, so the review never sees the stale result. Each `--rereview` costs one extra full verify and one more review and revise cycle, in exchange for not rebuilding finished work.
