# prototype — "what should this look like / behave like"

Upstream only, HITL, in the main working tree (no worktree — the dev server hot-reloads). Adapts the global `prototype` skill's UI branch; the designer Role runs it, not you.

1. **Flash a scout brief first** — one quick pass naming the files the designer should open. The designer opens only files this brief names.
2. **Start** — `lime prototype <slug> "<ask>" [--ref <path>]…`. The designer builds 2–3 *substantively* different variants on one throwaway route, switchable via `/proto-<slug>?variant=a|b|c`. The write fence (`**/proto-<slug>/**`, enforced by `permissions.py` + `{{never}}`) means it can't touch anything else — if it needs to, that's a sign the ask isn't prototype-shaped.
3. **Iterate live** — `lime prototype <slug> --continue "<feedback>"` keeps the same Session; the user reacts to the running variants, not a description of them.
4. **Lock** — `lime prototype <slug> --lock` writes `NOTES.md` next to the route: fixed, free, and open. This is the only artifact that survives the prototype.
5. **Cut the ticket** — you (the Orchestrator) write the feature ticket per [run.md](run.md) with `Prototype: <route dir>` in its header; the planner reads `NOTES.md` then the route before writing the plan. The feature Run's docs commit keeps a file prototype, and removes a route directory only when no other open ticket still names it or a descendant — uncertainty about the main checkout's ticket list also keeps it, and the last eligible close removes it. Never delete the shared route yourself.
6. Traced separately as `kind=prototype` — it never counts against a feature Run's budget.
