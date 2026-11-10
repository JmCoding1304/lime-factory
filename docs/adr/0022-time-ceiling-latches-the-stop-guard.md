# Time ceiling latches the stop Guard before the nudge

[ADR 0011](0011-timeout-nudge-before-kill-per-role.md) hit the wall-clock ceiling by stopping the process's next model call and sending one nudge into the same Session to submit. But the nudge was **text only**: the Guard still read whatever the Budget mailbox last said, so unless the Budget happened to be at stop the substantive tools stayed open. Observed runs (`b6b23eaf`, `8a3d9866`) show the consequence — the model kept working through the nudge window and died without submitting, exactly the data loss ADR 0011 set out to prevent. A nudge that asks the model to stop while the tools it keeps calling are still allowed is a suggestion, not a bound.

This ADR makes the ceiling enforce the same stop the Budget already enforces: at the first `timeout_s` expiry the Spine latches `state: stop` into the mailbox **before** the nudge is sent, so the finalization window has only the Guard's existing exemptions — handoff-directory writes/edits and `submit`.

1. **Reuse the Budget's stop, not a new Guard state.** The latch is `last_state = "stop"` plus a full `budget.write(... state="stop")`, the same latch [ADR 0015](0015-budget-stop-latches-per-session.md) established. No new mailbox field or Guard branch decides *whether* to close tools; the ceiling only decides *when*. `_reset_budget()` clears the latch at the start of a fresh Session, so a Continuation opens with tools re-armed.

2. **The mailbox carries *why* it stopped.** `budget.write()` gains an optional `reason`, serialized only when present, cleared on any write without one. At the ceiling the reason is `time_limit`; a plain context-budget stop stays reason-less. `_cmd_guard()` picks its deny wording from the reason — the time ceiling names the time ceiling, not the context budget — while the truthful success/partial guidance and the exemption set are unchanged. A missing or unknown reason keeps the old context-budget wording.

3. **The latch pairs a Session-local reason.** `last_reason` lives beside `last_state`, set to `time_limit` at the ceiling and cleared by `_reset_budget()`. Later `_track_budget` calls in the same Session keep publishing `stop` + `time_limit` as live turn measurements arrive, so a resumed turn's lower occupancy cannot flip the state back to `ok` and cannot erase the cause.

4. **The published occupancy is the just-returned timeout result's own measurement,** never a fabricated threshold count and never a cumulative total, so the mailbox stays a truthful reading of the Session.

We considered,

- **Leave the nudge as text.** Rejected: the observed runs show a model will keep working while its tools are open, and the ceiling must be a bound, not a request.
- **A dedicated Guard state for the time ceiling.** Rejected: it duplicates the stop/latch bookkeeping across the Spine and the Guard (see ADR 0015) and leaves two code paths deciding whether work may continue.
- **Close tools by raising the context threshold at the ceiling.** Rejected: it would lie about occupancy in the mailbox the Guard and the trace both read.

Trade-offs accepted: a time-ceiling stop and a context stop are indistinguishable to a reader that ignores `reason`, so the field must travel with the state; and a context-budget stop reached *before* the ceiling keeps the context wording until the ceiling relabels it `time_limit` — the ceiling is the later, more actionable fact. Whether the nudge window expires without an envelope still fails the Phase with today's explicit timeout error, and the allowlist, grace window, nudge count and Continuation limits are unchanged.