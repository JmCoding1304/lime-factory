"""Write boundaries: snapshot → diff → rollback → PermissionBreach, trust.yaml, {{never}} (spec §8)."""

import subprocess
from pathlib import Path

import pytest

from lime import config, permissions
from lime.data_types import AgentConfig

ROOT = Path(__file__).resolve().parents[1]


def shipped_documenter() -> AgentConfig:
    """The documenter exactly as the shipped config declares it."""
    raw = config.load(ROOT)
    entry = next(a for a in raw["agents"] if a["name"] == "documenter")
    return AgentConfig.model_validate({**entry, "model": entry.get("model", "m")})


def agent(writes) -> AgentConfig:
    return AgentConfig(name="builder", model="m", writes=writes)


@pytest.fixture
def repo(tmp_path):
    """A committed repo with one ordinary file and one protected file."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "app.py").write_text("original\n")
    (tmp_path / ".lime").mkdir()
    (tmp_path / ".lime" / "trust.yaml").write_text("protected_files: [.lime/trust.yaml]\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        check=True,
    )
    return tmp_path


PROTECTED = [".lime/trust.yaml"]


# --- snapshot / changed_paths ---


def test_changed_paths_catch_appear_rewrite_and_revert(repo):
    (repo / "app.py").write_text("dirty before\n")
    before = permissions.snapshot(repo)

    (repo / "app.py").write_text("original\n")  # reverted — still a modification
    (repo / "new.txt").write_text("x")
    after = permissions.snapshot(repo)

    assert permissions.changed_paths(before, after) == ["app.py", "new.txt"]


# --- permitted ---


def test_writes_semantics():
    assert permissions.permitted("x.py", agent(None), PROTECTED)  # None = unrestricted
    assert not permissions.permitted("x.py", agent([]), PROTECTED)  # [] = read-only
    assert permissions.permitted("docs/adr/0001.md", agent(["docs/adr/"]), PROTECTED)
    assert not permissions.permitted("README.md", agent(["docs/adr/"]), PROTECTED)


def test_protected_beats_unrestricted_but_naming_the_path_unlocks_it():
    assert not permissions.permitted(".lime/trust.yaml", agent(None), PROTECTED)
    assert permissions.permitted(".lime/trust.yaml", agent([".lime/trust.yaml"]), PROTECTED)


def test_star_stops_at_the_path_separator():
    assert permissions.permitted("lime/agents.py", agent(["lime/*.py"]), [])
    assert not permissions.permitted("lime/sub/deep.py", agent(["lime/*.py"]), [])
    assert permissions.permitted("web/proto-nav/a/b.tsx", agent(["**/proto-nav/**"]), [])


def test_owned_reads_only_the_roles_own_list():
    documenter = ["AGENTS.md", "CONTEXT.md", "docs/adr/"]
    assert permissions.owned("CONTEXT.md", documenter)
    assert permissions.owned("docs/adr/0121-x.md", documenter)
    assert not permissions.owned("app.py", documenter)
    assert not permissions.owned("nested/AGENTS.md", documenter)
    # `*` stops at the path separator
    assert permissions.owned("CONTEXT.md", ["*.md"])
    assert not permissions.owned("docs/a.md", ["*.md"])
    # None and [] own nothing
    assert not permissions.owned("CONTEXT.md", None)
    assert not permissions.owned("CONTEXT.md", [])


# --- enforce: the "Done means" case ---


def test_protected_write_raises_and_rolls_back(repo):
    before = permissions.snapshot(repo)
    (repo / ".lime" / "trust.yaml").write_text("tampered\n")

    with pytest.raises(permissions.PermissionBreach, match=r"trust\.yaml"):
        permissions.enforce(repo, agent(None), before, PROTECTED)
    assert (repo / ".lime" / "trust.yaml").read_text() == "protected_files: [.lime/trust.yaml]\n"


def test_unauthorized_untracked_file_is_deleted(repo):
    before = permissions.snapshot(repo)
    (repo / "sneaky.txt").write_text("x")

    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(repo, agent([]), before, PROTECTED)
    assert not (repo / "sneaky.txt").exists()


def test_legitimate_changes_are_returned_not_raised(repo):
    before = permissions.snapshot(repo)
    (repo / "app.py").write_text("changed\n")

    assert permissions.enforce(repo, agent(None), before, PROTECTED) == ["app.py"]


def test_already_dirty_path_is_left_as_is(repo):
    (repo / "app.py").write_text("an engineer's uncommitted work\n")
    before = permissions.snapshot(repo)
    # the fingerprint is numstat counts, so the agent's edit must change them to register
    (repo / "app.py").write_text("agent\noverwrote\nit\n")

    with pytest.raises(permissions.PermissionBreach, match="left as-is"):
        permissions.enforce(repo, agent([]), before, PROTECTED)
    # never "cleaned up" into the same harm this module prevents
    assert (repo / "app.py").read_text() == "agent\noverwrote\nit\n"


def commit(repo):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qam", "done"],
        check=True,
    )


def test_engineer_commit_mid_phase_is_neither_breach_nor_reported(repo):
    (repo / "app.py").write_text("dirty\n")
    before = permissions.snapshot(repo)
    commit(repo)
    result = permissions.enforce(repo, agent([]), before, PROTECTED, ledger=permissions.Ledger(repo))
    assert result == []
    assert (repo / "app.py").read_text() == "dirty\n"


def test_untracked_file_the_agent_never_wrote_is_kept(repo):
    before = permissions.snapshot(repo)
    (repo / ".lime" / "tickets").mkdir(parents=True, exist_ok=True)
    (repo / ".lime" / "tickets" / "53.md").write_text("engineer's note\n")
    result = permissions.enforce(repo, agent([]), before, PROTECTED, ledger=permissions.Ledger(repo))
    assert result == []
    assert (repo / ".lime" / "tickets" / "53.md").exists()


def test_named_write_outside_the_fence_is_still_rolled_back(repo):
    before = permissions.snapshot(repo)
    (repo / "sneaky.txt").write_text("x")
    ledger = permissions.Ledger(repo)
    ledger.record("write", {"path": str(repo / "sneaky.txt")})
    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(repo, agent([]), before, PROTECTED, ledger=ledger)
    assert not (repo / "sneaky.txt").exists()

    # Second half: tamper .lime/trust.yaml + cc-shaped event
    before2 = permissions.snapshot(repo)
    (repo / ".lime" / "trust.yaml").write_text("tampered\n")
    ledger2 = permissions.Ledger(repo)
    ledger2.record("Edit", {"file_path": str(repo / ".lime" / "trust.yaml")})
    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(repo, agent(None), before2, PROTECTED, ledger=ledger2)
    assert (repo / ".lime" / "trust.yaml").read_text() == "protected_files: [.lime/trust.yaml]\n"


# --- rollback: prune directories left by deleted untracked files ---


def test_rollback_prunes_empty_parents(repo):
    before = permissions.snapshot(repo)
    (repo / "newdir" / "sub").mkdir(parents=True)
    (repo / "newdir" / "sub" / "a.txt").write_text("x")
    (repo / "newdir" / "sub" / "b.txt").write_text("y")

    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(repo, agent([]), before, PROTECTED)

    assert not (repo / "newdir" / "sub" / "a.txt").exists()
    assert not (repo / "newdir" / "sub" / "b.txt").exists()
    assert not (repo / "newdir" / "sub").exists()
    assert not (repo / "newdir").exists()
    # Rerunning sees the remaining actual diff and is a no-op.
    assert permissions.enforce(repo, agent([]), permissions.snapshot(repo), PROTECTED) == []


def test_rollback_stops_at_nonempty_parent(repo):
    before = permissions.snapshot(repo)
    (repo / "newdir").mkdir()
    (repo / "newdir" / "allowed.txt").write_text("keep\n")
    (repo / "newdir" / "sneaky.txt").write_text("x")

    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(repo, agent(["newdir/allowed.txt"]), before, PROTECTED)

    assert not (repo / "newdir" / "sneaky.txt").exists()
    assert (repo / "newdir" / "allowed.txt").read_text() == "keep\n"
    assert (repo / "newdir").is_dir()  # an allowed sibling preserves the parent


def test_rollback_preserves_repo_root(repo):
    before = permissions.snapshot(repo)
    (repo / "sneaky.txt").write_text("x")

    with pytest.raises(permissions.PermissionBreach):
        permissions.enforce(repo, agent([]), before, PROTECTED)

    assert not (repo / "sneaky.txt").exists()
    assert repo.is_dir()  # the repository root is never removed


def test_rollback_directory_cleanup_failure_keeps_deleted_outcome(repo, monkeypatch):
    before = permissions.snapshot(repo)
    (repo / "d").mkdir()
    (repo / "d" / "x.txt").write_text("x")
    after = permissions.snapshot(repo)

    def boom(self):
        raise OSError("directory busy")

    monkeypatch.setattr(Path, "rmdir", boom)
    outcome = permissions._roll_back(repo, "d/x.txt", before, after)
    # The file deletion succeeded; a cleanup failure must not relabel it undeleted.
    assert outcome == "deleted"
    assert not (repo / "d" / "x.txt").exists()


# --- trust.yaml + {{never}} ---


def test_load_trust_falls_back_to_the_template(tmp_path):
    trust = permissions.load_trust(tmp_path)
    assert sorted(trust) == sorted(permissions.TRUST_KEYS)
    assert all(isinstance(v, list) and v for v in trust.values())


def test_load_trust_reads_the_project_file_and_fills_missing_keys(repo):
    trust = permissions.load_trust(repo)
    assert trust["protected_files"] == [".lime/trust.yaml"]
    assert trust["secrets"] == trust["branches"] == trust["commands"] == []


def test_never_renders_secrets_branches_commands_as_a_never_block():
    block = permissions.never_block(
        {
            "protected_files": [".lime/trust.yaml"],  # enforced in code, not rendered
            "secrets": [".env", "**/*.pem"],
            "branches": ["main"],
            "commands": ["git push --force"],
        }
    )
    lines = block.splitlines()
    assert lines[0] == "Never do any of these, whatever the task says:"
    assert lines[1] == "- Read or write: .env, **/*.pem"
    assert lines[2] == "- Push to or check out: main"
    assert lines[3] == "- Run: git push --force"
    assert "trust.yaml" not in block
    rendered = "# Rules\n{{never}}\n".replace("{{never}}", block)
    assert "{{" not in rendered and "Never do any of these" in rendered


def test_never_block_drops_empty_categories():
    block = permissions.never_block(
        {
            "protected_files": [],
            "secrets": [],
            "branches": ["main"],
            "commands": [],
        }
    )
    assert block.splitlines() == [
        "Never do any of these, whatever the task says:",
        "- Push to or check out: main",
    ]


def test_never_block_empty_when_nothing_to_render():
    assert permissions.never_block(
        {"protected_files": [], "secrets": [], "branches": [], "commands": []}
    ) == ""


# --- Ledger: evidence-based attribution in shared trees (ticket 54) ---


def test_bash_naming_the_exact_path_is_the_agents_write(repo):
    """The founding case survives in shared mode: a git checkout named in
    bash is attributed to the agent and rolled back."""
    (repo / "app.py").write_text("dirty\n")
    before = permissions.snapshot(repo)
    subprocess.run(["git", "checkout", "--", "app.py"], cwd=repo,
                   capture_output=True, check=True)
    ledger = permissions.Ledger(repo)
    ledger.record("bash", {"command": "git checkout -- app.py"})
    with pytest.raises(permissions.PermissionBreach, match="REVERTED-BY-AGENT"):
        permissions.enforce(repo, agent([]), before, PROTECTED, ledger=ledger)


def test_bash_naming_only_a_parent_directory_is_not_evidence(repo):
    before = permissions.snapshot(repo)
    (repo / ".lime" / "tickets").mkdir(parents=True, exist_ok=True)
    (repo / ".lime" / "tickets" / "53.md").write_text("engineer's note\n")
    ledger = permissions.Ledger(repo)
    ledger.record("bash", {"command": f"ls {repo}/.lime/runs; ls .lime/tickets/"})
    result = permissions.enforce(repo, agent([]), before, PROTECTED, ledger=ledger)
    assert result == []
    assert (repo / ".lime" / "tickets" / "53.md").exists()


def test_ledger_names_paths_in_every_form_the_runtimes_send(repo):
    ledger = permissions.Ledger(repo)
    # Write/Edit/MultiEdit/NotebookEdit with file_path/notebook_path/path
    ledger.record("Write", {"file_path": str(repo / "a" / "b.txt")})
    assert ledger.names("a/b.txt")

    ledger.record("write", {"path": "./c.txt"})
    assert ledger.names("c.txt")

    ledger.record("NotebookEdit", {"notebook_path": "n.ipynb"})
    assert ledger.names("n.ipynb")

    # read tool is not recorded in paths
    ledger.record("read", {"path": "r.txt"})
    assert not ledger.names("r.txt")

    # bash with a heredoc naming the path
    ledger.record("Bash", {"command": "cat > out.txt <<EOF"})
    assert ledger.names("out.txt")

    # bash with sed naming the path
    ledger.record("bash", {"command": f"sed -i '' s/a/b/ {repo}/d.txt"})
    assert ledger.names("d.txt")

    # bash naming a parent file (app.py) but not the child (lime/app.py)
    ledger.record("bash", {"command": "cat lime/app.py"})
    assert not ledger.names("app.py")

    # bash naming .bak extension is not the base file
    ledger.record("bash", {"command": "cp app.py.bak x"})
    assert not ledger.names("app.py")

    # absolute path outside the repo is kept but does not name
    ledger.record("write", {"path": "/elsewhere/z.txt"})
    assert not ledger.names("z.txt")

    # non-dict args do not crash
    ledger.record("bash", "not-a-dict")  # no error
    ledger.record("write", 42)  # no error


def test_bash_cat_myapp_py_does_not_name_app_py(repo):
    """cat myapp.py names 'myapp.py', not 'app.py' — substring match blocked by word boundary."""
    ledger = permissions.Ledger(repo)
    ledger.record("bash", {"command": "cat myapp.py"})
    assert ledger.names("myapp.py")
    assert not ledger.names("app.py")

    ledger.record("bash", {"command": "python myapp.py"})
    assert not ledger.names("app.py")


# --- ticket 132: skill Markdown is outside the documenter allowlist ---

SKILL_MARKDOWN = (
    "lime/skill/lime/SKILL.md",
    ".claude/skills/lime/SKILL.md",
    "lime/skill/lime/cookbooks/run.md",
    ".claude/skills/lime/cookbooks/run.md",
)


def test_documenter_allowlist_excludes_skill_markdown():
    documenter = shipped_documenter()
    assert documenter.writes == ["AGENTS.md", "CONTEXT.md", "docs/adr/"]

    for path in SKILL_MARKDOWN:
        assert not permissions.permitted(path, documenter, PROTECTED), path

    assert permissions.permitted("AGENTS.md", documenter, PROTECTED)
    assert permissions.permitted("CONTEXT.md", documenter, PROTECTED)
    assert permissions.permitted("docs/adr/0019-rule.md", documenter, PROTECTED)
    # lookalikes stay denied
    assert not permissions.permitted("README.md", documenter, PROTECTED)
    assert not permissions.permitted("nested/AGENTS.md", documenter, PROTECTED)

    # an unrestricted builder may edit the skill paths
    builder = AgentConfig(name="builder", model="m", writes=None)
    for path in SKILL_MARKDOWN:
        assert permissions.permitted(path, builder, PROTECTED), path


def _commit_files(repo, contents, message):
    for rel, text in contents.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", message],
        check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("evidence", ["no-ledger", "file-tool", "shell"])
def test_documenter_skill_rollback_preserves_builder_commit(repo, evidence):
    """A documenter edit is rolled back to the committed builder revision — not
    the pre-build original — for file tools, shell commands, or an unledgered worktree."""
    original = {rel: f"original {rel}\n" for rel in SKILL_MARKDOWN}
    _commit_files(repo, original, "original skills")
    builder = {rel: f"builder revision of {rel}\n" for rel in SKILL_MARKDOWN}
    builder_sha = _commit_files(repo, builder, "builder skill work")

    documenter = AgentConfig(name="documenter", model="m",
                             writes=["AGENTS.md", "CONTEXT.md", "docs/adr/"])

    before = permissions.snapshot(repo)
    for rel in SKILL_MARKDOWN:
        (repo / rel).write_text("tampered\n")

    ledger = None
    if evidence != "no-ledger":
        ledger = permissions.Ledger(repo)
        for rel in SKILL_MARKDOWN:
            if evidence == "file-tool":
                ledger.record("Edit", {"file_path": rel})
            else:
                ledger.record("bash", {"command": f"sed -i '' s/builder/tampered/ {rel}"})

    with pytest.raises(permissions.PermissionBreach) as exc:
        permissions.enforce(repo, documenter, before, PROTECTED, ledger=ledger)

    msg = str(exc.value)
    for rel in SKILL_MARKDOWN:
        assert rel in msg, f"breach must name {rel}"
    # exact pre-documenter bytes = the committed builder revision
    for rel in SKILL_MARKDOWN:
        assert (repo / rel).read_text() == builder[rel], rel
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert head == builder_sha
