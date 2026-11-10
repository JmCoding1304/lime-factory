# Customizing lime

Everything here is verified against the code; `lime check [--profile P]` validates most of it and prints the resolved roster. Per-project settings live in `.lime/config.yaml` and `.lime/trust.yaml` (stamped once by `lime init`, never overwritten). Factory behaviour lives in the clone you installed editable, so edits to `lime/` are live on the next command.

## 1. Roster: Roles, models, profiles

File: `.lime/config.yaml`, keys `defaults:`, `agents:` (a list, one entry per Role), `profiles:`. An agent entry is merged key by key over `defaults`. Accepted keys (`AgentConfig` in `lime/data_types.py`, checks in `config._agent_failures`):

| Key | Values |
|---|---|
| `runtime` | `pi` or `claude_code` |
| `model` | pi: `provider/model-id`, must be in pi's catalog, `anthropic/*` is rejected (use `openrouter/anthropic/*`). claude_code: `haiku`, `sonnet`, `opus`, `fable` or a `claude-*` id |
| `thinking` | `off minimal low medium high xhigh max` (pi checks it against the model's supported levels) |
| `timeout_s` | whole seconds, default 1800: one Session's wall clock |
| `tool_choice` | `auto` or `required` (honoured on pi only) |
| `tools` | subset of `read bash edit write grep find ls subagent web`; `ls` is dropped on claude_code |
| `writes` | omitted = anything not protected; `[]` = read-only; globs = only those paths |
| `budget` | `{warn: N, stop: N}` token override for this Role |
| `purpose`, `color` | free text and a hex colour, shown in the dashboard |
| `prompt_engineering` | section 4 |
| `harness_engineering` | pi extension paths, section 7 |

A Run needs the Roles `scout planner builder reviewer documenter` (`ROLES_FEATURE` in `lime/cli.py`); `researcher` and `designer` serve `lime research` and `lime prototype`.

A profile patches only `runtime model thinking timeout_s tool_choice`, per Role or under `defaults:`. Merge order is `defaults <- agent entry <- profile` (`config.merged_agents`), so a profile's `defaults` do not beat a value an agent entry sets itself.

```yaml
profiles:
  cheap-reviewer:
    agents:
      - {name: reviewer, runtime: claude_code, model: sonnet, thinking: medium}
```
Use with `lime check --profile cheap-reviewer` and `lime run N --profile cheap-reviewer`. The roster is frozen into a Run at launch; `--resume` ignores `--profile`.

Single vendor. Claude only: the shipped `all-claude` profile. OpenRouter only through pi: set every Role to `runtime: pi` and an `openrouter/...` model; check ids with `lime models` (pi's catalog as JSON). Not verified here: scout, planner and documenter on pi (the shipped roster runs them on claude_code), and the `[1m]` long-window ids exist only on claude_code. A pi Role that uses `subagent` or `web` loads extensions by path (section 7), which is how the shipped `api-research` profile's `lime/harness_engineering/context7.ts` works in any project.
Validated by: `lime check`; tests in `tests/test_config.py`.

## 2. Project commands and delivery

Keys under `commands:`, plus `deploy:`, `runs:`, `commit:`, `auto_deploy:` (`config.validate`, `lime/quality.py`):

| Key | During a Run |
|---|---|
| `commands.setup` | once, in the fresh worktree (shell, `cwd`=worktree); env `LIME_RUN_ID LIME_PORT LIME_DB_NAME`; non-zero fails the Run. Also run by the pre-merge check of `lime deploy` |
| `commands.test` | **required**; the placeholder `lime init` writes when it finds no test command is refused (`config.PLACEHOLDER_TEST`). First of verify's `test, lint, typecheck, build`; absent keys are skipped; first failure goes back to the builder |
| `lint typecheck build` | optional; 120 s limit each except `build` 600 s |
| `commands.test_timeout` | whole seconds >= 1, replaces the 600 s limit of `test` |
| `commands.teardown` | at `lime deploy` and `lime runs rm` |
| `deploy:` | list of shell lines handed to an interactive Claude Code deploy session after merge; empty or absent skips it |
| `runs.ports` | `"3100-3102"`; the range is the parallel-Run cap and each Run gets one as `$LIME_PORT` |
| `commit.exclude` | patterns kept out of `git add -A` (default `context_handoff/`) |
| `commit.uncounted` | still committed, not counted against `max_added_lines` |
| `commit.max_added_lines` | default 1500; a sizing budget shown to the planner and logged by verify, never a stop |
| `auto_deploy` | `off`, `staging`, `prod`; only `lime run A B C --deploy` reads it, and `off` refuses `--deploy` |

Validated by: `lime check`, `tests/test_quality.py`, `tests/test_config.py`, `tests/test_diff_accounting.py`.

## 3. Trust file

`.lime/trust.yaml`, loaded by `permissions.load_trust`, four lists and nothing else:

| List | Enforced |
|---|---|
| `protected_files` | in code: `permissions.snapshot` before and `permissions.enforce` after every agent Phase; a changed protected path is rolled back and the Phase fails with `PermissionBreach`. A Role unlocks a path only by naming it in its own `writes` |
| `secrets`, `branches`, `commands` | prompt only: rendered by `permissions.never_block` into the `{{never}}` block of every prompt, then judged by the reviewer. No code denies them. Trace text is separately scrubbed of credential-shaped strings (`lime/mask.py`) |

A Role's `writes` list is also enforced at call time by the Guard (section 7). Example: add `docs/generated/` to `protected_files`. Tests: `tests/test_permissions.py`.

## 4. Role prompts

Defaults: `lime/prompts/<role>/system.md` and `user.md`. A project overrides one kind per Role in `.lime/config.yaml`; paths are relative to the project root and the file must exist, otherwise `lime check` and the Run fail (the package default is never a fallback):
```yaml
agents:
  - name: builder
    prompt_engineering: {user: .lime/prompts/builder/user.md}
```
Placeholders substituted in both files (`agents.py`): `{{prompt}}` (ticket text or phase input), `{{previous_envelope}}`, `{{context_handoff_dir}}`, `{{never}}`, `{{rules}}` (project `AGENTS.md`, else `CLAUDE.md`, plus an ADR filename index) and `{{report}}` (the Envelope contract generated from the pydantic type, differing per runtime). Keep `{{report}}` in a custom `user.md`; the shipped ones all carry it. Code also appends text you do not control from the prompt: the builder and planner limits line (`lime/limits.py`), gate corrections, continuation prompts and the budget nudge. The exact rendered prompts are saved per Session under `.lime/runs/<run-id>/`. Tests: `tests/test_prompts.py`.

## 5. Workflows and Phases

A Chain is a Python function in `lime/workflows/`; Phases are functions in `lime/workflows/phases.py`. Order is the call order in `feature._run_chain`: `engineer, setup, scout, plan, (build, verify)xN, review, (revise, verify, review), commit, changes, document, commit_docs, push_pr` (the `commit` function records its Phase as `commit_code`). `bug_fix.py` reuses it by passing a `ChainPrompts` addendum.

Change the order or add a step: edit `_run_chain`. A new Phase looks like the existing ones:
```python
def lint_docs(run) -> None:
    with run.phase(PhaseParams(name="lint_docs", kind="code", stage="Review",
                               owner="git", description="Run the docs linter.")):
        ...  # raise to fail the Phase
```
An agent Phase uses `owner="<role>"`, `kind="agent"` and `ph.call(AgentCall(output_type=SomeEnvelope, prompt=..., gates=[...]))`. New envelope types subclass `EnvelopeBase` in `data_types.py`; a gate is `gate(envelope, run) -> GateReport` in `gates.py`. For `--resume` a Phase must start with `_replayed(run, name, OutputType)` as the others do. `stage` is `Prepare|Build|Review|Ship`.

New workflow: add its name to `WORKFLOWS` in `config.py` (ticket lint), write `lime/workflows/<name>.py` with `run(run, ticket, base_branch)`, and dispatch to it from `feature.run` next to the `bug-fix` branch; the `--resume` path in `cli.py` (search for `bug-fix`) also special-cases its prompt. A new Role also needs a roster entry, prompts, and an entry in `ROLES_FEATURE`. Tests: `tests/test_workflows_<name>.py` (see `test_workflows_feature.py`, `stage_helpers.py`), `tests/test_gates.py`.

## 6. Budgets, ceilings, retries

| What | Where | Value |
|---|---|---|
| context warn / stop | `lime/budget.py` `WARN STOP RESERVE`; override `budget:` in config or per agent | 150k / 250k; stop is `min(stop, window - 40k)`; the Spine kills the process at `window - 20k` |
| time ceiling | `timeout_s`, default `DEFAULT_TIMEOUT_S` in `data_types.py` | 1800 s, then one nudge window `NUDGE_TIMEOUT_S` (`agents.py`) 300 s |
| repeat-call stop | `REPEAT_LIMIT` (`agents.py`) | 3 identical tool calls in a row, handled like the time ceiling |
| stream retry (pi Roles only) | `STREAM_RETRIES`, `STREAM_RETRY_DELAY_S` | 2 in place (15 s, 30 s), then one fresh Session, then fail |
| malformed Envelope | `JSON_FIX_ATTEMPTS` (`agents.py`) | 2 correction turns, then the Phase fails |
| gate corrections | `retries=` on `PhaseParams`; `GATE_RETRIES` (`phases.py`) | 1 correction round |
| build / revise | `MAX_BUILD_ATTEMPTS`, `MAX_REVISE_ATTEMPTS` (`feature.py`) | 3 / 2 |
| builder `bash` call | `BASH_DEADLINE_S` (`data_types.py`), pi builder only | 900 s |
| Continuation | `_handle_partials` in `agents.py` | one per Phase |

Tests: `tests/test_budget.py`, `tests/test_agents.py`.

## 7. Guard and harness extensions

The Guard decides on every tool call. `lime guard` (`_cmd_guard` in `cli.py`) reads the Session's budget mailbox: after a stop it denies everything except handoff-directory writes, before that it denies native `write`/`edit` outside the Role's `writes` (`_write_fence`) and delivers the one-time warn message. On Claude Code it is the `PreToolUse` hook command; on pi `guard/guard.ts` shells out to it (and caps builder `bash` timeouts). It fails open on internal errors. To change a rule edit `_cmd_guard` or `_write_fence`; to change how pi calls it edit `guard/guard.ts`. `guard/` must stay beside the `lime/` package (hence the editable install).

`lime/harness_engineering/*.ts` are pi extensions: `submit.ts` (the Envelope tool, schema-validated), `scout.ts`, `subagents.ts`, `context7.ts`. Attach one to a Role with `harness_engineering: [<path>.ts]` (pi only, inert on claude_code). An entry is an absolute path or a path relative to the project root, and must exist; a project file at that path wins. If a relative path is absent from the project, lime looks for it inside the installed package and accepts it only when it names one of the extensions above, so `lime/harness_engineering/context7.ts` resolves in every stamped project and is handed to pi by its absolute install path. Remote sources (`npm:`, `git:`, URLs) are refused. Both hooks call `lime` by name, so it must be on `PATH` inside agent Sessions. Tests: `tests/test_agent_pi.py`, `tests/test_cli.py`.

## 8. The Claude Code skill

`lime/skill/lime/` (`SKILL.md` plus `cookbooks/`) is what the Orchestrator session follows. `lime init` deletes and re-copies it to `<project>/.claude/skills/lime/` every time (and merges a budget hook into `.claude/settings.json`); `.claude/skills/lime/` is in `protected_files`, so Roles cannot edit it. To tailor: edit the clone's `lime/skill/lime/`, then run `lime init` in each project to re-stamp. Tests: `tests/test_init.py`.

## 9. Hacking on lime itself

```bash
uv sync                 # dev deps: pytest, pytest-xdist, jsonschema
uv run pytest -q        # parallel (-n auto), excludes tests marked real
uv run pytest -m real   # opt-in: drives real, API-billed model calls
```
Every test gets a fresh empty HOME (`tests/conftest.py`), so your own logins, git config and pi settings do not leak in. Needs `git`. Without the optional tools some tests skip: 10 when `node` is not on PATH (they run lime's TypeScript extensions), and 1 more unless both `pi` and `node` are on PATH.
Convention: one `tests/test_<module>.py` per `lime/<module>.py`. Dashboard: `cd dashboard && bun install && bun run dev:all` (API 4600, UI 4601); `bun test`, `bun run typecheck`.

| Module | Role |
|---|---|
| `cli.py` | argparse, every command, `lime guard`, deploy |
| `config.py` | load, merge, validate, tool vocabulary |
| `runner.py`, `session.py` | Run object and `run.phase`; Run creation or rejoin |
| `workflows/` | Chains and the Phase library |
| `agents.py` | Session execution: prompts, gates, budget, retries |
| `agent_pi.py`, `agent_cc.py` | runtime adapters |
| `data_types.py`, `gates.py` | Envelopes and claim checks |
| `budget.py`, `limits.py` | thresholds, handoff note, limits line |
| `permissions.py`, `mask.py` | write fences, trust lists, trace masking |
| `quality.py`, `diff_accounting.py`, `changes.py` | verify commands, size measure and binary-file gate, diff capture |
| `wizard.py` | the `lime init` setup wizard (stack detection, roster choice, pi compaction, starter ticket) |
| `git_helper.py`, `worktree.py`, `numbering.py`, `overlap.py` | git, worktrees, ticket and ADR ledgers, launch overlap guard |
| `tracer.py`, `console.py` | SQLite and JSONL trace, run output |
| `tracker.py`, `prompts.py`, `rules.py` | ticket parsing, prompt rendering, rules block |
| `batch.py`, `docs_merge.py` | multi-ticket launch, docs-only merge conflict resolution |
| `status.py`, `focus.py`, `home.py`, `summaries.py` | the `lime status` report and dashboard home |

## 10. Another runtime

Runtimes are the `Runtime` literal in `data_types.py` and the `ADAPTERS` dict in `agents.py`. An adapter is `run(req: AgentRequest, on_event, on_spawn, on_exit) -> AgentResult`: build a headless command from the request, export the Guard mailbox variables (`LIME_BUDGET_FILE LIME_HANDOFF_DIR LIME_HANDOFF_PATH LIME_WRITES`), stream events, and return text, usage, context size and the Envelope. Also touch `config._agent_failures`, `agents._guard_entry` (how the Guard attaches) and `config.resolve_tools`. Read `agent_cc.py` (hook-based) and `agent_pi.py` (extension-based, tool-delivered Envelope) as the two examples; no third runtime exists.
