# A batch's queue in lime.db is a mirror, never the authority for a launch

Status: accepted

A `lime run` batch keeps its queue only in memory (`_run_batch`), so a batch that dies leaves no record of what it was about to launch. Ticket 254 mirrors that queue into lime.db: a `batch` process row that beats, and one `batch_children` row per ticket argument with a `queued`, `started` or `dropped` state. Those states look like a queue, and the queue it replaces the in-memory one for is the one the factory deleted unbuilt (tickets 104–119, `~/.lime/queue.db`), which was the authority for launches, admission and restart repair. A future reader could easily make the table the thing the batch reads back.

1. **The in-memory `pending` list is the only scheduler.** Nothing reads `batch_children` to decide what to launch, skip or resume. The rows describe the batch's memory for observers. Only `lime/batch.py` writes them.
   *Superseded in part by [ADR 0120](0120-dead-batch-retried-once-by-a-watchdog.md) item 3: a retry batch reads its dead parent's `queued` rows once, at claim time. The live scheduler is unchanged.*

2. **A mirror write never blocks the batch.** A failed write is logged and retried on the next loop pass. The launch, the outcome, the exit code and the report never depend on the ledger, so the rows can lag or miss a write while the batch still runs every ticket.

3. **A dead batch's queue is not resumed.** Its process row stops beating, so liveness reads the batch dead and its queued rows stop counting. Nothing restarts from those rows. Any retry is a new batch with its own rows.
   *Superseded in part by [ADR 0120](0120-dead-batch-retried-once-by-a-watchdog.md) item 3: a watchdog launches one retry batch, which relaunches the `queued` children. "Any retry is a new batch with its own rows" still stands.*

We considered,

- **Making `batch_children` the queue the batch reads back, so a dead batch resumes from it.** Rejected: that needs admission, dedupe and restart repair, the machinery of tickets 104–119 that was deleted. A table that is never read cannot strand a launch.
- **Keeping the queue in memory only, with no table.** Rejected: a dead batch would vanish without a trace, and the dashboard could never show a queued child.

Trade-offs accepted:

- A dead batch's queued tickets are not relaunched by lime. Recovering them needs a separate retry, which is new work, not a property of this table.
  *Superseded by [ADR 0120](0120-dead-batch-retried-once-by-a-watchdog.md): one retry batch relaunches them.*
- The mirror can be stale for a while after a failed write, and the failure is only logged. A reader of the rows sees the batch's last successful write, not its live state.
- The `queued` / `started` / `dropped` vocabulary reads like a queue to anyone who does not know this ADR. The module docstring in `lime/batch.py` is the only other place that says the table is not one.
