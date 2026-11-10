"""`lime init` — the idempotent owner-split stamp (spec §13), on a scratch dir."""

import ast
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from lime import cli, config
from lime.cli import SKILL_SRC, TEMPLATES_DIR, main

TEMPLATES = TEMPLATES_DIR
REPO_ROOT = Path(__file__).resolve().parent.parent
COOKBOOK = SKILL_SRC / "cookbooks" / "install.md"


def _step(text: str, n: int) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"{n}. "))


def _section(text: str, start_heading: str, end_heading: str) -> str:
    """Slice a heading-delimited section so text elsewhere cannot satisfy an assertion."""
    start = text.index(start_heading)
    end = text.index(end_heading, start + len(start_heading))
    return text[start:end]


def _numbered_step(text: str, n: int) -> str:
    """Return a complete numbered step, up to the next numbered step's boundary."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{n}. "))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith(f"{n + 1}. ")),
        len(lines),
    )
    return "\n".join(lines[start:end])


@pytest.fixture
def scratch(tmp_path, monkeypatch, capsys):
    """A scratch project as cwd, with HOME rerouted so ~/.lime lands in tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LIME_PROJECTS", raising=False)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_first_run_stamps_everything(scratch):
    assert main(["init"]) == 0
    assert (scratch / ".lime/config.yaml").read_text() == (TEMPLATES / "config.yaml").read_text()
    assert (scratch / ".lime/trust.yaml").read_text() == (TEMPLATES / "trust.yaml").read_text()
    for d in ("maps", "tickets", "specs", "prompts"):
        assert (scratch / ".lime" / d / ".gitkeep").exists()
    assert (scratch / ".lime/runs").is_dir()
    gitignore = (scratch / ".gitignore").read_text()
    assert ".lime/runs/" in gitignore
    assert "__pycache__/" in gitignore
    assert (scratch / ".claude/skills/lime/SKILL.md").read_text() == (SKILL_SRC / "SKILL.md").read_text()
    home = Path.home()
    assert (home / ".lime/worktrees").is_dir()
    assert str(scratch.resolve()) in (home / ".lime/projects").read_text().splitlines()


def test_template_stamps_auto_deploy_off(scratch):
    assert main(["init"]) == 0
    text = (scratch / ".lime/config.yaml").read_text()
    lines = [line for line in text.splitlines() if line.startswith("auto_deploy: off")]
    assert len(lines) == 1
    assert "off | staging | prod" in lines[0]
    assert config.auto_deploy(yaml.safe_load(text)) == "off"
    failures = config.validate(yaml.safe_load(text))
    assert not any("auto_deploy" in failure for failure in failures)


def test_run_twice_identical_tree(scratch):
    assert main(["init"]) == 0
    before = tree_snapshot(scratch)
    registry_before = (Path.home() / ".lime/projects").read_text()
    assert main(["init"]) == 0
    assert tree_snapshot(scratch) == before
    assert (Path.home() / ".lime/projects").read_text() == registry_before


def test_user_edits_survive_restamp_while_skill_is_rewritten(scratch):
    assert main(["init"]) == 0
    config = scratch / ".lime/config.yaml"
    trust = scratch / ".lime/trust.yaml"
    config.write_text("# mine\ncommands:\n  test: pytest\n")
    trust.write_text("protected_files: [.env]\n")
    skill = scratch / ".claude/skills/lime/SKILL.md"
    skill.write_text("clobbered")
    stray = scratch / ".claude/skills/lime/stray.md"
    stray.write_text("left behind by an old version")
    assert main(["init"]) == 0
    assert config.read_text() == "# mine\ncommands:\n  test: pytest\n"
    assert trust.read_text() == "protected_files: [.env]\n"
    assert skill.read_text() == (SKILL_SRC / "SKILL.md").read_text()
    assert not stray.exists()


