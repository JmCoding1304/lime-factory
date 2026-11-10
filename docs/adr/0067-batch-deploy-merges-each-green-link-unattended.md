# A `--deploy` batch merges each green link unattended

Status: proposed

A batch (`lime run A B C`) gave up on any ticket whose `Blocked by:` blocker had not merged — `_report_stalled` printed "still blocked, giving up" — so 289 merged tickets spent 331 h inside Runs and 1,437–2,548 h waiting between chain links. The waiting is the chain serialising on an engineer running `lime deploy` between links. This makes the deploy a step of the batch, opt-in per project.

1. **Opt-in is a top-level `auto_deploy` key.** `auto_deploy: off | staging | prod` says what an unattended merge ships to; a missing key means `off`. `lime run ... --deploy` is refused (exit 2, before anything launches) when the key is off or missing, and `lime check` rejects any value outside the three. The default template stamps `auto_deploy: off`, so no project merges unattended until its engineer writes `staging` or `prod`.

2. **The batch deploys through the unchanged `lime deploy` path.** Each child that ends success is carried through `_cmd_deploy` — the same gates as an engineer's `lime deploy <ticket>`: PR open, CI green, the merged-tree test ([ADR 0029](0029-deploy-tests-the-merged-tree-before-merging.md)), merge, fast-forward, ticket-file delete, teardown, `deployed` status. No gate is skipped or weakened; the batch adds no merge, fast-forward, gate, session or status writer of its own.

3. **A fixed CI ceiling before deploying.** The batch polls the child's PR CI read-only for a fixed 30-minute ceiling of waiting (`DEPLOY_CI_CEILING_S` / `DEPLOY_CI_POLL_S`: a bounded count of 60 polls 30 s apart, so each `gh pr view`'s own time adds to it), and gives up as `pending` at the ceiling. It never decides a deploy — `_cmd_deploy` re-checks PR state and CI itself.

4. **A deployed blocker unblocks its dependents in the same batch.** `_pick_ready` already treats a `Blocked by:` whose ticket file is gone as ready; a successful deploy deletes that file, so the next scheduling pass launches the dependent instead of reporting it stalled. A red link leaves its ticket file, so its descendants stay held while independent tickets go on; the batch returns 1 when any ticket was not deployed.

5. **One launch lock serialises fetches and deploys.** Every launch fetch (`unpushed_commits`, `live_overlap`, `reserve`, `worktree.create`) and the batch's deploy share one project-wide kernel lock (`fcntl.flock` on `.lime/runs/launch.lock`), so a deploy's fetch never races a child's launch fetch (`cannot lock ref`). No child launches while a deploy runs, and no deploy starts while a child is in its launch fetch.

6. **Two exit-0 deploy outcomes must not advance the chain.** A refused fast-forward leaves the ticket file behind, and the Deploy session runs only for a non-empty `deploy:` list. The batch stops at either link and says so: the surviving ticket file holds the dependents, and a non-empty `deploy:` prints the attended-deploy instruction instead of running the session.

7. **Fresh tickets only.** `--deploy` combines with neither `--resume` nor `--allow-overlap`.

We considered,

- **Deploy outside the batch, from a cron or daemon.** Rejected: the factory is a single process per batch and a Run itself has no human in it; a second scheduler is a new surface the ticket did not need.
- **Make every deploy unattended by default.** Rejected: a merge is the one irreversible step; the engineer opts a project in with `auto_deploy` rather than inheriting it.
- **Let the batch run the Deploy session.** Rejected: the session is interactive and the batch is not; a non-empty `deploy:` stops the link and names the attended command.
- **Treat CI pending as success and merge.** Rejected: a pending check is not green; the batch waits a bounded time and then holds the link.

Trade-offs accepted: an unattended batch can merge to the default branch and ship to `staging`/`prod` with no human watching, bounded only by the same gates an engineer's `lime deploy` runs; the CI ceiling is a fixed 30 minutes, so a slow PR holds its dependents until the next batch. The Deploy session stays a human step by design.
