# Delete the needs_rescope recovery; legacy Runs stay readable and discardable

Status: proposed

[ADR 0060](0060-diff-size-is-measured-never-a-stop.md) stopped a size overflow from closing a Run, so no code path sets a Run to `needs_rescope` any more. What was left was dead code: the status and its `needs_rescope.json` handoff, and the engineer-authorized path that reopened such a Run ([ADR 0021](0021-event-bound-recovery-of-a-preserved-overflow.md)). That path was the `--rescope-event` and `--recovery-decision` flags, the tracer's validate, authorize, record and consume functions, the session refusal and the console panel. ADR 0060 left this removal as a separate decision. This ADR makes it and supersedes ADR 0021.

1. **Nothing sets `needs_rescope` or writes its handoff.** The status is no longer a target of any transition, the rescope flags are not accepted, and no rescope or recovery event is written.

2. **Old Runs keep loading.** A Run in `needs_rescope`, with its `needs_rescope` handoff and `rescope_recovery_*` events, still loads and shows in `lime runs` and `lime trace`. The status keeps one legacy transition, to `discarded`, so a preserved Run can still be removed. `lime run --resume` refuses it before any mutation.

3. **A preserved legacy Run can only be discarded.** No path reopens it. If the engineer still wants the work, a fresh Run on the ticket replaces it.

We considered,

- **Keep the recovery for legacy Runs only.** Rejected: the only Runs it could serve were preserved before ADR 0060, and keeping it would keep the flags, the validation and the consume bookkeeping alive for them.
- **Drop the legacy status along with the code.** Rejected: rows and traces written before the removal would stop loading in `lime runs` and `lime trace`, and the database and traces are history that must keep reading.
- **Keep ADR 0021 as the active design.** Rejected: its flags and recovery path no longer exist, so an active ADR would describe a command that cannot run.

Trade-offs accepted: an engineer who wanted to reopen a preserved overflow loses that step, and a preserved legacy Run is discarded rather than re-scoped. The code keeps a small legacy read-and-discard path for as long as old databases and traces exist.