def test_settings_json_merge_keeps_other_keys_and_refreshes_the_entry(scratch):
    settings = scratch / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "model": "opus",
        "hooks": {
            "UserPromptSubmit": [
                {"hooks": [{"type": "command", "command": "lime hook budget --old-flag"}]},
                {"hooks": [{"type": "command", "command": "echo theirs"}]},
            ],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "true"}]}],
        },
    }))
    assert main(["init"]) == 0
    merged = json.loads(settings.read_text())
    assert merged["model"] == "opus"
    assert merged["hooks"]["PreToolUse"]
    entries = merged["hooks"]["UserPromptSubmit"]
    commands = [h["command"] for e in entries for h in e["hooks"]]
    assert commands == ["echo theirs", "lime hook budget"]


def test_invalid_settings_json_fails_without_clobbering(scratch, capsys):
    settings = scratch / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json")
    assert main(["init"]) == 1
    assert settings.read_text() == "{not json"
    assert "settings.json" in capsys.readouterr().err


def test_gitignore_lines_never_duplicated(scratch):
    (scratch / ".gitignore").write_text("node_modules/\n.lime/runs/\n")
    assert main(["init"]) == 0
    lines = (scratch / ".gitignore").read_text().splitlines()
    assert lines.count(".lime/runs/") == 1
    assert lines.count("__pycache__/") == 1
    assert lines[0] == "node_modules/"


def test_existing_lime_dirs_left_alone(scratch):
    tickets = scratch / ".lime/tickets"
    tickets.mkdir(parents=True)
    (tickets / "07-thing.md").write_text("# 07")
    assert main(["init"]) == 0
    assert (tickets / "07-thing.md").exists()
    assert not (tickets / ".gitkeep").exists()  # dir existed → user-owned, untouched


def test_never_stamps_placeholder_docs(scratch):
    assert main(["init"]) == 0
    for doc in ("AGENTS.md", "CONTEXT.md", "CLAUDE.md", "docs"):
        assert not (scratch / doc).exists()


def test_registry_respects_lime_projects_override(scratch, monkeypatch, tmp_path):
    registry = tmp_path / "elsewhere" / "projects"
    monkeypatch.setenv("LIME_PROJECTS", str(registry))
    assert main(["init"]) == 0
    assert str(scratch.resolve()) in registry.read_text().splitlines()
    assert not (Path.home() / ".lime/projects").exists()


def test_init_stamps_a_dir_with_no_git_repo(scratch, monkeypatch, tmp_path):
    """`lime init` needs no repo — the cookbook's step 1, not the CLI, does the `git init`."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # git must not find a repo above tmp
    assert not (scratch / ".git").exists()
    assert subprocess.run(["git", "rev-parse", "--git-dir"], cwd=scratch, capture_output=True).returncode != 0
    assert main(["init"]) == 0
    assert (scratch / ".lime/config.yaml").exists()
    assert (scratch / ".claude/skills/lime/cookbooks/install.md").read_text() == COOKBOOK.read_text()
    assert not (scratch / ".git").exists()


def test_install_cookbook_bootstraps_greenfield():
    """Step 1 bootstraps a no-`.git` project, step 8 creates the missing origin; the stop clause survives;
    this repo's stamped copy is byte-identical to the source."""
    text = COOKBOOK.read_text()
    step1, step8 = _step(text, 1), _step(text, 8)
    assert "git init" in step1
    assert "stop and say why" in step1
    assert "gh repo create" in step8 and "--private" in step8 and "gh pr create" in step8
    assert (REPO_ROOT / ".claude/skills/lime/cookbooks/install.md").read_bytes() == COOKBOOK.read_bytes()


def test_stamped_skill_is_byte_identical_to_the_source():
    assert tree_snapshot(SKILL_SRC) == tree_snapshot(REPO_ROOT / ".claude/skills/lime")


