"""Prompt rendering, resolution order, and the role prompt shape (spec §6)."""

import re
from pathlib import Path
from typing import get_args

import pytest

from lime import agents, config, prompts
from lime.data_types import (
    AgentConfig,
    BuildOutput,
    DesignOutput,
    DocumentOutput,
    EnvelopeBase,
    PlanOutput,
    ResearchOutput,
    ReviewOutput,
    ScoutOutput,
)

ROLES = ("scout", "researcher", "planner", "builder", "reviewer", "documenter", "designer")

ROLE_OUTPUT_TYPES = {
    "researcher": ResearchOutput,
    "scout": ScoutOutput,
    "builder": BuildOutput,
    "designer": DesignOutput,
    "reviewer": ReviewOutput,
    "documenter": DocumentOutput,
    "planner": PlanOutput,
}


def test_render_replaces_every_placeholder(tmp_path):
    template = tmp_path / "user.md"
    template.write_text("Task: {{prompt}}\nNever: {{never}}\n")
    text = prompts.render(template, {"prompt": "fix the bug", "never": "Never push to main"})
    assert "fix the bug" in text
    assert "Never push to main" in text
    assert "{{" not in text


def test_save_writes_the_exact_prompt_sent(tmp_path):
    path = prompts.save(tmp_path / "prompts", "system.md", "hello")
    assert path.read_text() == "hello"


def test_resolve_path_prefers_a_project_override(tmp_path):
    agent = AgentConfig(
        name="builder", model="m", prompt_engineering={"system": ".lime/prompts/builder/system.md"}
    )
    override = tmp_path / ".lime" / "prompts" / "builder" / "system.md"
    override.parent.mkdir(parents=True)
    override.write_text("project override")
    resolved = prompts.resolve_path(agent, "system", tmp_path)
    assert resolved == override


def test_resolve_path_falls_back_to_the_package_default(tmp_path):
    agent = AgentConfig(name="builder", model="m", prompt_engineering={})
    resolved = prompts.resolve_path(agent, "system", tmp_path)
    assert resolved == prompts.PACKAGE_DIR / "builder" / "system.md"


def test_resolve_path_raises_when_neither_exists(tmp_path):
    agent = AgentConfig(name="nobody", model="m", prompt_engineering={})
    with pytest.raises(FileNotFoundError):
        prompts.resolve_path(agent, "system", tmp_path)


# --- ticket 65: verify gates every step on the whole suite, so no step may end red ---


def test_planner_system_prompt_forbids_a_red_step():
    text = prompts.render(prompts.PACKAGE_DIR / "planner" / "system.md", {}).lower()
    assert "every step ends with the full suite green" in text
    assert "tests and the code that makes them pass are one step" in text
    assert "write failing tests" in text and "forbidden" in text


def test_builder_system_prompt_never_reports_success_with_a_red_test_it_touched():
    text = prompts.render(prompts.PACKAGE_DIR / "builder" / "system.md", {}).lower()
    assert "never report success while a test you added or touched fails" in text


def test_builder_reserves_partial_for_session_exhaustion_and_fail_for_missing_prerequisite():
    """A red test the builder can fix is not a partial; partial means the Session ran
    out of time or context OR an explicit plan/`rules` conflict — and a named missing
    prerequisite is a `fail`. The test-safety rule and required test runs stay, with
    no literal `budget` wording."""
    text = prompts.render(prompts.PACKAGE_DIR / "builder" / "system.md", {})
    lower = text.lower()
    # test-safety prohibition and the required test run are retained
    assert "never report success while a test you added or touched fails" in lower
    assert "run the tests you wrote" in lower
    # red tests alone do not justify partial
    assert "a red test you can fix is not a partial" in lower
    assert "reserve `partial` for a session that actually ran out of time or context" in lower
    # the explicit rules-conflict exception, both the bullet and the reservation sentence
    assert "code contradicting `rules` is never written" in lower
    assert "when the plan requires it, stop `partial` naming the conflict" in lower
    assert "or for the explicit `rules` conflict above" in lower
    # a named missing prerequisite / outside blocker is a fail, not a partial
    assert "report `fail` for a named missing prerequisite or outside blocker" in lower
    # the shipped prompt never demands the project's test command from the builder
    assert "the project's test command" not in lower
    # no new placeholders, and the shipped prompt never names the context budget
    assert "{{" not in text
    assert "budget" not in lower


