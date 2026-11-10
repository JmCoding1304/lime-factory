"""Config merge, tool vocabulary and validate-before-spawn (spec §5)."""

import copy
import io
import json
import re
import subprocess

import pytest
import yaml

from lime import cli, config
from pathlib import Path

TEMPLATE_RAW = yaml.safe_load(config.TEMPLATE.read_text())


def cfg(**over):
    """A minimal valid config; the builder role has package-default prompts."""
    base = {
        "defaults": {
            "runtime": "pi",
            "model": "openrouter/deepseek/deepseek-v4-flash",
            "thinking": "medium",
            "tools": ["read", "bash"],
            "harness_engineering": [],
        },
        "agents": [{"name": "builder", "purpose": "build", "prompt_engineering": {}}],
        "profiles": {},
        "commands": {"test": "pytest"},
        "budget": {"warn": 150000, "stop": 250000},
        "runs": {"ports": "3100-3102"},
    }
    base.update(over)
    return base


# --- merge: defaults ← agent ← profile ---


def roster_map(profile=None):
    return {
        a.name: (a.runtime, a.model, a.thinking)
        for a in config.roster(TEMPLATE_RAW, profile)
    }


def test_default_roster_matches_the_spec_table():
    assert roster_map() == {
        "scout": ("claude_code", "claude-haiku-5-5", "medium"),
        "researcher": ("claude_code", "sonnet", "low"),
        "planner": ("claude_code", "claude-opus-5-5[1m]", "xhigh"),
        "builder": ("pi", "openrouter/deepseek/deepseek-v4.1-flash", "high"),
        "reviewer": ("pi", "openai-codex/gpt-6-astra", "high"),
        "documenter": ("claude_code", "claude-haiku-5-5", "high"),
        "designer": ("pi", "openrouter/moonshotai/kimi-k3", "medium"),
    }


def test_quality_profile_raises_defaults_thinking_and_swaps_the_builder():
    r = roster_map("quality")
    assert r["builder"] == ("pi", "openrouter/anthropic/claude-sonnet-5", "high")
    # profile defaults patch the defaults layer only — explicit agent values survive
    assert r["scout"] == ("claude_code", "claude-haiku-5-5", "medium")
    assert r["documenter"] == ("claude_code", "claude-haiku-5-5", "high")
    assert r["reviewer"] == ("pi", "openai-codex/gpt-6-astra", "high")


def test_all_claude_profile_puts_every_role_on_the_subscription():
    assert roster_map("all-claude") == {
        "scout": ("claude_code", "claude-haiku-5-5", "medium"),
        "researcher": ("claude_code", "sonnet", "low"),
        "planner": ("claude_code", "claude-opus-5-5[1m]", "xhigh"),
        "builder": ("claude_code", "sonnet", "high"),
        "reviewer": ("claude_code", "fable", "high"),
        "documenter": ("claude_code", "claude-haiku-5-5", "high"),
        "designer": ("claude_code", "opus", "medium"),
    }


def test_api_research_profile_moves_only_the_researcher():
    r = roster_map("api-research")
    assert r["researcher"] == ("pi", "openrouter/google/gemini-3.7-flash", "low")
    assert {k: v for k, v in r.items() if k != "researcher"} == {
        k: v for k, v in roster_map().items() if k != "researcher"
    }


@pytest.mark.parametrize(
    "role,thinking", [("scout", "medium"), ("documenter", "high")]
)
def test_template_subscription_roles_are_explicit_and_unpatched(role, thinking):
    """Ticket 182: scout/documenter carry explicit runtime/model/thinking in the
    base roster, and no profile re-patches them (no redundant haiku patch, no
    silent fall back to inherited defaults)."""
    base = next(a for a in TEMPLATE_RAW["agents"] if a["name"] == role)
    assert base["runtime"] == "claude_code"
    assert base["model"] == "claude-haiku-5-5"
    assert base["thinking"] == thinking
    for pname in ("quality", "all-claude", "api-research"):
        patches = TEMPLATE_RAW["profiles"][pname].get("agents", [])
        assert role not in {e["name"] for e in patches}, pname


def test_default_profile_needs_no_entry():
    assert config.roster(TEMPLATE_RAW, "default") == config.roster(TEMPLATE_RAW)


def test_unknown_profile_raises():
    with pytest.raises(ValueError, match="nope"):
        config.roster(TEMPLATE_RAW, "nope")


@pytest.mark.parametrize("root", [
    None,  # template fallback config.load(tmp_path)
    Path(__file__).resolve().parent.parent,  # real local config
], ids=["template-fallback", "project"])
def test_builder_default_v41_flash_high(root):
    """Regression: both load paths give defaults.model = v4.1-flash,
    builder = pi/v4.1-flash/high with explicit thinking: high in the raw entry."""
    import tempfile
    if root is None:
        tmp = Path(tempfile.mkdtemp())
        raw = config.load(tmp)
    else:
        raw = config.load(root)
    assert raw["defaults"]["model"] == "openrouter/deepseek/deepseek-v4.1-flash"
    builder_raw = next(a for a in raw["agents"] if a["name"] == "builder")
    assert builder_raw["thinking"] == "high", "raw builder entry must have explicit thinking: high"
    agents = {a.name: a for a in config.roster(raw)}
    b = agents["builder"]
    assert b.runtime == "pi"
    assert b.model == "openrouter/deepseek/deepseek-v4.1-flash"
    assert b.thinking == "high"


def test_identity_keys_never_come_from_a_profile():
    """tools/writes/prompts are fences — only runtime/model/thinking merge in."""
    base = config.roster(TEMPLATE_RAW)
    quality = config.roster(TEMPLATE_RAW, "quality")
    for before, after in zip(base, quality):
        assert before.tools == after.tools
        assert before.writes == after.writes
        assert before.prompt_engineering == after.prompt_engineering


# --- tool vocabulary (one vocabulary — pi's — everywhere) ---


def test_cc_mapping_covers_the_whole_vocabulary():
    tools, extensions, warnings = config.resolve_tools(
        "claude_code",
        ["read", "bash", "edit", "write", "grep", "find", "ls", "subagent", "web"],
    )
    assert tools == ["Read", "Bash", "Edit", "Write", "Grep", "Glob", "Agent", "WebSearch", "WebFetch"]
    assert extensions == []
    assert warnings and "ls" in warnings[0]  # dropped with a warning


def test_pi_words_pass_through_and_the_two_extension_words_split_off():
    tools, extensions, warnings = config.resolve_tools(
        "pi", ["read", "bash", "ls", "subagent", "web"]
    )
    assert tools == ["read", "bash", "ls", "subagent_create", "subagent_continue", "subagent_list", "subagent_remove"]
    assert extensions == [config.PI_EXTENSIONS["subagent"], "pi-web-access"]
    assert warnings == []


def test_unknown_tool_word_raises():
    with pytest.raises(ValueError, match="browse"):
        config.resolve_tools("pi", ["read", "browse"])


def test_only_the_planner_may_spawn_subagents():
    for raw in (TEMPLATE_RAW, yaml.safe_load(Path(".lime/config.yaml").read_text())):
        tools = {a.name: a.tools for a in config.roster(raw)}
        assert "subagent" not in tools["scout"]
        assert "subagent" in tools["planner"]


def test_the_reviewer_cannot_run_shell():
    """Ticket 68: the review envelope inlines the diff, changed files and verify result —
    `bash` only ever let the reviewer re-run tests and re-fetch the diff."""
    for raw in (TEMPLATE_RAW, yaml.safe_load(Path(".lime/config.yaml").read_text())):
        tools = {a.name: a.tools for a in config.roster(raw)}
        assert "bash" not in tools["reviewer"]
        assert {"read", "grep", "find", "ls", "write"} <= set(tools["reviewer"])


def test_pi_subagent_extension_is_a_packaged_file_that_exists():
    _, extensions, _ = config.resolve_tools("pi", ["subagent"])
    path = Path(extensions[0])
    assert path.is_absolute()
    assert path.exists()
    assert path.name == "subagents.ts"
    assert path.parent.name == "harness_engineering"


def test_pi_subagent_word_enables_the_four_extension_tools():
    tools, _, _ = config.resolve_tools("pi", ["read", "subagent"])
    assert tools == ["read", "subagent_create", "subagent_continue", "subagent_list", "subagent_remove"]


# --- §5 validation failures, one test per listed check ---


def test_valid_config_validates_clean():
    assert config.validate(cfg()) == []


def test_chain_agent_missing_from_roster_fails():
    failures = config.validate(cfg(), roles=["planner"])
    assert any("planner" in f for f in failures)


def test_unknown_runtime_fails():
    c = cfg()
    c["agents"][0]["runtime"] = "docker"
    assert any("docker" in f for f in config.validate(c))


def test_pi_model_without_provider_fails():
    c = cfg()
    c["agents"][0]["model"] = "deepseek-v4-flash"
    assert any("provider" in f for f in config.validate(c))


def test_anthropic_on_pi_fails():
    c = cfg()
    c["agents"][0]["model"] = "anthropic/claude-sonnet-5"
    assert any("openrouter/anthropic" in f for f in config.validate(c))


def test_cc_model_must_be_alias_or_claude_id():
    c = cfg()
    c["agents"][0] |= {"runtime": "claude_code", "model": "gpt-6"}
    assert any("gpt-6" in f for f in config.validate(c))
    c["agents"][0]["model"] = "claude-fable-5"
    assert config.validate(c) == []


def test_missing_model_fails():
    c = cfg()
    del c["defaults"]["model"]
    assert any("model" in f for f in config.validate(c))


def test_unknown_thinking_fails():
    c = cfg()
    c["agents"][0]["thinking"] = "ultra"
    assert any("ultra" in f for f in config.validate(c))


def test_unknown_tool_word_fails_validation():
    c = cfg()
    c["agents"][0]["tools"] = ["read", "browse"]
    assert any("browse" in f for f in config.validate(c))


def test_missing_prompt_files_fail():
    c = cfg()
    c["agents"][0]["name"] = "ghostrole"  # no package default, nothing configured
    failures = config.validate(c)
    assert any("prompt" in f for f in failures)


def test_configured_prompt_override_is_found(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "system.md").write_text("x")
    (tmp_path / "prompts" / "user.md").write_text("x")
    c = cfg()
    c["agents"][0] |= {
        "name": "ghostrole",
        "prompt_engineering": {"system": "prompts/system.md", "user": "prompts/user.md"},
    }
    assert config.validate(c, root=tmp_path) == []


# --- ticket 158: explicit prompt overrides and the AGENTS.md ceiling ---


