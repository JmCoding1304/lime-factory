# A `Prototype:` header may name a file, and must name something that exists

A ticket's `Prototype:` header was written for a route *directory* the designer leaves behind. Nothing enforced that shape, so `commit_docs` ran `shutil.rmtree` on whatever the header held: demo-app ticket 18 carried `Prototype: prototypes/tokens.css`, the last phase died with `[Errno 20] Not a directory` after every agent was paid (run `183fd601`, 12/13 phases, $1.73), and the brief sent the planner to `prototypes/tokens.css/NOTES.md`, a path that cannot exist. A single-file prototype was always a legal thing for the engineer to lock; the close path treated it as a directory anyway.

1. **`commit_docs` deletes directories only and keeps a file.** `route_dir` is deleted with `shutil.rmtree` only when it `is_dir()`; a file is left on disk and the keep is logged (`kept`, reason "Prototype is a file; kept at close") without being appended to `deleted`. The ticket file is still unlinked first with `missing_ok=True`, so the interrupted state the old crash left behind — ticket gone, docs uncommitted — commits cleanly on re-run. `route_dir=None`, a directory, the replay short-circuit and the commit itself are unchanged.

2. **A file `Prototype:` briefs the planner at the sibling `NOTES.md`, else the file.** The header line is emitted as before; because the path is a file, the guidance points at `NOTES.md` in that file's parent folder when it exists and at the file alone when it does not, and never at `<file>/NOTES.md`. A directory `Prototype:` keeps the folder wording byte-for-byte.

3. **A `Prototype:` path missing under the Run root is a `where_failures` fault.** The existing launch-and-check diagnostic gains a third fault: a nonempty `Prototype:` whose `root / ticket.prototype` does not exist (file or directory) reports one failure naming the ticket and the path. Existence is checked against the filesystem with the Run root explicit, never Git tracking, so a tracked, untracked or git-less path is accepted alike. `lime/cli.py` is unchanged: `lime run` refuses the ticket at launch and `lime check` reports it through the call sites that already consume `where_failures`.

We considered,

- **Delete a file `Prototype:` too, or leave it to `commit.exclude`.** Rejected: the file is the engineer's reference after close, and no ticket asks for it to be removed; deleting a named file is never done.
- **Infer directory-vs-file only from `is_dir()` and say nothing in the brief.** Rejected: the planner would still be sent to `<file>/NOTES.md`, which is the second half of the bug.
- **Leave a missing `Prototype:` to the planner.** Rejected: a ticket pointing at nothing burns a full Run before anything notices; the check belongs beside the `## Where` lint it already shares.
- **Leave `CONTEXT.md`/the skill text to say a directory is the only legal shape.** Rejected: a file prototype is legal, and the close path must not crash on a legal ticket.

Trade-offs accepted: like the `## Where` lint, a missing-`Prototype:` verdict depends on the repo's current files, so the same ticket text can lint differently as paths appear — a legitimate prototype that is cut before its file exists is refused until the file lands, and the fix is to name something that exists. Keeping a file prototype means the delete-on-close commit no longer clears every prototype path; a directory route, `Blocked by:` semantics, reservation and the never-merge-main rule are unchanged.