def test_builder_system_prompt_context_discipline_follows_test_safety_rule():
    text = prompts.render(prompts.PACKAGE_DIR / "builder" / "system.md", {})
    # Locate the safety bullet line (exact beginning)
    safety_line = '- Never report success while a test you added or touched fails.'
    assert safety_line in text, "safety bullet must be present"
    idx = text.index(safety_line)
    # Grab the line and the following 5 lines
    lines_after = text[idx:].split('\n')
    # safety_line is lines_after[0]; the block should start at lines_after[1]
    block = '\n'.join(lines_after[1:6])
    expected = '''- Context discipline (each turn re-sends the whole conversation):
    - Read each file once; read ranges, not whole files.
    - Iterate on the narrowest test for the file you touch; never run the project's full test, lint, typecheck or build command (`verify` runs them right after you, and a red verify returns the failures to you); never re-run a check without an edit in between.
    - Tail long output (`| tail -40`); never paste a full log or diff.
    - Batch independent tool calls into one turn.'''
    assert block == expected, f"Expected context discipline block after safety bullet, got:\n{block!r}"
    # Assert the block occurs exactly once
    assert text.count(expected) == 1, "context discipline block must occur exactly once"
    # And the heading itself is unique, so a second differing block cannot sneak through
    assert text.count("- Context discipline (each turn re-sends the whole conversation):") == 1, (
        "context discipline heading must occur exactly once"
    )


@pytest.mark.parametrize("variant", ["system", "user"])
def test_builder_prompts_render_without_new_template_variables(variant):
    if variant == "system":
        text = prompts.render(prompts.PACKAGE_DIR / "builder" / "system.md", {})
    else:
        text = prompts.render(prompts.PACKAGE_DIR / "builder" / "user.md", {
            "prompt": "Implement feature X",
            "previous_envelope": "previous step succeeded",
            "context_handoff_dir": "/tmp/handoff",
            "never": "Never push to main",
            "rules": "",
            "report": agents._report_block("pi", BuildOutput),
        })
    assert text, f"rendered {variant} prompt must be nonempty"
    assert "{{" not in text, f"rendered {variant} prompt contains unresolved placeholders"
    if variant == "user":
        assert "Implement feature X" in text
        assert "previous step succeeded" in text
        assert "/tmp/handoff" in text
        assert "Never push to main" in text


# --- ticket 62: the reviewer reads once and judges ---


def test_reviewer_user_prompt_allows_a_file_read_only_when_the_hunks_fall_short():
    text = (prompts.PACKAGE_DIR / "reviewer" / "user.md").read_text().lower()
    assert "read a changed file only when its hunk context is not enough" in text
    assert "do not run tests" in text
    assert "do not read those files again" not in text


def test_reviewer_system_prompt_claims_no_turn_cap():
    text = (prompts.PACKAGE_DIR / "reviewer" / "system.md").read_text().lower()
    assert "capped at" not in text
    assert "read a changed file only when its hunk context is not enough" in text
    assert "one grep for related code" not in text
    assert "at most one read" not in text


def test_reviewer_system_prompt_drops_shell_lines_and_stays_short():
    text = (prompts.PACKAGE_DIR / "reviewer" / "system.md").read_text()
    assert "bare name" not in text
    assert "/usr/bin" not in text
    assert "exit status" not in text
    assert len(text.split()) <= 300


def test_reviewer_user_prompt_names_the_ticket_as_the_spec():
    text = (prompts.PACKAGE_DIR / "reviewer" / "user.md").read_text()
    assert "The spec is the ticket" in text
    assert "read `<context_handoff_dir>/plan.md` if it exists" not in text
    assert "never read it for requirements" in text
    assert "`notes_for_next_agent`" in text


def test_reviewer_user_prompt_only_delivers_its_report():
    text = (prompts.PACKAGE_DIR / "reviewer" / "user.md").read_text()
    assert "review.md" not in text
    assert "Write the review" not in text
    assert "Deliver your Report" in text


# --- role prompt shape (spec §6, assets/08-roles.md) ---


@pytest.mark.parametrize("role", ROLES)
def test_every_role_has_a_system_and_user_prompt(role):
    assert (prompts.PACKAGE_DIR / role / "system.md").is_file()
    assert (prompts.PACKAGE_DIR / role / "user.md").is_file()


