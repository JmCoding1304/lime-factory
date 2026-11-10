# map — foggy, multi-PR, or greenfield work

Adapts the global `wayfinder` skill to lime's tracker: local markdown, not an issue tracker. The map is `.lime/maps/<name>.md`; its children are `.lime/tickets/NN-slug.md` carrying `Type: grilling|research|prototype|task` (wayfinder tickets never carry `Workflow:` — that marks an implementation ticket). Ticket numbering is global and monotonic across planning and build; a number is never reused — `lime ticket next` gives the next one (derived from git + `.lime/tickets/`); never count files.

No native blocking exists on files, so wayfinder's fallback applies everywhere: `Blocked by: <path>` in the header. A ticket is unblocked when every file it names is gone or its header reads `Status: closed`; a closed ticket's file is retained on disk, not deleted. Closing a ticket is `lime ticket close <project> <NN>`, run by the owner or the session that resolved it; the tripwire refuses it only inside a Run Role Session. The **frontier** = open, unblocked, unclaimed tickets. **Claim** = write `Status: claimed (who, date)` before any work, so concurrent sessions skip it.

## Chart the map

Loose idea arrives. A product is mapped one vertical slice at a time — thin, end to end, usable on its own, never "all backend, then all UI"; each later slice plans against merged code.

**Skeleton, once per product** — before its first slice, lock `specs/<product>-skeleton.md`: about one screen naming the product's destination, the data model, and the cross-slice interfaces with their owners. It is locked once; every slice spec cites it by path, so no slice map re-charts the whole product.

1. Name the **destination** — the next useful vertical slice, never the whole product: [grill.md](grill.md) (which pulls in `domain-modeling`) until it's fixed; the destination sets scope.
2. Grill again, breadth-first, for the frontier and the fog. If nothing foggy surfaces, you don't need a map — say so and go straight to [run.md](run.md).
3. Create `.lime/maps/<name>.md`: Destination, Notes, empty Decisions so far, the fog under Not yet specified.
4. Cut the tickets you can specify now, then wire `Blocked by:` in a second pass.
5. Stop — charting is one session; don't resolve tickets in the same pass.

## Work through the map

1. Load the map (low-res; zoom into a ticket body only on demand).
2. Pick the ticket — named by the user, or the first frontier ticket. Claim it.
3. Resolve it: `research` → `lime research`; `prototype` → [prototype.md](prototype.md); `grilling`/most `task` → [grill.md](grill.md). Never resolve more than one ticket per session.
4. Record the answer in the ticket body and append a one-line gist + link to the map's Decisions so far, then close the ticket with `lime ticket close <project> <NN>`: it sets the header `Status: closed` (`lime ticket reopen <project> <NN>` undoes it), rewriting only that one line and leaving the file on disk. On a hands-on ticket (prototype, grilling, owner QA) the agent guiding the owner closes it only once the owner confirms it done. Close through the command, never by hand-editing `Status:`.
5. Cut newly-surfaced tickets; graduate fog that's now specifiable; rule out-of-scope tickets out rather than resolving them.

## The Captain (driving a whole map to its locked spec)

`/lime map <map>` makes this session the Captain — the single writer of the map file, ticket creation, and numbering (each new ticket gets `lime ticket next`). Ticket sessions write only their own ticket + assets; they never touch the map or number a new ticket themselves, only propose one in their resolution.

- **AFK frontier ticket** → launch headless in the background on the launch line: `claude -p "/lime map <map> <NN>"` (cap 3 concurrent, untraced, one handoff-resumed retry on failure).
- **HITL frontier ticket** → one at a time, in a foreground pane (a Herdr pane when `HERDR_ENV=1`; otherwise print the command for the user to run).
- **Wake signal** = a shell watch on the ticket file until `Status: closed` — never poll in a sleep-loop.
- **Stateless recovery** — the Captain holds no memory of its own; everything it needs to resume is re-derived from the map and ticket files on disk.

## Map close

At "locked", spawn one **fresh cutter session** from the slice's `specs/<effort>.md` and the skeleton it cites, alone (not the map, not the tickets):

1. Cut tracer-bullet implementation tickets — thin end-to-end slices, each sized to the resolved limits per SKILL.md's ticket-cutting rule (the selected profile's clock, token and added-lines ceilings, with headroom) so one Run/PR fits.
2. Any HITL prerequisite becomes a `Type: task` ticket, wired to physically block its dependents.
3. The cut's last ticket is an owner QA `Type: task` ticket, `Blocked by:` the slice's last ticket (every leaf, if the cut ends in more than one). Its job: the owner tries the slice on its target, turns each bug into a new ticket, accepts the slice's ADRs (they merged `proposed`), and folds what was learned into the next slice's map — drafted while this slice built — before that map locks and is cut.
4. Walk the human through a numbered-list quiz confirming the cut before writing a single ticket file.
5. Write the tickets, then delete the map and every wayfinder ticket under it — never the tickets this cut just wrote, QA included. The slice spec and the skeleton survive — the spec keeps its `Out of scope` section; linked assets stay in `specs/assets/`.
6. The Orchestrator launches the ratified cut as one batch ([run.md](run.md) 3); the next slice's map is charted while it builds and locks only once this slice's QA ticket closes.
