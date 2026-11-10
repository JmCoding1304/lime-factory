# 0014 — The Orchestrator is harness-agnostic; scouts and researchers come from the roster

**Status:** accepted · 2026-09-04

## Context

The skill assumed Claude Code: its `Explore` agent as scout, `run_in_background` to launch Runs. In pi the model reached for the globally installed `pi-subagents` tool instead, wrapped `lime run 93` in an async delegate that died, and would otherwise have done the ticket by hand with pi-subagents' own scouts rather than the roster.

## Decision

- The skill is the contract, not the harness. Any session that loads it is an Orchestrator; the model is the engineer's choice. SKILL.md carries one launch line per harness.
- Runs launch only through the shell tool: backgrounded where it can, else one blocking call with no timeout. Never a subagent.
- `lime scout "<q>"` (repo) and `lime research "<q>"` (outside) spawn the Role from the roster, book a `kind=scout|research` row or join the calling Run, and print the brief inline.
- Bare `lime scout` (no `LIME_RUN_ID`) uses an unpersisted throwaway trace session — it never creates or mutates the project's `.lime/runs/lime.db`. An in-Run scout joins the caller's trace row, leaving no throwaway trace behind. `lime research` alone retains its standalone `kind=research` row.
- pi launches with `--no-extensions` plus two explicit `-e` packaged paths: the budget nudge (`guard/budget.ts`) and the roster-backed scout bridge (`harness_engineering/scout.ts`). No `subagent` tool, nothing else to be tempted by. The dedicated `scout` tool is pi's repo-question route, distinct from the still-forbidden general-purpose `subagent`.

## Consequences

- pi gets the same 150k nudge from `guard/budget.ts`, one of two explicit `-e` paths on its launch line (`--no-extensions` still honours explicit paths): on `turn_end` it reads the session's context usage and shells out to `lime hook budget --tokens N`, so the text lives once, in Python, beside the Claude Code `UserPromptSubmit` delivery.
- The scout extension (`harness_engineering/scout.ts`) is the second explicit `-e` path. It registers a single `scout` tool (with optional `profile` parameter) that shells out to `lime scout "<q>"`, traversing the same roster `.scout()`/`agents.py` path as the shell invocation. The Orchestrator never reads roster configuration, selects a model, launches pi directly, or duplicates scout prompts/schema handling in TypeScript.
- The scout's roster model is billed per Orchestrator question in every harness, where Claude Code's `Explore` was subscription.
- `.pi/settings.json` is unnecessary — the extension ships with the installed `lime` (ADR 0001), nothing is stamped into the project.
- Bare `lime scout` leaves no trace in `.lime/runs/lime.db` — the throwaway session is cleaned up on completion. An in-Run scout books its phase onto the parent row without changing the parent's lifecycle. Research retains its standalone `kind=research` row regardless of the scout change.
