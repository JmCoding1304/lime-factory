# Diff gate — Spine-enforced diff quality before agent review

Before ticket 122, verify ran only the target project's quality commands (test, lint, typecheck, build) and returned. No code checked how many lines the working tree added or whether it carried binaries; the first time anyone saw the diff's shape was the reviewer. That let a builder's stray artefacts — Playwright measurement JSON, PNGs, root-level handoff notes — become PR content unchallenged.

Two changes make the Spine enforce diff quality as a deterministic code check, not as an agent judgement:

1. **Diff gate in verify, after quality commands.** The last action of `verify` measures the working-tree diff against base (resolved via merge-base) and fails when added lines exceed `commit.max_added_lines` (default 1500) or when non-deleted binary files are present. Offending paths are named in the failure text. The gate runs *after* quality commands so command outputs (e.g. a test that writes measurement JSON) are included in the measurement. It runs *before* review so the builder's revise loop removes the artefacts before a reviewer ever sees them.

2. **Re-verify after revise, before re-review.** When a revise session ends, `verify` runs again before the next review. This catches artefacts the builder introduced during revision. If the re-verify fails, its failures are fed as blocking findings to the next revise session — the builder never sees a reviewer ask about generated output.

3. **Commit exclusion.** `commit_all` takes an optional `exclude` list of Git pathspec patterns (default: `["context_handoff/"]`). Before `git add -A`, already-staged changes matching those patterns are unstaged via `git restore --staged --source=HEAD` (index-only, worktree preserved). Then `git add -A` is called with `:(exclude)<pattern>` pathspecs. The `commit_code` and `commit_docs` phases log every excluded path.

We considered,

- **Letting the reviewer catch artefacts.** Rejected: by the time the reviewer runs, the artefacts are already in the commit — the reviewer can reject, but the builder must revise *and* re-commit. Catching them in verify saves a full review round.
- **A separate diff-gate phase.** Rejected: verify already runs just before review; adding a new phase would clutter the phase library for no behavioural gain. The gate is parameterless from the caller's view.
- **Using `git add -A` then `git reset` for exclusion.** Rejected: reset removes staged changes but also discards add metadata; pathspec exclusions on `git add` are the canonical Git approach.

Trade-offs accepted: the gate is a hard ceiling, not a warning — the builder cannot proceed until the diff fits. This forces artefact cleanup but also means a genuine large change (e.g. a migration script) must either raise `max_added_lines` in config, be broken into smaller steps, or be named in `commit.uncounted` when the engineer judges it appropriate — see [ADR 0018](0018-uncounted-paths-ship-without-counting.md). The builder does not exempt its own work; it reports the required file and the engineer decides. Running verify before review also means the verify phase must measure the diff against the configured base, not HEAD; `feature.run` passes `base_branch` into every `verify` call.