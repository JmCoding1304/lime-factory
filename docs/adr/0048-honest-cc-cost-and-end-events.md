# Claude Code cost is booked per-send, and every Session ends in the trace

Status: proposed

Claude Code's `result.total_cost_usd` is cumulative for a Session across `--resume`, but `execute` added it to the Run on every send, so a Session's repeated sends booked the same dollars again and again: 26 cumulative sends booked $29.6 that was never spent, and acmeweb Run `95969083` showed $9.0 for a Session that cost $4.62. Separately, `agent_end` was emitted only on the success path, so a killed, denied or errored Session left no end row and read as $0 in every per-role and per-model table — the dashboard's per-role view never saw about $67. And `agent_cc.run` handed `on_exit` the child's *returncode* where `end_process` expects a *pid*, so 255 of 255 Claude Code process rows ended at the parent Run's close time instead of when the child actually exited.

1. **A Claude Code send books `max(0, total_cost_usd - cost_reported)`.** The cumulative report is a Session-scoped gauge, not a per-send increment, so the orchestrator keeps the last reported cumulative value as the baseline `cost_reported` on the Session's agent-map entry and books only the positive difference. The baseline is bound to the request's Session identity: if the map no longer names that Session when the result returns, the send fails explicitly rather than booking stale spend or overwriting a replacement. pi is untouched — its per-send cost is already an increment and keeps its full priced-component merge.

2. **The baseline survives a plain rejoin and resets on a fresh identity.** A `lime run --resume` that rejoins an open Claude Code Session keeps its persisted `cost_reported`, so a resumed Session adds its final cumulative cost to the Run exactly once. A new session id, a changed runtime or model, a closed entry, or a legacy entry without the key starts the baseline at zero and books the first cumulative cost in full. `max`-zero, never a high-water mark: an equal or decreasing report books zero and persists the newest reported value.

3. **`agent_end` is emitted once on every Session exit, from the execution finalizer.** The single emitter moved into `execute`'s `finally`, so success, reported failure, denial, error, parse exhaustion, gate rejection, permission breach, exhaustion, kill and catchable interrupt all finalize exactly one end event. Its `payload.status` is `ok` only when the typed success, all gates and permissions, and normal finalization completed; otherwise `fail`. The event type is unchanged, so the dashboard needs no change. Tokens and cost are what was spent so far — for a resumed invocation, only that invocation's booked increment.

4. **`agent_cc.run` passes the child's pid to `on_exit`.** `end_process` identifies a process row by pid, so the adapter now hands the callback `proc.pid`; the child's process row ends when the child exits, not when the parent Run closes. `AgentResult.returncode` still carries the real return code.

We considered,

- **Read the per-send cost from a new field.** Rejected: Claude Code reports only the cumulative total, so the increment must be derived from a stored baseline; inventing a per-send field would claim a number the runtime never sends.
- **A high-water mark for the baseline.** Rejected: it would book zero for a genuine later increase below a prior peak and silently under-report; the ticket's literal formula is `max(0, report - baseline)`, and equal/decreasing reports book zero.
- **Give `agent_end` a new event type or a `fail` companion.** Rejected: the same event type keeps the existing dashboard readers working; the added `status` field is additive.
- **Emit the end event from each exception handler.** Rejected: that is one emitter per path, and a path added later would be missed; a single finalizer guarantees exactly once.

Trade-offs accepted: past Runs' over-booked cost is not corrected (out of scope), so historical totals stay inflated while new Runs book the honest increment; a resumed invocation's end event carries only its own booked delta, not the Session's cumulative total, so per-event sums must be read as increments; and the decreasing-report case trusts the newest reported value as the next baseline, so a runtime that reports a genuinely lower cumulative cost is believed rather than flagged.
