# `base unmoved` proves only the Run's own launch commits

The **Merged-tree gate** (ADR 0029) skips the pre-merge check when the Run's **Run base pin** equals the fetched default tip, printing `base unmoved`. But a real Run's pin is its own `ticket: <stem>` launch commit on the Run branch — one commit past the default tip, written by `worktree.create` — so the pin and the tip are never equal and the skip was dead for every real Run: a Run launched from an unchanged default still paid a fetch, a throwaway worktree and a full setup-plus-test before it could merge. The check the ticket asks for is that "base unmoved" hold when the default branch tip is the commit the Run was launched from.

1. **The skip is a proof, not an equality test.** `_deploy_base_unmoved` returns true on exact equality (the birth case where the identical ticket was already tracked, so `worktree.create` made no launch commit), or when the fetched default tip is an ancestor of the pin and every commit in `default_sha..base_sha` is one of this Run's own launch commits. A NULL legacy pin is never unmoved.

2. **A launch commit is recognized exactly.** Each commit in the range is inspected against its own parent: subject `ticket: <stem>`, trailers `Lime-Run: <run_id>` and `Lime-Role: orchestrator`, and a changed-path set restricted to `.lime/tickets/<stem>.md`. The range must be nonempty and linear (exactly one parent per commit), paths are read NUL-separated with renames disabled, and a merge, ordinary work, another Run's ticket commit or an extra path fails the proof.

3. **Never skip on doubt.** A non-ancestor pin, a missing or unreadable object, malformed metadata, an unrecognized branch shape or any failed git read returns false, and the caller runs the full merged-tree gate. The predicate is read-only: it writes no ref, checkout, row or cache, and interrupts propagate.

4. **`_check_deploy_merged_tree` and what it runs are unchanged after the decision.** The skip happens at the same point — after the OPEN/CI-green refusals and before any merge, fast-forward, teardown or interactive session — and the configured setup, test and teardown and the merged tree they run against are left exactly as ADR 0029 and ADR 0032 wrote them.

We considered,

- **Infer the launch parent from the pin alone** (`base_sha^` is the default tip). Rejected: it lets any commit whose parent is the tip — ordinary work, another Run's ticket commit — read as unmoved, so the skip would lose the guarantee that only this Run's launch intervened.
- **Trust the stored pin to be a launch commit without reading the range.** Rejected: the pin is a hash, not a promise about the history between the tip and it; checking each commit and its paths is what makes the claim hold.
- **Skip on any ancestor relationship.** Rejected: a default tip that is an ancestor of the pin says nothing about the commits between them; a merge or a sibling's work between tip and pin must run the gate.
- **Keep the exact-equality skip and document the check as always running for real Runs.** Rejected: it leaves every unchanged-default deploy paying the full merged-tree cost for a tree its own verify already tested.

Trade-offs accepted: the deploy path now runs several read-only git reads to prove a launch-only range before it may skip — a small, bounded cost paid on every deploy to decide the gate. We accepted that because the alternative is either a skip that never fires for real Runs or a skip that fires on a parent-only guess and can merge an unverified tree. The proof fails closed, so a wrong answer can only cost a gate run, never a bad merge.