def test_skill_text_numbers_tickets_with_lime_ticket_next():
    for path in (SKILL_SRC / "SKILL.md", SKILL_SRC / "cookbooks" / "run.md", SKILL_SRC / "cookbooks" / "map.md"):
        text = path.read_text()
        assert "lime ticket next" in text, f"{path} must mention 'lime ticket next'"


def test_run_cookbook_has_no_rescope_recovery():
    """The Red Run guidance has no needs_rescope recovery path — in both the
    source and the stamped copy."""
    for path in (
        SKILL_SRC / "cookbooks" / "run.md",
        REPO_ROOT / ".claude/skills/lime/cookbooks/run.md",
    ):
        text = path.read_text()
        assert "needs_rescope" not in text
        assert "--rescope-event" not in text
        assert "--recovery-decision" not in text
        assert "needs_rescope.json" not in text
        assert "--rereview" in text


def test_init_then_check_is_green_without_override_files(scratch, monkeypatch):
    """init→check on a fresh project: the stamped config has no prompt overrides, no
    role override files exist, and check is green with the live probes mocked."""
    assert main(["init"]) == 0
    stamped = yaml.safe_load((scratch / ".lime" / "config.yaml").read_text())
    assert all("prompt_engineering" not in entry for entry in stamped["agents"])
    for role in ("scout", "researcher", "planner", "builder", "reviewer", "documenter", "designer"):
        assert not (scratch / ".lime" / "prompts" / role).exists()
    # the same live-check mocks tests/test_cli.py::green_environment uses
    monkeypatch.setattr(cli.config, "credential_failures", lambda agents: ([], []))
    monkeypatch.setattr(cli.config, "compaction_failures", lambda: [])
    monkeypatch.setattr(cli, "_probe_failures", lambda: [])
    monkeypatch.setattr(cli.config, "_extension_failures", lambda agent, root: [])
    monkeypatch.setattr(cli.config, "capability_failures", lambda agents: ([], []))
    assert main(["check"]) == 0


def test_install_cookbook_documents_explicit_overrides_and_rules():
    text = COOKBOOK.read_text()
    assert "prompt_engineering" in text
    assert "no package default rescues it" in text
    assert "owner-managed" in text
    assert "filename-only ADR index as the `rules` variable" in text
    assert "stops `partial`" in text
    assert "≤40" in text and "above 60" in text
    assert (REPO_ROOT / ".claude/skills/lime/cookbooks/install.md").read_text() == text


def test_install_cookbook_caps_context_entries():
    line = next(
        l for l in COOKBOOK.read_text().splitlines() if l.startswith("- **CONTEXT.md**")
    )
    assert "350" in line
    assert "`##`" in line


def test_init_stamps_commit_defaults(scratch):
    """The stamped YAML includes the commit block with correct defaults."""
    assert main(["init"]) == 0
    text = (scratch / ".lime" / "config.yaml").read_text()
    raw = yaml.safe_load(text)
    commit = raw.get("commit", {})
    assert commit.get("exclude") == ["context_handoff/"]
    assert commit.get("uncounted") == []
    assert commit.get("max_added_lines") == 1500
    # the empty list is stamped literally, not omitted
    assert "uncounted: []" in text


def test_init_explains_exclude_versus_uncounted(scratch):
    """The stamped comments explain the accounting-only exemption versus staging exclusions."""
    assert main(["init"]) == 0
    text = (scratch / ".lime" / "config.yaml").read_text()
    lowered = text.lower()
    # accounting-only exemption vs staging exclusion
    assert "accounting exemption" in lowered
    assert "staging exclusion" in lowered
    # example lockfile
    assert "lockfile" in lowered
    # exclude precedence on overlap
    assert "exclude wins" in lowered or "precedence" in lowered
    # ordinary ignore behavior, no force-add
    assert "force-add" in lowered or "force add" in lowered
    # continued binary rules
    assert "binary" in lowered
    # neither list removes already-committed branch content
    assert "already" in lowered and "committed" in lowered
    # the default cap stays, and no built-in lockfile list
    assert "max_added_lines: 1500" in text


