# Behavior-bearing Markdown belongs to the builder, not the documenter

The documenter's write boundary is an allowlist — `AGENTS.md`, `CONTEXT.md`, `docs/adr/**` — enforced by the Fence, which rolls back any edit outside it. Before this change the prompts said only "never modify source code, tests or config", leaving `.md` files like `SKILL.md` and `cookbooks/run.md` under `lime/skill/lime/` and `.claude/skills/lime/` ambiguously in scope: they are Markdown, but they are instructions that change agent behavior, not prose about code. Run `c3290d4b` (ticket 127) exposed the gap — the build and review phases passed, the Run committed its code, then the documenter tried to fix the required skill files, the Fence rejected all four edits, and the Run failed late with the work already done.

Three instruction changes close it, without widening the documenter's permissions or removing the guard:

1. **Planner and builder own skill Markdown.** Both prompts now classify required behavior-bearing Markdown changes under either skill root as builder work, distinguished from the documenter's prose docs.

2. **The reviewer sends missing skill changes back to the builder.** A missing required skill change is a blocking finding, revised by the builder before the document phase — never deferred to the documenter.

3. **The documenter's scope is the allowlist it already had.** Its instruction now states the allowlist directly and replaces the ambiguous source/tests/config exclusion: required work outside it — including behavior-bearing Markdown under either skill root — is left untouched and named in `notes_for_next_agent` and the handoff file. The documenter cannot fix that work and reports `success` for its own completed documentation work (including a no-op); there is no document→build/revise route, so the reviewer must catch required skill work before the document phase, and any outside work it misses needs an explicit follow-up rather than a purported Run `partial` status. A documenter `partial` now means only that its Session actually ran out of time or context.

We considered,

- **Widen the documenter's allowlist to include the skill roots.** Rejected: it would make the last phase the owner of behavior-bearing instructions, the exact misrouting this fixes, and it would blur the write boundary the Fence exists to hold.
- **Remove the guard and let the documenter edit freely.** Rejected: the rollback is what kept `c3290d4b` from committing unreviewed skill changes; the fix belongs in routing, not in weakening the fence.
- **An automatic backward phase loop from document to build.** Rejected: the Spine runs a fixed phase order; the reviewer's blocking path already returns work to the builder, and a new loop is a larger change than the misrouting warrants.

Trade-offs accepted: a missing skill change that reaches the document phase is no longer fixed there — the documenter cannot edit it and reports it in its notes, and the Run does not manufacture a `partial` for it. Review must catch required skill work before document; any outside work it misses needs an explicit follow-up, not a purported Run `partial` status. Filesystem sandboxing and new tool interception remain out of scope.