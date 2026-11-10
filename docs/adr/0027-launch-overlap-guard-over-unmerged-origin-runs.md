# Launch refuses a ticket whose Where paths collide with an unmerged origin Run

Every Run started from the same origin tip without looking at what sibling Runs were editing: shop-api 35, 37 and 38 launched in the same second and merged 24 s apart, and 37's direct writer landed after 35's sole-writer rule. Two Runs editing the same path converge or clobber by merge order, and nobody notices until the merge. The tracker knew the ticket's `## Where` and git knew the live `origin/lime/*` branches; nothing compared them at launch.

1. **Where paths are the first backticked path on each `## Where` line.** `tracker.where_paths()` takes each line's first single-backtick token and keeps only path-shaped ones (a slash, or a filename with an extension, dotfiles included), dropping symbols, signatures, comma lists and prose, deduplicated on a lexical `normalize_path` that strips a leading `./` and a trailing slash. Existence is not required, so a new or remote-only path counts exactly like an existing one. This is the only contract the ticket shape exposes to the guard; the cookbook tells the author it exists.

2. **Overlap is exact or directory-prefix, in either direction, against unmerged origin Runs.** `overlap.live_overlap()` refreshes `--prune`, enumerates `refs/remotes/origin/lime/**` not merged into `origin/<default>`, reads each branch's *own* ticket via `git show <rev>:<path>` — at its tip, else at the newest commit the branch carried it in, since `commit_docs` deletes it before push (boundary-safe: `35-` never matches `350-`), and returns `{branch: intersecting paths}`. A branch named under the candidate's `Blocked by:` is exempt from this check only — blocker refusal itself is unchanged.

3. **The guard refuses before reserving anything.** In `_run_ticket`, a non-empty overlap without `--allow-overlap` prints each branch and its paths and exits 2 before `reserve`, worktree creation or a session; nothing is reserved. `--allow-overlap` is a single fresh-ticket escape hatch, rejected (exit 2) for a batch or `--resume`.

4. **A batch never picks a held ticket and says why it stopped.** `_pick_ready` skips (never pops) an overlapping candidate so a later check can clear it; once no child is active and only held/blocked tickets remain, `_report_stalled` prints `held: overlap with <branch>` (or `still blocked`) and the batch exits non-zero.

5. **Read-only, best-effort, fail-closed without origin.** Everything runs through list-form `git -C <root>` with no checkout/merge/push and no long-lived cache. A repo with no origin returns `{}` and launches unchanged; a remote that cannot be fetched, enumerated or read raises `OverlapError` and the launch is refused rather than assumed permission.

We considered,

- **File locks or a scheduler.** Rejected (and out of scope, ticket 162's merged-tree test covers overlap by table or row): the guard is a read-only observation of what a sibling already pushed, not a mutual-exclusion protocol. It promises no atomic simultaneous-launch exclusion — two tickets that overlap and launch in the same instant may still both start; it catches the case that matters, a ticket launched against a Run that is already live.
- **A local active-ticket lock.** Rejected: the live set is the unmerged origin branches, which survive a machine restart and are visible from any clone; a local lock would be blind to a Run started elsewhere.
- **Treat a broken remote as "nothing overlaps".** Rejected: silently allowing a launch the guard could not check defeats it; the failure is reported and the launch refused.
- **Infer paths from Ask/Done means or the filesystem.** Rejected: the guard is lexical and deterministic, so it agrees with what the ticket actually names and never depends on which files exist locally.

Trade-offs accepted: a ticket whose `## Where` is malformed (no backticked paths) is never guarded, so authoring discipline is load-bearing; the guard can hold a ticket longer than a human would once a branch is live but not yet merged, cleared only by that branch merging or `--allow-overlap`; and `--allow-overlap` is deliberately narrow (one fresh ticket) rather than a general override. Reservation, `Blocked by:` semantics and the never-merge-main rule are unchanged.