@pytest.mark.parametrize("kind", ["system", "user"])
def test_missing_configured_override_fails_naming_the_path_despite_a_package_default(tmp_path, kind):
    """builder ships package defaults, yet a configured path that is missing fails and
    names the exact selected path — never the default."""
    c = cfg()
    c["agents"][0]["prompt_engineering"] = {kind: f"prompts/{kind}.md"}
    failures = config.validate(c, root=tmp_path)
    assert any(str(tmp_path / "prompts" / f"{kind}.md") in f for f in failures), failures
    assert any("builder" in f and kind in f for f in failures), failures


def test_configured_valid_override_passes_validation(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "system.md").write_text("x")
    (tmp_path / "prompts" / "user.md").write_text("x")
    c = cfg()
    c["agents"][0]["prompt_engineering"] = {
        "system": "prompts/system.md",
        "user": "prompts/user.md",
    }
    assert config.validate(c, root=tmp_path) == []


def test_unconfigured_kind_uses_the_package_default_and_passes(tmp_path):
    c = cfg()
    c["agents"][0]["prompt_engineering"] = {}
    assert config.validate(c, root=tmp_path) == []


def test_no_agents_md_passes_the_length_check(tmp_path):
    assert not (tmp_path / "AGENTS.md").exists()
    assert config.validate(cfg(), root=tmp_path) == []


@pytest.mark.parametrize("newline", [True, False], ids=["final-newline", "no-final-newline"])
def test_agents_md_sixty_passes_sixty_one_fails(tmp_path, newline):
    body = "\n".join(f"line {i}" for i in range(60))
    suffix = "\n" if newline else ""
    (tmp_path / "AGENTS.md").write_text(body + suffix)
    assert config.validate(cfg(), root=tmp_path) == []
    (tmp_path / "AGENTS.md").write_text(body + "\nline 60" + suffix)
    failures = config.validate(cfg(), root=tmp_path)
    assert any("AGENTS.md" in f and "60" in f for f in failures), failures


def test_blank_lines_count_toward_the_agents_limit(tmp_path):
    (tmp_path / "AGENTS.md").write_text("\n" * 61)
    assert any("AGENTS.md" in f for f in config.validate(cfg(), root=tmp_path))