def test_ticket_cutting_guidance_uses_limits_and_runnable_checks():
    """SKILL's Request→ticket rule and both cookbook references agree on resolved
    limits, headroom, runnable checks and `Blocked by:` splits, in source and stamped."""
    for root in (SKILL_SRC, REPO_ROOT / ".claude/skills/lime"):
        skill = (root / "SKILL.md").read_text()
        assert "resolved limits" in skill
        assert "added-lines ceilings" in skill
        assert "headroom" in skill
        assert "runnable" in skill
        assert "Blocked by:" in skill
        run = (root / "cookbooks" / "run.md").read_text()
        assert "ticket-cutting rule" in run
        assert "resolved builder limits" in run
        assert "--replan" in run and "limits" in run
        map_text = (root / "cookbooks" / "map.md").read_text()
        assert "resolved limits" in map_text
        assert "≲500 lines" not in map_text
        # thin slices and human-prerequisite blockers preserved
        assert "thin end-to-end slices" in map_text
        assert "Blocked by:" in map_text


TICKET_RULE_PHRASES = [
    # failure / re-run triggers and the six-bullet budget, with no follow-up loophole
    "job",
    "poll",
    "upload",
    "external call",
    "writer of rows another module owns",
    "one of its six `Done means` bullets on the failure or re-run case",
    "never `Out of scope`",
    "follow-up ticket does not make omitting it safe",
    "six-bullet ceiling",
    # every exclusion names a ticket or never
    "ticket number or the literal word `never`",
    # ownership guard, Where/scout, same-batch prohibition
    "import guard",
    "behavioural test",
    "every existing writer the scout identified",
    "never runs in the same batch as a ticket writing those rows",
    "`Blocked by:`",
    # scaffold defaults, web and backend
    "query defaults",
    "auth caching",
    "error boundary",
    "not-found",
    "nav",
    "chunking",
    "settings that fail closed",
    # real corpus, no synthetic substitutes
    "emitted corpus plus invalid and unknown values",
    "never synthetic strings in place of that corpus",
    # production factories and tested explicit override
    "production config factories",
    "test-only override is explicit and itself tested",
    # ADR meaning, status, owner, timing and all three review flags
    "Architecture Decision Record",
    "`proposed`",
    "ADRs merge `proposed`",
    "owner accepts them in the slice's owner QA `Type: task` ticket",
    "`stays open`",
    "`halts`",
    "`runs inline`",
    "**review flag**",
]


@pytest.mark.parametrize("root", [SKILL_SRC, REPO_ROOT / ".claude/skills/lime"])
def test_ticket_authoring_rules_in_stamped_sections(root):
    """The ticket-cutting rule appears, independently, in the stamped SKILL ticket
    section and the stamped run.md complete step 1 — sliced at their boundaries."""
    skill_section = _section(
        (root / "SKILL.md").read_text(), "## Request → ticket", "## Monitor"
    )
    run_step1 = _numbered_step((root / "cookbooks" / "run.md").read_text(), 1)
    for section_text in (skill_section, run_step1):
        for phrase in TICKET_RULE_PHRASES:
            assert phrase in section_text, f"{phrase!r} missing from a ticket-cutting section"


def test_stamped_run_launch_names_all_refusals():
    """Step 3 names reservation/renumbering beside the Where-overlap and plan-gate
    refusals, keeping the single-fresh-ticket limitation on the override."""
    for root in (SKILL_SRC, REPO_ROOT / ".claude/skills/lime"):
        step3 = _numbered_step((root / "cookbooks" / "run.md").read_text(), 3)
        assert "reserves the number on origin" in step3
        assert "refused" in step3 and "renumber with `lime ticket next`" in step3
        # the refusal summary sits with the reservation guidance
        assert "refusals sit with this reservation" in step3
        assert "refuses Where overlap" in step3
        assert "--allow-overlap" in step3
        assert "single-fresh-ticket restriction" in step3
        # the plan gate refuses a step missing either literal label
        assert "plan gate refuses a step missing `failure:` or `writers:`" in step3


