# The Run row keeps its PR, and refusals are trace events

Status: accepted

Ticket 61 kept PR numbers out of the ledger: a PR was found only by its head branch, through `gh`, one call per item. Ready and Done in the report need the PR of every Run, and a refusal (an overlap hold, red CI, a merge conflict) left no record at all, so the report had no source for a `refusal` row. This records both in the trace ledger, and reverses ticket 61's "PR numbers are never persisted".

1. **The Run row stores `pr_number` and `pr_url`.** `push_pr` writes them through `Tracer.set_pr`, the only writer of the pair. A resumed Run whose PR already exists gets that PR back from `gh pr view` and keeps one number and one URL. A legacy ledger gains both columns on its next read-write open. Older Runs stay NULL and are never backfilled; `merged_prs` keeps the head branch as its join key for them.

2. **A refusal is a `refusal` event.** Its `name` is the reason (`overlap_refused`, `overlap_held`, `pr_not_open`, `hand_merge`, `ci_<state>`, `merge_conflict`, `merged_tree_failed`) and its payload carries the ticket stem plus the detail (the holder map and paths, the PR state, the failed check). A refusal raised before any Run row exists is stored under the sentinel `run_id` `-` (`tracer.NO_RUN`). That row has no Run folder and no `events.jsonl` line, so `lime.db` is its only record. This is the one exception to "Raw JSONL is the source of truth" in CONTEXT.md **Trace**.

3. **Recording is best-effort.** A failed column or event write is swallowed, so a command's printed output and exit code never depend on the ledger. A `NO_RUN` refusal is not a launch and holds nothing, because the Local Run holder reads Run rows only.

We considered,

- **Create a Run row for a refusal raised before the Run exists.** Rejected: a row is a Run. The Local Run holder reads Run rows, so a refused launch would hold the very ticket it refused, and `lime runs` would list a Run that never started.
- **Call `gh` per item for the PR instead.** Rejected: Ready and Done would cost one `gh` call per item.
- **Make a failed ledger write fail the command.** Rejected: the delivery and the refusal are already decided when the write runs; a ledger fault must not turn a refused deploy into a crash or a delivered PR into an error.

Trade-offs accepted: with an unwritable ledger, a refusal or a PR number can be silently missing, so an absent row is not proof that nothing was refused. Runs before this change have no PR number. The `NO_RUN` row is the one place the ledger holds a fact that no JSONL file holds.
