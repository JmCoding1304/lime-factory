"""The `lime init` setup wizard, driven with scripted answers and no TTY."""

import json
import os
import re
import subprocess

import pytest
import yaml

from lime import cli, config, tracker, wizard
from lime.cli import TEMPLATES_DIR

TEMPLATE = (TEMPLATES_DIR / "config.yaml").read_text()
# six commands, the roster, compaction declined: the answers that reach the ticket question
TO_TICKET = [""] * 7 + ["n"]
OWNED = [".lime/config.yaml", ".lime/tickets/.gitkeep", ".gitignore", ".claude/skills/lime", ".claude/settings.json"]


def commit_line(paths):
    listed = " ".join(paths)
    return f"git add {listed} && git commit -m 'chore: lime init' -- {listed}"


COMMIT = commit_line(OWNED)
CREATE = "gh repo create --private --source=. --remote=origin"


def scripted(*answers):
    queue = list(answers)

    def ask(prompt):
        if not queue:
            raise EOFError
        return queue.pop(0)

    return ask


def quiet(_text):
    pass


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A git project with the template stamped, pi settings and HOME inside tmp."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(config, "PI_SETTINGS", tmp_path / "pi" / "settings.json")
    root = tmp_path / "proj"
    (root / ".lime" / "tickets").mkdir(parents=True)
    (root / ".lime" / "config.yaml").write_text(TEMPLATE)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    monkeypatch.chdir(root)
    return root


def files(root, **named):
    for name, text in named.items():
        (root / name.replace("__", ".")).write_text(text)


def command_lines(text):
    """The lines a user pastes into a shell: next steps indent them seven spaces."""
    return [line.strip() for line in text.splitlines() if line.startswith("       ")]


def bare_dir(tmp_path, monkeypatch, name="bare"):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / name
    (root / ".lime/tickets").mkdir(parents=True)
    monkeypatch.chdir(root)
    return root


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   check=True, capture_output=True)