@pytest.mark.parametrize("root", [SKILL_SRC, REPO_ROOT / ".claude/skills/lime"])
def test_adr_acceptance_moved_off_merge(root):
    """ADR acceptance no longer rides merge, in source and stamped copy."""
    for rel in ("SKILL.md", "cookbooks/run.md"):
        assert "accepts at merge" not in (root / rel).read_text()


@pytest.mark.parametrize("root", [SKILL_SRC, REPO_ROOT / ".claude/skills/lime"])
def test_stamped_run_launch_names_deploy_and_its_key(root):
    """Step 3 names `--deploy`, its `auto_deploy` key and its end report."""
    step3 = _numbered_step((root / "cookbooks" / "run.md").read_text(), 3)
    assert "[--deploy]" in step3
    assert "--deploy" in step3
    assert "auto_deploy: staging" in step3
    assert "prod" in step3
    assert "the template stamps `off`" in step3
    assert "deployed · failed (with its resume command) · held (with its blocker)" in step3


@pytest.mark.parametrize("root", [SKILL_SRC, REPO_ROOT / ".claude/skills/lime"])
def test_map_charts_the_next_vertical_slice_on_a_locked_skeleton(root):
    """Chart-the-map names a slice-at-a-time destination on a once-locked skeleton."""
    text = (root / "cookbooks" / "map.md").read_text()
    chart = _section(text, "## Chart the map", "## Work through the map")
    step1 = _numbered_step(chart, 1)
    assert "**destination**" in step1
    assert "next useful vertical slice" in step1
    assert "`specs/<product>-skeleton.md`" in chart
    assert "locked once" in chart
    assert "every slice spec cites it" in chart
    assert 'never "all backend, then all UI"' in chart
    assert "merged code" in chart


@pytest.mark.parametrize("root", [SKILL_SRC, REPO_ROOT / ".claude/skills/lime"])
def test_map_close_ends_each_slice_cut_with_an_owner_qa_ticket(root):
    """Map close ends each cut with an owner QA task ticket and one-batch launch."""
    text = (root / "cookbooks" / "map.md").read_text()
    close = text[text.index("## Map close"):]
    assert "tracer-bullet implementation tickets" in _numbered_step(close, 1)
    qa = _numbered_step(close, 3)
    assert "The cut's last ticket is an owner QA `Type: task` ticket" in qa
    assert "`Blocked by:` the slice's last ticket" in qa
    for phrase in (
        "tries the slice on its target",
        "turns each bug into a new ticket",
        "accepts the slice's ADRs",
        "next slice's map",
        "before that map locks",
    ):
        assert phrase in qa, phrase
    assert "numbered-list quiz" in _numbered_step(close, 4)
    launch = _numbered_step(close, 6)
    assert "one batch" in launch
    assert "[run.md](run.md) 3" in launch
    assert "QA ticket closes" in launch


@pytest.mark.parametrize("root", [SKILL_SRC, REPO_ROOT / ".claude/skills/lime"])
def test_sessions_close_their_own_map_tickets(root):
    """Ticket 245: the resolving session closes its ticket with `lime ticket close`; a hands-on
    ticket closes once the owner confirms; only a Run Role Session is refused, by the tripwire."""
    text = (root / "cookbooks" / "map.md").read_text()
    intro = text[: text.index("## Chart the map")]
    assert "never something an agent does" not in intro
    assert "only inside a Run Role Session" in intro
    assert "tripwire" in intro
    step4 = _numbered_step(_section(text, "## Work through the map", "## The Captain"), 4)
    assert "then close the ticket with `lime ticket close <project> <NN>`" in step4
    assert "hands-on ticket" in step4
    assert "only once the owner confirms it done" in step4
    assert "Closing is the owner's" not in step4
    assert "never ticks a person-ticket" not in step4
    skill = (root / "SKILL.md").read_text()
    line = next(l for l in skill.splitlines() if "agent guiding the owner through a person-ticket" in l)
    assert "`lime ticket close <project> <NN>`" in line
    assert "once the owner confirms it done" in line