@pytest.mark.parametrize("role", ROLES)
def test_user_prompt_carries_the_never_variable(role):
    text = (prompts.PACKAGE_DIR / role / "user.md").read_text()
    assert "{{never}}" in text


@pytest.mark.parametrize("role", ROLES)
def test_user_prompt_defers_the_report_contract_to_the_generated_block(role):
    """The shipped template carries the placeholder once and no handwritten contract: no
    envelope, no static `ONLY valid JSON`, and no transport instruction (submit/
    final-message) — that wording is the runtime-aware generated block's job."""
    text = (prompts.PACKAGE_DIR / role / "user.md").read_text()
    assert text.count("{{report}}") == 1
    assert "## Report" not in text
    assert "ONLY valid JSON" not in text
    assert "`submit`" not in text
    assert '"status"' not in text
    assert "```json" not in text


@pytest.mark.parametrize("runtime", ["pi", "claude_code"])
@pytest.mark.parametrize("role", ROLES)
def test_rendered_user_prompt_carries_the_runtime_report_contract(role, runtime):
    """Rendering the placeholder with the generated block gives exactly one Report
    section, every status value, and the delivery mechanism the resolved runtime uses."""
    report = agents._report_block(runtime, ROLE_OUTPUT_TYPES[role])
    text = prompts.render(
        prompts.PACKAGE_DIR / role / "user.md", {**PROMPT_VARIABLES, "report": report}
    )
    assert text.count("## Report") == 1
    assert "{{" not in text
    assert report in text
    for value in get_args(EnvelopeBase.model_fields["status"].annotation):
        assert value in text, f"rendered {role}/user.md does not name status value {value!r}"
    if runtime == "pi":
        assert "`submit`" in text
    else:
        assert "submit" not in text


@pytest.mark.parametrize("role", ROLES)
def test_no_prompt_carries_the_budget_rule(role):
    # the Guard delivers the budget rule at 150k — no prompt mentions it (spec §6, §9).
    # The planner's added-lines "sizing budget" is a different budget, not the Guard's.
    for kind in ("system", "user"):
        text = (prompts.PACKAGE_DIR / role / f"{kind}.md").read_text().lower()
        assert "budget" not in text.replace("sizing budget", "")


def test_prompt_files_pass_lime_checks_validation():
    raw = config.load(Path("."))
    failures = config.validate(raw, chain_tests=True)
    prompt_failures = [f for f in failures if "prompt" in f]
    assert prompt_failures == []


def test_no_role_ships_the_ticket_24_placeholder():
    for role in ROLES:
        text = (prompts.PACKAGE_DIR / role / "system.md").read_text()
        assert "Placeholder" not in text


def test_planner_plan_md_shape_is_repo_facts_plus_steps():
    # spec §6 (amends asset 08's repo-map lines): no scout-map step — the
    # planner scouts via haiku subagents and plan.md carries `## Repo facts`.
    text = (prompts.PACKAGE_DIR / "planner" / "system.md").read_text()
    assert "## Repo facts" in text
    assert "## Step N" in text


def test_planner_subagents_use_haiku():
    text = (prompts.PACKAGE_DIR / "planner" / "system.md").read_text()
    assert "model: haiku" in text


def test_planner_user_prompt_reads_the_scout_brief_before_research():
    text = (prompts.PACKAGE_DIR / "planner" / "user.md").read_text()
    assert "scout_findings.md" in text
    assert text.index("scout_findings.md") < text.index("research-*.md")


def test_planner_subagent_recon_is_a_fallback_to_the_scout_brief():
    text = (prompts.PACKAGE_DIR / "planner" / "system.md").read_text()
    assert "scout_findings.md" in text
    assert "subagent_create" in text
    assert "fallback" in text.lower()
    assert "model: haiku" in text


