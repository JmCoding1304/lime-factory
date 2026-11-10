# Deploy tests the merged tree before merging

The **Run base pin** (ADR 0024) gives every Run an immutable birth base, but `lime deploy` merged as soon as the PR was OPEN and GitHub's check rollup was green. That rollup was computed when the PR opened and does not re-run when the default branch moves afterwards, so a Run verified against a base that has since advanced is merged on evidence about a tree that no longer exists: shop-api 37's direct writer merged 24 s after 35's sole-writer rule that 37's own verify never saw. Nothing in the deploy path ever ran the target project's `commands.test` on default-plus-this-branch.

1. **The pre-merge gate.** After the PR-not-open and CI-red refusals, and before any merge, fast-forward, teardown or interactive session, `_cmd_deploy` fetches origin, resolves `origin/<default>`, and — only when the Run's `base_sha` is behind that tip — builds the actual merged tree to test it. A NULL pin is never read as unmoved: a **Legacy Run** keeps the conservative branch-based behaviour and is tested, never skipped. When the pin is proven unmoved (see ADR 0033) the gate is skipped and prints `base unmoved`, because the Run's own verify already tested that exact tree.

2. **A throwaway detached worktree.** The merged tree is `git worktree add --detach` at the default tip with the PR branch merged in `--no-commit --no-ff`, in a temporary directory. Only that throwaway path is written: the Run's checkout, the default checkout and every ref are untouched — main is never merged or rebased into the Run branch (ticket 144), no checkout switches, no merge commit. The worktree is removed on every exit — success, refusal, body exception, interruption and a partial `worktree add` — and a cleanup failure refuses the deploy rather than merging.

3. **Setup and test through verify's own runner.** The gate runs `commands.setup` then `commands.test` in that tree via `quality.run_check` (the same `_run` verify uses), in a `context_handoff/deploy-check/<uuid>/` log dir so evidence never overwrites the Run's verify and survives the worktree's deletion. A non-zero command refuses with exit 1 before any merge, printing the tail and the log path. No retry and no re-run of verify inside the Run.

4. **Refusals are the existing refusals.** A merge conflict refuses naming the unmerged files; a missing remote/default/PR ref, an unreadable config, or a failed fetch all refuse rather than falling through to a merge. Every refusal is exit 1 before `merge_pr`, like the PR-not-open and CI-red refusals — the Run stays `success`, ready for a later deploy.

5. **`lime deploy --check` reports base movement.** For every open Run whose pin is behind the fetched default tip, it prints `base moved: N merges since base`, where `N` counts first-parent merge commits on default since the pin — not total commits, so a linear-behind history legitimately reports zero. It is read-only: no status writes, no worktree, no checkout modification.

We considered,

- **Require GitHub branch protection or a merge queue on the PR.** Rejected: the factory cannot assume the target project's remote enforces either, and the evidence GitHub keeps is about the PR-open tree, not the tree main is now.
- **Re-run verify inside the Run.** Rejected: verify's job is to gate the Run's own branch against its birth base; re-running it does not test the merge with a moved default.
- **Merge main into the Run branch, then verify.** Rejected outright (ticket 144): the Run branch must never carry default's commits; the merge belongs in a throwaway tree, never in the Run.
- **Retry a red merged-tree test.** Rejected: a red test is a real finding, not a transient; an automatic retry would only delay the refusal.
- **Skip the gate when `commands.test` is unset.** Rejected for the moved case: a moved base with no test command cannot be verified, so the deploy refuses and says so.

Trade-offs accepted: every deploy whose base has moved pays a fetch, a temporary worktree and a full setup-plus-test run before it is allowed to merge — deploy gets slower and can now refuse a PR that GitHub calls green. We accepted that because a merge that skips the merged-tree test can land a combination nobody verified, and the refusal is cheap and reversible while a bad merge on default is neither. Equality skips the gate and prints `base unmoved`, because the Run's own verify already tested that exact tree; ADR 0033 widens that skip to a pin whose only commits past the tip are this Run's own launch commits.
