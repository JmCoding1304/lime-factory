# The write fence refuses at the call, and fails open

Status: proposed

A Role's `writes` boundary was checked only after a phase ended, by the diff-and-rollback pass in `permissions.enforce`. A read-only Role therefore owned the whole repository for the length of its Session: in shop-web Run `e5a571a9` the scout created ten files with `write` and rewrote one forty times, and the change was only rolled back once the phase closed — long after the model had burned its turns on an edit it could never keep. The phase-end check is authoritative but far too late to steer a Session, and `lime guard` already sees every tool call on both Runtimes.

This ADR adds a call-time half of the fence: the Role's `writes` travels to the child as `LIME_WRITES` (JSON), and the Guard refuses a `write`/`edit` whose target is neither under the handoff dir nor granted by that boundary, in the same reason already used to point the model at the handoff dir and `submit`. The phase-end check stays as the backstop it always was.

1. **The policy is exported, not re-derived.** The `AgentRequest` gains `writes: list[str] | None`; `agents.py` sets it from the Role's `AgentConfig.writes` on every send, including retries, and both Adapters export it as `LIME_WRITES` JSON. `None` unset (no call-time restriction), `[]` read-only, `[paths]` allowlisted — the same three meanings the phase-end check already gives the field. The Adapter never inherits a stale value: `None` removes `LIME_WRITES` from the child env so policy cannot leak across Roles or Sessions.

2. **The Guard reuses the phase-end matcher.** The fence resolves the target against the cwd, then asks the same `permissions.permitted(relative, agent, [])` the phase-end check uses, so call-time and after-the-fact decisions cannot disagree on what a boundary grants. Handoff-directory targets are exempt exactly as they are at the stop (the phase-end check never sees them). A target outside the cwd is refused even for a broad `**` grant.

3. **The fence fails open; the phase-end check fails closed.** Unset or malformed `LIME_WRITES`, a call whose target cannot be read, and any internal Guard error all let the call proceed. A Guard that can block a Session must never turn its own bug into a bricked Session, and the phase-end rollback already covers the change. A malformed policy skips only the fence — the Budget's stop keeps its own wording and still denies.

4. **The fence sits after the Budget stop.** A `writes` grant never reopens a substantive tool at the stop: the stop is decided first, and the fence only adds a reason in the allow path. Handoff writes and `submit` stay exempt throughout.

5. **A rollback that deletes a breached file also removes the empty folders it left.** `_roll_back` prunes the deleted file's parents upward with `rmdir()`, strictly below the repository root, stopping at the first folder that still holds anything — never recursive, never the root, never a resolved symlink into an external tree. Cleanup is best-effort and never relabels a successful deletion.

We considered,

- **Move the check entirely to call time.** Rejected: the phase-end diff is the only thing that sees changes a `bash` command or a missing hook made, so the two are complements, not alternatives. The ticket puts `bash` inspection explicitly out of scope for the same reason.
- **Fail closed on a malformed or crashed Guard.** Rejected: a fence that refuses every write once its own input is unparseable would brick a Session on the Guard's bug, while the change it fails to block is still rolled back at the phase end.
- **Pass the boundary as a Guard argument instead of the child env.** Rejected: the Guard is invoked by the Runtime's hook, not by the Spine, so the child env is the only channel that already exists on both pi and Claude Code.
- **Refuse the call and also skip the rollback.** Rejected: a refusal the model can see is not proof the change did not land — an earlier call in the same Session may have written it — so the phase-end enforcement stays unconditional.

Trade-offs accepted: a Session can still change the repository through `bash` or through a hook that never fired, so the call-time fence is a steering signal and not a security boundary; and a genuinely malformed `LIME_WRITES` is silently ignored rather than reported, so a policy-export bug looks like an unrestricted Role until the phase-end diff catches it.
