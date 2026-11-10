# grill — an unclear ask, one question at a time

Adapts the global `grilling` and `domain-modeling` skills to lime's endpoint: a ratified ticket, not a design doc.

1. **Interview, don't guess.** One question at a time, waiting for the answer before the next. Never ask what the codebase can already tell you — look it up (`lime scout "<q>"`) instead of asking.
2. **Recommend, then wait.** Every question carries your best answer; the user picks or overrides. Don't proceed on an assumption.
3. **Sharpen nouns.** When a term is vague or overloaded ("the dashboard", "the config"), pin it down before building on it — this is `domain-modeling`'s discipline: challenge against `CONTEXT.md`, propose the precise term, update the glossary the moment it resolves. Don't let a fuzzy noun ride into the ticket.
4. **Stop when every noun points at one thing** — not when you've asked enough questions. If the destination is still fuzzy after a good pass, that's a signal this is a map, not a ticket (see [map.md](map.md)), not a reason to keep grilling solo.
5. **Land it as a ticket** — fold the resolved answers into the four-line prompt (`## Ask` / `## Where` / `## Done means` / `## Out of scope`) and ratify per [run.md](run.md). The conversation is the scratchpad; the ticket is the only artifact that survives it.
6. **Offer an ADR sparingly** — only when a decision surfaced here is hard to reverse, would surprise a future reader, and came from a real trade-off. Otherwise it's not worth a doc; the ticket already carries it.
