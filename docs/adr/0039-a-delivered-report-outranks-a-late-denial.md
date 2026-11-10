# A delivered report outranks a late denial

Both Adapters turned any hook or rule denial in a Session into `status='denied'`, and a phase whose transport status is not `ok` never delivers its Envelope — the Spine treats it as a failed Session and moves on. In acmeweb Run `a3805f35` the planner had already written `plan.md` and its final report when its context budget crossed the stop (164,613 of 160,000) and the Guard refused one further `Bash` call. The phase failed as `denied`, and the `--replan` that followed discarded the finished plan. The denial and the delivery are unrelated facts: the denial is about one refused tool call at the end, the delivery is the whole Session's product.

1. **A valid Envelope wins the Adapter's terminal status.** In `agent_cc`, when the stream subtype is `success` and denials are present, the adapter validates the Session text as the common `EnvelopeBase` before letting the denial list set the status; on success the status is `ok` and the text is preserved for the role-specific parser, so an envelope `partial` or `fail` stays a partial or fail report — the transport only stops reporting `denied`. In `agent_pi` the accepted-envelope branch already took `ok`; it now logs the denials it overrides. A Session with denials and no valid envelope still returns `denied` in both adapters.

2. **Every overridden denial is logged with the denied tool's name.** One `warning` per retained denial entry per Adapter invocation: `denial ignored because a report was delivered: tool=<name>` (CC reads `tool_name`, pi reads `tool`; an unnamed entry logs `unknown`). The denial list on the `AgentResult` is left untouched, so the audit survives even though the status does not reflect it.

3. **This changes no Guard threshold and no latched stop.** The Budget's warn/stop and the latch (ADR 0015, ADR 0022) are untouched: a denied tool call is still denied and the finalization window still carries only handoff writes and submit.

We considered,

- **Keep denial-winning status.** Rejected: it throws away a finished report and forces a re-run, which for a planner is exactly the expensive re-plan the ticket had to pay for.
- **Require the envelope to arrive before the first denial.** Rejected: pi accepts the submit before the denial events but CC reports the denial list only in the terminal result, so the ordering is not observable in the same way across the two Runtimes; presence of a valid envelope, not its position, is the fact the adapter can check.
- **Downgrade the status to `partial` instead of `ok`.** Rejected: `partial` is an Envelope status meaningful to the Phase, and the Adapter has no business inventing one; the report's own status is parsed downstream.

Trade-offs accepted: a hook or rule denial is no longer by itself evidence that a phase failed, so a genuine denial in a Session that nonetheless delivered a Report is invisible except in the log line and on `AgentResult.denials`; and the CC adapter now imports `extract_envelope` from `agents.py` at call time to validate the common envelope, a local import kept to avoid the import cycle, so the Adapter gains a dependency on the orchestrator's parser that it did not have before.
