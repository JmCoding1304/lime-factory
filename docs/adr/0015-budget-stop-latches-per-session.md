# Budget stop latches once reached within a Session

The old Guard's `budget.evaluate()` returned the state that matched the *latest* measurement — if context fell from 251k (stop) to 213k (warn), the mailbox flipped back to `warn` and the next tool call was allowed. In run 0377fe17 this was harmless (the model had already submitted), but a model that ignored the nudge could keep working after a stop, contradicting the "hard stop" contract.

We replaced the stateless evaluation with a latched `stop`: once `last_state == "stop"`, all subsequent turns in the same Session return `"stop"` regardless of the measured occupancy. The latch is reset at the start of a fresh Session (continuation or initial execution).

We considered two alternatives:

1. **Keep stateless evaluation** — the mailbox always reflects the latest measurement. Rejected: a model that ignores the nudge and sends another tool call gets "budget exhausted" → "warn" → "allow" on the next turn, which defeats the hard stop. The bug was real (run 0377fe17 showed the flip), and the fix costs nothing.
2. **Latch stop at the Guard (hook) level** — the pi extension or Claude Code hook remembers the stop state. Rejected: the Guard is a thin relay that reads `budget.json` and shells out to `lime hook budget`; it has no Session-level bookkeeping. Keeping the latch in the Spine (Python) avoids duplicating state across two runtimes.

Trade-off accepted: if a genuine drop in occupancy (e.g. a long thinking block that resolves into a concise answer) follows a stop, the Session remains stopped. In practice this is negligible — once a Session has crossed the 250k stop ceiling, the only useful action is writing a handoff and submitting; the extra ~60k of headroom from a dropped measurement doesn't change the outcome. The safety of a hard stop outweighs the marginal responsiveness.