# The dashboard writes Home only by spawning lime

Status: accepted

The Focus tab's checkboxes (ticket 238) need the dashboard to change owner state: a goal's done flag in `focus.md`, a person-ticket's `Status:`. Every Home write already commits and pushes, and `lime focus` and `lime ticket close|reopen` own that path, with the owner tripwire and the argument checks. The server could write those files itself, which saves a process per click, but then the commit, push and tripwire would exist twice, once in Python and once in Node.

1. **A tick is one lime command.** `POST /api/home/tick` checks its body (`tickArgs` in `dashboard/server/limeProxy.ts`), then spawns exactly one of `lime focus done`, `lime focus edit --reopen`, `lime ticket close` or `lime ticket reopen` with an argument array, never a shell. The server never writes `focus.md` or a ticket file: a source-guard test fails if `index.ts` or `limeProxy.ts` names a file-write call. `homeVisit` (`lime dash visit`) already follows this pattern; the tick extends it to owner data.

2. **A failed write is reported, not repaired.** A non-zero exit returns the command's first stderr line. The Focus tab unticks the row and shows that line; the server does not retry.

3. **The route sits behind the local guards.** It takes POST only, and the same Host and Origin checks that refuse foreign writes apply to it.

We considered,

- **The server writes `focus.md` and ticket headers itself.** Rejected: it copies lime's commit-and-push and tripwire into a second language, and the two copies would drift.
- **A long-lived lime process the server talks to.** Rejected: one writer does not need a daemon, and reads already spawn lime per request.

Trade-offs accepted: each tick costs one Python start, so the row shows "writing…" for that time. A dashboard click can now reach the owner's Home through lime, and `tickArgs` is the only check on that path, so a new tick command needs its own arm there and its own test.
