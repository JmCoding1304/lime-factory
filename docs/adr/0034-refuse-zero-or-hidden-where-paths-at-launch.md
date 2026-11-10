# Refuse a ticket whose `## Where` parses zero paths or hides a path

The **launch overlap guard** (ADR 0027) compares exactly one path per `## Where` line — the line's first backticked token — and reads nothing else. A ticket that writes its paths without backticks parses zero paths, and a line naming several paths hides every path after the first, so the guard passes while protecting nothing: shop-api parsed zero paths in 46 of 47 tickets, and limefactory 148–157 hid 4 to 9 paths each. ADR 0027 accepted "a ticket whose `## Where` is malformed is never guarded, so authoring discipline is load-bearing"; that trade-off is now closed at `lime run` and reported at `lime check`.

1. **The lint is a read-only diagnostic, never a change to extraction.** `tracker.where_failures(ticket, root)` returns one string per fault and touches no state. `where_paths`, `_FIRST_BACKTICK` and `normalize_path` are unchanged, so the guard still compares exactly the paths it did before; the lint only names what the guard cannot see.

2. **Two faults are refused, each naming the fix "one backticked path per line".** *Zero paths* — the section names none, so overlap detection sees nothing; the message names the ticket file. *Hidden occurrence* — a later backticked token on a line that is a path; the message names the line and the hidden token. The first `finditer` match is always treated as the designated path slot and never re-examined, regardless of whether it looks like a path; every later match is judged on its own.

3. **A hidden token is a path if it has no whitespace and either contains `/` or names an existing file under `root`.** `_cmd_run`, `commands.setup` and `lime_<run_id>` are symbols and pass; so do prose and whitespace-bearing tokens. The existence check uses the Run root explicitly, never the process cwd, so it does not depend on where the command runs. A directory-only basename is not a file and passes. Faults are one per occurrence, never deduplicated.

4. **Launch refuses before any effect; check reports every file.** In `_run_ticket` the lint runs immediately after load — before blockers, overlap (even under `--allow-overlap`), config/spawn checks and `reserve` — and exits 2, so nothing is reserved, no worktree or session is created and the ticket is not rewritten. `_run_batch` lints every explicit argument once before scheduling, so a fault in a later ticket launches nothing and reserves nothing. `lime check` visits every `.lime/tickets/*.md` (never short-circuiting behind `any(...)`) and returns 1 with one line per fault; maps are not globbed.

We considered,

- **Warn instead of refusing at launch.** Rejected: a warning leaves the guard silently protecting nothing, which is the exact fault being fixed; the ticket must be repaired before it can reserve a number.
- **Refuse every later backticked token.** Rejected: a `## Where` routinely names symbols (`_cmd_run`, `commands.setup`) beside its path, and refusing those would block well-formed tickets. The lexical-or-existent test is what separates a hidden path from a symbol.
- **Treat a directory-only basename as hidden.** Rejected: `is_file()` is exact; a bare directory name is far more often a symbol or prose than a hidden second path, and the false positive would block a valid ticket.
- **Fix the tickets instead of the code.** Rejected and out of scope: re-authoring existing tickets is never done, and the fault recurs every time a new ticket is written.
- **Document only, with no code check.** Rejected: the shop-api numbers are what a doc-only rule produces.

Trade-offs accepted: the hidden-token test is a heuristic — a path-like symbol with a slash (`lime/<run_id>`) would be flagged, and a genuine second path whose basename happens to match a file in the root is flagged while a nonexistent bare word is not — but it fails toward refusal of a repairable ticket rather than a silent pass; and the existence check makes the lint's verdict depend on the repo's current files, so the same ticket text can lint differently as files appear. Reservation, `Blocked by:` semantics, `--allow-overlap` and the never-merge-main rule are unchanged.
