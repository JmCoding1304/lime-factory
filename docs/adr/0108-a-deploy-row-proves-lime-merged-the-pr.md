# A deploy row proves lime merged the PR, so a re-run finishes the deploy

Status: accepted

`lime deploy` merges a Run's PR, then fast-forwards local main, unlinks the ticket copy, tears down the worktree and marks the Run `deployed`. If the process died after the merge, the re-run found the PR MERGED, refused it as `hand_merge` and left the Run `success` with its dependents held. Ticket 252 opens a `kind='deploy'` process row on the Run before the merge, so a MERGED PR whose Run has an earlier deploy row is a deploy lime began, not a hand merge.

1. **An earlier deploy row is the proof.** A MERGED PR whose Run has a `deploy` process row from an earlier deploy is finished, not refused. The re-run skips the merged-tree gate, the CI check and the merge, then runs the fast-forward, the ticket unlink, the teardown and the `deployed` status. Each of those steps passes when it is already done. A MERGED PR with no earlier row for that Run is still refused as `hand_merge`, and another Run's row does not count.

2. **The gate runs before lime's merge, not again.** ADR 0029's merged-tree gate still runs before every merge lime makes. A finishing re-run makes no merge, so it has no gate to run.

3. **A finishing run that dies again stays `success`.** The next re-run finishes it the same way. The finishing run opens its own deploy row, so the proof holds for every later re-run.

We considered,

- **Keep refusing a MERGED PR as `hand_merge`.** Rejected: the remaining steps are lime's own work, and the dependents stay held until someone finishes them by hand.
- **Accept any MERGED PR as lime's.** Rejected: a hand merge would then skip the merged-tree gate and the CI check, which is what the refusal exists to stop.

Trade-offs accepted:

- The row proves that a deploy was opened for the Run, not that lime's `merge_pr` returned. The row opens before the merged-tree gate, so a deploy that the gate refused before any merge leaves a row too. If the engineer then merges that PR by hand, the re-run finishes it and skips the gate. The row cannot tell the two cases apart. The owner should decide on this gap at QA.
- A finishing re-run relies on the first attempt's checks having passed. It does not re-check CI or re-test the merged tree.
