# The Report block is rendered in code per Runtime

Every Role's `user.md` hand-wrote its own `## Report` section, and the same text was sent to both Runtimes. So the pi-only `submit` tool was named in prompts an unauthenticated Claude Code Role also received: acmeweb's Claude Code documenter made a median of 1 tool call, sent 10 of 37 invalid envelopes, and changed no docs in 24 of 26 phases. The continuation prompt had no Report section at all, so all 15 of 15 researcher continuations opened with an invalid reply. Seven hand-written copies also drifted — each restated the envelope shape, and nothing tied any of them to the pydantic model that actually validates the answer.

1. **One generated block, per Runtime and output type.** `_report_block(runtime, output_type)` builds the section from the output type's inlined `submit_schema` (shape, nested refs) and `_field_hints` (every field and Literal choice), plus exactly one illustrative envelope drawn from that schema. It is pure and stateless — same inputs, same bytes. pi's wording tells the Role to call `submit`; Claude Code's asks for the JSON envelope as the final message and never names `submit`.

2. **The templates defer, they do not restate.** Each of the seven `user.md` files ends in `{{report}}`; no template hand-writes the envelope shape, the `ONLY valid JSON` wording, or a transport instruction. Role-specific guidance (the reviewer's verdict rules, the builder's and documenter's commit-subject line) stays above the placeholder.

3. **The same block rides a continuation.** `execute` computes it once and reuses the exact string for the Phase's first prompt and every continuation, after the handoff and git facts — so a fresh Session is told how to report, not only what to do. The blocked-continuation wording says `report fail`, never `submit`, on Claude Code.

We considered,

- **Keep the seven hand-written sections and add a runtime branch per file.** Rejected: it multiplies drift across fourteen texts and still leaves the continuation uncovered, which is the half that failed outright.
- **Render the block once at import and cache it.** Rejected: the inputs are cheap and a module-global would make the output depend on import order; a pure function keeps the schema the single source and the result reproducible.
- **Have Claude Code roles call `submit` too.** Rejected: `submit` is a pi tool; a Claude Code Role cannot call it, which is the bug being fixed. Claude Code delivers its envelope as the final message until it gets its own MCP `submit` server (ADR 0007).

Trade-offs accepted: Role-specific hand wording in the Report section is gone — anything the generator cannot express must live above `{{report}}`; the illustrative envelope is shape, not a filled Report, so a model may still emit a wrong value within a valid shape; and the two delivery wordings now live in one Python function rather than next to each Role's task.
