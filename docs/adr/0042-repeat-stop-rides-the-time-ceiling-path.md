# A repeated tool call rides the time ceiling's stop path

A Session that repeats itself looked healthy to every alarm the Spine had: the stall watch kept seeing events and the Budget saw a small context, so nothing fired. In shop-web Run `e5a571a9` the builder issued the same `write` 40 times over 20 minutes and was stopped only by hand. ADR 0015's Budget stop and ADR 0022's time-ceiling latch both bound *how much* a Session does or *how long* it runs; neither looks at *what* it does, so a tight loop that keeps the events flowing and the context small slips between them.

This ADR adds one more way to reach a stop the Spine already knows how to enforce: consecutive identical tool calls in one Session trip a repeat stop, which then latches the Budget's stop and runs the time ceiling's existing nudge-then-kill path.

1. **The stop is the time ceiling's stop, with a different cause.** The repeat stop does not introduce a new terminal path, a new Budget state or a new Guard branch. It latches `state: stop` exactly as the ceiling does and lets the ceiling's one `_nudge` path run unchanged apart from the reason: the mailbox `reason` is `repeat` instead of `time_limit`, the nudge and the `timeout_kill` RuntimeError name the repeated tool, and `lime guard`'s refusal blames the repeated call rather than the clock. The nudge and kill texts gain one cause sentence, not new prompt functions.

2. **The count is Session-local, beside the Budget latch.** Two plain variables — the last call's identity and its consecutive count — live in `execute` next to `last_state`/`last_reason`; no new class. Identity is the tool name plus its full **unclipped** arguments, so two writes to one path with different content are distinct calls. `_reset_budget()` — the fresh-Session init — zeroes both, so a Continuation starts counting from zero and a re-run cannot inherit a stale streak. The count freezes once the limit is reached, so the `repeat_stop` trace event fires exactly once per Session and later calls (the nudge included) never re-fire it.

3. **Either Adapter ends the Session on the request's stop.** `AgentRequest` carries one `should_stop` callable the Spine sets per Session; both `agent_pi` and `agent_cc` stop on it next to the accepted-submit stop, ending the child with the existing `timeout` status and the one bounded SIGINT ladder. In pi the accepted-envelope branch is checked first, so a delivered submit still outranks a repeat that lands in the same tick.

4. **A repeat's partial is a `timeout` partial.** The repeat stop rides the one nudge, so `_classify_partial` still returns `timeout` when `nudged`; the Continuation therefore gets the same single fresh-Session allowance a time-ceiling partial gets, and its prompt names the repeated call instead of the clock and asks the model not to repeat it.

We considered,

- **A first-class `repeat` continuation reason.** Rejected: `PartialReason` and the continuation accounting already branch on measured ceilings, and a repeat stop *is* a nudge-carrying ceiling for those purposes; a fourth reason would thread through the Continuation limits without changing their outcome.
- **Counting any repeat, not consecutive ones.** Rejected: a call reissued once after unrelated work is normal, and a global repeat count would stop a legitimate Session; the ticket's own non-goal rules out alternating A/B loops, which consecutive counting leaves unbounded by design — the time ceiling still bounds them.
- **A dedicated kill error and path.** Rejected: it duplicates the note-writing, trace and nudge bookkeeping the ceiling already owns (the same duplication ADR 0022 avoided in the Guard).

Trade-offs accepted: a repeat stop is indistinguishable from a time-ceiling stop to a reader that ignores `reason`, so the reason must travel with the `timeout_nudge`/`timeout_kill` payloads and the mailbox; the `repeat_stop` event is added to the closed `EVENT_TYPES` set and the dashboard's `EventType` union, so both must move together; and alternating-loop repeats remain uncovered, bounded only by the time ceiling.