@pytest.mark.parametrize("root", [SKILL_SRC, REPO_ROOT / ".claude/skills/lime"])
def test_run_launch_names_one_deploy_batch_per_ratified_cut(root):
    """Run step 3 launches a ratified cut as one `--deploy` batch, QA ticket excluded;
    SKILL names the slice-at-a-time map and the one-batch launch."""
    step3 = _numbered_step((root / "cookbooks" / "run.md").read_text(), 3)
    assert "A ratified cut launches as one `lime run <every ticket of the cut> --deploy`" in step3
    assert "never link by link" in step3
    assert "`Blocked by:` is the queue" in step3
    assert "owner QA `Type: task` ticket stays out" in step3
    skill = _section((root / "SKILL.md").read_text(), "## Request → ticket", "## Monitor")
    assert "one vertical slice at a time" in skill
    assert "a ratified cut launches as one `lime run … --deploy`" in skill


@pytest.mark.parametrize("root", [SKILL_SRC, REPO_ROOT / ".claude/skills/lime"])
def test_skill_restamps_siblings_after_a_skill_change_deploys(root):
    """The deploy route tells the orchestrator to restamp siblings with `lime init`."""
    skill = (root / "SKILL.md").read_text()
    assert "restamp" in skill
    assert "sibling" in skill
    assert "`lime init --no-input`" in skill
    assert "after it lands" in skill


@pytest.mark.parametrize("root", [SKILL_SRC, REPO_ROOT / ".claude/skills/lime"])
def test_skill_authoring_line_budgets(root):
    assert len((root / "SKILL.md").read_text().splitlines()) < 70
    assert len((root / "cookbooks" / "run.md").read_text().splitlines()) < 45


def test_init_skill_copy_uses_canonical_source():
    """`_cmd_init` stamps by copying the production `SKILL_SRC` symbol, never an
    ad-hoc or diverging source — the single-source stamp invariant."""
    tree = ast.parse(Path(cli.__file__).read_text())
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_cmd_init"
    )
    copies = [
        node for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "copytree"
    ]
    assert copies, "_cmd_init must stamp the skill with shutil.copytree"
    call = copies[0]
    assert isinstance(call.args[0], ast.Name) and call.args[0].id == "SKILL_SRC"
    assert isinstance(call.args[1], ast.Name) and call.args[1].id == "skill_dest"
    dest_assign = next(
        node for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "skill_dest" for t in node.targets)
    )
    dest_parts = [n.value for n in ast.walk(dest_assign.value) if isinstance(n, ast.Constant)]
    assert sorted(dest_parts) == [".claude", "lime", "skills"]


def test_init_restamps_entire_canonical_skill_tree(scratch):
    """Running the real production stamp writer restores the whole canonical tree:
    a corrupted file and an obsolete stray both disappear; the result is idempotent."""
    dest = scratch / ".claude" / "skills" / "lime"
    assert main(["init"]) == 0
    canonical = tree_snapshot(SKILL_SRC)
    assert tree_snapshot(dest) == canonical
    (dest / "SKILL.md").write_text("corrupted")
    (dest / "cookbooks" / "run.md").write_text("corrupted")
    (dest / "obsolete.md").write_text("left behind by an old version")
    assert main(["init"]) == 0
    assert tree_snapshot(dest) == canonical
    assert main(["init"]) == 0
    assert tree_snapshot(dest) == canonical


def test_run_cookbook_documents_rereview_in_source_and_stamped_copy():
    source = SKILL_SRC / "cookbooks" / "run.md"
    stamped = REPO_ROOT / ".claude/skills/lime/cookbooks/run.md"
    for path in (source, stamped):
        text = path.read_text()
        assert "--rereview" in text
        assert "never reached review" in text
    assert source.read_bytes() == stamped.read_bytes()


