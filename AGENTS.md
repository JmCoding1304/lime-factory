# limefactory — agent guide

## Run
commands: `.lime/config.yaml` → `commands:`

## Layout
- `lime/` — the CLI package (`lime.cli:main`); `agents.py`/`runner.py`/`tracer.py` orchestrate, execute, trace
- `lime/workflows/` — Chain + Phase definitions
- `lime/skill/` — the `lime` skill source; `lime init` stamps it into a target project's `.claude/skills/lime/`
- `lime/templates/` — the `config.yaml`/`trust.yaml` templates `lime init` stamps
- `lime/numbering.py` — `next_number()`/`reserve()` (tickets) + `next_adr_number()`/`reserve_adr()` (ADRs, refs/lime/adr); both ledgers are CAS refs on origin, never deleted; `lime/overlap.py` — `live_overlap()` refuses/holds a launch whose Where paths hit an unmerged `origin/lime/*` Run branch or this project's own unpushed Run row in `.lime/runs/lime.db`
- `dashboard/` — Vue 3 + Vite + Bun observability SPA (`server/`/`src/`/`shared/`)
- `tests/` — pytest, one file per `lime/` module (1:1 mirror)

## Rules
never-list: `.lime/trust.yaml`
- factory code ships once as the installed `lime` tool; never copy it into a target project (ADR 0001)
- transient stream errors retry in-place (ADR 0008), then once via fresh session (ADR 0009), then fail; a Session's time ceiling (`timeout_s`, per role) and three consecutive identical tool calls latch the budget stop before its one nudge, so only handoff writes and final reporting remain; the nudge reports status: success (only when every requirement is met) or partial with a handoff, never a silent kill; once stop is reached within a Session it latches, so later lower measurements cannot reopen substantive tools

## Docs
CONTEXT.md = glossary · docs/adr/ = why · .lime/ = factory state
