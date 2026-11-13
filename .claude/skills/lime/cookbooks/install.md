# install — putting the factory into a target project

By hand, once, in the target project: `uv tool install limefactory && lime init` (idempotent, owner-split — factory files always rewritten, user files never touched). Then `/lime install` here, steps below.

1. **Precondition** — no `.git`: `git init -b main`, put trust `secrets:` in `.gitignore`, `git add -A`, commit `chore: initial commit` with trailer `Lime-Role: orchestrator`, carry on. An existing repo: clean tree, on the default branch — anything else: stop and say why, don't improvise a workaround.
2. **Branch** — `lime/install`. Two commits land here, both trailer `Lime-Role: orchestrator` (no Run, so no `Lime-Run`).
3. **Scouts** — five `lime scout "<question>"` calls, one question each, ≤20 lines of paths + notes, never file contents:
   - **commands** — install/test/lint/typecheck/build/dev-serve(+port)/DB migrate-seed, from `package.json` scripts / justfile / Makefile / CI. → `commands:` proposal.
   - **structure** — framework+version, top-level dirs, entry points, source/tests location, one-line conventions. → AGENTS.md `## Layout`.
   - **infra & secrets** — env file *names*, CI/CD, deploy targets, git hooks, prod identifiers. → trust `secrets:`/`commands:`, `deploy:`.
   - **decisions in history** — pivots evidenced in code + `git log`, each with commit/file as evidence; zero candidates is a fine answer, never manufacture one. → ADR candidates.
   - **existing docs** — every doc file, line count, per-section verdict *derivable from code* vs *not derivable*. → deletion list.
4. **Commit ① stamp** — `lime init`'s owner-split output for this project (`.lime/{config,trust}.yaml` if absent, `.lime/{maps,tickets,specs,prompts}/`, `.claude/skills/lime/`, `.claude/settings.json` budget hook) plus this cookbook's own source if it changed. `.lime/trust.yaml` stamps with the package's generic template patterns (not empty) — `lime check` can already be green here; step 5 is about fitting those patterns to *this* project, not filling a blank. After a step-1 bootstrap the stamp is already in the initial commit — nothing to commit here.
5. **Grillings** — eight, one key each, pre-filled from the scouts, "ok / change?": `commands` → `deploy` → trust `protected_files` → `secrets` → `branches` → trust `commands` → roster `defaults` → `profiles`. Trust keys are never skipped — an empty list is a decision, not an omission. Land each accepted answer in its file immediately.
6. **Doc refurbish** — write to the [doc standard](#doc-standard) below; fold anything non-derivable into AGENTS.md / CONTEXT.md / an ADR *before* deleting its source line; delete in this branch, one reason per deletion. Layering: global `~/.claude/CLAUDE.md` → deleted, its rules → `permissions.ask` in `~/.claude/settings.json`; project `CLAUDE.md` → symlink to AGENTS.md (fold and replace a pre-existing one); memory stays Orchestrator-only.
7. **Commit ② refurbish** — AGENTS.md, CONTEXT.md, docs/adr/, the CLAUDE.md symlink, the deletions, the filled config + trust.
8. **Deliver** — `lime check` green. No `origin` remote: `gh repo create --private --source=. --remote=origin`, `git push -u origin main`, `git push -u origin lime/install`. Then `gh pr create`. PR body, one screen: written · deleted (+ reason each) · ADRs · `lime check` result · "next: merge, then `/lime` for your first ticket." Human merges.

## Doc standard

- **AGENTS.md** — ≤40 lines, four sections: `## Run` points at `.lime/config.yaml` → `commands:`, `## Layout` (5–8 lines, only where the dir name doesn't already say it), `## Rules` points at `.lime/trust.yaml` plus conventions the code can't say, `## Docs` points at CONTEXT.md/docs/adr//`.lime/`. Never duplicate what the pointer already says.
- **CONTEXT.md** — glossary, one `- **Term** — definition` line per term, one or two sentences on what it is; each entry ≤350 characters (`lime check` and the document gate fail above it); `##` group headings allowed. Only words the code can't give an agent. Created only once a term actually surfaces.
- **ADR** — `docs/adr/NNNN-<slug>.md`, title + one ≤15-line paragraph (context → decision → why), no headings. Only when a decision is hard to reverse *and* surprising *and* evidenced — existing docs confirm it, never source it.

Re-running `/lime install` on an installed project: `lime init` re-stamps only; the refurbish is offered, not repeated by default.

## Prompts and rules

Shipped defaults need no override files — `lime init` stamps none, and every Role resolves its package `lime/prompts/<role>/{system,user}.md`. An override activates only through an explicit per-agent `prompt_engineering` `system:`/`user:` path in `.lime/config.yaml`; a configured path that is missing fails `lime check` and the Run at render time, and no package default rescues it. `lime init` preserves an existing `.lime/config.yaml` (owner-managed), so obsolete configured override paths must be deliberately removed or supplied by the owner.

Every Role receives the project's `AGENTS.md` (CLAUDE.md fallback) plus a filename-only ADR index as the `rules` variable, rendered after `never` in its user prompt. A rules conflict is blocking, never bypassed: the reviewer flags a contradicting diff, the planner names the conflict and `fail`s, and the builder stops `partial` rather than writing it. AGENTS.md authors still target ≤40 lines; `lime check` fails above 60.
