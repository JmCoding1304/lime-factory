# An unknown Claude Code window sizes the stop at 250k

Status: proposed

Every planner budget stop or kill since 09-30 came from a Claude Code planner: `_context_window` mapped any claude_code model without `[1m]` to a 200k estimate, so the Budget stopped the Session at 160k and the backstop killed it at 180k — 14 plus 3 stops, 8 of 65 cc-planner Runs handed to a human. The estimate was never measured; it stood in only because Claude Code's real number arrives in the stream's final `result` event, too late for a live per-turn check.

We made the Claude Code window deliberately unknown instead of estimating it.

1. **An unmeasured window is `0`, and `0` means the uniform defaults.** `_context_window` still returns pi's exact catalog lookup for pi; for every other Runtime it returns `0` — no alias table, no `[1m]` branch, no date-suffix heuristic. `budget.thresholds(None, None, 0)` is the uniform `(150k warn, 250k stop, 270k backstop)`, so a Claude Code Session is stopped and killed at the north star rather than at a guessed fraction of a guessed window. The number Claude Code reports still lands in `AgentResult.context_window` and the Trace; it just never moves the stop.

2. **Adapter-reported telemetry is not a policy input.** The `result` event's `context_window` is accepted for the record and refused for the decision. Making it a policy input would size one Session's stop from a value that only appears after that Session is over, so the policy would still not be live per turn — it would only be a later estimate wearing a measured number.

3. **pi is untouched.** pi's catalog lookup is exact and reachable before the first turn, so its warn/stop/backstop stay exactly what they were, including a small real window capping the stop and a 1M window still killing at 980k.

We considered,

- **Reading Claude Code's reported `contextWindow` to size the thresholds.** Rejected: the value is not available before the Session's first turn, so it cannot drive the live Guard check, and trusting it would let an adapter's own telemetry widen the budget the Spine enforces.
- **Keeping a per-alias estimate table (`[1m]` → 1M, everything else → 200k).** Rejected: the table was the bug — an unmeasured guess that was wrong for every model that did not carry `[1m]`, and 1M would have put the backstop near 980k, far past the smart-zone ceiling.
- **Dropping `[1m]` but keeping a flat 200k.** Rejected: 200k is just as unmeasured, and it is the value that produced the 160k/180k stops in the first place.

Trade-offs accepted: a Claude Code model whose real window is genuinely 200k is now allowed to run past it before the 250k stop, so an over-window failure is possible on those models — the stop is a policy ceiling, not a fit to the model; and a Claude Code model with a 1M window is stopped at 250k well below what it could hold, which is the smart-zone ceiling by design, not headroom to spend.