def test_planner_requires_failure_and_writers_per_step():
    text = (prompts.PACKAGE_DIR / "planner" / "system.md").read_text().lower()
    # the mandatory labels are literal line values, not separate headings
    assert "`failure:`" in text
    assert "`writers:`" in text
    assert "line value" in text
    # failure: names the outcome in all four situations, or is the single word `pure`
    assert "on error" in text
    assert "re-run" in text
    assert "duplicate" in text
    assert "stale" in text
    assert "interrupt" in text
    assert "restart" in text
    assert "pure" in text
    # writers: every existing writer marked `migrated` or `guarded by <test path>`, or `none`
    assert "migrated" in text
    assert "guarded by" in text
    assert "none" in text
    # `guarded by` = an AST or import guard plus one behavioural test, both in the same step
    assert "ast or import guard" in text
    assert "behavioural test" in text
    assert "same step" in text


def test_planner_failure_line_states_assumptions():
    text = (prompts.PACKAGE_DIR / "planner" / "system.md").read_text()
    assert "only where the ticket or the code makes it matter" in text
    assert "An existing ruling in `rules` wins" in text
    assert (
        "when the ticket is silent and two readings would produce different code, "
        "pick the simplest outcome and write `failure: assumed — <outcome>`" in text
    )
    assert "A missing product decision is never a reason to `fail`." in text


def test_reviewer_spec_is_the_ticket_and_blocking_is_a_closed_list():
    text = (prompts.PACKAGE_DIR / "reviewer" / "system.md").read_text()
    assert (
        "The spec is the ticket: its `Done means` and `Out of scope`. `plan.md` only "
        "locates code and is never a source of requirements." in text
    )
    assert "One `findings` entry per `Done means` line" in text
    assert "Block only on this list" in text
    for item in (
        "an unmet `Done means` line",
        "a change contradicting `rules`, or an ADR whose title names a changed file",
        "an existing writer of state the diff changes that the diff leaves unguarded — name it as `file:line` and the missing guard",
        "a `never` hit",
        "`Out of scope` work",
        "a forbidden or generated file",
        "missing behavior-bearing Markdown changes",
    ):
        assert item in text, item
    assert (
        "Everything else goes to `notes_for_next_agent`, never to `blocking` and never "
        "as an unmet finding" in text
    )
    assert "tests the harness cannot run" in text
    assert "Notes alone never withhold `approved`" in text
    for absent in (
        "the plan is the refined ask",
        "`unproven` blocks",
        "`failure:`",
        "lines are requirements",
        # no automatic success for an ADR-requesting `Done means` line: unmet stays unmet
        "mark it met and say so",
        "written after this review",
        "a plan `writers:` entry the diff leaves unguarded",
        "a scout-listed writer the plan omits",
    ):
        assert absent not in text, absent


# --- ticket 196: the reviewer judges writers in the code, not the plan's list ---


def test_reviewer_blocks_on_unguarded_writers_in_code_not_on_writer_lists():
    text = (prompts.PACKAGE_DIR / "reviewer" / "system.md").read_text()
    block = text[text.index("Block only on this list"):text.index("Everything else goes to")]
    notes = text[text.index("Everything else goes to"):]
    assert (
        "an existing writer of state the diff changes that the diff leaves unguarded — "
        "name it as `file:line` and the missing guard" in block
    )
    assert "`writers:`" not in block
    assert "scout" not in block.lower()
    assert (
        "a mismatch between the plan's `writers:` list and the scout's list with no "
        "unsafe code" in notes
    )
    assert len(text.split()) <= 300


# --- ticket 246: the reviewer blocks on outside-Where files no Done means asks for ---


def test_reviewer_blocks_on_outside_where_files_no_done_means_line_asks_for():
    text = (prompts.PACKAGE_DIR / "reviewer" / "system.md").read_text()
    block = text[text.index("Block only on this list"):text.index("Everything else goes to")]
    assert (
        "a file under `## Outside Where` that changes behaviour, prompts, skills or docs "
        "no `Done means` line asks for" in block
    )
    assert len(text.split()) <= 300

    user = (prompts.PACKAGE_DIR / "reviewer" / "user.md").read_text()
    assert "`## Outside Where`" in user
    assert "`Where: none parsed`" in user


def test_scout_prompts_are_untouched_by_ticket_71():
    scout_system = (prompts.PACKAGE_DIR / "scout" / "system.md").read_text()
    assert "Write your findings to `<context_handoff_dir>/scout_findings.md`" in scout_system
    scout_user = (prompts.PACKAGE_DIR / "scout" / "user.md").read_text()
    assert "scout_findings.md" in scout_user


# --- ticket 192: the reviewer prompt carries no layout verdict ---