def git_out(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def stamp(root):
    """Every OWNED path on disk and untracked, as a first `lime init` leaves them."""
    for rel in OWNED:
        path = root / rel / "SKILL.md" if rel.endswith("lime") else root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def origin_head(root, branch="main"):
    """An origin whose HEAD names `branch`, as a clone of it has."""
    git(root, "remote", "add", "origin", str(root.parent / "origin.git"))
    git(root, "commit", "--allow-empty", "-q", "-m", "x")
    git(root, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
    git(root, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")


def step(text, n):
    """Step `n` of the next steps, its command lines included."""
    return text.split(f"\n  {n}. ", 1)[1].split(f"\n  {n + 1}. ", 1)[0]


def test_detects_a_node_project_from_its_lockfile_and_existing_scripts(tmp_path):
    files(tmp_path, package__json=json.dumps({"scripts": {"test": "vitest", "build": "vite build"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert wizard.detect_commands(tmp_path) == {
        "setup": "pnpm install --frozen-lockfile", "test": "pnpm test", "build": "pnpm build"}


def test_node_default_npm_test_stub_is_not_a_test_command(tmp_path):
    # exactly what `npm init -y` writes
    files(tmp_path, package__json=json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}))
    assert wizard.detect_commands(tmp_path) == {"setup": "npm install"}


@pytest.mark.parametrize("package", ['{"scripts": ["test"]}', '{"scripts": "jest"}', "[]"])
def test_package_json_of_an_odd_shape_detects_only_the_install(tmp_path, package):
    files(tmp_path, package__json=package)
    assert wizard.detect_commands(tmp_path) == {"setup": "npm install"}


def test_npm_with_a_lockfile_uses_ci_and_run(tmp_path):
    files(tmp_path, package__json=json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}))
    (tmp_path / "package-lock.json").write_text("{}")
    assert wizard.detect_commands(tmp_path) == {"setup": "npm ci", "test": "npm test", "lint": "npm run lint"}


def test_detects_python_with_uv_and_only_the_tools_pyproject_names(tmp_path):
    files(tmp_path, pyproject__toml="[tool.ruff]\n")
    (tmp_path / "uv.lock").write_text("")
    assert wizard.detect_commands(tmp_path) == {
        "setup": "uv sync", "test": "uv run pytest", "lint": "uv run ruff check ."}


def test_detects_cargo_and_go(tmp_path):
    files(tmp_path, Cargo__toml="")
    assert wizard.detect_commands(tmp_path)["test"] == "cargo test"
    other = tmp_path / "go"
    other.mkdir()
    files(other, go__mod="module x\n")
    assert wizard.detect_commands(other) == {
        "test": "go test ./...", "lint": "go vet ./...", "build": "go build ./..."}


def test_makefile_targets_override_the_language_guess(tmp_path):
    files(tmp_path, Cargo__toml="", Makefile="test:\n\tcargo nextest run\nbuild: x\n\nCC := gcc\n")
    found = wizard.detect_commands(tmp_path)
    assert found["test"] == "make test" and found["build"] == "make build"
    assert found["lint"].startswith("cargo")


def test_makefile_setup_is_only_ever_a_setup_target_never_install(tmp_path):
    files(tmp_path, Makefile="install:\n\tcp app /usr/local/bin\nsetup:\n\tnpm ci\n")
    assert wizard.detect_commands(tmp_path)["setup"] == "make setup"
    other = tmp_path / "install-only"
    other.mkdir()
    files(other, Makefile="install:\n\tcp app /usr/local/bin\n")
    assert "setup" not in wizard.detect_commands(other)


def test_makefile_match_follows_the_name_order_not_the_file_order(tmp_path, monkeypatch):
    monkeypatch.setitem(wizard.MAKE_TARGETS, "typecheck", ("typecheck", "types"))
    files(tmp_path, Makefile="types:\n\tx\ntypecheck:\n\ty\n")
    assert wizard.detect_commands(tmp_path)["typecheck"] == "make typecheck"


def test_python_without_a_uv_lockfile_proposes_no_setup_and_installs_nothing(tmp_path):
    files(tmp_path, pyproject__toml="[tool.ruff]\n[tool.mypy]\n")
    assert wizard.detect_commands(tmp_path) == {
        "test": "python3 -m pytest", "lint": "python3 -m ruff check .", "typecheck": "python3 -m mypy ."}


def test_wizard_says_why_a_python_project_gets_no_setup(project):
    files(project, pyproject__toml="[project]\nname = 'x'\n")
    said = []
    assert wizard.run(project, True, scripted(*TO_TICKET, "n"), said.append)
    assert [line for line in said if "uv.lock" in line] == [wizard.NO_UV_SETUP]
    assert "setup" not in config.load(project)["commands"]


def test_unknown_stack_detects_nothing(tmp_path):
    assert wizard.detect_commands(tmp_path) == {}


def test_write_commands_keeps_comments_and_loads():
    text = wizard.write_commands(TEMPLATE, {"test": "make test", "lint": "", "setup": "a: b # c"})
    raw = yaml.safe_load(text)
    assert raw["commands"] == {"test": "make test", "setup": "a: b # c"}
    assert raw["deploy"] == []
    assert "# test_timeout: 600" in text and "lime (ADR 0001)" in text
    assert "profiles:" in text and "# Every non-obvious choice" in text


def test_claude_only_roster_equals_the_all_claude_profile():
    got = config.merged_agents(yaml.safe_load(wizard.claude_only(TEMPLATE)))
    assert got == config.merged_agents(yaml.safe_load(TEMPLATE), "all-claude")
    assert {a["runtime"] for a in got} == {"claude_code"}


@pytest.mark.parametrize("profile", [None, *yaml.safe_load(TEMPLATE)["profiles"]])
def test_claude_only_roster_leaves_every_profile_valid(tmp_path, profile):
    raw = yaml.safe_load(wizard.claude_only(TEMPLATE))
    assert config.validate(raw, profile=profile, root=tmp_path) == []


def test_enter_everywhere_ends_with_a_valid_config(project):
    files(project, package__json=json.dumps({"scripts": {"test": "jest"}}))
    # six commands, the roster, compaction, no ticket
    assert wizard.run(project, True, scripted(*[""] * 8, "n"), quiet) is True
    raw = config.load(project)
    assert raw["commands"]["test"] == "npm test"
    assert raw["deploy"] == []
    assert config.validate(raw, root=project) == []
    assert {a.runtime for a in config.roster(raw)} == {"claude_code"}
    assert json.loads(config.PI_SETTINGS.read_text()) == {"compaction": {"enabled": False}}


def test_undetected_stack_gets_a_placeholder_test_that_loads_but_refuses_a_launch(project):
    assert wizard.run(project, True, scripted(*[""] * 8, "n"), quiet)
    raw = config.load(project)
    assert raw["commands"]["test"] == config.PLACEHOLDER_TEST
    assert config.validate(raw, root=project) == [config.PLACEHOLDER_TEST_FAILURE]


def test_answers_replace_and_clear(project):
    files(project, package__json=json.dumps({"scripts": {"test": "jest", "lint": "eslint"}}))
    # setup, test, lint, typecheck, build, teardown, roster, compaction, ticket
    answers = ["", "make t", "-", "tsc", "", "-", "2", "n", "n"]
    assert wizard.run(project, True, scripted(*answers), quiet)
    commands = config.load(project)["commands"]
    assert commands["test"] == "make t" and commands["typecheck"] == "tsc"
    assert "lint" not in commands and "build" not in commands and "teardown" not in commands
    assert not config.PI_SETTINGS.exists()


def test_required_test_cannot_be_cleared(project):
    files(project, package__json=json.dumps({"scripts": {"test": "jest"}}))
    wizard.run(project, True, scripted("", "-", *[""] * 7, "n"), quiet)
    assert config.load(project)["commands"]["test"] == "npm test"


def test_default_roster_choice_keeps_the_stamped_roster(project):
    wizard.run(project, True, scripted(*[""] * 6, "2", "n", "n"), quiet)
    assert config.roster(config.load(project))[3].runtime == "pi"


def test_an_unknown_roster_answer_asks_again(project):
    asked = []
    answers = scripted(*[""] * 6, "two", "2", "n", "n")
    assert wizard.run(project, True, lambda p: asked.append(p) or answers(p), quiet)
    assert [p for p in asked if p.startswith("Choice")] == ["Choice [1]: "] * 2
    assert config.roster(config.load(project))[3].runtime == "pi"


def test_existing_config_is_not_touched_and_questions_are_skipped(project):
    before = (project / ".lime/config.yaml").read_text()
    said, asked = [], []
    assert wizard.run(project, False, lambda p: asked.append(p) or "n", said.append)
    assert (project / ".lime/config.yaml").read_text() == before
    assert "never modified" in said[0]
    assert len(asked) == 1 and "compaction" in asked[0]  # no starter-ticket question on a restamp


def test_compaction_merges_into_existing_settings(project):
    config.PI_SETTINGS.parent.mkdir(parents=True)
    config.PI_SETTINGS.write_text(json.dumps({"theme": "dark", "compaction": {"enabled": True, "keep": 3}}))
    wizard.run(project, False, scripted("y", "n"), quiet)
    assert json.loads(config.PI_SETTINGS.read_text()) == {
        "theme": "dark", "compaction": {"enabled": False, "keep": 3}}


def test_compaction_not_asked_when_already_off(project):
    config.PI_SETTINGS.parent.mkdir(parents=True)
    config.PI_SETTINGS.write_text('{"compaction": {"enabled": false}}')
    asked = []
    wizard.run(project, False, lambda p: asked.append(p) or "n", quiet)
    assert not any("compaction" in p for p in asked)


def test_declining_compaction_prints_the_manual_fix_and_changes_nothing(project):
    said = []
    wizard.run(project, False, scripted("n", "n"), said.append)
    assert not config.PI_SETTINGS.exists()
    assert any(wizard.compaction_fix_command(config.PI_SETTINGS) in line for line in said)


def test_broken_settings_json_is_left_alone(project):
    config.PI_SETTINGS.parent.mkdir(parents=True)
    config.PI_SETTINGS.write_text("{not json")
    said = []
    wizard.run(project, False, scripted("y", "n"), said.append)
    assert config.PI_SETTINGS.read_text() == "{not json"
    assert any("could not update" in line for line in said)


@pytest.mark.parametrize("body", ["[]", '"x"'])
def test_settings_that_are_not_a_json_object_are_left_alone(project, body):
    config.PI_SETTINGS.parent.mkdir(parents=True)
    config.PI_SETTINGS.write_text(body)
    said = []
    assert wizard.run(project, False, scripted("y", "n"), said.append)
    assert config.PI_SETTINGS.read_text() == body
    fix = next(line for line in said if "could not update" in line)
    assert fix.endswith('\n  fix by hand: make the file {"compaction": {"enabled": false}}')  # as `lime check` says
    assert config.compaction_failures()[0].endswith('make the file {"compaction": {"enabled": false}}')


def test_broken_settings_json_still_says_to_add_the_key(project):
    config.PI_SETTINGS.parent.mkdir(parents=True)
    config.PI_SETTINGS.write_text("{not json")
    said = []
    wizard.run(project, False, scripted("y", "n"), said.append)
    assert next(line for line in said if "could not update" in line).endswith(
        '\n  fix by hand: add {"compaction": {"enabled": false}} to it')


@pytest.mark.parametrize("compaction", [True, "off", None, [1]])
def test_a_compaction_value_that_is_not_an_object_is_replaced(project, compaction):
    config.PI_SETTINGS.parent.mkdir(parents=True)
    config.PI_SETTINGS.write_text(json.dumps({"theme": "dark", "compaction": compaction}))
    assert wizard.run(project, False, scripted("y", "n"), quiet)
    assert json.loads(config.PI_SETTINGS.read_text()) == {"theme": "dark", "compaction": {"enabled": False}}


def test_compaction_write_keeps_a_symlinked_settings_file_and_non_ascii_text(project, tmp_path):
    target = tmp_path / "dotfiles" / "pi.json"
    target.parent.mkdir()
    target.write_text('{"name": "café 漢字", "compaction": {"enabled": true}}', encoding="utf-8")
    config.PI_SETTINGS.parent.mkdir(parents=True)
    config.PI_SETTINGS.symlink_to(target)
    assert wizard.run(project, False, scripted("y", "n"), quiet)
    assert config.PI_SETTINGS.is_symlink() and config.PI_SETTINGS.resolve() == target.resolve()
    assert '"café 漢字"' in target.read_text(encoding="utf-8")
    assert json.loads(target.read_text(encoding="utf-8")) == {"name": "café 漢字", "compaction": {"enabled": False}}
    assert sorted(p.name for p in target.parent.iterdir()) == ["pi.json"]  # no temp file left behind


def test_unfilled_starter_ticket_passes_the_header_lint_but_its_launch_is_refused(project, capsys):
    (project / ".lime/tickets/7-old.md").write_text("# 7 — old\nWorkflow: feature\nStatus: open\n")
    said = []
    wizard.run(project, True, scripted(*TO_TICKET, "y", "Add a --limit flag!"), said.append)
    path = project / ".lime/tickets/8-add-a-limit-flag.md"
    assert tracker.load(path).title == "Add a --limit flag!"
    assert [f for f in config.ticket_failures(project) if not f.startswith("7-")] == []
    refusal = f"{path}: Where parses zero paths — use one backticked path per line"
    assert tracker.where_failures(tracker.load(path), project) == [refusal]
    capsys.readouterr()
    assert cli.main(["run", "8"]) == 2
    assert capsys.readouterr().err == f"lime run: {refusal}\n"
    steps = said[-1]
    assert "lime run 8" in command_lines(steps)
    assert steps.index("edit .lime/tickets/8-add-a-limit-flag.md") < steps.index("       lime check")


# --- next steps: every command line survives a paste into an interactive zsh ---


def test_next_steps_with_an_origin_push_the_checked_out_branch(project):
    git(project, "remote", "add", "origin", str(project.parent / "origin.git"))
    git(project, "commit", "--allow-empty", "-q", "-m", "x")
    git(project, "switch", "-q", "-c", "trunk")
    stamp(project)
    text = wizard.next_steps(project, None, OWNED)
    lines = command_lines(text)
    assert lines[:2] == [COMMIT, "git push -u origin trunk"]
    assert CREATE not in lines
    assert "Runs start from" not in text  # origin names no default branch: Runs start from the one checked out


def test_next_steps_without_an_origin_create_one_then_push(project):
    stamp(project)
    lines = command_lines(wizard.next_steps(project, None, OWNED))
    assert lines[:3] == [COMMIT, CREATE, "git push -u origin main"]


def test_next_steps_on_an_unborn_master_push_master(tmp_path, monkeypatch):
    root = bare_dir(tmp_path, monkeypatch)
    subprocess.run(["git", "init", "-q", "-b", "master", str(root)], check=True)
    stamp(root)
    assert command_lines(wizard.next_steps(root, None, OWNED))[:3] == [COMMIT, CREATE, "git push -u origin master"]


def test_next_steps_on_a_detached_head_say_so_instead_of_a_push(project):
    git(project, "commit", "--allow-empty", "-q", "-m", "x")
    git(project, "checkout", "-q", "--detach")
    stamp(project)
    text = wizard.next_steps(project, None, OWNED)
    assert not any("git push" in line for line in command_lines(text))
    assert "detached" in text and "switch to the branch Runs should start from" in text


def test_detached_head_note_names_the_branch_runs_start_from(project):
    origin_head(project)
    git(project, "checkout", "-q", "--detach")
    stamp(project)
    first = step(wizard.next_steps(project, None, OWNED), 1)
    assert command_lines(first) == [COMMIT]
    assert ("No branch is checked out (detached HEAD): Runs start from origin/main, so switch to main,"
            " run the lines below there, then push main to origin.") in first


def test_next_steps_on_a_branch_runs_do_not_start_from_say_where_they_do(project):
    origin_head(project)
    git(project, "switch", "-q", "-c", "feature-x")
    stamp(project)
    first = step(wizard.next_steps(project, None, OWNED), 1)
    assert command_lines(first) == [COMMIT, "git push -u origin feature-x"]
    assert ("Runs start from origin/main, not from feature-x, so the stamped files have to reach main"
            " (merge feature-x, or open a pull request) before the first launch.") in first


def test_next_steps_on_origins_default_branch_add_no_branch_note(project):
    origin_head(project)
    stamp(project)
    text = wizard.next_steps(project, None, OWNED)
    assert command_lines(text)[:2] == [COMMIT, "git push -u origin main"]
    assert "Runs start from" not in text


def test_next_steps_outside_a_repo_init_commit_create_and_push(tmp_path, monkeypatch):
    root = bare_dir(tmp_path, monkeypatch)
    assert command_lines(wizard.next_steps(root, None, OWNED)) == [
        "git init -b main", COMMIT, CREATE, "git push -u origin main",
        "lime ticket next", "lime check", "lime run N", "gh pr list", "lime deploy N"]


def test_next_steps_launch_in_the_foreground_and_say_to_keep_the_terminal(project):
    ticket = wizard.write_ticket(project, 3, "x")
    text = wizard.next_steps(project, ticket, OWNED)
    lines = text.splitlines()
    launch = lines.index("       lime run 3")
    assert "terminal" in lines[launch - 1] and "lime trace --follow" in lines[launch - 1]
    assert command_lines(text)[-3:] == ["lime run 3", "gh pr list", "lime deploy 3"]


@pytest.mark.parametrize("with_ticket", [True, False])
def test_no_command_line_carries_a_comment_redirect_or_background(tmp_path, monkeypatch, project, with_ticket):
    stamp(project)
    ticket = wizard.write_ticket(project, 3, "x") if with_ticket else None
    texts = [wizard.next_steps(project, ticket, OWNED),
             wizard.next_steps(bare_dir(tmp_path, monkeypatch), None, OWNED)]
    for line in [line for text in texts for line in command_lines(text)]:
        assert "#" not in line and "!" not in line and "$" not in line
        assert not re.search(r"[<>|;]|(?<!&)&(?!&)", line), line


def test_the_printed_commit_line_commits_exactly_the_owned_paths_and_nothing_staged_before(project):
    stamp(project)
    ticket = wizard.write_ticket(project, 1, "first")
    (project / "notes.txt").write_text("unrelated")
    (project / "staged.txt").write_text("the user's own work in progress")
    git(project, "add", "staged.txt")
    (project / ".claude" / "settings.local.json").write_text("{}")
    (project / ".lime" / "maps").mkdir()
    (project / ".lime" / "maps" / "old.md").write_text("unrelated")
    line = command_lines(wizard.next_steps(project, ticket, OWNED))[0]
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    subprocess.run(line, shell=True, cwd=project, check=True, capture_output=True,
                   env={**os.environ, **env})  # an unborn branch: this is the root commit
    committed = git_out(project, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert committed == sorted([".claude/settings.json", ".claude/skills/lime/SKILL.md", ".gitignore",
                                ".lime/config.yaml", ".lime/tickets/.gitkeep"])
    assert git_out(project, "diff", "--cached", "--name-only").split() == ["staged.txt"]  # still staged, uncommitted


def test_commit_line_lists_every_owned_path_git_has_something_to_commit_for(project):
    stamp(project)
    (project / ".gitignore").write_text("node_modules/\n")
    git(project, "add", ".gitignore", ".lime/config.yaml", ".claude/settings.json")
    git(project, "commit", "-q", "-m", "x")
    (project / ".gitignore").write_text("node_modules/\n.lime/runs/\n")  # an init appended a line: changed
    (project / ".claude/settings.json").write_text("{}")  # committed, then staged and changed again
    git(project, "add", ".claude/settings.json")
    # .lime/config.yaml is committed and unchanged: nothing to commit, so it drops out
    rest = [".lime/tickets/.gitkeep", ".gitignore", ".claude/skills/lime", ".claude/settings.json"]
    assert command_lines(wizard.next_steps(project, None, OWNED))[0] == commit_line(rest)


def test_a_path_the_project_gitignores_never_reaches_the_commit_line(project):
    stamp(project)
    (project / ".git/info/exclude").write_text(".claude/\n")  # as a global ignore would
    assert command_lines(wizard.next_steps(project, None, OWNED))[0] == commit_line(
        [".lime/config.yaml", ".lime/tickets/.gitkeep", ".gitignore"])


@pytest.mark.parametrize("state", ["no origin", "origin", "detached"])
def test_nothing_left_to_commit_prints_no_commit_line_and_says_so(project, state):
    stamp(project)
    git(project, "add", *OWNED)
    git(project, "commit", "-q", "-m", "x")
    if state != "no origin":
        origin_head(project)
    if state == "detached":
        git(project, "checkout", "-q", "--detach")
    first = step(wizard.next_steps(project, None, OWNED), 1)
    assert "the files lime init stamped are already committed" in first
    assert not any(line.startswith("git add") or "git commit" in line for line in command_lines(first))
    assert command_lines(first) == {"no origin": [CREATE, "git push -u origin main"],
                                    "origin": ["git push -u origin main"], "detached": []}[state]
    if state == "detached":
        assert "Runs start from origin/main" in first and "run the lines below" not in first


def test_ticket_declined_or_untitled_writes_nothing(project):
    wizard.run(project, True, scripted(*TO_TICKET, "n"), quiet)
    (project / ".lime/config.yaml").write_text(TEMPLATE)
    wizard.run(project, True, scripted(*TO_TICKET, "y", "  "), quiet)
    assert list((project / ".lime/tickets").glob("*.md")) == []


def test_ticket_skipped_outside_a_git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(config, "PI_SETTINGS", tmp_path / "s.json")
    root = bare_dir(tmp_path, monkeypatch)
    (root / ".lime/config.yaml").write_text(TEMPLATE)
    said, asked = [], []
    answers = scripted(*TO_TICKET)
    assert wizard.run(root, True, lambda p: asked.append(p) or answers(p), said.append)
    assert list((root / ".lime/tickets").glob("*.md")) == []
    assert any("not a git repository" in line for line in said)
    assert not any("starter ticket?" in p or p.startswith("One-line title") for p in asked)  # never asked, then dropped


def test_slugify():
    assert wizard.slugify("Add --limit to `list`!") == "add-limit-to-list"
    assert wizard.slugify("???") == "first-ticket"
    assert len(wizard.slugify("x" * 100)) == 40


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
@pytest.mark.parametrize("at", ["lint", "Choice"])
def test_interrupt_before_the_roster_is_answered_leaves_the_template_untouched(project, exc, at):
    def ask(prompt):
        if at in prompt:
            raise exc
        return ""

    said = []
    assert wizard.run(project, True, ask, said.append) is False
    assert (project / ".lime/config.yaml").read_text() == TEMPLATE
    assert "lime check" in said[-1]
    assert "delete .lime/config.yaml, then rerun `lime init`" in said[-1]


@pytest.mark.parametrize("created", [True, False])
def test_finish_by_hand_names_the_commit_and_push_a_run_needs(project, created):
    said = []
    assert wizard.run(project, created, scripted(), said.append) is False
    by_hand = said[-1].split("Or finish by hand:\n", 1)[1].splitlines()
    assert by_hand == [
        "  1. edit `commands:`, `deploy:` and the roster (`agents:`) in .lime/config.yaml",
        "  2. lime check",
        "  3. commit the files lime init stamped and push them to origin (a Run starts from what origin has)",
        "  4. lime ticket next, then write .lime/tickets/<N>-<slug>.md (see README, First Run)",
    ]


def test_the_config_is_written_once_atomically(project, monkeypatch):
    writes = []
    real = config._atomic_write
    monkeypatch.setattr(config, "_atomic_write", lambda path, text: writes.append(path) or real(path, text))
    assert wizard.run(project, True, scripted(*TO_TICKET, "n"), quiet)
    assert writes == [project / ".lime/config.yaml"]
    assert {a.runtime for a in config.roster(config.load(project))} == {"claude_code"}


def test_interrupt_on_a_kept_config_never_says_to_delete_it(project):
    said = []
    assert wizard.run(project, False, scripted(), said.append) is False  # EOF at the compaction question
    assert "delete" not in said[-1] and "lime check" in said[-1]