def test_long_claude_md_alone_does_not_trigger_the_agents_limit(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("\n".join(f"line {i}" for i in range(200)) + "\n")
    assert not (tmp_path / "AGENTS.md").exists()
    assert config.validate(cfg(), root=tmp_path) == []


def test_agents_limit_uses_the_passed_root_not_cwd(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("\n" * 61)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert any("AGENTS.md" in f for f in config.validate(cfg(), root=tmp_path))


def test_template_has_no_prompt_engineering_keys():
    assert "prompt_engineering" not in config.TEMPLATE.read_text()
    assert all("prompt_engineering" not in entry for entry in TEMPLATE_RAW["agents"])


def test_missing_extension_path_fails_validation(tmp_path):
    c = cfg()
    c["agents"][0]["harness_engineering"] = ["ext/missing.ts"]
    failures = config.validate(c, root=tmp_path)
    assert len(failures) == 1
    assert "ext/missing.ts" in failures[0]
    assert failures[0].startswith("builder:")


def test_missing_subagent_extension_fails_validation(tmp_path, monkeypatch):
    monkeypatch.setitem(config.PI_EXTENSIONS, "subagent", str(tmp_path / "gone" / "subagents.ts"))
    c = cfg()
    c["agents"][0]["tools"] = ["read", "subagent"]
    failures = config.validate(c, root=tmp_path)
    assert any("subagents.ts" in f for f in failures)
    assert any("subagent" in f for f in failures)


def test_existing_extension_paths_pass_validation(tmp_path):
    (tmp_path / "ext").mkdir(parents=True)
    (tmp_path / "ext" / "ok.ts").write_text("")
    c = cfg()
    c["agents"][0]["harness_engineering"] = ["ext/ok.ts", str(tmp_path / "ext" / "ok.ts")]
    assert config.validate(c, root=tmp_path) == []


@pytest.mark.parametrize("entry", [*config.PI_EXTENSIONS.values(), "ext/ok.ts"])
def test_known_extensions_and_existing_paths_pass_validation(tmp_path, entry):
    (tmp_path / "ext").mkdir(parents=True)
    (tmp_path / "ext" / "ok.ts").write_text("")
    c = cfg()
    c["agents"][0]["harness_engineering"] = [entry]
    assert config.validate(c, root=tmp_path) == []


@pytest.mark.parametrize("entry", [
    "npm:x", "git:x", "https://x", "evil-pkg", "ext/missing.ts", "~nosuchuser-lime/x.ts",
    "lime/config.py", "lime/harness_engineering/../config.py", "lime/harness_engineering/missing.ts",
])
def test_unknown_extension_entries_fail_validation(tmp_path, entry):
    c = cfg()
    c["agents"][0]["harness_engineering"] = [entry]
    failures = config.validate(c, root=tmp_path)
    assert len(failures) == 1
    assert failures[0].startswith("builder:")
    assert repr(entry) in failures[0]


def test_a_project_root_extension_resolves_to_the_entry_unchanged(tmp_path):
    # the factory repo itself: its own lime/harness_engineering/ shadows the package's
    (tmp_path / "lime" / "harness_engineering").mkdir(parents=True)
    (tmp_path / "lime" / "harness_engineering" / "context7.ts").write_text("")
    (tmp_path / "ext").mkdir()
    (tmp_path / "ext" / "ok.ts").write_text("")
    for entry in ["lime/harness_engineering/context7.ts", "ext/ok.ts", str(tmp_path / "ext" / "ok.ts")]:
        assert config.resolve_extension(entry, tmp_path) == entry


def test_a_package_extension_resolves_from_a_project_that_lacks_it(tmp_path):
    resolved = config.resolve_extension("lime/harness_engineering/context7.ts", tmp_path)
    assert resolved == str(config.HARNESS_DIR / "context7.ts")
    assert Path(resolved).is_absolute() and Path(resolved).is_file()


@pytest.mark.parametrize("entry", [
    "lime/config.py", "lime/harness_engineering/../config.py", "lime/templates/config.yaml",
    "lime/harness_engineering", "lime/harness_engineering/missing.ts", "ext/missing.ts",
    "npm:x", "git:x", "https://x", "npm:lime/harness_engineering/context7.ts",
])
def test_the_package_fallback_never_reaches_past_the_extension_dir(tmp_path, entry):
    assert config.resolve_extension(entry, tmp_path) is None


def test_a_refused_package_path_names_both_places_it_looked(tmp_path):
    c = cfg()
    c["agents"][0]["harness_engineering"] = ["lime/harness_engineering/missing.ts"]
    [failure] = config.validate(c, root=tmp_path)
    assert "is neither a known extension nor a local path that exists" in failure
    assert str(tmp_path.resolve() / "lime" / "harness_engineering" / "missing.ts") in failure
    assert str(config.HARNESS_DIR / "missing.ts") in failure


def test_a_remote_source_fails_even_when_a_same_named_file_exists(tmp_path):
    (tmp_path / "npm:x").write_text("")
    c = cfg()
    c["agents"][0]["harness_engineering"] = ["npm:x"]
    failures = config.validate(c, root=tmp_path)
    assert len(failures) == 1
    assert "'npm:x'" in failures[0]


def test_web_tool_word_resolves_to_the_allowlisted_package():
    c = cfg()
    c["agents"][0]["tools"] = ["read", "web"]
    assert config.validate(c) == []
    _, extensions, _ = config.resolve_tools("pi", ["web"])
    assert extensions == ["pi-web-access"]


def test_extension_paths_are_checked_for_pi_only(tmp_path):
    c = cfg()
    c["agents"][0] |= {"runtime": "claude_code", "model": "sonnet", "harness_engineering": ["ext/missing.ts"]}
    assert config.validate(c, root=tmp_path) == []


def test_unknown_profile_fails_validation():
    assert any("nope" in f for f in config.validate(cfg(), profile="nope"))


def test_profile_patching_more_than_runtime_model_thinking_fails():
    c = cfg(profiles={"p": {"agents": [{"name": "builder", "tools": ["read"]}]}})
    assert any("runtime/model/thinking/timeout_s" in f for f in config.validate(c, profile="p"))
    c = cfg(profiles={"p": {"defaults": {"writes": []}}})
    assert any("runtime/model/thinking/timeout_s" in f for f in config.validate(c, profile="p"))


def test_profile_patching_an_unknown_agent_fails():
    c = cfg(profiles={"p": {"agents": [{"name": "ghost", "model": "sonnet"}]}})
    assert any("ghost" in f for f in config.validate(c, profile="p"))


def test_missing_commands_test_fails_when_the_chain_tests():
    c = cfg(commands={})
    assert any("commands.test" in f for f in config.validate(c))
    assert config.validate(c, chain_tests=False) == []


def test_budget_warn_must_stay_below_stop():
    c = cfg(budget={"warn": 250000, "stop": 250000})
    assert any("budget" in f for f in config.validate(c))


def test_template_config_validates_clean():
    assert config.validate(TEMPLATE_RAW) == []
    for profile in TEMPLATE_RAW["profiles"]:
        assert config.validate(TEMPLATE_RAW, profile=profile) == []


# --- runs.ports: the parallel-Run cap (spec §7) ---


def test_port_range_parses_the_inclusive_range():
    assert config.port_range({"runs": {"ports": "3100-3102"}}) == [3100, 3101, 3102]


def test_port_range_accepts_a_single_port():
    assert config.port_range({"runs": {"ports": "3100"}}) == [3100]


def test_port_range_empty_when_unset():
    assert config.port_range({}) == []
    assert config.port_range({"runs": {}}) == []


def test_template_port_range_matches_the_annotated_cap():
    assert config.port_range(TEMPLATE_RAW) == [3100, 3101, 3102]


# --- auto_deploy: opt-in for --deploy batches (ticket 199) ---


def test_auto_deploy_missing_means_off():
    assert config.auto_deploy({}) == "off"
    assert config.auto_deploy(None) == "off"
    assert config.auto_deploy({"auto_deploy": None}) == "off"


def test_auto_deploy_reads_yaml_off_as_off():
    assert config.auto_deploy(yaml.safe_load("auto_deploy: off")) == "off"
    assert config.auto_deploy(yaml.safe_load("auto_deploy: 'off'")) == "off"


def test_auto_deploy_returns_staging_and_prod():
    assert config.auto_deploy({"auto_deploy": "staging"}) == "staging"
    assert config.auto_deploy({"auto_deploy": "prod"}) == "prod"


def test_auto_deploy_rejects_anything_else():
    for value in (True, "Prod", "yes", 1):
        assert config.auto_deploy({"auto_deploy": value}) is None, value


def test_validate_rejects_an_invalid_auto_deploy():
    assert any("auto_deploy" in f for f in config.validate(cfg(auto_deploy=True)))
    assert not any("auto_deploy" in f for f in config.validate(cfg(auto_deploy="staging")))
    assert not any("auto_deploy" in f for f in config.validate(cfg(auto_deploy=False)))
    assert not any("auto_deploy" in f for f in config.validate(cfg()))


# --- runs.stall_minutes: a retired key is ignored (ticket 191) ---


@pytest.mark.parametrize("leftover", [10, 0, "ten"])
def test_a_leftover_runs_stall_minutes_still_loads(leftover):
    assert config.validate(cfg(runs={"ports": "3100-3102", "stall_minutes": leftover})) == []


def test_nothing_reads_runs_stall_minutes_any_more():
    assert "stall_minutes" not in TEMPLATE_RAW["runs"]
    assert not [p for p in Path(config.__file__).parent.rglob("*.py") if "stall_minutes" in p.read_text()]


# --- timeout_s: the per-role time ceiling (ticket 82) ---


def test_timeout_s_defaults_to_1800_and_reads_the_role_entry():
    for a in config.roster(TEMPLATE_RAW):
        assert a.timeout_s == 1800
    c = cfg()
    c["agents"][0]["timeout_s"] = 900
    assert config.validate(c) == []
    assert config.roster(c)[0].timeout_s == 900


def test_timeout_s_is_profile_patchable():
    c = cfg(profiles={"p": {"defaults": {"timeout_s": 900}}})
    assert config.validate(c, profile="p") == []
    assert config.roster(c, "p")[0].timeout_s == 900
    c = cfg(profiles={"p": {"agents": [{"name": "builder", "timeout_s": 2400}]}})
    assert config.validate(c, profile="p") == []
    assert config.roster(c, "p")[0].timeout_s == 2400


@pytest.mark.parametrize("bad", [0, -5, "soon", True, 1.5])
def test_timeout_s_must_be_a_positive_whole_number(bad):
    c = cfg()
    c["agents"][0]["timeout_s"] = bad
    failures = config.validate(c)
    assert len([f for f in failures if "timeout_s" in f]) == 1


def test_template_documents_timeout_s():
    assert TEMPLATE_RAW["defaults"]["timeout_s"] == 1800


# --- commands.test_timeout (ticket 272) ---


def test_commands_test_timeout_is_optional_and_accepts_whole_seconds():
    from lime import quality as _quality

    assert config.validate(cfg()) == []
    assert config.validate(cfg(commands={"test": "pytest", "test_timeout": 1500})) == []
    assert _quality.test_timeout_failure({"test_timeout": 5000}) is None
    assert config.validate(cfg(commands={"test": "pytest", "test_timeout": 5000})) == []


@pytest.mark.parametrize("bad", [0, -5, "soon", True, 1.5, None])
def test_commands_test_timeout_must_be_a_positive_whole_number(bad):
    c = cfg(commands={"test": "pytest", "test_timeout": bad})
    failures = config.validate(c)
    assert len([f for f in failures if "commands.test_timeout" in f]) == 1
    failures = config.validate(c, chain_tests=False)
    assert len([f for f in failures if "commands.test_timeout" in f]) == 1


def test_template_documents_commands_test_timeout_commented_out():
    assert "# test_timeout: 600" in config.TEMPLATE.read_text()
    assert "test_timeout" not in TEMPLATE_RAW["commands"]


def test_repo_config_sets_test_timeout_1500():
    from lime import quality as _quality

    commands = yaml.safe_load(Path(".lime/config.yaml").read_text())["commands"]
    assert commands["test_timeout"] == 1500
    assert _quality.test_timeout_failure(commands) is None


# --- credentials: presence only, never read or stored ---


def fake_run(status="ready", rc=0):
    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, rc, stdout=json.dumps({"status": status}), stderr="")

    return _run


def test_pi_credentials_checked_per_provider_in_the_resolved_roster(monkeypatch):
    seen = []

    def _run(cmd, **kwargs):
        seen.append(cmd[cmd.index("--provider") + 1])
        return subprocess.CompletedProcess(cmd, 0, stdout='{"status":"ready"}', stderr="")

    monkeypatch.setattr(config.subprocess, "run", _run)
    monkeypatch.setattr(config, "claude_credentials_present", lambda: True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    failures, warnings = config.credential_failures(config.roster(TEMPLATE_RAW))
    assert failures == [] and warnings == []
    assert seen == ["openai-codex", "openrouter"]  # sorted, deduplicated, pi runtimes only


def test_pi_credentials_missing_fails(monkeypatch):
    monkeypatch.setattr(config.subprocess, "run", fake_run(status="missing", rc=1))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    failures, _ = config.credential_failures(config.roster(TEMPLATE_RAW))
    assert any("openrouter" in f for f in failures)


def test_claude_code_credentials_missing_fails(monkeypatch):
    monkeypatch.setattr(config.subprocess, "run", fake_run())
    monkeypatch.setattr(config, "claude_credentials_present", lambda: False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    failures, _ = config.credential_failures(config.roster(TEMPLATE_RAW))
    assert any("Claude Code" in f for f in failures)


def test_anthropic_api_key_in_env_only_warns(monkeypatch):
    monkeypatch.setattr(config.subprocess, "run", fake_run())
    monkeypatch.setattr(config, "claude_credentials_present", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    failures, warnings = config.credential_failures(config.roster(TEMPLATE_RAW))
    assert failures == []
    assert any("subscription" in w for w in warnings)


# --- pi compaction off ---


def test_pi_compaction_enabled_false_passes(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text('{"compaction": {"enabled": false}}')
    monkeypatch.setattr(config, "PI_SETTINGS", settings)
    assert config.compaction_failures() == []


@pytest.mark.parametrize("body", ["{}", '{"compaction": {"enabled": true}}', "not json"])
def test_pi_compaction_not_off_fails(tmp_path, monkeypatch, body):
    settings = tmp_path / "settings.json"
    settings.write_text(body)
    monkeypatch.setattr(config, "PI_SETTINGS", settings)
    assert any("compaction" in f for f in config.compaction_failures())


@pytest.mark.parametrize("body", ["[]", '"x"', "3", '{"compaction": true}', '{"compaction": "off"}'])
def test_pi_settings_of_an_odd_shape_fail_in_plain_words(tmp_path, monkeypatch, body):
    settings = tmp_path / "settings.json"
    settings.write_text(body)
    monkeypatch.setattr(config, "PI_SETTINGS", settings)
    failures = config.compaction_failures()
    assert len(failures) == 1
    assert str(settings) in failures[0] and "not a JSON object" in failures[0]
    assert '{"enabled": false}' in failures[0]


# --- capability preflight (ticket 84): the tuple against a fixture models-store ---


def entry(model_id, *, reasoning=True, levels=None, image=True, window=1_000_000):
    e = {"id": model_id, "reasoning": reasoning, "input": ["text", "image"] if image else ["text"]}
    if window is not None:
        e["contextWindow"] = window
    if levels is not None:
        e["thinkingLevelMap"] = levels
    return e


def write_store(directory, providers: dict[str, list[dict]], name="models-store.json"):
    body = {p: {"models": ms} for p, ms in providers.items()}
    if name == "models.json":
        body = {"providers": body}
    (directory / name).write_text(json.dumps(body))


DEEPSEEK_MAP = {"off": "none", "minimal": None, "low": None, "medium": None, "high": "high", "xhigh": "xhigh", "max": None}
GLM_MAP = {"off": None, "minimal": None, "low": "low", "medium": None, "high": "high", "xhigh": None, "max": "max"}


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """Every pi model the template and this repo's config name (all profiles), every level on, images in."""
    providers: dict[str, dict[str, dict]] = {}
    for raw in (TEMPLATE_RAW, config.load()):
        for profile in [None, *(raw.get("profiles") or {})]:
            for a in config.roster(raw, profile):
                if a.runtime == "pi":
                    provider, _, mid = a.model.partition("/")
                    providers.setdefault(provider, {})[mid] = entry(mid, levels={level: level for level in config.THINKING})
    write_store(tmp_path, {p: list(m.values()) for p, m in providers.items()})
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    return tmp_path


def test_pi_catalog_merges_the_store_and_the_providers_shaped_models_json(tmp_path):
    write_store(tmp_path, {"openrouter": [entry("a")]})
    write_store(tmp_path, {"custom": [entry("b")]}, name="models.json")
    config.PI_AGENT_DIR = tmp_path
    assert config.pi_model_entry("custom/b") is not None
    assert config.pi_model_entry("openrouter/a") is not None


def test_pi_catalog_is_none_without_either_file(tmp_path):
    config.PI_AGENT_DIR = tmp_path
    assert config.pi_catalog() is None


def test_pi_model_entry_unknown_is_none(tmp_path):
    write_store(tmp_path, {"openrouter": [entry("a")]})
    config.PI_AGENT_DIR = tmp_path
    assert config.pi_model_entry("openrouter/nonexistent") is None


def test_supported_thinking_follows_pi(tmp_path):
    # non-reasoning -> off only
    assert config.supported_thinking(entry("m", reasoning=False)) == ("off",)
    # DEEPSEEK_MAP -> off, high, xhigh
    assert config.supported_thinking(entry("m", levels=DEEPSEEK_MAP)) == ("off", "high", "xhigh")
    # no map: xhigh/max excluded
    assert config.supported_thinking(entry("m")) == ("off", "minimal", "low", "medium", "high")
    # explicit non-null for xhigh and max
    assert config.supported_thinking(entry("m", levels={"off": None, "xhigh": "xhigh", "max": "max"})) == (
        "minimal", "low", "medium", "high", "xhigh", "max",
    )


def test_thinking_support_lists_every_catalog_model_in_pi_terms(tmp_path, monkeypatch):
    write_store(tmp_path, {"openrouter": [
        entry("deepseek/deepseek-v4-flash", levels=DEEPSEEK_MAP),
        entry("z-ai/glm-5.3-flash", levels=GLM_MAP),
        entry("qwen/qwen3-coder-flash", reasoning=False),
    ]})
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    assert config.thinking_support() == {
        "openrouter/deepseek/deepseek-v4-flash": ["off", "high", "xhigh"],
        "openrouter/z-ai/glm-5.3-flash": ["low", "high", "max"],
        "openrouter/qwen/qwen3-coder-flash": ["off"],
    }


def test_catalog_models_covers_every_provider_pi_knows(tmp_path, monkeypatch):
    codex = {"id": "gpt-5.5", "name": "GPT-5.5", "cost": {"input": 5, "output": 30}, "contextWindow": 272000}
    write_store(tmp_path, {
        "openai-codex": [codex],
        "openrouter": [{"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash"}],
    })
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    rows = {r["id"]: r for r in config.catalog_models()}
    # the subscription provider is in the tray, not only the OpenRouter ids (ticket 92)
    assert rows["openai-codex/gpt-5.5"] == {
        "id": "openai-codex/gpt-5.5", "name": "GPT-5.5", "provider": "openai-codex",
        "prompt_price": 5.0, "completion_price": 30.0, "context_length": 272000,
    }
    # an OpenRouter id keeps its family as the label, and a priceless row is null, not 0
    assert rows["openrouter/deepseek/deepseek-v4-flash"] == {
        "id": "openrouter/deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash",
        "provider": "deepseek", "prompt_price": None, "completion_price": None,
        "context_length": None,
    }


def test_catalog_models_is_empty_without_a_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    assert config.catalog_models() == []


def test_cli_models_prints_the_pi_catalog(tmp_path, monkeypatch, capsys):
    write_store(tmp_path, {"openai-codex": [{"id": "gpt-5.5", "contextWindow": 272000}]})
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    assert cli.main(["models"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "pi"
    assert [m["id"] for m in payload["models"]] == ["openai-codex/gpt-5.5"]
    assert payload["models"][0]["name"] == "gpt-5.5"  # no name in the store -> the id


def test_thinking_support_is_empty_without_a_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    assert config.thinking_support() == {}


def test_cli_profiles_get_carries_the_thinking_support_table(project, monkeypatch, capsys):
    pi_dir = project / "pi"
    pi_dir.mkdir()
    write_store(pi_dir, {"openrouter": [entry("deepseek/deepseek-v4-flash", levels=DEEPSEEK_MAP)]})
    monkeypatch.setattr(config, "PI_AGENT_DIR", pi_dir)
    monkeypatch.chdir(project)
    assert cli.main(["profiles", "get"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["thinking_support"] == {
        "openrouter/deepseek/deepseek-v4-flash": ["off", "high", "xhigh"],
    }


def test_capability_typod_model_id_fails_and_suggests_the_close_match(tmp_path, monkeypatch):
    write_store(tmp_path, {"openrouter": [entry("deepseek/deepseek-v4-flash")]})
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    agents = [
        a for a in config.roster(TEMPLATE_RAW)
        if a.name == "builder"
    ]
    # typo the model id
    agents[0] = config.AgentConfig(
        name="builder", runtime="pi", model="openrouter/deepseek/deepseek-v4-flsh",
        thinking="high", tools=["read"], writes=[], prompt_engineering={}, budget={},
    )
    failures, _ = config.capability_failures(agents)
    assert len(failures) == 1
    assert "builder" in failures[0]
    assert "deepseek-v4-flsh" in failures[0]
    assert "did you mean openrouter/deepseek/deepseek-v4-flash" in failures[0]


def test_capability_unknown_model_without_a_close_match_still_fails(tmp_path, monkeypatch):
    write_store(tmp_path, {"openrouter": [entry("a")]})
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    agents = [
        config.AgentConfig(
            name="builder", runtime="pi", model="unknown/zzzzzzzz",
            thinking="high", tools=["read"], writes=[], prompt_engineering={}, budget={},
        )
    ]
    failures, _ = config.capability_failures(agents)
    assert len(failures) == 1
    assert "404" in failures[0]


def test_capability_unsupported_thinking_fails_naming_the_supported_levels(tmp_path, monkeypatch):
    write_store(tmp_path, {"openrouter": [entry("deepseek/deepseek-v4-flash", levels=DEEPSEEK_MAP)]})
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    agents = [
        config.AgentConfig(
            name="builder", runtime="pi", model="openrouter/deepseek/deepseek-v4-flash",
            thinking="medium", tools=["read"], writes=[], prompt_engineering={}, budget={},
        )
    ]
    failures, _ = config.capability_failures(agents)
    assert len(failures) == 1
    assert failures[0] == (
        "builder: thinking 'medium' is not supported by openrouter/deepseek/deepseek-v4-flash"
        " \u2014 supported: ('off', 'high', 'xhigh')"
    )


def test_capability_non_reasoning_model_supports_off_only(tmp_path, monkeypatch):
    write_store(tmp_path, {"openrouter": [entry("m", reasoning=False)]})
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    agents = [
        config.AgentConfig(
            name="builder", runtime="pi", model="openrouter/m",
            thinking="low", tools=["read"], writes=[], prompt_engineering={}, budget={},
        )
    ]
    failures, _ = config.capability_failures(agents)
    assert len(failures) == 1
    assert "('off',)" in failures[0]
    # thinking off should pass
    agents[0] = config.AgentConfig(
        name="builder", runtime="pi", model="openrouter/m",
        thinking="off", tools=["read"], writes=[], prompt_engineering={}, budget={},
    )
    failures, _ = config.capability_failures(agents)
    assert failures == []


def test_capability_missing_context_window_warns_with_the_backstop(tmp_path, monkeypatch):
    write_store(tmp_path, {"openrouter": [entry("m", window=None)]})
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    agents = [
        config.AgentConfig(
            name="builder", runtime="pi", model="openrouter/m",
            thinking="high", tools=["read"], writes=[], prompt_engineering={}, budget={},
        )
    ]
    failures, warnings = config.capability_failures(agents)
    assert failures == []
    assert len(warnings) == 1
    assert "270,000" in warnings[0]


def test_capability_missing_catalog_warns_not_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    agents = [
        config.AgentConfig(
            name="builder", runtime="pi", model="openrouter/m",
            thinking="high", tools=["read"], writes=[], prompt_engineering={}, budget={},
        )
    ]
    failures, warnings = config.capability_failures(agents)
    assert failures == []
    assert len(warnings) == 1
    assert "catalog missing" in warnings[0]


def test_capability_skips_claude_code_roles(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    agents = config.roster(TEMPLATE_RAW, "all-claude")
    failures, warnings = config.capability_failures(agents)
    assert failures == []
    assert warnings == []


def test_preflight_warnings_are_the_capability_warnings(tmp_path, monkeypatch):
    write_store(tmp_path, {"xai": [entry("grok-mini", image=False, window=None)]})
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    raw = {
        "defaults": {"runtime": "pi", "model": "xai/grok-mini", "thinking": "high"},
        "agents": [{"name": "reviewer", "purpose": "review"}],
        "profiles": {},
        "commands": {"test": "pytest"},
        "budget": {"warn": 150000, "stop": 250000},
    }
    warnings = config.preflight_warnings(raw, ["reviewer"])
    assert len(warnings) == 1
    assert "contextWindow" in warnings[0] or "270,000" in warnings[0]
    assert not hasattr(config, "screenshot_warnings")


def test_validate_before_spawn_refuses_an_unsupported_thinking_level(tmp_path, monkeypatch):
    monkeypatch.setattr(config.subprocess, "run", fake_run())
    monkeypatch.setattr(config, "claude_credentials_present", lambda: True)
    settings = tmp_path / "settings.json"
    settings.write_text('{"compaction": {"enabled": false}}')
    monkeypatch.setattr(config, "PI_SETTINGS", settings)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    write_store(tmp_path, {"openrouter": [entry("deepseek/deepseek-v4-flash", levels=DEEPSEEK_MAP)]})
    monkeypatch.setattr(config, "PI_AGENT_DIR", tmp_path)
    c = cfg()
    c["agents"][0]["thinking"] = "medium"
    failures = config.validate_before_spawn(c, ["builder"])
    assert any("supported:" in f for f in failures)


# --- validate_before_spawn: any failure = no Run ---


def test_validate_before_spawn_green(catalog, monkeypatch, tmp_path):
    monkeypatch.setattr(config.subprocess, "run", fake_run())
    monkeypatch.setattr(config, "claude_credentials_present", lambda: True)
    settings = tmp_path / "settings.json"
    settings.write_text('{"compaction": {"enabled": false}}')
    monkeypatch.setattr(config, "PI_SETTINGS", settings)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert config.validate_before_spawn(TEMPLATE_RAW, ["scout", "planner", "builder"]) == []
    assert config.validate_before_spawn(config.load(), ["scout", "planner", "builder"]) == []


def test_compaction_asserted_even_on_an_all_subscription_roster(catalog, monkeypatch, tmp_path):
    """§1: no auto-compaction on either runtime, ever — not gated on pi being in the roster."""
    monkeypatch.setattr(config.subprocess, "run", fake_run())
    monkeypatch.setattr(config, "claude_credentials_present", lambda: True)
    monkeypatch.setattr(config, "PI_SETTINGS", tmp_path / "absent.json")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    failures = config.validate_before_spawn(TEMPLATE_RAW, ["builder"], profile="all-claude")
    assert any("compaction" in f for f in failures)


def test_validate_before_spawn_collects_env_failures(catalog, monkeypatch, tmp_path):
    monkeypatch.setattr(config.subprocess, "run", fake_run(status="missing"))
    monkeypatch.setattr(config, "claude_credentials_present", lambda: False)
    monkeypatch.setattr(config, "PI_SETTINGS", tmp_path / "absent.json")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    failures = config.validate_before_spawn(TEMPLATE_RAW, ["planner", "builder"])
    assert any("openrouter" in f for f in failures)
    assert any("Claude Code" in f for f in failures)
    assert any("compaction" in f for f in failures)


def test_validate_before_spawn_fails_fast_on_a_missing_extension(catalog, monkeypatch, tmp_path):
    monkeypatch.setattr(config.subprocess, "run", fake_run())
    monkeypatch.setattr(config, "claude_credentials_present", lambda: True)
    settings = tmp_path / "settings.json"
    settings.write_text('{"compaction": {"enabled": false}}')
    monkeypatch.setattr(config, "PI_SETTINGS", settings)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = cfg()
    c["agents"][0]["harness_engineering"] = ["ext/missing.ts"]
    failures = config.validate_before_spawn(c, ["builder"], root=tmp_path)
    assert any("ext/missing.ts" in f for f in failures), "extension failure expected"
    # cfg() uses the old model id which is no longer in the catalog fixture
    # (fixture-only old-id cfg() preserved; catalog tracks the changed configs)


# --- write path: canonicalize, validate, span-preserving rewrite (ticket 87) ---


@pytest.fixture
def project(tmp_path):
    """A tmp project whose .lime/config.yaml is the annotated template."""
    (tmp_path / ".lime").mkdir()
    (tmp_path / ".lime" / "config.yaml").write_text(config.TEMPLATE.read_text())
    return tmp_path


def test_api_research_validates_in_a_stamped_project_that_is_not_the_factory(project):
    # the researcher's lime/harness_engineering/context7.ts is not in this project: it resolves from the package
    assert not (project / "lime").exists()
    assert config.validate(config.load(project), profile="api-research", root=project) == []


def template_with_builder_model(model):
    agents = copy.deepcopy(TEMPLATE_RAW["agents"])
    next(a for a in agents if a["name"] == "builder")["model"] = model
    return agents


def test_write_profiles_round_trips_the_template(project):
    agents, profiles = TEMPLATE_RAW["agents"], TEMPLATE_RAW["profiles"]
    assert config.write_profiles(project, agents, profiles) == (True, [])
    raw = config.load(project)
    assert raw["agents"] == agents
    assert raw["profiles"] == profiles
    assert config.validate(raw, root=project) == []
    for pname in ("default", *profiles):
        arg = None if pname == "default" else pname
        assert config.merged_agents(raw, arg) == config.merged_agents(TEMPLATE_RAW, arg)
    # a second identical write is a canonical no-op: the file is not touched at all
    mtime = (project / ".lime" / "config.yaml").stat().st_mtime_ns
    assert config.write_profiles(project, agents, profiles) == (True, [])
    assert (project / ".lime" / "config.yaml").stat().st_mtime_ns == mtime


def test_invalid_write_returns_validate_messages_and_writes_nothing(project):
    path = project / ".lime" / "config.yaml"
    before = path.read_bytes()
    agents = copy.deepcopy(TEMPLATE_RAW["agents"])
    next(a for a in agents if a["name"] == "builder")["runtime"] = "docker"
    profiles = {"bad": {"agents": [{"name": "builder", "tools": ["read"]}]}}
    ok, errors = config.write_profiles(project, agents, profiles)
    assert ok is False
    assert errors  # sanity: the bad runtime and the tools patch are both caught
    assert errors == config.validate(
        {**TEMPLATE_RAW, "agents": agents, "profiles": profiles}, root=project
    )
    assert path.read_bytes() == before


def test_write_preserves_every_line_outside_the_two_spans(project):
    path = project / ".lime" / "config.yaml"
    before = path.read_text().splitlines()
    agents = template_with_builder_model("openrouter/anthropic/claude-sonnet-5")
    assert config.write_profiles(project, agents, TEMPLATE_RAW["profiles"]) == (True, [])
    after = path.read_text().splitlines()

    def outside(lines):
        top, keep, in_span = re.compile(r"^[A-Za-z0-9_-]+:"), [], False
        for line in lines:
            if top.match(line):
                in_span = line.split(":", 1)[0] in ("agents", "profiles")
            if not in_span:
                keep.append(line)
        return keep

    assert outside(after) == outside(before)  # comments, commands, budget, runs, deploy


def test_profile_patch_stores_only_the_diff_and_a_second_write_is_a_noop(project):
    path = project / ".lime" / "config.yaml"
    base = {a["name"]: a for a in config.merged_agents(TEMPLATE_RAW)}["builder"]
    entry = {
        "name": "builder",
        "runtime": base["runtime"],
        "thinking": base["thinking"],
        "timeout_s": base["timeout_s"],
        "model": "openrouter/anthropic/claude-sonnet-5",
    }
    agents = copy.deepcopy(TEMPLATE_RAW["agents"])
    assert config.write_profiles(project, agents, {"p": {"agents": [entry]}}) == (True, [])
    raw = config.load(project)
    assert raw["profiles"]["p"]["agents"] == [
        {"name": "builder", "model": "openrouter/anthropic/claude-sonnet-5"}
    ]
    mtime, content = path.stat().st_mtime_ns, path.read_bytes()
    assert config.write_profiles(project, agents, {"p": {"agents": [entry]}}) == (True, [])
    assert path.read_bytes() == content
    assert path.stat().st_mtime_ns == mtime


def test_write_profiles_keeps_a_newly_created_empty_profile_and_takes_its_roster_edits(project):
    """Ticket 92: createProfile sends {agents: []}; the profile must survive
    its first save, accept roster edits, and leave only when unnamed."""
    path = project / ".lime" / "config.yaml"

    agents = copy.deepcopy(TEMPLATE_RAW["agents"])
    profiles = {**copy.deepcopy(TEMPLATE_RAW["profiles"]), "fast-lane": {"agents": []}}

    assert config.write_profiles(project, agents, profiles) == (True, [])
    raw = config.load(project)
    assert raw["profiles"]["fast-lane"] == {}
    assert list(raw["profiles"]) == [*TEMPLATE_RAW["profiles"], "fast-lane"]
    assert config.validate(raw, root=project) == []
    assert config.merged_agents(raw, "fast-lane") == config.merged_agents(raw)
    assert "  fast-lane: {}\n" in path.read_text()

    # Same payload again is a canonical no-op: bytes and mtime unchanged
    content, mtime = path.read_bytes(), path.stat().st_mtime_ns
    assert config.write_profiles(project, agents, profiles) == (True, [])
    assert path.read_bytes() == content
    assert path.stat().st_mtime_ns == mtime

    # Roster editing
    profiles["fast-lane"] = {
        "agents": [{"name": "builder", "model": "openrouter/anthropic/claude-sonnet-5"}]
    }
    assert config.write_profiles(project, agents, profiles) == (True, [])
    raw = config.load(project)
    assert raw["profiles"]["fast-lane"] == {
        "agents": [{"name": "builder", "model": "openrouter/anthropic/claude-sonnet-5"}]
    }
    merged_builder = next(
        a for a in config.merged_agents(raw, "fast-lane") if a["name"] == "builder"
    )
    assert merged_builder["model"] == "openrouter/anthropic/claude-sonnet-5"
    for pname in TEMPLATE_RAW["profiles"]:
        assert raw["profiles"][pname] == TEMPLATE_RAW["profiles"][pname]

    # Deletion: payload no longer names it
    del profiles["fast-lane"]
    assert config.write_profiles(project, agents, profiles) == (True, [])
    assert "fast-lane" not in config.load(project)["profiles"]


def test_canonicalize_keeps_a_name_only_member_and_an_empty_profile():
    """Ticket 89: an entry whose overrides equal the roster is still membership.
    Ticket 92: a profile with no entries is still a profile — `{}`, never dropped."""
    agents = copy.deepcopy(TEMPLATE_RAW["agents"])
    base_builder = {a["name"]: a for a in config.merged_agents(TEMPLATE_RAW)}["builder"]
    profiles = {
        "member": {"agents": [{"name": "builder", "model": base_builder["model"]}]},
        "real": {"agents": [{"name": "builder", "model": "sonnet"}]},
        "empty": {"agents": []},
        "bare": {},
        "null": None,
    }
    canonical = config.canonicalize_profiles(agents, profiles, TEMPLATE_RAW["defaults"])
    assert canonical["member"]["agents"] == [{"name": "builder"}]  # override stripped, name kept
    assert canonical["real"]["agents"] == [{"name": "builder", "model": "sonnet"}]
    assert canonical["empty"] == {}
    assert canonical["bare"] == {}
    assert canonical["null"] == {}
    assert list(canonical) == ["member", "real", "empty", "bare", "null"]


def test_adding_a_role_at_its_own_defaults_joins_the_profile_and_removal_still_removes(project):
    """Ticket 89: planner at claude_code/<its model> added to all-claude lands as
    `- name: planner`; the same payload again is a no-op; dropping it drops it."""
    path = project / ".lime" / "config.yaml"
    agents = copy.deepcopy(TEMPLATE_RAW["agents"])
    base = {a["name"]: a for a in config.merged_agents(TEMPLATE_RAW)}
    profiles = copy.deepcopy(TEMPLATE_RAW["profiles"])
    for role in ("planner", "researcher"):
        profiles["all-claude"]["agents"].append(
            {"name": role, "model": base[role]["model"], "runtime": base[role]["runtime"]}
        )
    assert config.write_profiles(project, agents, profiles) == (True, [])
    raw = config.load(project)
    assert raw["profiles"]["all-claude"]["agents"] == [
        *TEMPLATE_RAW["profiles"]["all-claude"]["agents"],
        {"name": "planner"},
        {"name": "researcher"},
    ]
    profiles_span = path.read_text().split("\nprofiles:\n", 1)[1].split("\ncommands:", 1)[0]
    assert "- name: planner\n" in profiles_span and "- name: researcher\n" in profiles_span
    # a name-only member changes nothing about how the role resolves
    merged = {a["name"]: a for a in config.merged_agents(raw, "all-claude")}
    for role in ("planner", "researcher"):
        assert (merged[role]["runtime"], merged[role]["model"]) == (base[role]["runtime"], base[role]["model"])
    # the untouched profiles came through unchanged
    for pname in ("quality", "api-research"):
        assert raw["profiles"][pname] == TEMPLATE_RAW["profiles"][pname]
    # the same payload again is a canonical no-op: bytes and mtime untouched
    mtime, content = path.stat().st_mtime_ns, path.read_bytes()
    assert config.write_profiles(project, agents, profiles) == (True, [])
    assert path.read_bytes() == content and path.stat().st_mtime_ns == mtime
    # removing the role removes it — a kept name-only entry never resurrects
    profiles["all-claude"]["agents"] = [
        e for e in profiles["all-claude"]["agents"] if e["name"] != "planner"
    ]
    assert config.write_profiles(project, agents, profiles) == (True, [])
    names = [e["name"] for e in config.load(project)["profiles"]["all-claude"]["agents"]]
    assert names == ["designer", "reviewer", "researcher"]


def test_canonicalize_collapses_duplicate_roles_later_wins():
    patch = {"agents": [{"name": "builder", "model": "m/b"}, {"name": "builder", "model": "m/c"}]}
    canonical = config.canonicalize_profiles([{"name": "builder"}], {"p": patch}, {})
    assert canonical["p"]["agents"] == [{"name": "builder", "model": "m/c"}]


def test_canonicalize_raises_naming_the_unknown_agent():
    with pytest.raises(ValueError, match="ghost"):
        config.canonicalize_profiles(
            [{"name": "builder"}], {"p": {"agents": [{"name": "ghost", "model": "m/a"}]}}, {}
        )


def test_write_profiles_surfaces_an_unknown_agent_and_writes_nothing(project):
    path = project / ".lime" / "config.yaml"
    before = path.read_bytes()
    ok, errors = config.write_profiles(
        project, TEMPLATE_RAW["agents"], {"p": {"agents": [{"name": "ghost", "model": "m/a"}]}}
    )
    assert ok is False
    assert any("ghost" in e for e in errors)
    assert path.read_bytes() == before


def test_write_profiles_refuses_an_npm_extension_and_writes_nothing(project):
    path = project / ".lime" / "config.yaml"
    before = path.read_bytes()
    agents = copy.deepcopy(TEMPLATE_RAW["agents"])
    next(a for a in agents if a["name"] == "builder")["harness_engineering"] = ["npm:x"]
    ok, errors = config.write_profiles(project, agents, TEMPLATE_RAW["profiles"])
    assert ok is False
    assert any("'npm:x'" in e for e in errors)
    assert path.read_bytes() == before


def test_canonical_profile_patch_keeps_unknown_keys_for_validation():
    patch = config.canonical_profile_patch({"name": "builder", "tools": ["read"]}, {"name": "builder"})
    assert patch == {"name": "builder", "tools": ["read"]}


# --- lime profiles get/set (the dashboard's write path) ---


def test_cli_profiles_get_prints_the_merged_roster(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    assert cli.main(["profiles", "get"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [a["name"] for a in payload["merged"]["default"]] == [
        "scout", "researcher", "planner", "builder", "reviewer", "documenter", "designer",
    ]
    assert set(payload["merged"]) == {"default", *TEMPLATE_RAW["profiles"]}
    assert payload["profiles"]["quality"]["defaults"] == {"thinking": "high"}
    assert payload["agents"] == TEMPLATE_RAW["agents"]


def test_cli_profiles_set_with_invalid_stdin_exits_1_and_leaves_the_file(project, monkeypatch, capsys):
    path = project / ".lime" / "config.yaml"
    before = path.read_bytes()
    monkeypatch.chdir(project)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"agents": "nope"})))
    assert cli.main(["profiles", "set"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert path.read_bytes() == before


def test_cli_profiles_set_writes_a_valid_payload(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    payload = {
        "agents": template_with_builder_model("openrouter/anthropic/claude-sonnet-5"),
        "profiles": TEMPLATE_RAW["profiles"],
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert cli.main(["profiles", "set"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "wrote": True}
    assert config.load(project)["agents"] == payload["agents"]


def test_cli_profiles_set_then_get_lists_a_new_empty_profile(project, monkeypatch, capsys):
    """Ticket 92: createProfile {} is stored, listed, and resolves to the base roster."""
    monkeypatch.chdir(project)
    stdin = json.dumps({
        "agents": TEMPLATE_RAW["agents"],
        "profiles": {**TEMPLATE_RAW["profiles"], "fast-lane": {"agents": []}},
    })
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert cli.main(["profiles", "set"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "wrote": True}

    assert cli.main(["profiles", "get"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profiles_list"] == ["default", *TEMPLATE_RAW["profiles"], "fast-lane"]
    assert payload["profiles"]["fast-lane"] == {}
    assert [a["name"] for a in payload["merged"]["fast-lane"]] == [
        a["name"] for a in payload["merged"]["default"]
    ]


# --- profiles merge: profile-model delta write (ticket 91 / step 1) ---


# --- profiles merge: profile-model delta write (ticket 91 / step 1) ---


def test_merge_default_roster_model_runtime_thinking(project, monkeypatch):
    """Merge into default profile's base agent: preserves role metadata, unrelated profiles,
    and bytes outside the two writable spans."""
    monkeypatch.chdir(project)
    path = project / ".lime" / "config.yaml"
    before = path.read_bytes()

    changes = [
        {"profile": "default", "role": "builder", "patch": {
            "model": "openrouter/anthropic/claude-sonnet-5",
            "runtime": "pi",
            "thinking": "high",
        }}
    ]
    ok, wrote, errors = config.merge_profiles(project, changes)
    assert ok is True
    assert wrote is True
    assert errors == []

    raw = config.load(project)
    builder = next(a for a in raw["agents"] if a["name"] == "builder")
    assert builder["model"] == "openrouter/anthropic/claude-sonnet-5"
    assert builder["runtime"] == "pi"
    assert builder["thinking"] == "high"
    # Role metadata preserved
    assert builder.get("purpose") is not None
    assert builder["name"] == "builder"
    # Unrelated profiles still exist (canonical form may differ when base model
    # now matches a profile patch, but resolved values are correct)
    assert set(raw["profiles"]) == set(TEMPLATE_RAW["profiles"])
    for pname in TEMPLATE_RAW["profiles"]:
        merged = {a["name"]: a for a in config.merged_agents(raw, pname)}
        base_merged = {a["name"]: a for a in config.merged_agents(TEMPLATE_RAW, pname)}
        for name in merged:
            if name == "builder":
                assert merged[name]["model"] == "openrouter/anthropic/claude-sonnet-5"
            else:
                assert merged[name] == base_merged[name]
    # Unrelated agent metadata preserved
    scout = next(a for a in raw["agents"] if a["name"] == "scout")
    assert scout == next(a for a in TEMPLATE_RAW["agents"] if a["name"] == "scout")

    # Bytes outside the two spans are identical
    after = path.read_text().splitlines()
    before_lines = before.decode().splitlines()

    def outside(lines):
        top = re.compile(r"^[A-Za-z0-9_-]+:")
        keep, in_span = [], False
        for line in lines:
            if top.match(line):
                in_span = line.split(":", 1)[0] in ("agents", "profiles")
            if not in_span:
                keep.append(line)
        return keep

    assert outside(after) == outside(before_lines)


def test_merge_profiles_refuses_a_hand_written_npm_extension_and_writes_nothing(project, monkeypatch):
    monkeypatch.chdir(project)
    path = project / ".lime" / "config.yaml"
    raw = config.load(project)
    next(a for a in raw["agents"] if a["name"] == "builder")["harness_engineering"] = ["npm:x"]
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    before = path.read_bytes()

    ok, wrote, errors = config.merge_profiles(project, [
        {"profile": "default", "role": "builder", "patch": {
            "model": "openrouter/anthropic/claude-sonnet-5",
            "runtime": "pi",
            "thinking": "high",
        }}
    ])
    assert ok is False
    assert wrote is False
    assert any("'npm:x'" in e for e in errors)
    assert path.read_bytes() == before


def test_merge_named_profile_role_upsert_existing_and_absent(project, monkeypatch):
    """Named-profile role upsert into existing and absent profiles, including a newly added
    member and retention of target-specific defaults/timeout/other entries."""
    monkeypatch.chdir(project)
    path = project / ".lime" / "config.yaml"

    # 1. Upsert into an existing profile (quality)
    changes = [
        {"profile": "quality", "role": "builder", "patch": {
            "model": "openrouter/anthropic/claude-sonnet-5",
            "runtime": "pi",
            "thinking": "high",
        }}
    ]
    ok, wrote, errors = config.merge_profiles(project, changes)
    assert ok is True
    assert wrote is True
    assert errors == []

    raw = config.load(project)
    assert raw["profiles"]["quality"]["agents"] is not None
    builder_entry = next(e for e in raw["profiles"]["quality"]["agents"] if e["name"] == "builder")
    assert builder_entry["model"] == "openrouter/anthropic/claude-sonnet-5"
    # Profile-specific defaults preserved
    assert raw["profiles"]["quality"]["defaults"]["thinking"] == "high"

    # 2. Create a new profile and add a member
    changes2 = [
        {"profile": "fast-lane", "role": "planner", "patch": {
            "model": "openrouter/anthropic/claude-sonnet-5",
            "runtime": "pi",
        }}
    ]
    ok, wrote, errors = config.merge_profiles(project, changes2)
    assert ok is True
    assert wrote is True

    raw = config.load(project)
    assert "fast-lane" in raw["profiles"]
    fast_lane_agents = raw["profiles"]["fast-lane"]["agents"]
    assert len(fast_lane_agents) == 1
    planner_entry = fast_lane_agents[0]
    assert planner_entry["name"] == "planner"
    assert planner_entry["model"] == "openrouter/anthropic/claude-sonnet-5"
    # Runtime preserved because it differs from base planner's "claude_code"
    assert planner_entry.get("runtime") == "pi"

    # 3. Add another member to the same new profile
    changes3 = [
        {"profile": "fast-lane", "role": "builder", "patch": {
            "model": "openrouter/deepseek/deepseek-v4.1-flash",
            "runtime": "pi",
        }}
    ]
    ok, wrote, errors = config.merge_profiles(project, changes3)
    assert ok is True
    assert wrote is True

    raw = config.load(project)
    agents_list = raw["profiles"]["fast-lane"]["agents"]
    planner_entry = next(e for e in agents_list if e["name"] == "planner")
    assert planner_entry["model"] == "openrouter/anthropic/claude-sonnet-5"
    # Builder entry may be canonicalized (model stripped if it matches the
    # merged base) — check the resolved merged agent instead
    merged = {a["name"]: a for a in config.merged_agents(raw, "fast-lane")}
    assert merged["builder"]["model"] == "openrouter/deepseek/deepseek-v4.1-flash"
    assert merged["planner"]["model"] == "openrouter/anthropic/claude-sonnet-5"

    # 4. Unrelated profiles untouched
    assert "api-research" in raw["profiles"]
    assert raw["profiles"]["api-research"] == TEMPLATE_RAW["profiles"]["api-research"]
    # Runtime preserved when not specified in patch
    assert planner_entry.get("runtime") == "pi"  # from merge 2 patch


def test_merge_named_profile_defaults_model(project, monkeypatch):
    """Named-profile defaults model merge: create/merge only the named profile's defaults block."""
    monkeypatch.chdir(project)
    path = project / ".lime" / "config.yaml"

    changes = [
        {"profile": "quality", "role": None, "patch": {
            "model": "openrouter/anthropic/claude-sonnet-5",
            "runtime": "pi",
            "thinking": "low",
        }}
    ]
    ok, wrote, errors = config.merge_profiles(project, changes)
    assert ok is True
    assert wrote is True
    assert errors == []

    raw = config.load(project)
    # runtime matches base defaults (pi) so it gets canonicalized away;
    # model and thinking differ from base defaults so they are kept
    quality_defaults = raw["profiles"]["quality"]["defaults"] or {}
    assert quality_defaults.get("model") == "openrouter/anthropic/claude-sonnet-5"
    assert quality_defaults.get("thinking") == "low"
    # Existing agent overrides in the profile preserved
    assert raw["profiles"]["quality"]["agents"] == TEMPLATE_RAW["profiles"]["quality"]["agents"]


def test_merge_repeated_identical_is_noop(project, monkeypatch):
    """Repeated identical merge returns wrote:false."""
    monkeypatch.chdir(project)
    path = project / ".lime" / "config.yaml"

    changes = [
        {"profile": "default", "role": "builder", "patch": {
            "model": "openrouter/anthropic/claude-sonnet-5",
            "runtime": "pi",
        }}
    ]
    ok, wrote1, errors = config.merge_profiles(project, changes)
    assert ok is True
    assert wrote1 is True

    mtime1 = path.stat().st_mtime_ns

    ok, wrote2, errors = config.merge_profiles(project, changes)
    assert ok is True
    assert wrote2 is False
    assert path.stat().st_mtime_ns == mtime1


def test_merge_malformed_changes_rejected(project, monkeypatch):
    """Malformed changes, unknown target roles, invalid default scope, and incompatible
    model/runtime/thinking leave bytes unchanged."""
    monkeypatch.chdir(project)
    path = project / ".lime" / "config.yaml"
    before = path.read_bytes()

    # Malformed patch keys
    ok, wrote, errors = config.merge_profiles(project, [
        {"profile": "quality", "role": "builder", "patch": {"model": "m", "runtime": "pi", "tools": ["read"]}}
    ])
    assert ok is False
    assert wrote is False
    assert any("invalid patch keys" in e for e in errors)
    assert path.read_bytes() == before

    # Unknown target role
    ok, wrote, errors = config.merge_profiles(project, [
        {"profile": "quality", "role": "ghost", "patch": {"model": "m", "runtime": "pi"}}
    ])
    assert ok is False
    assert wrote is False
    assert any("ghost" in e for e in errors)
    assert path.read_bytes() == before

    # Invalid default scope (role=null on "default" profile)
    ok, wrote, errors = config.merge_profiles(project, [
        {"profile": "default", "role": None, "patch": {"model": "m", "runtime": "pi"}}
    ])
    assert ok is False
    assert wrote is False
    assert any("null role is invalid" in e for e in errors)
    assert path.read_bytes() == before

    # Unsupported runtime
    ok, wrote, errors = config.merge_profiles(project, [
        {"profile": "default", "role": "builder", "patch": {"model": "m", "runtime": "docker"}}
    ])
    assert ok is False
    assert wrote is False
    assert any("runtime" in e for e in errors)
    assert path.read_bytes() == before

    # Invalid thinking level
    ok, wrote, errors = config.merge_profiles(project, [
        {"profile": "default", "role": "builder", "patch": {"model": "m", "runtime": "pi", "thinking": "ultra"}}
    ])
    assert ok is False
    assert wrote is False
    assert any("thinking" in e for e in errors)
    assert path.read_bytes() == before

    # Empty profile name
    ok, wrote, errors = config.merge_profiles(project, [
        {"profile": "", "role": "builder", "patch": {"model": "m", "runtime": "pi"}}
    ])
    assert ok is False
    assert wrote is False
    assert any("profile must be a non-empty" in e for e in errors)
    assert path.read_bytes() == before

    # Empty changes list
    ok, wrote, errors = config.merge_profiles(project, [])
    assert ok is False
    assert wrote is False
    # Structural validation catches empty before any write, but merge_profiles
    # itself returns (False, False, []) because it always validates.
    # The CLI layer rejects empty changes before calling merge_profiles.
    assert path.read_bytes() == before


def test_merge_cli_valid_stdin(project, monkeypatch, capsys):
    """CLI: valid profiles merge returns JSON success result."""
    monkeypatch.chdir(project)
    stdin = json.dumps({"changes": [
        {"profile": "default", "role": "builder", "patch": {
            "model": "openrouter/anthropic/claude-sonnet-5",
            "runtime": "pi",
        }}
    ]})
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert cli.main(["profiles", "merge"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": True, "wrote": True}


def test_merge_cli_noop_stdin(project, monkeypatch, capsys):
    """CLI: repeated identical merge returns wrote:false."""
    monkeypatch.chdir(project)
    changes = [{"profile": "default", "role": "builder", "patch": {
        "model": "openrouter/anthropic/claude-sonnet-5",
        "runtime": "pi",
    }}]
    stdin = json.dumps({"changes": changes})
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert cli.main(["profiles", "merge"]) == 0
    capsys.readouterr()  # discard first result

    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert cli.main(["profiles", "merge"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"ok": True, "wrote": False}


def test_merge_cli_invalid_stdin(project, monkeypatch, capsys):
    """CLI: invalid stdin returns 1 with ok:false."""
    monkeypatch.chdir(project)

    # Malformed JSON
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cli.main(["profiles", "merge"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False

    # Empty changes list
    monkeypatch.setattr("sys.stdin", io.StringIO('{"changes": []}'))
    assert cli.main(["profiles", "merge"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert any("changes" in e for e in result["errors"])

    # Missing changes key
    monkeypatch.setattr("sys.stdin", io.StringIO('{"agents": []}'))
    assert cli.main(["profiles", "merge"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False


# --- ticket/map template check (§10 header) ---

GOOD_TICKET = """# 41 — Example

Workflow: feature
Status: open
Spec: .lime/specs/foundation.md

## Ask
x

## Where
x

## Done means
x

## Out of scope
x
"""


def write_ticket(root, name, text):
    tickets = root / ".lime" / "tickets"
    tickets.mkdir(parents=True, exist_ok=True)
    (tickets / name).write_text(text)


def test_good_ticket_passes(tmp_path):
    write_ticket(tmp_path, "41-example.md", GOOD_TICKET)
    assert config.ticket_failures(tmp_path) == []


def test_ticket_missing_section_fails(tmp_path):
    write_ticket(tmp_path, "41-example.md", GOOD_TICKET.replace("## Done means\nx\n", ""))
    assert any("Done means" in f for f in config.ticket_failures(tmp_path))


def test_ticket_with_unknown_workflow_fails(tmp_path):
    write_ticket(tmp_path, "41-example.md", GOOD_TICKET.replace("Workflow: feature", "Workflow: refactor"))
    assert any("refactor" in f for f in config.ticket_failures(tmp_path))


def test_ticket_without_status_fails(tmp_path):
    write_ticket(tmp_path, "41-example.md", GOOD_TICKET.replace("Status: open\n", ""))
    assert any("Status" in f for f in config.ticket_failures(tmp_path))


def test_ticket_over_one_screen_fails(tmp_path):
    write_ticket(tmp_path, "41-example.md", GOOD_TICKET + "\n" * 70)
    assert any("one screen" in f for f in config.ticket_failures(tmp_path))


def test_this_repos_tickets_pass():
    assert config.ticket_failures() == []

def test_pi_orchestrator_scout_extension_is_a_packaged_file(tmp_path, monkeypatch):
    # SCOUT_EXTENSION sits alongside subagent extension pattern
    from lime.cli import SCOUT_EXTENSION
    path = Path(SCOUT_EXTENSION)
    assert path.is_absolute()
    assert path.exists()
    assert path.name == "scout.ts"
    assert path.parent.name == "harness_engineering"
    # Not added to the roster tool vocabulary
    assert "scout" not in config.VOCABULARY


# --- tool_choice ---


def test_tool_choice_defaults_to_auto_and_reads_role_override():
    """Pydantic AgentConfig defaults to auto; explicit required/auto accepted."""
    c = cfg()
    merged = config.merged_agents(c)[0]
    assert merged.get("tool_choice", "auto") == "auto"
    assert config.roster(c)[0].tool_choice == "auto"
    # Explicit tool_choice=required
    c["agents"][0]["tool_choice"] = "required"
    assert config.roster(c)[0].tool_choice == "required"
    # Explicit tool_choice=auto
    c["agents"][0]["tool_choice"] = "auto"
    assert config.roster(c)[0].tool_choice == "auto"
    # Also validates
    assert config.validate(c) == []


def test_tool_choice_is_profile_patchable():
    """Profile defaults and per-role patches; explicit role wins over profile defaults."""
    c = cfg(profiles={"p": {"defaults": {"tool_choice": "required"}}})
    merged = config.merged_agents(c, "p")[0]
    assert merged["tool_choice"] == "required"
    assert config.roster(c, "p")[0].tool_choice == "required"
    # Per-role patch overrides profile defaults
    c = cfg(profiles={"p": {"defaults": {"tool_choice": "required"},
                             "agents": [{"name": "builder", "tool_choice": "auto"}]}})
    assert config.roster(c, "p")[0].tool_choice == "auto"
    # Profile defaults patch on all-claude: scout back to auto
    from copy import deepcopy
    raw = deepcopy(TEMPLATE_RAW)
    raw["profiles"]["all-claude"] = {
        "defaults": {"runtime": "claude_code", "model": "sonnet"},
        "agents": [{"name": "scout", "tool_choice": "auto"}],
    }
    merged_scout = {a["name"]: a for a in config.merged_agents(raw, "all-claude")}["scout"]
    assert merged_scout.get("tool_choice", "auto") == "auto"


def test_tool_choice_rejects_invalid_values():
    """Config validation names tool_choice and pydantic rejects invalid values."""
    for bad in ("none", "force", "", None, True, 1):
        c = cfg()
        c["agents"][0]["tool_choice"] = bad
        if bad is None:
            with pytest.raises((TypeError, ValueError)):
                config.roster(c)
        elif isinstance(bad, (bool, int)):
            with pytest.raises((TypeError, ValueError)):
                config.roster(c)
        else:
            failures = config.validate(c)
            assert any("tool_choice" in f for f in failures), f"expected tool_choice failure for {bad!r}"


@pytest.mark.parametrize("yaml_path", [
    str(Path(__file__).resolve().parent.parent / "lime" / "templates" / "config.yaml"),
    str(Path(__file__).resolve().parent.parent / ".lime" / "config.yaml"),
], ids=["template", "project"])
def test_scout_requires_tools_in_both_configs(yaml_path):
    """Read both repository YAML files by stable paths, assert base scout required
    and other base pi roles auto; all-Claude template profile still validates."""
    raw = yaml.safe_load(Path(yaml_path).read_text())
    agents = {a.name: a for a in config.roster(raw)}
    assert agents["scout"].tool_choice == "required"
    for name in ("planner", "builder", "reviewer", "documenter", "designer"):
        if agents[name].runtime == "pi":
            assert agents[name].tool_choice == "auto", f"{name} should be auto"
    # All-Claude profile validates
    for pname in raw.get("profiles", {}):
        try:
            config.roster(raw, pname)
        except (ValueError, TypeError) as e:
            pytest.fail(f"profile {pname} failed: {e}")


# --- commit settings (ticket 122): gate + commit exclusions and line budget ---


def test_commit_settings_missing_block_uses_defaults(tmp_path, monkeypatch):
    """A minimal saved .lime/config.yaml (no commit block) passed through load() gets defaults."""
    (tmp_path / ".lime").mkdir()
    (tmp_path / ".lime" / "config.yaml").write_text(
        "commands:\n  test: pytest\n"
    )
    monkeypatch.chdir(tmp_path)
    raw = config.load()
    exclude, uncounted, budget = config.resolve_commit_settings(raw)
    assert exclude == ["context_handoff/"]
    assert uncounted == []
    assert budget == 1500


@pytest.mark.parametrize("commit_block", [
    {"exclude": None, "uncounted": None, "max_added_lines": None},
    {},
    None,
], ids=["null-block", "empty-block", "missing-block"])
def test_commit_settings_partial_and_empty_blocks_use_defaults(commit_block):
    raw = {"commit": commit_block} if commit_block is not None else {}
    exclude, uncounted, budget = config.resolve_commit_settings(raw)
    assert exclude == ["context_handoff/"]
    assert uncounted == []
    assert budget == 1500


def test_commit_uncounted_missing_null_and_empty_defaults():
    defaults_cases = [
        ({}, "no-block"),
        ({"commit": None}, "null-block"),
        ({"commit": {}}, "empty-mapping"),
        ({"commit": {"max_added_lines": None}}, "missing-key"),
        ({"commit": {"exclude": None, "uncounted": None, "max_added_lines": None}}, "null-key"),
    ]
    for raw, label in defaults_cases:
        exclude, uncounted, budget = config.resolve_commit_settings(raw)
        assert exclude == ["context_handoff/"], label
        assert uncounted == [], label
        assert budget == 1500, label

    # explicit empties override the defaults
    exclude, uncounted, budget = config.resolve_commit_settings(
        {"commit": {"exclude": [], "uncounted": []}}
    )
    assert exclude == []
    assert uncounted == []
    assert budget == 1500

    # zero cap is preserved
    _, _, budget = config.resolve_commit_settings({"commit": {"max_added_lines": 0}})
    assert budget == 0


def test_commit_settings_empty_exclude_disables_exclusions():
    exclude, uncounted, _ = config.resolve_commit_settings({"commit": {"exclude": []}})
    assert exclude == []
    assert uncounted == []


def test_commit_settings_zero_budget_allows_zero_additions():
    _, _, budget = config.resolve_commit_settings({"commit": {"max_added_lines": 0}})
    assert budget == 0


def test_commit_settings_explicit_lists_replace_defaults():
    exclude, uncounted, budget = config.resolve_commit_settings(
        {"commit": {"exclude": ["dist/", "*.png"], "uncounted": ["pnpm-lock.yaml"], "max_added_lines": 7}}
    )
    assert exclude == ["dist/", "*.png"]
    assert uncounted == ["pnpm-lock.yaml"]
    assert budget == 7


def test_commit_pattern_lists_are_independent_and_do_not_mutate_raw():
    raw = {"commit": {"exclude": ["dist/"], "uncounted": ["pnpm-lock.yaml"], "max_added_lines": 5}}
    first = config.resolve_commit_settings(raw)
    second = config.resolve_commit_settings(raw)
    # fresh lists: mutating one result never touches the next call or the raw config
    first[0].append("extra/")
    first[1].append("also/")
    assert second[0] == ["dist/"]
    assert second[1] == ["pnpm-lock.yaml"]
    assert raw["commit"]["exclude"] == ["dist/"]
    assert raw["commit"]["uncounted"] == ["pnpm-lock.yaml"]
    # the two fields from one call are distinct objects, even aliased in the raw config
    assert first[0] is not first[1]


def test_commit_pattern_lists_aliased_in_raw_are_independent():
    shared = ["shared/"]
    exclude, uncounted, _ = config.resolve_commit_settings(
        {"commit": {"exclude": shared, "uncounted": shared}}
    )
    assert exclude == uncounted == ["shared/"]
    assert exclude is not uncounted
    exclude.append("x/")
    assert uncounted == ["shared/"]
    assert shared == ["shared/"]


def test_commit_settings_partial_block_resolves_each_key_independently():
    exclude, uncounted, budget = config.resolve_commit_settings({"commit": {"exclude": ["x/"]}})
    assert exclude == ["x/"]
    assert uncounted == []
    assert budget == 1500  # absent budget still default
    exclude2, uncounted2, budget2 = config.resolve_commit_settings({"commit": {"max_added_lines": 42}})
    assert exclude2 == ["context_handoff/"]  # absent exclude still default
    assert uncounted2 == []
    assert budget2 == 42


def test_template_commit_values_equal_accessor_defaults():
    exclude, uncounted, budget = config.resolve_commit_settings(TEMPLATE_RAW)
    assert exclude == ["context_handoff/"]
    assert uncounted == []
    assert budget == 1500
    assert TEMPLATE_RAW["commit"]["exclude"] == ["context_handoff/"]
    assert TEMPLATE_RAW["commit"]["uncounted"] == []
    assert TEMPLATE_RAW["commit"]["max_added_lines"] == 1500


@pytest.mark.parametrize("bad_block", ["nope", 5, True, ["a"]])
def test_validate_rejects_non_mapping_commit_block(bad_block):
    failures = config.validate(cfg(commit=bad_block))
    assert any("commit must be a mapping" in f for f in failures)


@pytest.mark.parametrize("field", ["exclude", "uncounted"])
def test_validate_rejects_bad_pattern_list_shapes(field):
    assert any(f"commit.{field} must be a list" in f
               for f in config.validate(cfg(commit={field: "dist"})))
    assert any(f"commit.{field} entries must be strings" in f
               for f in config.validate(cfg(commit={field: ["dist", 5]})))
    for bad in (5, True, {"a": 1}):
        failures = config.validate(cfg(commit={field: bad}))
        assert any(f"commit.{field} must be a list" in f for f in failures), bad


@pytest.mark.parametrize("field", ["exclude", "uncounted"])
@pytest.mark.parametrize("entry", [
    "", "/abs/path", "../escape", "a/../b", ":(top)foo", ":!foo", ":foo", "^foo", "!foo",
])
def test_validate_rejects_dangerous_pattern_entries(field, entry):
    c = cfg(commit={field: ["ok/", entry]})
    failures = config.validate(c)
    assert any(f"commit.{field}" in f for f in failures), entry


def test_validate_rejects_exact_cross_list_duplicates():
    # explicit duplicate — error names both settings and the pattern
    failures = config.validate(
        cfg(commit={"exclude": ["dist/", "context_handoff/"], "uncounted": ["dist/"]})
    )
    assert any("commit.exclude" in f and "commit.uncounted" in f and "dist/" in f for f in failures)
    # uncounted collides with the default exclude when exclude is omitted
    failures = config.validate(cfg(commit={"uncounted": ["context_handoff/"]}))
    assert any("commit.exclude" in f and "commit.uncounted" in f and "context_handoff/" in f for f in failures)
    # ... and when exclude is explicit null
    failures = config.validate(cfg(commit={"exclude": None, "uncounted": ["context_handoff/"]}))
    assert any("commit.exclude" in f and "commit.uncounted" in f for f in failures)


def test_validate_accepts_distinct_overlapping_patterns():
    # directory/subdirectory overlap: exclude a child, uncount the parent
    assert config.validate(cfg(commit={"exclude": ["generated/private/"],
                                       "uncounted": ["generated/"]})) == []
    # glob/literal overlap
    assert config.validate(cfg(commit={"exclude": ["*.tmp"], "uncounted": ["cache.tmp"]})) == []
    # prefix overlap on a different exact string
    assert config.validate(cfg(commit={"exclude": ["dist/build/"], "uncounted": ["dist/"]})) == []
    # explicit empty exclude permits context_handoff/ in uncounted
    assert config.validate(cfg(commit={"exclude": [], "uncounted": ["context_handoff/"]})) == []


def test_validate_rejects_bad_max_added_lines():
    for bad in [-1, 1.5, "1500", True]:
        c = cfg(commit={"max_added_lines": bad})
        failures = config.validate(c)
        assert len([f for f in failures if "max_added_lines" in f]) == 1, bad
    assert config.validate(cfg(commit={"max_added_lines": 0})) == []


def test_validate_accepts_valid_commit_settings():
    c = cfg(commit={"exclude": ["dist/", "**/*.tmp", "a b.txt"], "max_added_lines": 77})
    assert config.validate(c) == []
    # valid directories, globs and spaced filenames pass for uncounted too
    c = cfg(commit={"uncounted": ["pnpm-lock.yaml", "gen/**", "a b.txt"]})
    assert config.validate(c) == []


# --- ticket 234: glossary entry cap ---


def _glossary_entry_line(term: str, n: int) -> str:
    """A `- **Term** — …` line whose entry text is exactly n characters (em dashes count 1)."""
    prefix = f"- **{term}** — "
    return prefix + "—" * (n - len(prefix))


def test_glossary_entries_parse_term_and_continuation_lines():
    text = (
        "Intro paragraph here.\n"
        "\n"
        "## Group\n"
        "\n"
        "- **Alpha** — one\n"
        "continuation one\n"
        "\n"
        "continuation two\n"
        "- **Beta** — two\n"
        "- note\n"
        "stray text after the plain bullet\n"
        "- **Gamma** — three\n"
        "  - sub\n"
    )
    assert config.glossary_entries(text) == [
        ("Alpha", "- **Alpha** — one\ncontinuation one\ncontinuation two"),
        ("Beta", "- **Beta** — two"),
        ("Gamma", "- **Gamma** — three"),
    ]


def test_context_md_check_passes_without_a_file(tmp_path):
    assert config.context_md_failures(tmp_path) == []


def test_context_md_entry_350_passes_351_fails(tmp_path):
    (tmp_path / "CONTEXT.md").write_text(_glossary_entry_line("Long", 350) + "\n")
    assert config.context_md_failures(tmp_path) == []
    (tmp_path / "CONTEXT.md").write_text(_glossary_entry_line("Long", 351) + "\n")
    failures = config.context_md_failures(tmp_path)
    assert len(failures) == 1
    assert "Long" in failures[0] and "351" in failures[0]


def test_context_md_check_reports_every_long_entry_by_term(tmp_path):
    text = (
        _glossary_entry_line("First", 400) + "\n"
        + _glossary_entry_line("Second", 360) + "\n"
        + _glossary_entry_line("Short", 100) + "\n"
    )
    (tmp_path / "CONTEXT.md").write_text(text)
    failures = config.context_md_failures(tmp_path)
    assert len(failures) == 2
    assert "First" in failures[0]
    assert "Second" in failures[1]


def test_context_md_check_is_not_part_of_validate(tmp_path):
    (tmp_path / "CONTEXT.md").write_text(_glossary_entry_line("Long", 400) + "\n")
    assert config.validate(cfg(), root=tmp_path) == []


def test_context_md_check_leaves_the_file_untouched(tmp_path):
    path = tmp_path / "CONTEXT.md"
    path.write_text(_glossary_entry_line("Long", 400) + "\n")
    before = path.read_bytes()
    config.context_md_failures(tmp_path)
    assert path.read_bytes() == before