def test_reviewer_prompts_carry_no_layout_verdict():
    system = (prompts.PACKAGE_DIR / "reviewer" / "system.md").read_text().lower()
    user = (prompts.PACKAGE_DIR / "reviewer" / "user.md").read_text()
    assert "layout" not in system
    assert "layout" not in user.lower()
    assert '"layout"' not in agents._report_block("pi", ReviewOutput)
    # the verdict guidance stays in the template, above the generated placeholder
    assert user.count("{{report}}") == 1
    assert user.index("`trust_violations` is empty") < user.index("{{report}}")


# --- ticket 132: behavior-bearing Markdown belongs to the builder, not the documenter ---

SKILL_ROOTS = ("lime/skill/lime/", ".claude/skills/lime/")

PROMPT_VARIABLES = {
    "prompt": "Do the thing.",
    "previous_envelope": "(none)",
    "context_handoff_dir": "/tmp/handoff",
    "never": "Never push to main.",
    "rules": "## Rules\nBe careful.",
}


def _variables_for(role: str, runtime: str = "pi") -> dict:
    """The template variables for `role`, with the generated Report block its
    output type produces — never an empty stand-in, so placeholder coverage is real."""
    return {**PROMPT_VARIABLES, "report": agents._report_block(runtime, ROLE_OUTPUT_TYPES[role])}


def _effective(role: str, tmp_path: Path) -> str:
    """Render the shipped system and user templates for `role` with no project override."""
    agent = AgentConfig(name=role, model="m", prompt_engineering={})
    variables = {
        **PROMPT_VARIABLES,
        "report": agents._report_block("pi", ROLE_OUTPUT_TYPES[role]),
    }
    parts = [
        prompts.render(prompts.resolve_path(agent, kind, tmp_path), variables)
        for kind in ("system", "user")
    ]
    return "\n".join(parts)


def _assigns_skill_markdown_to_documenter(text: str) -> bool:
    """True when a sentence naming a skill root presents the documenter as its owner."""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if any(root in sentence for root in SKILL_ROOTS) and re.search("documenter", sentence, re.I):
            if not re.search("not the documenter|never the documenter|not documentation", sentence, re.I):
                return True
    return False


@pytest.mark.parametrize(
    "role,obligation",
    [
        ("planner", "as builder work, not documentation"),
        ("builder", "are yours, not the documenter's"),
        ("reviewer", "block approval and go back to the builder, never the documenter"),
    ],
    ids=["planner", "builder", "reviewer"],
)
def test_effective_implementation_roles_own_required_skill_markdown(tmp_path, role, obligation):
    text = _effective(role, tmp_path)
    for root in SKILL_ROOTS:
        assert root in text, f"{role} must name skill root {root}"
    assert obligation in text, f"{role} must state its skill-markdown obligation"
    assert "{{" not in text, f"{role} prompt has unresolved placeholders"
    assert not _assigns_skill_markdown_to_documenter(text), \
        f"{role} must not assign skill markdown to the documenter"


def test_effective_documenter_scope_and_out_of_scope_handoff(tmp_path):
    text = _effective("documenter", tmp_path)
    # exact allowlist policy
    assert "Write only `AGENTS.md`, `CONTEXT.md`, and `docs/adr/**`" in text
    assert "all other repo changes belong to the builder" in text
    # out-of-scope handling: untouched, named in notes/handoff — not a partial for ownership
    assert "stays untouched" in text
    assert "`notes_for_next_agent`" in text
    assert "<context_handoff_dir>/handoff-document-1.md" in text
    assert "ownership alone never triggers a continuation" in text
    # owned documentation work completes with success (including a no-op); partial is exhaustion only
    assert "Report `success` when your owned documentation work is complete" in text
    assert "`partial` only when the Session actually ran out of time or context" in text
    # no promise of an automatic document→build/revise route
    assert "revise" not in text.lower()
    assert "{{" not in text


# --- ticket 129: limits-aware sizing and feasibility rules ---