def test_init_without_a_terminal_never_prompts_and_says_to_edit_commands(scratch, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("init prompted without a terminal"))
    assert main(["init"]) == 0
    assert "Edit `commands:`" in capsys.readouterr().out
    assert (scratch / ".lime/config.yaml").read_text() == (TEMPLATES / "config.yaml").read_text()


def test_init_no_input_skips_the_wizard_even_on_a_terminal(scratch, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.wizard, "run", lambda *a, **k: pytest.fail("wizard ran under --no-input"))
    assert main(["init", "--no-input"]) == 0
    assert "Edit `commands:`" in capsys.readouterr().out


def test_init_never_asks_to_edit_a_config_it_kept(scratch, capsys):
    assert main(["init", "--no-input"]) == 0
    assert "Edit `commands:`" in capsys.readouterr().out
    assert main(["init", "--no-input"]) == 0
    out = capsys.readouterr().out
    assert "kept  .lime/config.yaml" in out and "another stack" not in out


@pytest.mark.parametrize("tty", ["stdin", "stdout"])
def test_init_with_only_one_terminal_stream_never_prompts(scratch, monkeypatch, tty):
    monkeypatch.setattr(f"sys.{tty}.isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("init prompted without a full terminal"))
    assert main(["init"]) == 0


def test_init_exits_130_when_the_wizard_is_interrupted(scratch, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr(config, "PI_SETTINGS", tmp_path / "pi" / "settings.json")

    def ask(prompt):
        if prompt.startswith("Choice"):
            raise KeyboardInterrupt
        return ""

    monkeypatch.setattr("builtins.input", ask)
    assert main(["init"]) == 130
    assert (scratch / ".lime/config.yaml").read_text() == (TEMPLATES / "config.yaml").read_text()


def test_check_names_the_placeholder_test_command(scratch, monkeypatch, capsys):
    assert main(["init", "--no-input"]) == 0
    path = scratch / ".lime/config.yaml"
    path.write_text(cli.wizard.write_commands(path.read_text(), {"test": config.PLACEHOLDER_TEST}))
    monkeypatch.setattr(cli.config, "credential_failures", lambda agents: ([], []))
    monkeypatch.setattr(cli.config, "compaction_failures", lambda: [])
    monkeypatch.setattr(cli, "_probe_failures", lambda: [])
    monkeypatch.setattr(cli.config, "_extension_failures", lambda agent, root: [])
    monkeypatch.setattr(cli.config, "capability_failures", lambda agents: ([], []))
    capsys.readouterr()
    assert main(["check"]) == 1
    assert f"FAIL  {config.PLACEHOLDER_TEST_FAILURE}" in capsys.readouterr().out



def test_init_on_a_terminal_runs_the_wizard_with_the_config_state(scratch, monkeypatch):
    calls = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.wizard, "run",
                        lambda root, created, ask, owned: calls.append((root, created)) or True)
    assert main(["init"]) == 0
    assert main(["init"]) == 0
    assert calls == [(scratch.resolve(), True), (scratch.resolve(), False)]


def test_init_hands_the_wizard_every_path_it_owns_that_exists(scratch, monkeypatch):
    calls = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.wizard, "run",
                        lambda root, created, ask, owned: calls.append(list(owned)) or True)
    (scratch / ".lime/specs").mkdir(parents=True)  # the user's own dir: init stamps no .gitkeep into it
    assert main(["init"]) == 0
    assert main(["init"]) == 0
    owned = [
        ".lime/config.yaml", ".lime/trust.yaml", ".lime/maps/.gitkeep", ".lime/tickets/.gitkeep",
        ".lime/prompts/.gitkeep", ".gitignore", ".claude/skills/lime", ".claude/settings.json",
    ]
    assert calls == [owned, owned]  # a rerun hands over what an earlier init wrote, too
    assert all((scratch / rel).exists() for rel in owned)


