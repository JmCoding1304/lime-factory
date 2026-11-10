# A dead batch is retried once, by a watchdog the batch starts beside itself

Status: accepted

A `lime run` batch that dies without closing its `batch` row (crash, OOM, `kill -9`, a closed window) used to leave its queued tickets unlaunched, with nothing to say so. Ticket 255 adds a **Watchdog**: a plain process the batch starts beside itself, in its own session, that waits on a pipe the batch holds. When the pipe closes, the watchdog reads the batch's row. Closed (finished, Ctrl-C, `kill`) means it exits. Open means it claims the batch and launches one **Retry batch**, `lime run <tickets> --deploy --retry-of <batch_id>`, with the dead batch's profile. That retry launches the dead batch's `queued` children. A retry starts no watchdog, so a retry that dies is never retried.

This supersedes ADR 0111 item 3 in part (a dead batch's queued children are now relaunched once) and item 1 in the same respect (a retry reads `batch_children` to decide what to launch). ADR 0111's other items stand.

1. **A plain process, never launchd.** The watchdog runs the same on a Mac and in a Linux sandbox. Its own session keeps Ctrl-C and a closed terminal from reaching it. If the machine dies with the batch, nothing retries after a reboot or power loss; restarting after that belongs to the hosting work (f1).

2. **One claim decides the retry.** The claim is one `UPDATE processes SET retried_at` guarded by `ended_at IS NULL AND retried_at IS NULL AND retry_of IS NULL`, and the retry starts only on rowcount 1. Two watchdogs racing start exactly one retry, so a second detector can never launch twice.

3. **A retry reads the dead batch's queue once.** The retry's tickets are the `queued` rows of that batch's `batch_children`, read at claim time. A live batch still schedules from its in-memory `pending` list. `started` children are not adopted here; ticket 256 owns that.

4. **A retry is never retried.** Its row carries `retry_of`, so it starts no watchdog and its claim is refused.

5. **A retry that fails to launch stays claimed.** The reason and the retry's exit go to `.lime/runs/watchdog-<batch-id>.log`. Nothing tries again.

We considered,

- **A launchd agent per batch.** Rejected: launchd is Mac-only, and the watchdog has to work wherever the batch runs.
- **The next `lime` command notices the death.** Rejected: it fires minutes to days later, and `status`, `runs` and `dash` are read-only by code. A report must not launch Runs.
- **A timer in the dashboard server.** Rejected: the server is read-only (ticket 39), and a timer that launches Runs widens what the server can do.

Trade-offs accepted:

- A batch whose machine or sandbox dies gets no retry. Its row stays open with a stale heartbeat, and status is to show it (ticket 257).
- One retry only. A retry that dies stays dead until a person launches it again.
- A retry reads `batch_children`, which ADR 0111 said nothing reads to decide a launch. That read happens once, at claim time, and the live scheduler is unchanged.