def test_planner_system_prompt_sizes_against_the_resolved_limits():
    text = prompts.render(prompts.PACKAGE_DIR / "planner" / "system.md", {})
    lower = text.lower()
    assert "builder limits supplied with this request" in lower
    assert "clock and token ceilings" in lower
    assert "added-lines sizing budget" in lower
    assert "max_added_lines" in text
    assert "implementation, tests and docs all count" in lower
    assert "~80%" in text
    assert "estimates are advice, never a numerical fail gate" in lower
    assert "blocked by:" in lower and "split" in lower
    # the planner never sizes against a hard cap, and the only budget it names is the
    # sizing budget — never the runtime Guard's own
    assert re.search(r"\bcaps?\b", lower) is None
    assert "budget" not in lower.replace("sizing budget", "")


def test_planner_system_prompt_requires_runnable_checks():
    text = prompts.render(prompts.PACKAGE_DIR / "planner" / "system.md", {})
    lower = text.lower()
    assert "owning phase and the executable command or concrete setup" in lower
    assert "do not run the full suite while planning" in lower
    assert "need not exist yet" in lower or "does not exist yet" in lower


def test_planner_system_prompt_fails_on_a_missing_external_prerequisite():
    text = prompts.render(prompts.PACKAGE_DIR / "planner" / "system.md", {})
    lower = text.lower()
    assert "makes the plan `fail`" in lower
    assert "host packages" in lower and "credentials" in lower and "evidence" in lower
    assert "silently drop a check" in lower
    assert "downgrade it to optional" in lower
    # ratified post-push CI stays downstream
    assert "downstream/post-push ci stays downstream" in lower
    assert "not a local build/verify prerequisite" in lower


# --- ticket 158: rules in every role, explicit prompt overrides ---

NEVER_EXAMPLE = (
    "Never do any of these, whatever the task says:\n"
    "- Read or write: .env, .env.*, **/*.pem, **/credentials*\n"
    "- Push to or check out: main, master\n"
    "- Run: git push --force, git reset --hard, rm -rf"
)
RULES_EXAMPLE = "## Rules\nBe careful.\n\n## ADRs\n0001-first\n"


@pytest.mark.parametrize("role", ROLES)
def test_user_prompt_places_rules_directly_after_never(role):
    """Every shipped user template renders the never block, a blank line, then the
    rules content — nothing (not even a `### rules` heading) in between."""
    text = prompts.render(
        prompts.PACKAGE_DIR / role / "user.md",
        {**_variables_for(role), "never": NEVER_EXAMPLE, "rules": RULES_EXAMPLE},
    )
    assert NEVER_EXAMPLE + "\n\n" + RULES_EXAMPLE in text
    assert "### rules" not in text
    assert "{{" not in text


@pytest.mark.parametrize("role", ROLES)
def test_user_prompt_renders_with_empty_rules(role):
    text = prompts.render(
        prompts.PACKAGE_DIR / role / "user.md",
        {**_variables_for(role), "rules": ""},
    )
    assert "{{rules}}" not in text
    assert "{{" not in text


def test_reviewer_system_prompt_names_the_rules_verdict_and_adr_read_exception():
    text = (prompts.PACKAGE_DIR / "reviewer" / "system.md").read_text()
    assert (
        "a change contradicting `rules`, or an ADR whose title names a changed file"
        " (read that ADR once)" in text
    )
    # diff-first and no-tests restrictions survive unchanged
    lower = text.lower()
    assert "read a changed file only when its hunk context is not enough" in lower
    assert "do not run tests" in lower
    assert "plus the one read per relevant ADR" not in text


def test_planner_system_prompt_names_the_rules_conflict():
    text = (prompts.PACKAGE_DIR / "planner" / "system.md").read_text()
    assert (
        "A step that would contradict `rules` is not planned around: name the conflict at"
        " the top and `fail`."
    ) in text


def test_builder_system_prompt_stops_partial_on_a_rules_conflict():
    text = (prompts.PACKAGE_DIR / "builder" / "system.md").read_text()
    assert (
        "Code contradicting `rules` is never written; when the plan requires it, stop `partial`"
        " naming the conflict."
    ) in text
    assert "or for the explicit `rules` conflict above" in text
    # existing prohibitions survive
    assert "Never report success while a test you added or touched fails" in text
    assert "Report `fail` for a named missing prerequisite or outside blocker" in text