def _git_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@example.com"),
                       ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@example.com")):
        monkeypatch.setenv(key, value)


def _on_a_terminal(monkeypatch, tmp_path, ask):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    monkeypatch.setattr(config, "PI_SETTINGS", tmp_path / "pi" / "settings.json")
    monkeypatch.setattr("builtins.input", ask)


@pytest.mark.parametrize("repo", [True, False])
def test_rerun_after_an_interrupt_commits_every_file_an_earlier_init_wrote(scratch, monkeypatch, tmp_path,
                                                                           capsys, repo):
    """The review's case: Ctrl-C at the roster, delete config.yaml as told, rerun; the printed
    lines leave nothing of init's uncommitted, only the user's own file and the ticket."""
    _git_env(monkeypatch, tmp_path)
    if repo:
        subprocess.run(["git", "init", "-q", "-b", "main"], check=True)
    (scratch / "app.py").write_text("print('mine')\n")

    def interrupt(prompt):
        if prompt.startswith("Choice"):
            raise KeyboardInterrupt
        return ""

    _on_a_terminal(monkeypatch, tmp_path, interrupt)
    assert main(["init"]) == 130
    (scratch / ".lime/config.yaml").unlink()
    answers = {"Create a starter ticket? [Y/n]: ": "y", "One-line title (what should the first Run change?): ": "First"}
    monkeypatch.setattr("builtins.input", lambda prompt: answers.get(prompt.lstrip("\n"), ""))
    capsys.readouterr()
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    first = out.split("\n  1. ", 1)[1].split("\n  2. ", 1)[0]
    lines = [line.strip() for line in first.splitlines() if line.startswith("       ")]
    for line in lines:
        if line.startswith(("git init", "git add")):  # not `gh repo create` or the push: no network
            subprocess.run(line, shell=True, check=True, capture_output=True)
    left = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                          capture_output=True, text=True, check=True).stdout.splitlines()
    assert left == (["?? .lime/tickets/1-first.md", "?? app.py"] if repo else ["?? app.py"])


def test_rerun_with_nothing_left_to_commit_prints_no_commit_line(scratch, monkeypatch, tmp_path, capsys):
    _git_env(monkeypatch, tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main"], check=True)
    assert main(["init", "--no-input"]) == 0
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "x"], check=True)
    _on_a_terminal(monkeypatch, tmp_path, lambda prompt: "")
    capsys.readouterr()
    assert main(["init"]) == 0
    out = capsys.readouterr().out
    assert "already committed" in out
    assert not any(line.strip().startswith("git add") for line in out.splitlines())


@pytest.mark.parametrize("no_input", [True, False])
def test_init_in_a_repo_subdirectory_warns_and_names_the_root(scratch, monkeypatch, tmp_path, capsys, no_input):
    _git_env(monkeypatch, tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main"], check=True)
    package = scratch / "packages" / "web"
    package.mkdir(parents=True)
    monkeypatch.chdir(package)
    if not no_input:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
        monkeypatch.setattr(cli.wizard, "run", lambda *a, **k: True)
    assert main(["init", "--no-input"] if no_input else ["init"]) == 0
    assert (package / ".lime/config.yaml").exists()  # warned, never refused
    root = scratch.resolve()
    warning = [line for line in capsys.readouterr().err.splitlines() if line]
    assert warning == [
        f"warning: this directory is packages/web inside the git repository at {root},"
        " and a Run always works from the repository root.",
        f"  Run `lime init` in {root} instead.",
    ]


def test_init_at_the_repo_root_prints_no_subdirectory_warning(scratch, monkeypatch, tmp_path, capsys):
    _git_env(monkeypatch, tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main"], check=True)
    assert main(["init", "--no-input"]) == 0
    assert "warning" not in capsys.readouterr().err