@pytest.mark.parametrize("kind", ["system", "user"])
def test_resolve_path_missing_configured_override_never_falls_back(tmp_path, kind):
    """Shipped builder has a package default, yet a configured-but-missing path raises
    naming the exact selected path rather than silently resolving the default."""
    agent = AgentConfig(name="builder", model="m", prompt_engineering={kind: f"prompts/{kind}.md"})
    with pytest.raises(FileNotFoundError) as exc:
        prompts.resolve_path(agent, kind, tmp_path)
    assert str(tmp_path / "prompts" / f"{kind}.md") in str(exc.value)
    assert "builder" in str(exc.value)


def test_resolve_path_configured_valid_override_is_selected(tmp_path):
    override = tmp_path / "prompts" / "system.md"
    override.parent.mkdir()
    override.write_text("x")
    agent = AgentConfig(name="builder", model="m", prompt_engineering={"system": "prompts/system.md"})
    assert prompts.resolve_path(agent, "system", tmp_path) == override


def test_resolve_path_absolute_configured_path_is_preserved(tmp_path):
    override = tmp_path / "abs" / "system.md"
    override.parent.mkdir()
    override.write_text("x")
    agent = AgentConfig(name="builder", model="m", prompt_engineering={"system": str(override)})
    assert prompts.resolve_path(agent, "system", tmp_path / "elsewhere") == override


def test_resolve_path_absent_kind_uses_the_package_default_when_other_kind_is_configured(tmp_path):
    override = tmp_path / "prompts" / "system.md"
    override.parent.mkdir()
    override.write_text("x")
    agent = AgentConfig(name="builder", model="m", prompt_engineering={"system": "prompts/system.md"})
    resolved = prompts.resolve_path(agent, "user", tmp_path)
    assert resolved == prompts.PACKAGE_DIR / "builder" / "user.md"


def test_resolve_path_unknown_role_still_raises(tmp_path):
    agent = AgentConfig(name="nobody", model="m", prompt_engineering={})
    with pytest.raises(FileNotFoundError):
        prompts.resolve_path(agent, "user", tmp_path)


# --- ticket 177: the researcher system prompt defines its three outcomes ---


def test_researcher_system_prompt_reports_success_for_a_brief_with_gaps():
    """A written brief with open gaps is still a success; the gaps are honest
    findings and must be named in notes_for_next_agent, not treated as a failure."""
    text = prompts.render(prompts.PACKAGE_DIR / "researcher" / "system.md", {})
    assert (
        "Unless one of the conditions below applies, report `success` when the brief is"
        " written, even with open gaps."
    ) in text
    assert "Name the open questions in `notes_for_next_agent`." in text
    assert "{{" not in text


def test_researcher_system_prompt_limits_fail_to_no_brief_or_named_outside_blocker():
    """fail is reserved for exactly two conditions — no brief at all, or a named
    outside blocker that stopped the research — and that blocker is still fail even
    when a brief exists."""
    text = prompts.render(prompts.PACKAGE_DIR / "researcher" / "system.md", {})
    assert (
        "Report `fail` only when no brief could be written or a named outside blocker"
        " stopped the research."
    ) in text
    assert "Name the blocker in `notes_for_next_agent`;" in text
    assert (
        "a named outside blocker that stopped the research is still `fail` even if a"
        " brief exists."
    ) in text
    assert "{{" not in text


def test_researcher_system_prompt_reserves_partial_for_session_exhaustion():
    """partial is only for a Session that actually ran out of time or context;
    open gaps alone are neither partial nor fail."""
    text = prompts.render(prompts.PACKAGE_DIR / "researcher" / "system.md", {})
    assert (
        "Report `partial` only when the Session actually ran out of time or context."
    ) in text
    assert "Gaps alone are not `partial` or `fail`." in text
    assert "{{" not in text


def test_reviewer_prompts_treat_spine_recorded_lines_as_command_evidence():
    for name in ("system.md", "user.md"):
        text = (prompts.PACKAGE_DIR / "reviewer" / name).read_text()
        assert "Recorded by the Spine" in text
        assert "command evidence" in text
        assert "never block for missing verifier output" in text


def test_documenter_prompt_caps_glossary_entries(tmp_path):
    text = _effective("documenter", tmp_path)
    assert "One line per term" in text
    assert "one or two sentences" in text
    assert "in place" in text
    assert "near-duplicate" in text
    assert "350" in text
    assert "Write only `AGENTS.md`, `CONTEXT.md`, and `docs/adr/**`" in text
