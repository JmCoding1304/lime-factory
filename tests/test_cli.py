import fcntl
import io
import json
import logging
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import ast

from collections import Counter

import pytest
import yaml

from lime import agents, cli, config, git_helper, tracker
from lime.cli import COMMANDS, main
from lime.data_types import AgentConfig, AgentResult, CostBreakdown, UsageBreakdown

from overlap_helpers import Origin, close_local_ticket, ticket_text, unpushed_run
from stage_helpers import assert_table_stages

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Ticket 255 — the real watchdog spawn, restored by the one test that proves a retry
# spawns none. Every other batch test keeps it stubbed.
_REAL_START_WATCHDOG = cli.batch.Batch.start_watchdog


def test_help_lists_every_subcommand(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in COMMANDS:
        assert name in out


def test_version_matches_package_metadata(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert version("limefactory") in capsys.readouterr().out


def test_no_args_prints_help_and_succeeds(capsys):
    assert main([]) == 0
    assert "usage: lime" in capsys.readouterr().out


@pytest.fixture(autouse=True)
def _clean_lime_run_env():
    """`lime run` sets these directly on os.environ (spec §7) — a real Run is one
    process and exits, but tests share this interpreter, so scrub around each one."""
    keys = ("LIME_RUN_ID", "LIME_PORT", "LIME_DB_NAME", "LIME_RUNS_DIR")
    for key in keys:
        os.environ.pop(key, None)
    yield
    for key in keys:
        os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def _no_real_watchdog(monkeypatch):
    """Ticket 255 — the batch spawns a watchdog subprocess; stub it so the fake-Popen
    batch tests stay watchdog-free (real spawns are the watchdog tests' job)."""
    monkeypatch.setattr(cli.batch.Batch, "start_watchdog", lambda self, **kw: None)


@pytest.fixture(autouse=True)
def _no_real_gh_pr_list(monkeypatch):
    """`lime deploy` reconciles tickets against `gh pr list` (ticket 61) — no test shells
    out to GitHub; a test that needs merged PRs sets its own list."""
    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: [])
    monkeypatch.setattr(cli.git_helper, "pr_head_refs", lambda: [])


def test_unknown_subcommand_errors():
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2


# --- hook budget: Orchestrator UserPromptSubmit advisory (ticket 35, spec §9) ---


def _usage(input_tokens=0, cache_write=0, cache_read=0):
    return {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
    }


def _transcript(tmp_path, *usages):
    path = tmp_path / "transcript.jsonl"
    lines = [
        json.dumps({"type": "assistant", "message": {"usage": usage}}) for usage in usages
    ]
    path.write_text("\n".join(lines) + "\n" if lines else "")
    return path


def _run_hook(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return main(["hook", "budget"])


def test_hook_budget_silent_under_warn(tmp_path, monkeypatch, capsys):
    transcript = _transcript(tmp_path, _usage(input_tokens=100, cache_write=50, cache_read=20))
    assert _run_hook(monkeypatch, {"transcript_path": str(transcript)}) == 0
    out = capsys.readouterr().out
    assert out == ""


def test_hook_budget_prints_advisory_at_warn(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    transcript = _transcript(tmp_path, _usage(input_tokens=150_000))
    assert _run_hook(monkeypatch, {"transcript_path": str(transcript)}) == 0
    out = capsys.readouterr().out
    assert "150,000" in out
    assert "/clear" in out
    assert "assets/<NN>-handoff.md" in out
    assert '/lime map <map> <NN>' in out


def test_hook_budget_uses_last_assistant_turn(tmp_path, monkeypatch, capsys):
    """An earlier huge turn must not stick around after the context has been cleared down."""
    transcript = _transcript(
        tmp_path, _usage(input_tokens=200_000), _usage(input_tokens=100)
    )
    assert _run_hook(monkeypatch, {"transcript_path": str(transcript)}) == 0
    assert capsys.readouterr().out == ""


def test_hook_budget_tokens_flag_is_the_pi_path(monkeypatch, capsys):
    """pi's budget.ts passes context usage as --tokens; same text, /new for pi's fresh session."""
    monkeypatch.delenv("HERDR_ENV", raising=False)
    assert main(["hook", "budget", "--tokens", "149999"]) == 0
    assert capsys.readouterr().out == ""
    assert main(["hook", "budget", "--tokens", "150000"]) == 0
    out = capsys.readouterr().out
    assert "150,000" in out and "/new" in out and "/clear" not in out
    assert "assets/<NN>-handoff.md" in out


def test_hook_budget_path_prints_the_pi_extension(capsys):
    assert main(["hook", "budget", "--path"]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.name == "budget.ts" and path.parent.name == "guard" and path.exists()


def test_hook_budget_silent_without_a_transcript(tmp_path, monkeypatch, capsys):
    assert _run_hook(monkeypatch, {"transcript_path": str(tmp_path / "missing.jsonl")}) == 0
    assert capsys.readouterr().out == ""


def test_hook_budget_silent_on_bad_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert main(["hook", "budget"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("payload", ["null", "[]", '"a string"'])
def test_hook_budget_silent_on_non_object_stdin(payload, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert main(["hook", "budget"]) == 0
    assert capsys.readouterr().out == ""


def test_hook_budget_mentions_herdr_pane_when_env_set(tmp_path, monkeypatch, capsys):
    transcript = _transcript(tmp_path, _usage(input_tokens=150_000))
    monkeypatch.setenv("HERDR_ENV", "1")
    assert _run_hook(monkeypatch, {"transcript_path": str(transcript)}) == 0
    out = capsys.readouterr().out
    assert "Herdr pane" in out
    assert '/lime map <map> <NN>' in out  # same hand-over command, just a different venue


# --- check (ticket 24) ---


ROSTER_BY_PROFILE = {
    None: {
        "scout": "claude_code · claude-haiku-5-5 · medium",
        "researcher": "claude_code · sonnet · low",
        "planner": "claude_code · claude-opus-5-5[1m] · xhigh",
        "builder": "pi · openrouter/deepseek/deepseek-v4.1-flash · high",
        "reviewer": "pi · openai-codex/gpt-6-astra · high",
        "documenter": "claude_code · claude-haiku-5-5 · high",
        "designer": "pi · openrouter/moonshotai/kimi-k3 · medium",
    },
    "quality": {
        "scout": "claude_code · claude-haiku-5-5 · medium",
        "researcher": "claude_code · sonnet · low",
        "planner": "claude_code · claude-opus-5-5[1m] · xhigh",
        "builder": "pi · openrouter/anthropic/claude-sonnet-5 · high",
        "reviewer": "pi · openai-codex/gpt-6-astra · high",
        "documenter": "claude_code · claude-haiku-5-5 · high",
        "designer": "pi · openrouter/moonshotai/kimi-k3 · medium",
    },
    "all-claude": {
        "scout": "claude_code · claude-haiku-5-5 · medium",
        "researcher": "claude_code · sonnet · low",
        "planner": "claude_code · claude-opus-5-5[1m] · xhigh",
        "builder": "claude_code · sonnet · high",
        "reviewer": "claude_code · fable · high",
        "documenter": "claude_code · claude-haiku-5-5 · high",
        "designer": "claude_code · opus · medium",
    },
    "api-research": {
        "researcher": "pi · openrouter/google/gemini-3.7-flash · low",
        "builder": "pi · openrouter/deepseek/deepseek-v4.1-flash · high",
        "scout": "claude_code · claude-haiku-5-5 · medium",
        "documenter": "claude_code · claude-haiku-5-5 · high",
    },
}


@pytest.fixture
def green_environment(tmp_path, monkeypatch):
    """A repo with no .lime/config.yaml (template fallback) and every live check mocked green."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.config, "credential_failures", lambda agents: ([], []))
    monkeypatch.setattr(cli.config, "compaction_failures", lambda: [])
    monkeypatch.setattr(cli, "_probe_failures", lambda: [])
    monkeypatch.setattr(cli.config, "_extension_failures", lambda agent, root: [])
    monkeypatch.setattr(cli.config, "capability_failures", lambda agents: ([], []))


def test_check_green_prints_the_resolved_roster(green_environment, capsys):
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "green" in out
    for role, cell in ROSTER_BY_PROFILE[None].items():
        assert cell in out, role
    # builder writes are unrestricted minus trust; scout is read-only in the repo
    assert "all − trust" in out
    # `ls` on a claude_code role is dropped with a warning, not a failure
    assert "warn  researcher: `ls`" in out


def test_check_red_on_a_missing_configured_prompt_override(green_environment, tmp_path, capsys):
    """A configured override path with no file fails `lime check` naming the exact path,
    even though the shipped builder has a package default."""
    raw = yaml.safe_load(config.TEMPLATE.read_text())
    builder = next(a for a in raw["agents"] if a["name"] == "builder")
    builder["prompt_engineering"] = {"system": ".lime/prompts/builder/system.md"}
    (tmp_path / ".lime").mkdir()
    (tmp_path / ".lime" / "config.yaml").write_text(yaml.safe_dump(raw))
    assert main(["check"]) == 1
    out = capsys.readouterr().out
    assert ".lime/prompts/builder/system.md" in out
    assert "builder" in out


def test_check_red_on_an_overlong_agents_md(green_environment, tmp_path, capsys):
    (tmp_path / "AGENTS.md").write_text("\n".join(f"line {i}" for i in range(61)) + "\n")
    assert main(["check"]) == 1
    assert "AGENTS.md" in capsys.readouterr().out


def test_check_green_at_the_sixty_line_agents_boundary(green_environment, tmp_path, capsys):
    (tmp_path / "AGENTS.md").write_text("\n".join(f"line {i}" for i in range(60)) + "\n")
    assert main(["check"]) == 0


def test_check_red_on_an_overlong_context_md_entry(green_environment, tmp_path, capsys):
    (tmp_path / "CONTEXT.md").write_text("- **Sprawl** — " + "x" * 400 + "\n")
    assert main(["check"]) == 1
    out = capsys.readouterr().out
    assert "Sprawl" in out and "CONTEXT.md" in out


def test_check_green_with_short_context_md_entries(green_environment, tmp_path, capsys):
    (tmp_path / "CONTEXT.md").write_text("- **Tight** — short\n")
    assert main(["check"]) == 0


@pytest.mark.parametrize("profile", ["quality", "all-claude", "api-research"])
def test_check_roster_print_matches_the_spec_table_per_profile(
    green_environment, capsys, profile
):
    assert main(["check", "--profile", profile]) == 0
    out = capsys.readouterr().out
    assert f"profile {profile}" in out
    for role, cell in ROSTER_BY_PROFILE[profile].items():
        assert cell in out, role


def test_check_unknown_profile_fails(green_environment, capsys):
    assert main(["check", "--profile", "nope"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_check_red_when_credentials_are_missing(green_environment, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.config, "credential_failures", lambda agents: (["pi credentials for 'xai': missing"], [])
    )
    assert main(["check"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  pi credentials for 'xai': missing" in out
    assert "also stop `lime run`" in out
    assert "no Run spawns" not in out


def test_check_red_summary_says_a_ticket_failure_stops_any_launch_that_includes_it(
    green_environment, tmp_path, capsys
):
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "41-bad.md").write_text("# 41 — bad\nWorkflow: feature\nStatus: open\n\n## Where\n- no path\n")
    assert main(["check"]) == 1
    summary = capsys.readouterr().out.splitlines()[-1]
    assert "a ticket's Where failure stops any launch that includes that ticket;" in summary
    assert "only that ticket" not in summary
    # a batch with the bad ticket in it refuses as a whole, before anything launches
    (tickets / "42-good.md").write_text("# 42 — good\nWorkflow: feature\nStatus: open\n\n## Where\n- `a.py`\n")
    assert main(["run", "42", "41"]) == 2
    assert "41-bad.md: Where parses zero paths" in capsys.readouterr().err
    assert "ticket-header, CONTEXT.md, gh, bun and claude-probe failures only fail this check" in summary


def test_check_red_on_a_broken_ticket(green_environment, tmp_path, monkeypatch, capsys):
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "41-bad.md").write_text("# 41 — bad\n\nWorkflow: feature\nStatus: open\n")
    assert main(["check"]) == 1
    assert "41-bad.md" in capsys.readouterr().out


def test_check_red_on_an_unsupported_thinking_level(green_environment, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.config, "capability_failures",
        lambda agents: (["builder: thinking 'medium' is not supported by openrouter/deepseek/deepseek-v4-flash — supported: ('off', 'high', 'xhigh')"], []),
    )
    assert main(["check"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  builder: thinking 'medium'" in out


def test_check_warns_on_an_unknown_context_window(green_environment, monkeypatch, capsys):
    monkeypatch.setattr(
        cli.config, "capability_failures",
        lambda agents: ([], ["builder: openrouter/deepseek/deepseek-v4-flash has no contextWindow in pi's catalog — the budget backstop falls back to 270,000 tokens"]),
    )
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "warn  builder:" in out
    assert "green" in out


def test_check_reads_no_dashboard_flag(green_environment, tmp_path, capsys):
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "50-dash.md").write_text(TICKET_TEXT.replace("app.py", "dashboard/src/App.vue"))
    assert main(["check"]) == 0
    out = capsys.readouterr().out
    assert "takes text only" not in out


def test_probe_failures_green(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/fake/bin")
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: type("P", (), {"returncode": 0, "stderr": ""})()
    )
    assert cli._probe_failures() == []


def test_probe_failures_name_every_missing_binary(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)

    def raise_oserror(*a, **k):
        raise OSError

    monkeypatch.setattr(cli.subprocess, "run", raise_oserror)
    failures = cli._probe_failures()
    assert any("gh" in f for f in failures)
    assert any("bun" in f for f in failures)
    assert any("claude" in f for f in failures)


# --- dash (ticket 39) ---


def test_dash_without_bun_fails_clearly(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda _: None)
    assert main(["dash"]) == 1
    assert "bun not found" in capsys.readouterr().err


def test_dash_without_registry_names_lime_init(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/fake/bun")
    monkeypatch.setenv("LIME_PROJECTS", str(tmp_path / "projects"))
    assert main(["dash"]) == 1
    err = capsys.readouterr().err
    assert "no project registry" in err
    assert "lime init" in err


def test_dash_refuses_an_empty_registry(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "projects"
    registry.write_text("# nothing registered yet\n\n")
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/fake/bun")
    monkeypatch.setenv("LIME_PROJECTS", str(registry))
    assert main(["dash"]) == 1
    assert "empty project registry" in capsys.readouterr().err


def _fake_dash(monkeypatch, tmp_path, poll=None):
    """The dashboard happy path, faked: a registry, `/fake/bun`, `PORT` scrubbed,
    `bun install`/vite recorded, and a background server that records its lifecycle."""
    registry = tmp_path / "projects"
    registry.write_text("/some/project\n")
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/fake/bun")
    monkeypatch.setenv("LIME_PROJECTS", str(registry))
    monkeypatch.delenv("PORT", raising=False)

    calls = []

    class FakeProc:
        def __init__(self, cmd, **kwargs):
            calls.append(("popen", cmd, kwargs))

        def terminate(self):
            calls.append(("terminate",))

        def wait(self):
            calls.append(("wait",))

        def poll(self):
            return poll

        returncode = 0

    class Done:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(("run", cmd, kwargs))
        return Done()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.subprocess, "Popen", FakeProc)
    return calls


def test_dash_boots_server_and_ui_with_the_registry(tmp_path, monkeypatch, capsys):
    """The one-line dev boot: bun install, API server in background, vite in front."""
    calls = _fake_dash(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_port_busy", lambda port: False)
    monkeypatch.setattr(cli, "_wait_listening", lambda server, port: None)

    assert main(["dash"]) == 0

    assert "http://localhost:4601/#/" in capsys.readouterr().out
    install, server, ui = calls[0], calls[1], calls[2]
    assert install[1] == ["/fake/bun", "install"]
    assert server[1] == ["/fake/bun", "run", "server/index.ts"]
    assert server[2]["env"]["LIME_PROJECTS"] == str(tmp_path / "projects")
    assert server[2]["env"]["PORT"] == "4600"
    assert ui[1] == ["/fake/bun", "x", "vite"]
    assert ui[2]["env"]["PORT"] == "4600"
    # The API server never outlives the UI.
    assert calls[-2:] == [("terminate",), ("wait",)]


@pytest.mark.parametrize(
    "argv, env, expected",
    [
        (["--port", "4710"], {"PORT": "4700"}, "4710"),
        ([], {"PORT": "4700"}, "4700"),
        ([], {"LIME_PORT": "3101"}, "3101"),
        ([], {"PORT": "", "LIME_PORT": ""}, "4600"),
        ([], {}, "4600"),
    ],
)
def test_dash_port_reaches_server_and_vite(tmp_path, monkeypatch, argv, env, expected):
    calls = _fake_dash(monkeypatch, tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    probed = []
    monkeypatch.setattr(cli, "_port_busy", lambda port: probed.append(port) or False)
    monkeypatch.setattr(cli, "_wait_listening", lambda server, port: None)

    assert main(["dash", *argv]) == 0

    server = next(c for c in calls if c[0] == "popen")
    ui = next(c for c in calls if c[0] == "run" and c[1] == ["/fake/bun", "x", "vite"])
    assert server[2]["env"]["PORT"] == expected
    assert ui[2]["env"]["PORT"] == expected
    assert probed == [int(expected)]


@pytest.mark.parametrize(
    "argv, env, source",
    [
        (["--port", "0"], {}, "--port"),
        ([], {"PORT": "abc"}, "PORT"),
    ],
)
def test_dash_refuses_a_bad_port(tmp_path, monkeypatch, capsys, argv, env, source):
    calls = _fake_dash(monkeypatch, tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    assert main(["dash", *argv]) == 1
    captured = capsys.readouterr()
    assert "not a port" in captured.err
    assert source in captured.err
    assert "http://localhost:4601" not in captured.out
    assert calls == []


def test_dash_busy_port_fails_naming_the_port_and_never_starts_vite(
    tmp_path, monkeypatch, capsys
):
    calls = _fake_dash(monkeypatch, tmp_path)
    holder = socket.socket()
    try:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        assert main(["dash", "--port", str(port)]) == 1
    finally:
        holder.close()
    err = capsys.readouterr().err
    assert "busy" in err
    assert str(port) in err
    assert calls == []


def test_dash_server_dying_before_listening_never_starts_vite(tmp_path, monkeypatch, capsys):
    calls = _fake_dash(monkeypatch, tmp_path, poll=1)
    monkeypatch.setattr(cli, "_port_busy", lambda port: False)
    port = cli._probe_free_port()

    assert main(["dash", "--port", str(port)]) == 1
    err = capsys.readouterr().err
    assert "exited (code 1)" in err
    assert str(port) in err
    assert ("run", ["/fake/bun", "x", "vite"], {}) not in calls
    assert calls[-2:] == [("terminate",), ("wait",)]


def test_wait_listening_returns_none_once_the_port_accepts():
    holder = socket.socket()
    try:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]

        class Proc:
            def poll(self):
                return None

        assert cli._wait_listening(Proc(), port, timeout=2) is None
    finally:
        holder.close()


def test_wait_listening_times_out_naming_the_port():
    class Proc:
        def poll(self):
            return None

    port = cli._probe_free_port()
    problem = cli._wait_listening(Proc(), port, timeout=0.3)
    assert "did not listen" in problem
    assert str(port) in problem


def test_dash_port_rule_mirrors_port_ts():
    import inspect

    port_ts = (cli.DASHBOARD_DIR / "server" / "port.ts").read_text()
    assert f"export const OWNER_PORT = {cli.DASH_DEFAULT_PORT};" in port_ts
    assert '[ ["PORT", env.PORT], ["LIME_PORT", env.LIME_PORT]]'.replace(" ", "") in port_ts.replace(" ", "")
    assert '("PORT", "LIME_PORT")' in inspect.getsource(cli._dash_port)


# --- dash init/visit (ticket 218) ---


def test_dash_init_dispatches_to_home_and_skips_the_server(monkeypatch):
    monkeypatch.setattr(cli.home, "init_command", lambda: 7)

    def boom(_args):
        raise AssertionError("the server must not start for `dash init`")

    monkeypatch.setattr(cli, "_cmd_dash", boom)
    assert main(["dash", "init"]) == 7


def test_dash_init_refuses_inside_a_role_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LIME_AGENT_RUNTIME", "pi")
    assert main(["dash", "init"]) == 2
    assert "owner-only" in capsys.readouterr().err
    assert not (tmp_path / ".lime" / "dashboard").exists()


def test_dash_visit_dispatches_to_home(monkeypatch):
    monkeypatch.setattr(cli.home, "visit_command", lambda: 5)
    assert main(["dash", "visit"]) == 5


def test_dash_visit_before_init_names_init(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIME_AGENT_RUNTIME", raising=False)
    assert main(["dash", "visit"]) != 0
    assert "run lime dash init" in capsys.readouterr().err
    assert not (tmp_path / ".lime" / "dashboard").exists()


# --- run: launch a Run per ticket (ticket 28, spec §3, §8) ---

TICKET_TEXT = """# 41 — Toy Ticket

Workflow: feature
Status: open

## Ask
Do X.

## Where
`app.py`

## Done means
It works.

## Out of scope
n/a
"""

BLOCKED_TICKET_TEXT = TICKET_TEXT.replace(
    "Status: open", "Status: open\nBlocked by: .lime/tickets/99-ghost.md"
)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


def test_run_refuses_when_no_matching_ticket(tmp_path, monkeypatch, capsys):
    (tmp_path / ".lime" / "tickets").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert main(["run", "99"]) == 1
    assert "no ticket matching" in capsys.readouterr().err


def test_run_refuses_a_ticket_whose_blocker_still_exists(tmp_path, monkeypatch, capsys):
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "41-toy.md").write_text(BLOCKED_TICKET_TEXT)
    (tickets / "99-ghost.md").write_text("# 99 — Ghost\n")
    monkeypatch.chdir(tmp_path)
    assert main(["run", "41"]) == 1
    err = capsys.readouterr().err
    assert "blocked by" in err
    assert "99-ghost.md" in err


# --- run: missing Prototype refuses before any launch effect (ticket 168) ---

_MISSING_PROTO_TICKET = TICKET_TEXT.replace(
    "Status: open", "Status: open\nPrototype: app/proto-missing"
)


def test_run_refuses_missing_prototype_before_launch(tmp_path, monkeypatch, capsys):
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "41-toy.md").write_text(_MISSING_PROTO_TICKET)
    monkeypatch.chdir(tmp_path)

    def fail_if_called(*a, **kw):
        raise AssertionError("must not be reached")

    monkeypatch.setattr(cli.config, "load", fail_if_called)
    monkeypatch.setattr(cli.worktree, "create", fail_if_called)
    monkeypatch.setattr(subprocess, "Popen", fail_if_called)

    assert main(["run", "41"]) == 2
    err = capsys.readouterr().err
    assert "41-toy.md" in err
    assert "app/proto-missing" in err


def test_run_batch_refuses_missing_prototype_before_launch(tmp_path, monkeypatch, capsys):
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "41-toy.md").write_text(TICKET_TEXT)  # valid Where, no Prototype
    (tickets / "42-toy.md").write_text(
        _MISSING_PROTO_TICKET.replace("# 41 — Toy Ticket", "# 42 — Toy Ticket")
    )
    monkeypatch.chdir(tmp_path)

    launched = []

    def fail_if_called(*a, **kw):
        launched.append(a)
        raise AssertionError("must not be reached")

    monkeypatch.setattr(cli.config, "load", fail_if_called)
    monkeypatch.setattr(subprocess, "Popen", fail_if_called)

    assert main(["run", "41", "42"]) == 2
    err = capsys.readouterr().err
    assert "42-toy.md" in err
    assert "app/proto-missing" in err
    assert launched == []  # no prefix ticket launched and no scheduling


def test_check_reports_missing_prototype(green_environment, tmp_path, capsys):
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "41-toy.md").write_text(_MISSING_PROTO_TICKET)
    assert main(["check"]) == 1
    out = capsys.readouterr().out
    assert "41-toy.md" in out
    assert "app/proto-missing" in out
    assert "Prototype" in out


# --- run: ticket committed on main but not on origin is refused (ticket 59) ---


def test_run_refuses_a_ticket_committed_on_main_but_not_on_origin(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)
    tickets = root / ".lime" / "tickets"
    (tickets / "42-toy.md").write_text(TICKET_TEXT.replace("# 41", "# 42"))
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "ticket: 42-toy")
    # do NOT push

    def fail_if_called(*a, **kw):
        raise AssertionError("must not be reached")

    monkeypatch.setattr(cli.worktree, "create", fail_if_called)

    assert main(["run", "42"]) == 1
    err = capsys.readouterr().err
    assert ".lime/tickets/42-toy.md" in err
    assert "origin/main" in err
    assert "reset --soft" in err


def test_run_accepts_more_than_one_ticket_as_a_batch(tmp_path, monkeypatch):
    # more than one positional ticket arg dispatches to the batch path (ticket 32)
    monkeypatch.chdir(tmp_path)
    called = {}

    def fake_run_batch(tickets, profile, deploy=False, retry_of=None):
        called["tickets"] = tickets
        called["deploy"] = deploy
        called["retry_of"] = retry_of
        return 0

    monkeypatch.setattr(cli, "_run_batch", fake_run_batch)
    assert main(["run", "1", "2"]) == 0
    assert called["tickets"] == ["1", "2"]
    assert called["deploy"] is False
    assert called["retry_of"] is None


def _toy_project(root: Path, ticket_text: str = TICKET_TEXT, *, track_ticket: bool = True) -> None:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "app.py").write_text("v1\n")
    (root / ".gitignore").write_text(".lime/runs/\n")
    tickets = root / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "41-toy.md").write_text(ticket_text)
    # A real launch leaves the ticket untracked on the default branch; only the opt-in
    # `track_ticket=False` reproduces that. Every unrelated caller keeps the default.
    if track_ticket:
        _git(root, "add", "-A")
    else:
        _git(root, "add", "app.py", ".gitignore")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")


def _toy_project_with_origin(root: Path, origin: Path, *, track_ticket: bool = True) -> None:
    """`_toy_project` plus a bare origin it tracks — deploy's real shape."""
    origin.parent.mkdir(parents=True, exist_ok=True)
    _git(origin.parent, "init", "-q", "--bare", "-b", "main", str(origin))
    _toy_project(root, track_ticket=track_ticket)
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "-u", "origin", "main")
    _git(root, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def _rev(repo_path: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", ref],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _merge_on_origin(origin: Path, branch: str, scratch: Path) -> None:
    """What `gh pr merge --merge` does server-side: a real merge commit."""
    _git(scratch.parent, "clone", "-q", str(origin), str(scratch))
    _git(scratch, "config", "user.email", "t@t")
    _git(scratch, "config", "user.name", "t")
    _git(scratch, "merge", "--no-ff", "-m", f"Merge {branch}", f"origin/{branch}")
    _git(scratch, "push", "-q", "origin", "main")
    _git(scratch, "push", "-q", "origin", "--delete", branch)


def _fake_chain(app_text: str):
    """Returns a fake `feature.run` that writes app.py, commits, pushes, and marks success."""
    from lime import git_helper

    def fake_feature_run(run_, ticket, base_branch):
        Path("app.py").write_text(app_text)
        git_helper.commit_all(f"{ticket.slug}: work")
        (Path(".lime/tickets") / Path(ticket.path).name).unlink()
        git_helper.commit_all(f"lime({run_.run_id}): close ticket {ticket.number}")
        git_helper.push(git_helper.current_branch())
        run_.tracer.set_run_status(run_.run_id, "success")
        return 0

    return fake_feature_run


def test_run_creates_a_worktree_and_delegates_to_the_feature_chain(tmp_path, monkeypatch):
    # Clean case (ticket is tracked and identical, so no ticket commit is made).
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["cwd"] = Path.cwd()
        captured["ticket"] = ticket.slug
        captured["base_branch"] = base_branch
        row = run_.tracer.con.execute(
            "SELECT branch, worktree FROM runs WHERE run_id=?", (run_.run_id,)
        ).fetchone()
        captured["branch"] = row["branch"]
        captured["worktree"] = row["worktree"]
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "41"]) == 0
    assert captured["ticket"] == "41-toy"
    assert captured["base_branch"] == "main"
    assert captured["branch"] == "lime/feature/41-toy"
    assert captured["cwd"] == Path(captured["worktree"])
    assert captured["cwd"] != root  # the chain ran inside the worktree, not the project
    assert Path.cwd() == root  # cwd is restored afterward regardless of outcome


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["run", "41"], None),
        (["run", "--batch-id", "b7e1a2c3", "41"], "b7e1a2c3"),
    ],
)
def test_run_batch_id_flag_lands_on_the_child_runs_row(tmp_path, monkeypatch, argv, expected):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["batch_id"] = run_.tracer.con.execute(
            "SELECT batch_id FROM runs WHERE run_id=?", (run_.run_id,)
        ).fetchone()["batch_id"]
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(argv) == 0
    assert captured["batch_id"] == expected


def test_run_prints_preflight_warnings(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    monkeypatch.setattr(cli.feature, "run", lambda *a, **kw: 0)
    captured = {}

    def fake_preflight_warnings(raw, roles, profile=None):
        captured["roles"] = roles
        return ["reviewer: m takes text only"]

    monkeypatch.setattr(cli.config, "preflight_warnings", fake_preflight_warnings)

    assert main(["run", "41"]) == 0
    err = capsys.readouterr().err
    assert "warn  reviewer: m takes text only" in err
    assert captured["roles"] == list(cli.ROLES_FEATURE)


def test_run_prints_the_resolved_roster_and_stores_it_in_run_json(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    roster = []

    def fake_feature_run(run_, ticket, base_branch):
        nonlocal roster
        roster = run_.tracer.card(run_.run_id)["roster"]
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "41"]) == 0
    assert [r["name"] for r in roster] == list(cli.ROLES_FEATURE)
    builder_row = next(r for r in roster if r["name"] == "builder")
    assert builder_row == {"name": "builder", "runtime": "pi", "model": "openrouter/deepseek/deepseek-v4.1-flash", "thinking": "high"}
    out = capsys.readouterr().out
    assert ROSTER_BY_PROFILE[None]["builder"] in out


def test_run_allocates_a_port_and_exports_lime_env_vars(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["run_id"] = run_.run_id
        captured["LIME_RUN_ID"] = os.environ.get("LIME_RUN_ID")
        captured["LIME_PORT"] = os.environ.get("LIME_PORT")
        captured["LIME_DB_NAME"] = os.environ.get("LIME_DB_NAME")
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "41"]) == 0
    assert captured["LIME_RUN_ID"] == captured["run_id"]
    assert captured["LIME_PORT"] in ("3100", "3101", "3102")  # the template's runs.ports
    assert captured["LIME_DB_NAME"] == f"lime_{captured['run_id']}"


def _seed_live_port(tr: "cli.tracer.Tracer", run_id: str, port: int) -> None:
    tr.start_run(run_id, workflow="feature", request="x", port=port)
    tr.start_process(run_id, kind="run", name="lime", pid=os.getpid(), command="lime run x")


def test_run_refuses_and_cleans_up_when_every_port_is_held(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "new_id", lambda n=8: "beefcafe")

    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    for i, port in enumerate((3100, 3101, 3102)):
        _seed_live_port(tr, f"holder{i}", port)

    assert main(["run", "41"]) == 1
    err = capsys.readouterr().err
    assert "no free port" in err
    assert "holder0" in err and "holder1" in err and "holder2" in err

    assert not (tmp_path / "worktrees" / root.name / "beefcafe").exists()
    branches = subprocess.run(
        ["git", "-C", str(root), "branch", "--list", "lime/feature/41-toy"],
        capture_output=True, text=True,
    ).stdout
    assert branches.strip() == ""


def test_run_restores_cwd_and_reports_the_error_when_the_chain_raises(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    def fake_feature_run(run_, ticket, base_branch):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "41"]) == 1
    assert "boom" in capsys.readouterr().err
    assert Path.cwd() == root


def test_run_refuses_when_the_roster_does_not_validate(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: ["planner: boom"])

    def fail_if_called(*a, **kw):
        raise AssertionError("no Run spawns until validation is green (spec §5)")

    monkeypatch.setattr(cli.worktree, "create", fail_if_called)

    assert main(["run", "41"]) == 1
    assert "planner: boom" in capsys.readouterr().err


def test_run_refuses_a_hand_written_npm_extension_before_spawn(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    raw = yaml.safe_load(config.TEMPLATE.read_text())
    next(a for a in raw["agents"] if a["name"] == "builder")["harness_engineering"] = ["npm:x"]
    (root / ".lime" / "config.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "credential_failures", lambda agents: ([], []))
    monkeypatch.setattr(cli.config, "compaction_failures", lambda: [])
    monkeypatch.setattr(cli.config, "capability_failures", lambda agents: ([], []))

    def fail_if_called(*a, **kw):
        raise AssertionError("no Run spawns until validation is green (spec §5)")

    monkeypatch.setattr(cli.worktree, "create", fail_if_called)

    assert main(["run", "41"]) == 1
    assert "npm:x" in capsys.readouterr().err


def test_run_without_a_ticket_or_resume_refuses(capsys):
    assert main(["run"]) == 1
    assert "a ticket number is required" in capsys.readouterr().err


# --- run: carry an untracked ticket into the run branch (ticket 59) ---


def test_run_carries_an_untracked_ticket_into_the_run_branch(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _toy_project(root)  # no origin
    monkeypatch.chdir(root)
    tickets = root / ".lime" / "tickets"
    ticket_file = tickets / "42-toy.md"
    ticket_file.write_text(TICKET_TEXT.replace("# 41", "# 42"))  # never committed
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.worktree, "CC_PROJECTS", tmp_path / "cc-projects")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["cwd"] = Path.cwd()
        captured["ticket_exists"] = Path(".lime/tickets/42-toy.md").exists()
        log_s = subprocess.run(
            ["git", "log", "-1", "--format=%s"], capture_output=True, text=True, check=True
        ).stdout.strip()
        captured["log_s"] = log_s
        log_b = subprocess.run(
            ["git", "log", "-1", "--format=%b"], capture_output=True, text=True, check=True
        ).stdout.strip()
        captured["log_b"] = log_b
        status = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
        captured["status"] = status
        captured["run_id"] = run_.run_id
        run_.tracer.set_run_status(run_.run_id, "success")
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "42"]) == 0
    # In the worktree: ticket exists, commit matches, clean
    assert captured["ticket_exists"] is True
    assert captured["log_s"] == "ticket: 42-toy"
    assert f"Lime-Run: {captured['run_id']}" in captured["log_b"]
    assert "Lime-Role: orchestrator" in captured["log_b"]
    assert captured["status"] == ""
    # Project root untouched: 42-toy.md still untracked, main still at init
    root_status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--", ".lime/tickets/"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert "?? .lime/tickets/42-toy.md" in root_status
    root_log = subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%s", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert root_log == "init"


# --- run NN NN…: one process, port-cap concurrent batch (ticket 32, spec §3, §7) ---


class _FakeBatchPopen:
    """Fake subprocess.Popen that reports `poll() is not None` on its 2nd call —
    long enough for the batch loop to see it alive for at least one tick, so any
    slot overrun would show up as `live` exceeding the port cap."""

    live = 0
    peak = 0
    launched: list[str] = []

    def __init__(self, cmd, cwd=None):
        self.returncode = None
        self._polls = 0
        _FakeBatchPopen.launched.append(cmd[-1])
        _FakeBatchPopen.live += 1
        _FakeBatchPopen.peak = max(_FakeBatchPopen.peak, _FakeBatchPopen.live)

    def poll(self):
        self._polls += 1
        if self._polls >= 2 and self.returncode is None:
            self.returncode = 0
            _FakeBatchPopen.live -= 1
        return self.returncode


def _batch_project(root: Path, extra_tickets: int) -> None:
    _toy_project(root)
    tickets = root / ".lime" / "tickets"
    # Distinct Where per ticket, so an in-flight child's active-ticket snapshot never
    # collides with a sibling's — concurrency/scheduling tests need genuinely disjoint work.
    for n in range(41, 42 + extra_tickets):
        (tickets / f"{n}-toy.md").write_text(
            TICKET_TEXT.replace("# 41", f"# {n}").replace("`app.py`", f"`app{n}.py`")
        )


def _batch_ledger(root: Path):
    """`(batch rows, children)` read straight from a batch's own ledger (ticket 254)."""
    db = root / ".lime" / "runs" / "lime.db"
    if not db.exists():
        return [], []
    con = cli.tracer.connect(db, readonly=True)
    try:
        batch = [
            (row["run_id"], row["ended_at"])
            for row in con.execute(
                "SELECT run_id, ended_at FROM processes WHERE kind='batch'"
            )
        ]
        children = [
            tuple(row)
            for row in con.execute(
                "SELECT position, ticket, state, reason FROM batch_children ORDER BY position"
            )
        ]
    finally:
        con.close()
    return batch, children


def test_run_batch_never_exceeds_the_port_cap(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=3)  # 41..44, cap = 3 (the template's runs.ports)
    monkeypatch.chdir(root)
    _FakeBatchPopen.live = 0
    _FakeBatchPopen.peak = 0
    _FakeBatchPopen.launched = []
    _patch_overlap(monkeypatch, {})
    monkeypatch.setattr(cli.subprocess, "Popen", _FakeBatchPopen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42", "43", "44"]) == 0
    assert sorted(_FakeBatchPopen.launched) == ["41", "42", "43", "44"]
    assert _FakeBatchPopen.peak == 3  # concurrency actually reached the cap...
    assert _FakeBatchPopen.peak <= 3  # ...and never went past it


def test_run_batch_reports_a_nonzero_child_but_keeps_going(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)  # 41, 42
    monkeypatch.chdir(root)

    class _Popen:
        def __init__(self, cmd, cwd=None):
            self.returncode = 1 if cmd[-1] == "41" else 0

        def poll(self):
            return self.returncode

    _patch_overlap(monkeypatch, {})
    monkeypatch.setattr(cli.subprocess, "Popen", _Popen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42"]) == 1


def test_run_batch_skips_a_still_blocked_ticket_and_gives_up_on_it(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    tickets = root / ".lime" / "tickets"
    (tickets / "99-blocker.md").write_text("# 99 — Blocker\n")
    (tickets / "42-toy.md").write_text(
        TICKET_TEXT.replace("# 41", "# 42").replace(
            "Status: open", "Status: open\nBlocked by: .lime/tickets/99-blocker.md"
        )
    )
    monkeypatch.chdir(root)

    launched = []

    class _Popen:
        def __init__(self, cmd, cwd=None):
            launched.append(cmd[-1])
            self.returncode = 0

        def poll(self):
            return self.returncode

    _patch_overlap(monkeypatch, {})
    monkeypatch.setattr(cli.subprocess, "Popen", _Popen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42"]) == 1
    assert launched == ["41"]
    assert "still blocked" in capsys.readouterr().err
    _, children = _batch_ledger(root)
    assert children == [
        (0, "41-toy", "started", None),
        (1, "42-toy", "dropped", "gave up: blocked 99"),
    ]

def test_run_batch_command_reenters_python_dash_m_lime(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)
    monkeypatch.chdir(root)

    calls = []

    class _Popen:
        def __init__(self, cmd, cwd=None):
            calls.append((cmd, cwd))
            self.returncode = 0

        def poll(self):
            return self.returncode

    _patch_overlap(monkeypatch, {})
    monkeypatch.setattr(cli.subprocess, "Popen", _Popen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.sys, "executable", "/fake/python")

    assert main(["run", "41", "42", "--profile", "quality"]) == 0
    assert all(cmd[:3] == ["/fake/python", "-m", "lime"] for cmd, _ in calls)
    assert all(cwd == root for _, cwd in calls)
    assert all("--profile" in cmd and "quality" in cmd for cmd, _ in calls)
    batch_rows, _ = _batch_ledger(root)
    assert len(batch_rows) == 1
    bid = batch_rows[0][0]
    assert all(cmd[4:6] == ["--batch-id", bid] for cmd, _ in calls)
    assert all(cmd[6] in ("41", "42") for cmd, _ in calls)


def test_run_batch_starts_one_watchdog_with_its_flags(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)  # 41, 42
    monkeypatch.chdir(root)
    _patch_overlap(monkeypatch, {})
    _ReadyPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _ReadyPopen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    calls = []

    def record(self, **kw):
        calls.append((self.batch_id, self.retry_of, kw))
        return None

    monkeypatch.setattr(cli.batch.Batch, "start_watchdog", record)

    assert main(["run", "41", "42", "--profile", "quality"]) == 0
    batch_rows, _ = _batch_ledger(root)
    assert len(batch_rows) == 1
    assert calls == [(batch_rows[0][0], None, {"deploy": False, "profile": "quality"})]


def test_batch_watch_dispatches_to_the_watchdog(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    recorded = {}

    def fake(project_root, batch_id, *, deploy=False, profile=None, stream=None):
        recorded["call"] = (project_root, batch_id, deploy, profile)
        return 0

    monkeypatch.setattr(cli.batch, "watch", fake)
    assert main(["batch", "watch", "abcd1234", "--deploy", "--profile", "q"]) == 0
    assert recorded["call"] == (tmp_path.resolve(), "abcd1234", True, "q")


def test_run_batch_records_its_queue_in_batch_children(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)  # 41, 42
    monkeypatch.chdir(root)
    _patch_overlap(monkeypatch, {})
    _ReadyPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _ReadyPopen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "nope", "42"]) == 0
    assert sorted(_ReadyPopen.launched) == ["41", "42"]
    batch_rows, children = _batch_ledger(root)
    assert children == [
        (0, "41-toy", "started", None),
        (1, "nope", "dropped", "no ticket file"),
        (2, "42-toy", "started", None),
    ]
    assert len(batch_rows) == 1
    assert batch_rows[0][1] is not None  # closed at the normal end


@pytest.mark.parametrize("fail_first", [2, 10**6])
def test_run_batch_launches_every_ticket_when_the_mirror_write_fails(
    tmp_path, monkeypatch, caplog, fail_first
):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=2)  # 41, 42, 43
    monkeypatch.chdir(root)
    _patch_overlap(monkeypatch, {})
    _FakeBatchPopen.live = 0
    _FakeBatchPopen.peak = 0
    _FakeBatchPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _FakeBatchPopen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    real_write = cli.batch.Batch._write
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] <= fail_first:
            raise sqlite3.OperationalError("database is locked")
        return real_write(self)

    monkeypatch.setattr(cli.batch.Batch, "_write", flaky)

    with caplog.at_level(logging.WARNING):
        assert main(["run", "41", "42", "43"]) == 0
    assert "mirror write failed" in caplog.text
    assert sorted(_FakeBatchPopen.launched) == ["41", "42", "43"]

    batch_rows, children = _batch_ledger(root)
    if fail_first == 2:
        # The retry caught up: every child mirrors as started and the row is closed.
        assert [(c[0], c[1], c[2]) for c in children] == [
            (0, "41-toy", "started"),
            (1, "42-toy", "started"),
            (2, "43-toy", "started"),
        ]
        assert len(batch_rows) == 1
        assert batch_rows[0][1] is not None
    else:
        assert batch_rows == []


# --- run --deploy: deploy each green link (ticket 199) ---


@pytest.mark.parametrize(
    "config_text",
    [
        None,  # no .lime/config.yaml → template fallback (auto_deploy: off)
        "auto_deploy: off\n",
        "auto_deploy: 'off'\n",
        "runs:\n  ports: 3100-3102\n",  # key missing
        "auto_deploy: yes\n",  # invalid (bare yes loads as True)
    ],
)
def test_run_deploy_refused_unless_auto_deploy_is_on(
    tmp_path, monkeypatch, capsys, config_text
):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)  # 41, 42
    if config_text is not None:
        (root / ".lime" / "config.yaml").write_text(config_text)
    monkeypatch.chdir(root)

    def fail_if_called(*a, **kw):
        raise AssertionError("--deploy must refuse before anything launches")

    monkeypatch.setattr(cli.subprocess, "Popen", fail_if_called)
    monkeypatch.setattr(cli, "_cmd_deploy", fail_if_called)

    assert main(["run", "41", "42", "--deploy"]) == 2
    assert "auto_deploy" in capsys.readouterr().err


def test_run_deploy_with_one_ticket_takes_the_batch_path(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=0)  # 41 only
    (root / ".lime" / "config.yaml").write_text("auto_deploy: staging\n")
    monkeypatch.chdir(root)
    recorded = {}

    def fake_run_batch(tickets, profile, deploy=False, retry_of=None):
        recorded["args"] = (tickets, profile, deploy)
        return 0

    def fail_if_called(*a, **kw):
        raise AssertionError("a single --deploy ticket must take the batch path")

    monkeypatch.setattr(cli, "_run_batch", fake_run_batch)
    monkeypatch.setattr(cli, "_run_ticket", fail_if_called)

    assert main(["run", "41", "--deploy"]) == 0
    assert recorded["args"] == (["41"], None, True)


def test_run_retry_of_takes_the_batch_path_and_marks_its_row(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=0)  # 41 only
    monkeypatch.chdir(root)
    _patch_overlap(monkeypatch, {})
    _ReadyPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _ReadyPopen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    def fail_if_called(*a, **kw):
        raise AssertionError("--retry-of must take the batch path")

    monkeypatch.setattr(cli, "_run_ticket", fail_if_called)
    # The real watchdog spawn: the retry batch's own call must be a no-op, so no
    # extra child is launched (only `_ReadyPopen.launched == ["41"]` below).
    monkeypatch.setattr(cli.batch.Batch, "start_watchdog", _REAL_START_WATCHDOG)

    assert main(["run", "41", "--retry-of", "deadbeef"]) == 0
    assert _ReadyPopen.launched == ["41"]
    assert "retry of dead batch deadbeef" in capsys.readouterr().out

    db = root / ".lime" / "runs" / "lime.db"
    con = cli.tracer.connect(db, readonly=True)
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM processes WHERE kind='batch'")]
    finally:
        con.close()
    assert len(rows) == 1
    assert rows[0]["retry_of"] == "deadbeef"
    assert rows[0]["ended_at"] is not None


def test_run_deploy_rejected_with_resume(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["run", "--resume", "abc", "--deploy"]) == 2
    assert "--deploy" in capsys.readouterr().err


def test_run_deploy_rejected_with_allow_overlap(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["run", "41", "--deploy", "--allow-overlap"]) == 2
    assert "--allow-overlap" in capsys.readouterr().err


def _success_run(root: Path, worktrees: Path) -> None:
    """A success Run row (branch `lime/feature/41-toy`) for `_await_ci` to find."""
    _seed_failed_run(
        root, worktrees, status="success",
        ticket_path=root / ".lime" / "tickets" / "41-toy.md",
    )


def test_await_ci_returns_green_without_waiting(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    _success_run(root, tmp_path / "worktrees")
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    sleeps: list[int] = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))

    assert cli._await_ci(root, "41") == "green"
    assert sleeps == []
    assert "waiting for CI on lime/feature/41-toy" in capsys.readouterr().out


def test_await_ci_waits_while_pending_then_returns_green(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    _success_run(root, tmp_path / "worktrees")
    views = [
        {"statusCheckRollup": [{"conclusion": "PENDING"}]},
        {"statusCheckRollup": [{"conclusion": "PENDING"}]},
        {"statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    ]
    calls: list[str] = []

    def fake_pr_view(branch):
        calls.append(branch)
        return views[len(calls) - 1]

    monkeypatch.setattr(cli.git_helper, "pr_view", fake_pr_view)
    sleeps: list[int] = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))

    assert cli._await_ci(root, "41") == "green"
    assert len(calls) == 3
    assert sleeps == [cli.DEPLOY_CI_POLL_S, cli.DEPLOY_CI_POLL_S]


def test_await_ci_returns_red_at_once(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    _success_run(root, tmp_path / "worktrees")
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"statusCheckRollup": [{"conclusion": "FAILURE"}]},
    )
    sleeps: list[int] = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))

    assert cli._await_ci(root, "41") == "red"
    assert sleeps == []


def test_await_ci_gives_up_pending_at_the_ceiling(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    _success_run(root, tmp_path / "worktrees")
    monkeypatch.setattr(cli, "DEPLOY_CI_CEILING_S", 90)
    monkeypatch.setattr(cli, "DEPLOY_CI_POLL_S", 30)
    views: list[str] = []

    def fake_pr_view(branch):
        views.append(branch)
        return {"statusCheckRollup": [{"conclusion": "PENDING"}]}

    monkeypatch.setattr(cli.git_helper, "pr_view", fake_pr_view)
    sleeps: list[int] = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))

    assert cli._await_ci(root, "41") == "pending"
    assert len(views) == 4
    assert sleeps == [30, 30, 30]


def test_await_ci_unknown_without_a_success_run_or_on_gh_error(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    # no trace db at all
    assert cli._await_ci(root, "41") == "unknown"

    # a non-success Run must not even reach `gh pr view`
    _seed_failed_run(
        root, tmp_path / "worktrees", status="fail",
        ticket_path=root / ".lime" / "tickets" / "41-toy.md",
    )

    def fail_if_called(*a, **kw):
        raise AssertionError("a non-success Run must not call pr_view")

    monkeypatch.setattr(cli.git_helper, "pr_view", fail_if_called)
    assert cli._await_ci(root, "41") == "unknown"

    # a success Run whose `gh pr view` errors ends the wait at once
    root2 = tmp_path / "project2"
    root2.mkdir()
    _success_run(root2, tmp_path / "worktrees2")

    def raise_runtime(branch):
        raise RuntimeError("gh pr view failed")

    monkeypatch.setattr(cli.git_helper, "pr_view", raise_runtime)
    assert cli._await_ci(root2, "41") == "unknown"


def test_await_ci_expecting_checks_reads_an_empty_rollup_as_pending(
    tmp_path, monkeypatch
):
    """Ticket 276: on a just-pushed head an empty rollup is `pending`, never `green`."""
    root = tmp_path / "project"
    root.mkdir()
    _success_run(root, tmp_path / "worktrees")
    views = [
        {"statusCheckRollup": []},
        {"statusCheckRollup": []},
        {"statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    ]
    calls: list[str] = []

    def fake_pr_view(branch):
        calls.append(branch)
        return views[len(calls) - 1]

    monkeypatch.setattr(cli.git_helper, "pr_view", fake_pr_view)
    sleeps: list[int] = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))

    assert cli._await_ci(root, "41", checks_expected=True) == "green"
    assert len(calls) == 3
    assert sleeps == [cli.DEPLOY_CI_POLL_S] * 2

    # An all-empty rollup never settles: the fixed ceiling returns "pending".
    monkeypatch.setattr(cli, "DEPLOY_CI_CEILING_S", 90)
    monkeypatch.setattr(cli, "DEPLOY_CI_POLL_S", 30)
    empty: list[str] = []

    def always_empty(branch):
        empty.append(branch)
        return {"statusCheckRollup": []}

    monkeypatch.setattr(cli.git_helper, "pr_view", always_empty)
    sleeps.clear()
    assert cli._await_ci(root, "41", checks_expected=True) == "pending"
    assert len(empty) == 4
    assert sleeps == [30, 30, 30]

    # The default call still reads an empty rollup as green, with no sleep.
    sleeps.clear()
    assert cli._await_ci(root, "41") == "green"
    assert sleeps == []


def _launch_lock_held(root: Path) -> bool:
    """True while `root`'s launch lock is held by another open file description."""
    path = root / ".lime" / "runs" / "launch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def test_launch_lock_is_exclusive_and_released(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    with cli._launch_lock(root):
        assert _launch_lock_held(root) is True
        assert (root / ".lime" / "runs" / "launch.lock").exists()
    assert _launch_lock_held(root) is False
    with pytest.raises(RuntimeError):
        with cli._launch_lock(root):
            raise RuntimeError("boom")
    assert _launch_lock_held(root) is False


def test_run_ticket_holds_the_launch_lock_around_every_launch_fetch(
    tmp_path, monkeypatch
):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    monkeypatch.setattr(cli.git_helper, "is_repo", lambda *a, **kw: True)

    probes: dict[str, bool] = {}

    def probe(name, result):
        probes[name] = _launch_lock_held(root)
        return result

    monkeypatch.setattr(
        cli.git_helper, "unpushed_commits",
        lambda *a, **kw: probe("unpushed_commits", []),
    )
    monkeypatch.setattr(
        cli.overlap, "live_overlap", lambda *a, **kw: probe("live_overlap", {})
    )
    monkeypatch.setattr(
        cli.numbering, "reserve", lambda *a, **kw: probe("reserve", None)
    )

    def fake_create(*a, **kw):
        probes["create"] = _launch_lock_held(root)
        raise cli.worktree.WorktreeError("stop")

    monkeypatch.setattr(cli.worktree, "create", fake_create)

    assert main(["run", "41"]) == 1
    assert probes == {
        "unpushed_commits": True,
        "live_overlap": True,
        "reserve": True,
        "create": True,
    }
    assert _launch_lock_held(root) is False


def test_run_ticket_where_and_blocker_refusals_take_no_lock(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _toy_project(root)
    tickets = root / ".lime" / "tickets"
    (tickets / "99-blocker.md").write_text("# 99 — Blocker\n")
    (tickets / "41-toy.md").write_text(
        TICKET_TEXT.replace(
            "Status: open", "Status: open\nBlocked by: .lime/tickets/99-blocker.md"
        )
    )
    monkeypatch.chdir(root)

    assert main(["run", "41"]) == 1
    assert (root / ".lime" / "runs" / "launch.lock").exists() is False


# --- run --deploy batch: deploy each green link (ticket 199, step 5) ---


def _deploy_batch_project(tmp_path: Path, deploy_line: str = "deploy: []") -> Path:
    """41 (untracked), 42 (`Blocked by:` 41), 43 (independent) + a --deploy-able config."""
    root = tmp_path / "project"
    _toy_project(root, track_ticket=False)  # 41-toy.md is left untracked, like a real launch
    tickets = root / ".lime" / "tickets"
    (tickets / "42-toy.md").write_text(
        TICKET_TEXT.replace("# 41", "# 42")
        .replace("`app.py`", "`app42.py`")
        .replace("Status: open", "Status: open\nBlocked by: .lime/tickets/41-toy.md")
    )
    (tickets / "43-toy.md").write_text(
        TICKET_TEXT.replace("# 41", "# 43").replace("`app.py`", "`app43.py`")
    )
    (root / ".lime" / "config.yaml").write_text(
        "auto_deploy: staging\n"
        f"{deploy_line}\n"
        "runs:\n  ports: 3100-3102\n"
        'commands:\n  test: "true"\n'
    )
    return root


def _make_deploy_batch_popen(events, returncodes, tmp_path):
    """Fake child `lime run`: records its launch, seeds a success Run row for the
    ticket (so `_await_ci`/`_cmd_deploy` find it), and exits with a chosen code."""

    class _Popen:
        def __init__(self, cmd, cwd=None):
            arg = cmd[-1]
            assert "--deploy" not in cmd, "a child must never gain --deploy"
            events.append(("launch", arg))
            root = Path(cwd).resolve()
            ticket_path = cli.tracker.find(root, arg)
            tr = cli.tracer.Tracer(root / ".lime" / "runs")
            tr.start_run(
                f"r{arg}", workflow="feature", request="x", ticket=str(ticket_path),
                branch=f"lime/feature/{ticket_path.stem}",
                worktree=str(tmp_path / "wt" / arg),
            )
            tr.set_run_status(f"r{arg}", "success")
            self.returncode = returncodes.get(arg, 0)

        def poll(self):
            return self.returncode

    return _Popen


def _patch_deploy_real_path(
    monkeypatch, events, *, ci_red=None, gate_red=None, merge_refused=None, ff_refused=None
):
    """Branch-aware fakes for the real `_cmd_deploy` path: PR open + CI green, the
    merged-tree gate, merge, and fast-forward — each optionally red for one branch."""
    current = {"branch": None}

    def fake_pr_view(branch):
        conclusion = "FAILURE" if branch == ci_red else "SUCCESS"
        return {"state": "OPEN", "statusCheckRollup": [{"conclusion": conclusion}]}

    def fake_gate(project_root, run_id, base_sha, branch, commands, *, excluded_ports=(), ticket=None, **kw):
        events.append(("gate", branch))
        return 1 if branch == gate_red else 0

    def fake_merge_pr(branch):
        events.append(("merge", branch))
        current["branch"] = branch
        if branch == merge_refused:
            raise RuntimeError("merge refused")

    def fake_fast_forward(base):
        if current["branch"] == ff_refused:
            raise RuntimeError("fast-forward refused")
        return "abc1234"

    monkeypatch.setattr(cli.git_helper, "pr_view", fake_pr_view)
    monkeypatch.setattr(cli, "_check_deploy_merged_tree", fake_gate)
    monkeypatch.setattr(cli.git_helper, "merge_pr", fake_merge_pr)
    monkeypatch.setattr(cli.git_helper, "fast_forward", fake_fast_forward)
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: "untracked")
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])
    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: [])
    monkeypatch.setattr(
        cli, "_run_deploy_session",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no deploy session")),
    )


def test_run_deploy_batch_deploys_through_lime_deploy_and_launches_the_dependent(
    tmp_path, monkeypatch, capsys
):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    _patch_deploy_real_path(monkeypatch, events)
    monkeypatch.setattr(
        cli.subprocess, "Popen", _make_deploy_batch_popen(events, {}, tmp_path)
    )
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})

    assert main(["run", "41", "42", "--deploy"]) == 0
    assert events == [
        ("launch", "41"), ("gate", "lime/feature/41-toy"), ("merge", "lime/feature/41-toy"),
        ("launch", "42"), ("gate", "lime/feature/42-toy"), ("merge", "lime/feature/42-toy"),
    ]
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    statuses = {r["run_id"]: r["status"] for r in con.execute("SELECT run_id, status FROM runs")}
    con.close()
    assert statuses == {"r41": "deployed", "r42": "deployed"}
    assert not (root / ".lime" / "tickets" / "41-toy.md").exists()
    assert not (root / ".lime" / "tickets" / "42-toy.md").exists()
    assert "still blocked" not in capsys.readouterr().err


@pytest.mark.parametrize("case", ["ci_red", "merged_tree_red", "merge_refused", "ff_refused"])
def test_run_deploy_batch_red_deploy_step_holds_dependents_while_independents_go_on(
    tmp_path, monkeypatch, capsys, case
):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    kwargs = {
        "ci_red": {"ci_red": "lime/feature/41-toy"},
        "merged_tree_red": {"gate_red": "lime/feature/41-toy"},
        "merge_refused": {"merge_refused": "lime/feature/41-toy"},
        "ff_refused": {"ff_refused": "lime/feature/41-toy"},
    }[case]
    _patch_deploy_real_path(monkeypatch, events, **kwargs)
    monkeypatch.setattr(
        cli.subprocess, "Popen", _make_deploy_batch_popen(events, {}, tmp_path)
    )
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})

    assert main(["run", "41", "42", "43", "--deploy"]) == 1
    assert ("launch", "42") not in events
    assert ("launch", "43") in events
    assert ("merge", "lime/feature/43-toy") in events
    assert (root / ".lime" / "tickets" / "41-toy.md").exists()
    out, err = capsys.readouterr()
    assert "still blocked" in err
    assert "41" in err
    if case == "ff_refused":
        assert "fast-forward" in err
        assert (
            "  failed: 41 — resume: git pull --ff-only, then delete"
            " .lime/tickets/41-toy.md"
        ) in out
    else:
        assert "  failed: 41 — resume: lime deploy 41" in out


@pytest.mark.parametrize(
    "case", ["run_fail", "ci_pending_at_ceiling", "deploy_exit_1", "deploy_raises"]
)
def test_run_deploy_batch_red_seam_steps_hold_dependents(
    tmp_path, monkeypatch, capsys, case
):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    returncodes = {"41": 1} if case == "run_fail" else {}
    monkeypatch.setattr(
        cli.subprocess, "Popen", _make_deploy_batch_popen(events, returncodes, tmp_path)
    )
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})

    if case == "ci_pending_at_ceiling":
        monkeypatch.setattr(
            cli, "_await_ci", lambda root_, arg: "pending" if arg == "41" else "green"
        )
    else:
        monkeypatch.setattr(cli, "_await_ci", lambda root_, arg: "green")

    def fail_if_41(args):
        assert args.ticket != "41", f"_cmd_deploy must not be called for 41 ({case})"

    def fake_cmd_deploy(args):
        fail_if_41(args)
        if args.ticket == "43":
            (root / ".lime" / "tickets" / "43-toy.md").unlink()
            return 0
        if case == "deploy_exit_1":
            return 1
        raise RuntimeError("deploy crashed")

    monkeypatch.setattr(cli, "_cmd_deploy", fake_cmd_deploy)

    assert main(["run", "41", "42", "43", "--deploy"]) == 1
    assert ("launch", "42") not in events
    assert ("launch", "43") in events
    out, err = capsys.readouterr()
    assert "41" in (out + err)


def test_run_deploy_batch_stops_at_a_link_when_deploy_lists_commands(
    tmp_path, monkeypatch, capsys
):
    root = _deploy_batch_project(tmp_path, deploy_line="deploy:\n  - vercel deploy --prod")
    monkeypatch.chdir(root)
    events: list[tuple] = []
    monkeypatch.setattr(
        cli.subprocess, "Popen", _make_deploy_batch_popen(events, {}, tmp_path)
    )
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})

    def fail_if_called(*a, **kw):
        raise AssertionError("a non-empty deploy: must stop before CI/deploy")

    monkeypatch.setattr(cli, "_cmd_deploy", fail_if_called)
    monkeypatch.setattr(cli, "_await_ci", fail_if_called)
    monkeypatch.setattr(cli, "_run_deploy_session", fail_if_called)

    assert main(["run", "41", "42", "--deploy"]) == 1
    assert ("launch", "42") not in events
    err = capsys.readouterr().err
    assert "deploy:" in err
    assert "lime deploy 41" in err


def test_run_deploy_batch_deploys_under_the_launch_lock_and_waits_for_ci_outside_it(
    tmp_path, monkeypatch
):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    monkeypatch.setattr(
        cli.subprocess, "Popen", _make_deploy_batch_popen(events, {}, tmp_path)
    )
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})

    def fake_await_ci(root_, arg):
        events.append(("await_ci", _launch_lock_held(root_)))
        return "green"

    def fake_cmd_deploy(args):
        events.append(("deploy", _launch_lock_held(root)))
        (root / ".lime" / "tickets" / "41-toy.md").unlink()
        return 0

    monkeypatch.setattr(cli, "_await_ci", fake_await_ci)
    monkeypatch.setattr(cli, "_cmd_deploy", fake_cmd_deploy)

    assert main(["run", "41", "--deploy"]) == 0
    assert ("await_ci", False) in events
    assert ("deploy", True) in events


def test_batch_deploy_reaches_merge_ff_and_status_only_through_cmd_deploy():
    """The `--deploy` batch never merges, fast-forwards, tears down or writes a status
    itself: it reaches those only through the unchanged `_cmd_deploy`."""
    forbidden = {
        "merge_pr", "fast_forward", "set_run_status", "_check_deploy_merged_tree",
        "_run_deploy_session", "teardown", "unlink",
    }
    cli_path = _REPO_ROOT / "lime" / "cli.py"
    for name in (
        "_run_batch",
        "_deploy_link",
        "_await_ci",
        "_refusal_mark",
        "_docs_merge_ci_pending",
        "_child_resume",
        "_retry_fate",
        "_print_deploy_report",
    ):
        func = _ast_func_source(cli_path, name)
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            if isinstance(callee, ast.Attribute):
                called = callee.attr
            elif isinstance(callee, ast.Name):
                called = callee.id
            else:
                continue
            assert called not in forbidden, f"{name} must not call {called} directly"
    assert _run_check_calls(_ast_func_source(cli_path, "_deploy_link"), "_cmd_deploy")


def test_batch_ci_rewait_reads_the_refusal_ledger_and_never_writes_it():
    """Ticket 276: the re-wait only reads the refusal ledger — no writer, and every
    `connect(...)` it makes is `readonly=True`."""
    forbidden = {"_record_refusal", "refusal", "event", "Tracer", "push_lease", "set_pr"}
    cli_path = _REPO_ROOT / "lime" / "cli.py"
    for name in ("_deploy_link", "_await_ci", "_refusal_mark", "_docs_merge_ci_pending"):
        func = _ast_func_source(cli_path, name)
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            if isinstance(callee, ast.Attribute):
                called = callee.attr
            elif isinstance(callee, ast.Name):
                called = callee.id
            else:
                continue
            assert called not in forbidden, f"{name} must not call {called}"
    for name in ("_await_ci", "_refusal_mark", "_docs_merge_ci_pending"):
        connects = _run_check_calls(_ast_func_source(cli_path, name), "connect")
        assert connects, f"{name} must read the ledger"
        for call in connects:
            assert any(
                kw.arg == "readonly"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in call.keywords
            ), f"{name} must connect with readonly=True"


@pytest.mark.parametrize("second", ["green", "pending"])
def test_run_deploy_batch_launches_the_dependent_after_ci_on_the_docs_merge(
    tmp_path, monkeypatch, capsys, second
):
    """Ticket 276: a link refused only for `docs_merge_ci_pending` waits once more for
    that CI outside the lock and deploys again; a second `pending` holds the dependent."""
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    monkeypatch.setattr(
        cli.subprocess, "Popen", _make_deploy_batch_popen(events, {}, tmp_path)
    )
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})

    def fake_await_ci(root_, arg, **kw):
        events.append(
            ("await_ci", arg, kw.get("checks_expected", False), _launch_lock_held(root_))
        )
        return second if kw.get("checks_expected") else "green"

    def fake_cmd_deploy(args):
        events.append(("deploy", args.ticket, _launch_lock_held(root)))
        first_41 = args.ticket == "41" and not any(
            event[:2] == ("deploy", "41") for event in events[:-1]
        )
        if first_41:
            cli._record_refusal(
                root, "41-toy", "docs_merge_ci_pending", run_id="r41", sha="abc"
            )
            return 1
        (root / ".lime" / "tickets" / f"{args.ticket}-toy.md").unlink()
        return 0

    monkeypatch.setattr(cli, "_await_ci", fake_await_ci)
    monkeypatch.setattr(cli, "_cmd_deploy", fake_cmd_deploy)

    code = main(["run", "41", "42", "--deploy"])
    out = capsys.readouterr().out
    deploys_41 = [i for i, event in enumerate(events) if event[:2] == ("deploy", "41")]

    if second == "green":
        assert code == 0
        assert ("await_ci", "41", True, False) in events
        assert len(deploys_41) == 2
        assert (
            events.index(("await_ci", "41", True, False))
            < deploys_41[1]
            < events.index(("launch", "42"))
        )
    else:
        assert code == 1
        assert ("launch", "42") not in events
        assert len(deploys_41) == 1
        assert "  failed: 41 — resume: lime deploy 41" in out


@pytest.mark.parametrize("target", ["staging", "prod"])
def test_run_deploy_launch_line_and_report_name_the_target(
    tmp_path, monkeypatch, capsys, target
):
    root = _deploy_batch_project(tmp_path)
    config_path = root / ".lime" / "config.yaml"
    config_path.write_text(
        config_path.read_text().replace("auto_deploy: staging", f"auto_deploy: {target}")
    )
    monkeypatch.chdir(root)
    events: list[tuple] = []
    _patch_deploy_real_path(monkeypatch, events)
    monkeypatch.setattr(
        cli.subprocess, "Popen", _make_deploy_batch_popen(events, {}, tmp_path)
    )
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})

    assert main(["run", "41", "42", "--deploy"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert (
        f"lime run: --deploy on — target {target}: each green link merges unattended"
        in lines
    )
    headers = [line for line in lines if "--deploy report" in line]
    assert len(headers) == 1
    assert f"(target: {target})" in headers[0]
    assert "  deployed: 41" in lines
    assert "  deployed: 42" in lines


def test_run_deploy_batch_ends_with_one_report_deployed_failed_held(
    tmp_path, monkeypatch, capsys
):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    monkeypatch.setattr(
        cli.subprocess, "Popen", _make_deploy_batch_popen(events, {"41": 1}, tmp_path)
    )
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})
    monkeypatch.setattr(cli, "_await_ci", lambda root_, arg: "green")

    def fake_cmd_deploy(args):
        assert args.ticket == "43", "only the green independent link may deploy"
        (root / ".lime" / "tickets" / "43-toy.md").unlink()
        return 0

    monkeypatch.setattr(cli, "_cmd_deploy", fake_cmd_deploy)

    assert main(["run", "41", "42", "43", "--deploy"]) == 1
    out, err = capsys.readouterr()
    assert out.count("--deploy report") == 1
    assert "1 deployed · 1 failed · 1 held" in out
    lines = out.splitlines()
    assert "  deployed: 43" in lines
    assert "  failed: 41 — resume: lime run --resume r41" in lines
    assert "  held: 42 — blocker: .lime/tickets/41-toy.md" in lines
    header = next(i for i, line in enumerate(lines) if "--deploy report" in line)
    finished = [i for i, line in enumerate(lines) if "finished (exit" in line]
    assert finished
    assert header > max(finished)
    assert "still blocked" in err


@pytest.mark.parametrize("case", ["deploy_refused", "no_fresh_run_row"])
def test_run_deploy_report_names_deploy_and_relaunch_commands(
    tmp_path, monkeypatch, capsys, case
):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})

    if case == "deploy_refused":
        monkeypatch.setattr(
            cli.subprocess, "Popen", _make_deploy_batch_popen(events, {}, tmp_path)
        )
        monkeypatch.setattr(cli, "_await_ci", lambda root_, arg: "pending")
    else:
        tr = cli.tracer.Tracer(root / ".lime" / "runs")
        tr.start_run(
            "old41",
            workflow="feature",
            request="x",
            ticket=str(root / ".lime" / "tickets" / "41-toy.md"),
            branch="lime/feature/41-toy",
            worktree=str(tmp_path / "wt" / "41"),
        )
        con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db")
        con.execute(
            "UPDATE runs SET started_at=? WHERE run_id='old41'",
            ((datetime.now(UTC) - timedelta(days=1)).isoformat(),),
        )
        con.commit()
        con.close()

        class _NoSeedPopen:
            def __init__(self, cmd, cwd=None):
                self.returncode = 2

            def poll(self):
                return self.returncode

        monkeypatch.setattr(cli.subprocess, "Popen", _NoSeedPopen)

    assert main(["run", "41", "--deploy"]) == 1
    out = capsys.readouterr().out
    if case == "deploy_refused":
        assert "  failed: 41 — resume: lime deploy 41" in out
    else:
        assert "  failed: 41 — resume: lime run 41" in out
        assert "old41" not in out


def test_run_deploy_report_survives_an_unreadable_trace_db(
    tmp_path, monkeypatch, capsys
):
    """An unreadable trace db must not abort the report — the child falls back to a
    plain relaunch command instead of raising out of `_child_resume`."""
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})
    runs = root / ".lime" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "lime.db").mkdir()  # exists() is True; readonly connect raises sqlite3.Error

    class _NoSeedPopen:
        def __init__(self, cmd, cwd=None):
            self.returncode = 2

        def poll(self):
            return self.returncode

    monkeypatch.setattr(cli.subprocess, "Popen", _NoSeedPopen)

    assert main(["run", "41", "--deploy"]) == 1
    out = capsys.readouterr().out
    assert "  failed: 41 — resume: lime run 41" in out
    assert "--deploy report" in out


def test_run_deploy_report_holds_overlap_and_missing_tickets(
    tmp_path, monkeypatch, capsys
):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    _patch_deploy_real_path(monkeypatch, events)
    monkeypatch.setattr(
        cli.subprocess, "Popen", _make_deploy_batch_popen(events, {}, tmp_path)
    )
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {"43-toy": {"lime/feature/70-x": ["app43.py"]}})

    assert main(["run", "41", "43", "99", "--deploy"]) == 1
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "  held: 43 — blocker: overlap with lime/feature/70-x" in lines
    assert "  held: 99 — blocker: no ticket file in .lime/tickets/" in lines
    assert "  deployed: 41" in lines


# --- runs [--all] / runs rm: list Runs / discard one (spec §3, §8) ---


def test_runs_lists_only_live_runs_by_default(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("r1", workflow="feature", request="x", port=3100)
    tr.start_run("r2", workflow="feature", request="y", port=3101)
    tr.set_run_status("r2", "success")
    tr.set_run_status("r2", "deployed")

    assert main(["runs"]) == 0
    out = capsys.readouterr().out
    assert "r1" in out
    assert "r2" not in out

    assert main(["runs", "--all"]) == 0
    out = capsys.readouterr().out
    assert "r1" in out and "r2" in out


@pytest.mark.parametrize("delta,text", [
    (timedelta(seconds=45), "45s"),
    (timedelta(minutes=34), "34m"),
    (timedelta(hours=2, minutes=5), "2h"),
    (timedelta(days=3), "3d"),
])
def test_age_formats_the_largest_whole_unit(delta, text):
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    assert cli._age((now - delta).isoformat(), now) == text


def test_age_is_empty_without_a_timestamp():
    assert cli._age(None, datetime.now(UTC)) == ""


def test_runs_last_column_is_the_age_of_the_newest_event(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("r1", workflow="feature", request="x", port=3100)
    tr.event("r1", "log", name="old")
    tr.event("r1", "log", name="newest")
    now = datetime.now(UTC)
    tr.con.execute("UPDATE events SET started_at=? WHERE name=?",
                   ((now - timedelta(minutes=120)).isoformat(), "old"))
    tr.con.execute("UPDATE events SET started_at=? WHERE name=?",
                   ((now - timedelta(minutes=34)).isoformat(), "newest"))
    tr.con.commit()
    tr.start_run("r2", workflow="feature", request="y", port=3101)

    assert main(["runs"]) == 0
    out = capsys.readouterr().out
    header, *body = out.splitlines()
    col = header.index("last")
    by_id = {l.split()[0]: l for l in body}
    assert by_id["r1"][col:].strip() == "34m"
    assert by_id["r2"][col:].strip() == ""


def test_runs_size_column_shows_the_last_measured_added_lines(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("r1", workflow="feature", request="x", port=3100)
    tr.start_run("r2", workflow="feature", request="y", port=3101)
    tr.start_run("r3", workflow="feature", request="z", port=3102)
    tr.event("r1", "log", name="verify_1",
             payload={"total_added": 120, "max_added_lines": 100})
    tr.event("r1", "log", name="verify_2",
             payload={"total_added": 40, "max_added_lines": 100})
    tr.event("r3", "log", name="diff_early_check",
             payload={"total_added": 500, "max_added_lines": 1500})

    assert main(["runs"]) == 0
    out = capsys.readouterr().out
    header, *body = out.splitlines()
    s = header.index("size")
    l = header.index("last")
    by_id = {line.split()[0]: line[s:l].strip() for line in body}
    assert by_id["r1"] == "40/100"
    assert by_id["r2"] == "-"
    assert by_id["r3"] == "-"


def test_runs_without_a_trace_database_fails(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    assert main(["runs"]) == 1
    assert "no trace database" in capsys.readouterr().err


def test_runs_rm_tears_down_worktree_branch_and_marks_discarded(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "load", lambda root=Path("."): {"commands": {}})

    worktree_path = cli.worktree.create(root, "r1", "lime/feature/41-toy")
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run(
        "r1", workflow="feature", request="x", ticket="t", branch="lime/feature/41-toy",
        worktree=str(worktree_path), port=3100, db_name="lime_r1",
    )
    tr.set_run_status("r1", "fail")

    assert main(["runs", "rm", "r1"]) == 0
    assert not worktree_path.exists()
    assert "discarded" in capsys.readouterr().out

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    assert con.execute("SELECT status FROM runs WHERE run_id='r1'").fetchone()["status"] == "discarded"
    branches = subprocess.run(
        ["git", "-C", str(root), "branch", "--list", "lime/feature/41-toy"],
        capture_output=True, text=True,
    ).stdout
    assert branches.strip() == ""


def test_runs_rm_refuses_a_run_that_is_still_running(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("r1", workflow="feature", request="x", port=3100)
    tr.start_process("r1", kind="run", name="lime", pid=os.getpid(), command="lime run 41")

    assert main(["runs", "rm", "r1"]) == 1
    assert "still running" in capsys.readouterr().err


def test_runs_rm_refuses_an_already_terminal_run(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("r1", workflow="feature", request="x")
    tr.set_run_status("r1", "fail")
    tr.set_run_status("r1", "discarded")

    assert main(["runs", "rm", "r1"]) == 1
    assert "already" in capsys.readouterr().err


def test_runs_rm_refuses_an_unknown_run(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    cli.tracer.Tracer(root / ".lime" / "runs")
    assert main(["runs", "rm", "ghost"]) == 1
    assert "unknown run" in capsys.readouterr().err


# --- run --resume: restart the failed Phase in the same worktree/branch/profile (spec §9) ---


def _plant_legacy_status(tr, run_id: str, status: str = "needs_rescope") -> None:
    # No code path sets `needs_rescope` any more (ticket 194): a preserved row from
    # before its removal is planted directly so `lime runs`/`lime trace` still show it.
    tr.con.execute(
        "UPDATE runs SET status=?, ended_at=coalesce(ended_at, ?) WHERE run_id=?",
        (status, datetime.now(UTC).isoformat(), run_id),
    )
    tr.con.commit()


def _seed_failed_run(
    root: Path, worktrees_root: Path, *, run_id="deadbeef", status="fail", ticket_path, workflow="feature",
    profile=None,
) -> Path:
    """A trace row + a bare worktree dir, as if a prior `lime run` had failed there."""
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    worktree_path = worktrees_root / run_id
    worktree_path.mkdir(parents=True)
    tr.start_run(
        run_id, workflow=workflow, request="do X", ticket=str(ticket_path),
        branch="lime/feature/41-toy", worktree=str(worktree_path), profile=profile,
    )
    if status == "needs_rescope":
        _plant_legacy_status(tr, run_id, status)
    else:
        tr.set_run_status(run_id, status)
    return worktree_path


def _dead_pid():
    """A pid that has certainly exited: a `true` subprocess reaped by `wait`."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _seed_dead_running_run(
    root: Path, worktrees_root: Path, *, pid: int, run_id="deadbeef"
) -> Path:
    """A `running` Run with a stuck `running` phase and one open `kind='run'`
    `processes` row (ticket 256). `pid` decides liveness: a dead pid reads dead, a
    live pid (or a fresh beat) reads live."""
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    worktree_path = worktrees_root / run_id
    worktree_path.mkdir(parents=True)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    tr.start_run(
        run_id, workflow="feature", request="do X", ticket=str(ticket_path),
        branch="lime/feature/41-toy", worktree=str(worktree_path),
    )
    phase_id = tr.add_phase(
        run_id, seq=0, name="builder", kind="agent", stage="Build", owner="builder"
    )
    tr.set_phase_status(phase_id, "running")
    tr.start_process(run_id, kind="run", name="lime", pid=pid, command="x")
    return worktree_path


def _stub_gate(monkeypatch) -> None:
    """Bypass the pre-merge merged-tree gate for tests about post-merge/session behavior."""
    monkeypatch.setattr(cli, "_check_deploy_merged_tree", lambda *a, **kw: 0)


def _forbid_gate(monkeypatch) -> None:
    """Assert the pre-merge gate is never reached (PR-not-open/CI-red/pending refusals)."""
    def fail_if_called(*a, **kw):
        raise AssertionError("the deploy merged-tree gate must not be reached")

    monkeypatch.setattr(cli, "_check_deploy_merged_tree", fail_if_called)


def _refusals(root: Path) -> list[tuple[str, str, dict]]:
    """Every `refusal` event in `root`'s trace db, oldest first (ticket 216).

    A read-only reader: an absent db is `[]`, never created."""
    db = root / ".lime" / "runs" / "lime.db"
    if not db.exists():
        return []
    con = cli.tracer.connect(db, readonly=True)
    try:
        rows = con.execute(
            "SELECT run_id, name, payload_json FROM events"
            " WHERE type='refusal' ORDER BY event_id"
        ).fetchall()
    finally:
        con.close()
    return [(row["run_id"], row["name"], json.loads(row["payload_json"])) for row in rows]


def _freeze_ledger(root: Path) -> None:
    """Make `root`'s trace db unwritable, WAL sidecars included.

    An earlier writer may still hold `lime.db-wal`/`-shm` open, so chmodding only the
    main file would leave the write path open (ticket 216)."""
    for path in (root / ".lime" / "runs").glob("lime.db*"):
        os.chmod(path, 0o444)


def test_resume_refuses_without_a_trace_database(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    assert main(["run", "--resume", "deadbeef"]) == 1
    assert "no trace database" in capsys.readouterr().err


def test_resume_refuses_an_unknown_run(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    cli.tracer.Tracer(root / ".lime" / "runs")  # just the db file, no matching row
    assert main(["run", "--resume", "ghost"]) == 1
    assert "unknown run" in capsys.readouterr().err


def test_resume_refuses_a_run_that_is_not_fail_or_budget_exhausted(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    assert main(["run", "--resume", "deadbeef"]) == 1
    assert "only fail or budget_exhausted" in capsys.readouterr().err


@pytest.mark.parametrize("flags", [[], ["--replan"], ["--rereview"]])
def test_resume_refuses_an_old_needs_rescope_run_before_mutation(
    tmp_path, monkeypatch, capsys, flags
):
    """ticket 194: a Run preserved as `needs_rescope` before the status was removed is
    refused by the ordinary 'only fail or budget_exhausted' check before any ticket/
    worktree check, config validation, port check or session/chain dispatch."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        cli.config, "validate_before_spawn",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not validate config")),
    )
    monkeypatch.setattr(
        cli.session, "ensure",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create a session")),
    )
    monkeypatch.setattr(
        cli.feature, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not dispatch the chain")),
    )
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(
        root, tmp_path / "worktrees", status="needs_rescope", ticket_path=ticket_path
    )
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    agent_map_path = tr.run_dir("deadbeef") / "agent_map.json"
    agent_map_path.write_text('{"builder": {"session_id": "keep"}}')
    before_map = agent_map_path.read_bytes()

    assert main(["run", "--resume", "deadbeef", *flags]) == 1

    assert "only fail or budget_exhausted" in capsys.readouterr().err
    assert tr.con.execute(
        "SELECT status FROM runs WHERE run_id='deadbeef'"
    ).fetchone()["status"] == "needs_rescope"
    assert agent_map_path.read_bytes() == before_map
    assert tr.con.execute(
        "SELECT 1 FROM events WHERE run_id='deadbeef'"
        " AND name IN ('replan_decision','rereview_decision')"
    ).fetchone() is None


@pytest.mark.parametrize("argv", [
    ["run", "--resume", "deadbeef", "--rescope-event", "1"],
    ["run", "41", "--recovery-decision", "x"],
])
def test_rescope_flags_are_gone(argv):
    """ticket 194: the event-bound recovery flags no longer exist."""
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_old_needs_rescope_run_still_lists_and_traces(tmp_path, monkeypatch, capsys):
    """ticket 194: a preserved needs_rescope row (raw SQL, since nothing sets the
    status) and its three legacy recovery events still show in `lime runs` and
    `lime trace`, without any recovery code path."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(
        root, tmp_path / "worktrees", status="needs_rescope", ticket_path=ticket_path
    )
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    plan = tr.add_phase("deadbeef", seq=1, name="plan", stage="Build", kind="agent", owner="planner")
    tr.set_phase_status(plan, "running")
    tr.set_phase_status(plan, "success")
    handoff_path = tr.run_dir("deadbeef") / "context_handoff" / "needs_rescope.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(json.dumps({"run_outcome": "needs_rescope", "run_id": "deadbeef"}) + "\n")
    event_id = tr.event(
        "deadbeef", "handoff", name="needs_rescope", payload={"path": str(handoff_path)}
    )
    tr.event(
        "deadbeef", "log", name="rescope_recovery_decision", parent_id=event_id,
        payload={"action": "rescope_and_resume", "decision": "approved"},
    )
    tr.event(
        "deadbeef", "log", name="rescope_recovery_started",
        payload={"decision_event_id": event_id},
    )

    assert main(["runs"]) == 0
    out = capsys.readouterr().out
    assert "deadbeef" in out
    assert "needs_rescope" in out

    assert main(["trace", "deadbeef"]) == 0
    out = capsys.readouterr().out
    assert "needs_rescope" in out
    assert "rescope_recovery_decision" in out
    assert "rescope_recovery_started" in out

    assert main(["trace", "deadbeef", "--summary"]) == 0
    out = capsys.readouterr().out
    assert "needs_rescope" in out
    assert len(out.splitlines()) <= 20


def test_trace_shows_a_pre_192_runs_layout_phases(tmp_path, monkeypatch, capsys):
    """ticket 192: a Run recorded before the layout phase was deleted keeps its
    `layout*` rows and `LayoutOutput` envelopes on disk; nothing deletes or parses
    them, and `lime trace` prints them like any other phase."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", ticket_path=ticket_path)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")

    layout_payload = json.dumps({
        "status": "success", "passed": False, "text": "passed: false\n",
        "screenshots": ["/r/visual/cost.png"], "problems": ["#/cost: overlap: a × b"],
    })
    for seq, name in ((1, "layout"), (2, "layout_retry1")):
        pid = tr.add_phase("deadbeef", seq=seq, name=name, stage="Build", kind="code", owner="git")
        tr.set_phase_status(pid, "running")
        tr.set_phase_status(pid, "success")
        tr.record_envelope(
            "deadbeef", pid, agent="git", output_type="LayoutOutput",
            payload_json=layout_payload, valid=True, attempt=1,
        )
    review = tr.add_phase("deadbeef", seq=3, name="review", stage="Build", kind="agent", owner="reviewer")
    tr.set_phase_status(review, "running")
    tr.set_phase_status(review, "success")
    tr.record_envelope(
        "deadbeef", review, agent="reviewer", output_type="ReviewOutput",
        payload_json=json.dumps({
            "status": "success", "approved": False,
            "blocking": ["#/cost: overlap: a × b"], "layout": "fail",
        }), valid=True, attempt=1,
    )

    assert main(["trace", "deadbeef"]) == 0
    out = capsys.readouterr().out
    assert "layout" in out
    assert "layout_retry1" in out
    assert "review" in out
    assert main(["trace", "deadbeef", "--summary"]) == 0


def test_resume_refuses_when_the_ticket_is_gone(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _seed_failed_run(
        root, tmp_path / "worktrees", ticket_path=root / ".lime" / "tickets" / "missing.md"
    )
    assert main(["run", "--resume", "deadbeef"]) == 1
    assert "ticket is gone" in capsys.readouterr().err


def test_resume_refuses_when_the_worktree_is_gone(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    worktree_path = _seed_failed_run(root, tmp_path / "worktrees", ticket_path=ticket_path)
    shutil.rmtree(worktree_path)
    assert main(["run", "--resume", "deadbeef"]) == 1
    assert "worktree" in capsys.readouterr().err


def test_resume_refuses_when_another_live_run_now_holds_the_port(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "capability_failures", lambda agents: ([], []))
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    worktree_path = tmp_path / "worktrees" / "deadbeef"
    worktree_path.mkdir(parents=True)

    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run(
        "deadbeef", workflow="feature", request="do X", ticket=str(ticket_path),
        branch="lime/feature/41-toy", worktree=str(worktree_path), port=3100, profile=None,
    )
    tr.set_run_status("deadbeef", "fail")
    # a second, currently-live Run has since claimed the port deadbeef used to hold
    tr.start_run("holder", workflow="feature", request="y", port=3100)
    tr.start_process("holder", kind="run", name="lime", pid=os.getpid(), command="lime run y")

    assert main(["run", "--resume", "deadbeef"]) == 1
    assert "held by holder" in capsys.readouterr().err


def test_resume_reenters_the_chain_in_the_same_worktree_and_keeps_the_agent_map_for_a_plain_fail(
    tmp_path, monkeypatch
):
    """ticket 76: a plain `fail` Run may have died mid-stream — its agent_map.json holds the
    session id `_remember_session()` wrote before the send that killed the phase, so a
    resumed pi phase can rejoin it instead of starting a brand-new session."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    worktree_path = _seed_failed_run(root, tmp_path / "worktrees", ticket_path=ticket_path)

    run_dir = root / ".lime" / "runs" / "deadbeef"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_map.json").write_text('{"builder": {"session_id": "stale"}}')

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["cwd"] = Path.cwd()
        captured["ticket"] = ticket.slug
        captured["base_branch"] = base_branch
        captured["agent_map"] = run_.agent_map
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef"]) == 0
    assert captured["ticket"] == "41-toy"
    assert captured["base_branch"] == "main"
    assert captured["cwd"] == worktree_path
    assert captured["agent_map"] == {"builder": {"session_id": "stale"}}  # kept — a plain fail may rejoin
    assert Path.cwd() == root

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()
    assert row["status"] == "running"


def test_resume_closes_a_dead_running_run_and_resumes_it(tmp_path, monkeypatch, capsys):
    """ticket 256: a `running` Run whose process died with its row still open is not
    resumable by the ordinary path (its phase is stuck `running`). `--resume` closes it
    as failed — phase and Run — then resumes it like a plain fail."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    worktree_path = _seed_dead_running_run(root, tmp_path / "worktrees", pid=_dead_pid())

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["cwd"] = Path.cwd()
        captured["ticket"] = ticket.slug
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef"]) == 0
    assert "was still running with no live process" in capsys.readouterr().out
    assert captured["cwd"] == worktree_path
    assert captured["ticket"] == "41-toy"

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    phase = con.execute(
        "SELECT status, error FROM phases WHERE run_id='deadbeef'"
    ).fetchone()
    assert phase["status"] == "fail"
    assert phase["error"] == "killed — its process died without closing the Run"
    row = con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()
    assert row["status"] == "running"


@pytest.mark.parametrize("live", ["null_beat", "fresh_beat"])
def test_resume_refuses_a_live_running_run_untouched(
    tmp_path, monkeypatch, capsys, live
):
    """ticket 256: a `running` Run with a live pid (NULL beat) or a fresh beat is not
    dead — the ordinary refusal stands and nothing is mutated."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    pid = os.getpid() if live == "null_beat" else _dead_pid()
    _seed_dead_running_run(root, tmp_path / "worktrees", pid=pid)
    if live == "fresh_beat":
        tr = cli.tracer.Tracer(root / ".lime" / "runs")
        tr.con.execute(
            "UPDATE processes SET heartbeat_at=? WHERE run_id='deadbeef'",
            (datetime.now(UTC).isoformat(),),
        )
        tr.con.commit()

    monkeypatch.setattr(
        cli.feature, "run",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not dispatch the chain")),
    )

    assert main(["run", "--resume", "deadbeef"]) == 1
    assert "only fail or budget_exhausted" in capsys.readouterr().err

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    run = con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()
    assert run["status"] == "running"
    phase = con.execute("SELECT status FROM phases WHERE run_id='deadbeef'").fetchone()
    assert phase["status"] == "running"
    proc = con.execute(
        "SELECT ended_at FROM processes WHERE run_id='deadbeef' AND kind='run'"
    ).fetchone()
    assert proc["ended_at"] is None


def test_run_and_phase_status_writers_are_pinned():
    """ticket 256: every writer of `runs.status` and `phases.status` is pinned by
    (file, innermost enclosing function), so a new caller cannot slip in un-noticed.
    The new dead-Run closer is reachable only from `_cmd_run_resume`."""
    run_writers: set[tuple[str, str]] = set()
    phase_writers: set[tuple[str, str]] = set()
    close_calls: set[tuple[str, str]] = set()

    def visit(node, filename: str, enclosing: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            enclosing = node.name
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in (
                "set_run_status",
                "set_phase_status",
            ):
                target = (
                    run_writers if func.attr == "set_run_status" else phase_writers
                )
                target.add((filename, enclosing))
            elif isinstance(func, ast.Name) and func.id == "_close_dead_run":
                close_calls.add((filename, enclosing))
        for child in ast.iter_child_nodes(node):
            visit(child, filename, enclosing)

    for path in sorted((_REPO_ROOT / "lime").rglob("*.py")):
        visit(
            ast.parse(path.read_text()),
            path.relative_to(_REPO_ROOT / "lime").as_posix(),
            None,
        )

    assert run_writers == {
        ("runner.py", "close_run"),
        ("cli.py", "_cmd_run_resume"),
        ("cli.py", "_cmd_prototype"),
        ("cli.py", "_cmd_runs_rm"),
        ("cli.py", "_cmd_deploy"),
        ("cli.py", "_close_dead_run"),
    }
    assert phase_writers == {
        ("runner.py", "phase"),
        ("agents.py", "_retry_phase"),
        ("cli.py", "_run_deploy_session"),
        ("cli.py", "_close_dead_run"),
    }
    assert close_calls == {("cli.py", "_cmd_run_resume")}


# --- ticket 256: the retry takes over its dead batch's started children ---


def _seed_batch_run(
    root: Path,
    *,
    run_id="deadbeef",
    status="success",
    ticket="41-toy",
    batch_id="deadbeef",
    deploy_row=False,
) -> None:
    """A `lime run` Run owned by the dead batch, plus an optional ended deploy row
    (253's proof of a merge) — the shapes a retry's take-over reads (ticket 256)."""
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    ticket_path = root / ".lime" / "tickets" / f"{ticket}.md"
    worktree_path = root.parent / "worktrees" / run_id
    worktree_path.mkdir(parents=True, exist_ok=True)
    tr.start_run(
        run_id, workflow="feature", request="x", ticket=str(ticket_path),
        branch=f"lime/feature/{ticket}", worktree=str(worktree_path), batch_id=batch_id,
    )
    if status == "deployed":
        tr.set_run_status(run_id, "success")
        tr.set_run_status(run_id, "deployed")
    elif status != "running":
        tr.set_run_status(run_id, status)
    if deploy_row:
        tr.start_process(run_id, kind="deploy", name="lime", pid=os.getpid(), command="x")
        tr.end_process(run_id, os.getpid())
    tr.con.close()


def _seed_hand_run(root: Path, *, run_id="H", ticket="41-toy") -> None:
    """A Run started by hand meanwhile: same ticket, `batch_id` NULL, newer."""
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    ticket_path = root / ".lime" / "tickets" / f"{ticket}.md"
    tr.start_run(
        run_id, workflow="feature", request="x", ticket=str(ticket_path),
        branch=f"lime/feature/{ticket}", worktree=str(root.parent / "wt" / run_id),
        batch_id=None,
    )
    tr.set_run_status(run_id, "success")
    tr.con.execute(
        "UPDATE runs SET started_at='2030-01-01T00:00:00+00:00' WHERE run_id=?", (run_id,)
    )
    tr.con.commit()
    tr.con.close()


def _seed_dead_batch_started(root: Path, *, batch_id="deadbeef", ticket="41-toy") -> None:
    """A dead batch whose child is `started` but left no Run row (an orphan)."""
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_process(batch_id, kind="batch", name="lime", pid=os.getpid(), command="x")
    tr.end_process(batch_id, os.getpid())
    tr.con.execute(
        "INSERT INTO batch_children (batch_id, position, ticket, state, reason, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (batch_id, 0, ticket, "started", None, datetime.now(UTC).isoformat()),
    )
    tr.con.commit()
    tr.con.close()


def _retry_children(root: Path, *, retry_of="deadbeef"):
    """The retry batch's `batch_children` rows as `(position, ticket, state, reason)`."""
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    try:
        batch_row = con.execute(
            "SELECT run_id FROM processes WHERE kind='batch' AND retry_of=?", (retry_of,)
        ).fetchone()
        if batch_row is None:
            return []
        return [
            (r["position"], r["ticket"], r["state"], r["reason"])
            for r in con.execute(
                "SELECT position, ticket, state, reason FROM batch_children"
                " WHERE batch_id=? ORDER BY position",
                (batch_row["run_id"],),
            )
        ]
    finally:
        con.close()


def _patch_no_launch(monkeypatch) -> None:
    """Any child launch fails the test — a take-over must birth no second Run."""
    def boom(cmd, cwd=None):
        raise AssertionError(f"unexpected launch: {cmd[-1]}")

    monkeypatch.setattr(cli.subprocess, "Popen", boom)


def test_retry_deploys_a_started_child_that_passed_unwatched(tmp_path, monkeypatch, capsys):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    _patch_deploy_real_path(monkeypatch, events)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})
    _seed_batch_run(root, status="success")
    _patch_no_launch(monkeypatch)

    assert main(["run", "41-toy", "--retry-of", "deadbeef", "--deploy"]) == 0
    out = capsys.readouterr().out
    assert events.count(("merge", "lime/feature/41-toy")) == 1
    assert "deployed: 41-toy" in out
    assert _run_status(root) == "deployed"


@pytest.mark.parametrize("keep_file", [True, False], ids=["file-kept", "file-removed"])
def test_retry_finishes_a_deploy_that_died_after_its_merge(
    tmp_path, monkeypatch, capsys, keep_file
):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    _patch_deploy_real_path(monkeypatch, events, ff_refused="__never__")
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "MERGED", "statusCheckRollup": []},
    )
    monkeypatch.setattr(
        cli.git_helper, "merge_pr",
        lambda branch: (_ for _ in ()).throw(AssertionError("must not merge again")),
    )
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})
    _seed_batch_run(root, status="success", deploy_row=True)
    if not keep_file:
        (root / ".lime" / "tickets" / "41-toy.md").unlink()
    _patch_no_launch(monkeypatch)

    assert main(["run", "41-toy", "--retry-of", "deadbeef", "--deploy"]) == 0
    assert _run_status(root) == "deployed"


def test_retry_drops_a_started_child_deployed_by_hand(tmp_path, monkeypatch):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})
    _seed_batch_run(root, status="deployed")
    (root / ".lime" / "tickets" / "41-toy.md").unlink()
    (root / ".lime" / "tickets" / "43-toy.md").unlink()

    def boom(args):
        raise AssertionError("must not deploy")

    monkeypatch.setattr(cli, "_cmd_deploy", boom)
    _patch_no_launch(monkeypatch)

    main(["run", "41-toy", "43-toy", "--retry-of", "deadbeef", "--deploy"])
    assert _retry_children(root) == [
        (0, "41-toy", "dropped", "no ticket file"),
        (1, "43-toy", "dropped", "no ticket file"),
    ]


def test_retry_drops_a_ticket_started_by_hand_meanwhile(tmp_path, monkeypatch, capsys):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})
    _seed_batch_run(root, run_id="R", status="discarded")
    _seed_hand_run(root, run_id="H")

    def boom(args):
        raise AssertionError("must not deploy")

    monkeypatch.setattr(cli, "_cmd_deploy", boom)
    _patch_no_launch(monkeypatch)

    assert main(["run", "41-toy", "--retry-of", "deadbeef", "--deploy"]) == 1
    out = capsys.readouterr()
    assert "started by hand meanwhile as Run H" in out.out + out.err
    assert _retry_children(root) == [(0, "41-toy", "dropped", "overlap Run H")]


def test_retry_drops_a_queued_ticket_held_by_an_overlap_meanwhile(
    tmp_path, monkeypatch, capsys
):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(
        monkeypatch,
        {"43-toy": {"Run hand1234 (lime/feature/43-toy)": ["app43.py"]}},
    )
    _patch_no_launch(monkeypatch)

    assert main(["run", "43-toy", "--retry-of", "deadbeef", "--deploy"]) == 1
    children = _retry_children(root)
    assert children[0][:3] == (0, "43-toy", "dropped")
    assert "overlap Run hand1234" in children[0][3]


def test_retry_leaves_a_child_that_failed_on_its_own(tmp_path, monkeypatch, capsys):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})
    _seed_batch_run(root, run_id="R", status="fail")
    _patch_no_launch(monkeypatch)

    assert main(["run", "41-toy", "42-toy", "--retry-of", "deadbeef", "--deploy"]) == 1
    out = capsys.readouterr()
    assert "lime run --resume R" in out.out + out.err
    children = _retry_children(root)
    assert children[0] == (0, "41-toy", "dropped", "failed: Run R")
    assert children[1] == (1, "42-toy", "dropped", "gave up: blocked 41")


def test_retry_launches_a_started_child_that_left_no_run_once(
    tmp_path, monkeypatch, capsys
):
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    _patch_deploy_real_path(monkeypatch, events)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})
    _seed_dead_batch_started(root)
    _ReadyPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _ReadyPopen)

    main(["run", "41-toy", "--retry-of", "deadbeef", "--deploy"])
    assert _ReadyPopen.launched == ["41-toy"]


# --- ticket 256: a child killed with the batch is resumed once ---


def _seed_killed_child(root: Path, *, run_id="R", ticket="41-toy", killed="fail") -> None:
    """A child the dead batch started that was killed (ticket 256): a `fail` with a
    `crash_detected` SIGHUP after its last `phase_start`, or a `running` with a
    dead-pid `kind='run'` process row."""
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    ticket_path = root / ".lime" / "tickets" / f"{ticket}.md"
    worktree_path = root.parent / "worktrees" / run_id
    worktree_path.mkdir(parents=True, exist_ok=True)
    tr.start_run(
        run_id, workflow="feature", request="x", ticket=str(ticket_path),
        branch=f"lime/feature/{ticket}", worktree=str(worktree_path), batch_id="deadbeef",
    )
    if killed == "fail":
        phase_id = tr.add_phase(
            run_id, seq=0, name="builder", kind="agent", stage="Build", owner="builder"
        )
        tr.set_phase_status(phase_id, "running")
        tr.event(run_id, "phase_start", phase_id=phase_id)
        tr.event(run_id, "crash_detected", payload={"signal": "SIGHUP", "exit_code": 129})
        tr.set_run_status(run_id, "fail")
    else:
        tr.start_process(run_id, kind="run", name="lime", pid=_dead_pid(), command="x")
    tr.con.close()


def _seed_live_batch_run(root: Path, *, run_id="R", ticket="41-toy", pid=None) -> None:
    """A `running` Run the dead batch left in flight, with one open `kind='run'`
    process row at a live pid (ticket 256): the shape a retry adopts."""
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    ticket_path = root / ".lime" / "tickets" / f"{ticket}.md"
    worktree_path = root.parent / "worktrees" / run_id
    worktree_path.mkdir(parents=True, exist_ok=True)
    tr.start_run(
        run_id, workflow="feature", request="x", ticket=str(ticket_path),
        branch=f"lime/feature/{ticket}", worktree=str(worktree_path), batch_id="deadbeef",
    )
    tr.start_process(
        run_id, kind="run", name="lime",
        pid=os.getpid() if pid is None else pid, command="x",
    )
    tr.con.close()


def _resume_recorder(root: Path, run_id="R", *, exit_code=0, mark_success=True):
    """A recording fake child Popen: every argv lands in `launched`; on the resume
    argv it moves `run_id` fail→running→success (so the retry can deploy it)."""

    class _Popen:
        launched: list[list[str]] = []

        def __init__(self, cmd, cwd=None):
            _Popen.launched.append(list(cmd))
            if "--resume" in cmd and mark_success:
                tr = cli.tracer.Tracer(root / ".lime" / "runs")
                status = tr.con.execute(
                    "SELECT status FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()["status"]
                if status == "fail":
                    tr.set_run_status(run_id, "running")
                tr.set_run_status(run_id, "success")
                tr.con.close()
            self.returncode = exit_code

        def poll(self):
            return self.returncode

    return _Popen


@pytest.mark.parametrize("killed", ["fail", "running"])
def test_retry_resumes_a_child_killed_with_the_batch_once(
    tmp_path, monkeypatch, capsys, killed
):
    """ticket 256: a child the dead batch started and that was killed with it (a
    `fail` at a signal, or a `running` whose process died) is resumed once — one
    spawn of `lime run --resume R`, no second birth — and deploys on success."""
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    _patch_deploy_real_path(monkeypatch, events)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})
    _seed_killed_child(root, run_id="R", killed=killed)
    Popen = _resume_recorder(root, "R")
    Popen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", Popen)

    assert main(["run", "41-toy", "--retry-of", "deadbeef", "--deploy"]) == 0
    assert Popen.launched == [[sys.executable, "-m", "lime", "run", "--resume", "R"]]
    assert ("merge", "lime/feature/41-toy") in events
    assert _run_status(root, "R") == "deployed"
    assert "resuming 41-toy (Run R, killed with batch deadbeef)" in capsys.readouterr().out


def test_a_resumed_child_that_fails_again_is_never_resumed_twice(
    tmp_path, monkeypatch, capsys
):
    """ticket 256: a resume that fails again is reported with its own line and never
    retried — one spawn, outcome `failed`, exit 1."""
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    _patch_deploy_real_path(monkeypatch, events)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    _patch_overlap(monkeypatch, {})
    _seed_killed_child(root, run_id="R", killed="fail")
    Popen = _resume_recorder(root, "R", exit_code=1, mark_success=False)
    Popen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", Popen)

    assert main(["run", "41-toy", "--retry-of", "deadbeef", "--deploy"]) == 1
    assert Popen.launched == [[sys.executable, "-m", "lime", "run", "--resume", "R"]]
    out = capsys.readouterr().out
    assert "resumed once and failed — resume: lime run --resume R" in out
    assert "failed: 41-toy — resume: lime run --resume R" in out


# --- ticket 256: a still-live child is adopted and re-polled each tick ---


def test_retry_adopts_a_live_child_waits_and_deploys_it(tmp_path, monkeypatch, capsys):
    """ticket 256: a child the dead batch left running is adopted — held (not given
    up) while its blocked dependent waits — then deployed once it ends, and only then
    does the dependent launch."""
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    _patch_deploy_real_path(monkeypatch, events)
    _patch_overlap(monkeypatch, {})
    _seed_live_batch_run(root)
    monkeypatch.setattr(
        cli.subprocess, "Popen", _make_deploy_batch_popen(events, {}, tmp_path)
    )
    ticks = {"n": 0}

    def fake_sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 2:
            tr = cli.tracer.Tracer(root / ".lime" / "runs")
            tr.set_run_status("R", "success")
            tr.con.close()

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    assert main(["run", "41-toy", "42-toy", "--retry-of", "deadbeef", "--deploy"]) == 0
    assert ("launch", "42-toy") in events
    assert events.count(("merge", "lime/feature/41-toy")) == 1
    assert events.index(("merge", "lime/feature/41-toy")) < events.index(
        ("launch", "42-toy")
    )
    assert _run_status(root, "R") == "deployed"
    out = capsys.readouterr().out
    assert "adopting 41-toy (Run R still running)" in out
    assert "41-toy: adopted Run R ended (passed)" in out


def test_an_adopted_child_holds_a_port_slot(tmp_path, monkeypatch, capsys):
    """ticket 256: an adopted child occupies a port slot, so a queued independent
    ticket waits behind it until the adopted Run ends and is deployed."""
    root = _deploy_batch_project(tmp_path)
    (root / ".lime" / "config.yaml").write_text(
        "auto_deploy: staging\ndeploy: []\nruns:\n  ports: 3100-3100\n"
        'commands:\n  test: "true"\n'
    )
    monkeypatch.chdir(root)
    events: list[tuple] = []
    _patch_deploy_real_path(monkeypatch, events)
    _patch_overlap(monkeypatch, {})
    _seed_live_batch_run(root)
    monkeypatch.setattr(
        cli.subprocess, "Popen", _make_deploy_batch_popen(events, {}, tmp_path)
    )
    ticks = {"n": 0}

    def fake_sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 2:
            tr = cli.tracer.Tracer(root / ".lime" / "runs")
            tr.set_run_status("R", "success")
            tr.con.close()

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    assert main(["run", "41-toy", "43-toy", "--retry-of", "deadbeef", "--deploy"]) == 0
    assert ("launch", "43-toy") in events
    assert events.index(("merge", "lime/feature/41-toy")) < events.index(
        ("launch", "43-toy")
    )


def test_an_adopted_child_whose_process_dies_is_resumed_once(
    tmp_path, monkeypatch, capsys
):
    """ticket 256: an adopted Run whose process dies mid-adoption is resumed once —
    exactly one spawn, no second birth — and deployed when that resume succeeds."""
    root = _deploy_batch_project(tmp_path)
    monkeypatch.chdir(root)
    events: list[tuple] = []
    _patch_deploy_real_path(monkeypatch, events)
    _patch_overlap(monkeypatch, {})
    _seed_live_batch_run(root)
    dead = _dead_pid()
    Popen = _resume_recorder(root, "R")
    Popen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", Popen)
    ticks = {"n": 0}

    def fake_sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 2:
            tr = cli.tracer.Tracer(root / ".lime" / "runs")
            tr.con.execute(
                "UPDATE processes SET pid=? WHERE run_id='R' AND kind='run'"
                " AND ended_at IS NULL",
                (dead,),
            )
            tr.con.commit()
            tr.con.close()

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    assert main(["run", "41-toy", "--retry-of", "deadbeef", "--deploy"]) == 0
    assert Popen.launched == [[sys.executable, "-m", "lime", "run", "--resume", "R"]]
    assert ("merge", "lime/feature/41-toy") in events
    assert _run_status(root, "R") == "deployed"
    assert "41-toy: adopted Run R ended (killed)" in capsys.readouterr().out


def test_run_cookbook_red_run_names_the_dead_batch_retry():
    """ticket 256: step 5's Red Run names the watchdog retry; step 4 no longer
    claims the retry runs only its queued children."""
    repo_root = Path(__file__).resolve().parent.parent
    text = (repo_root / "lime" / "skill" / "lime" / "cookbooks" / "run.md").read_text()
    step5 = text.split("5. **Red Run**", 1)[1].split("6. **", 1)[0]
    assert "retried once" in step5
    assert "lime status" in step5
    step4 = text.split("4. **Monitor", 1)[1].split("5. **", 1)[0]
    assert "only its still-`" not in step4


def test_resume_prints_preflight_warnings(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", ticket_path=ticket_path)

    captured = {}

    def fake_preflight_warnings(raw, roles, profile=None):
        captured["roles"] = roles
        return ["reviewer: m takes text only"]

    monkeypatch.setattr(cli.config, "preflight_warnings", fake_preflight_warnings)
    monkeypatch.setattr(cli.feature, "run", lambda *a, **kw: 0)

    assert main(["run", "--resume", "deadbeef"]) == 0
    err = capsys.readouterr().err
    assert "warn  reviewer: m takes text only" in err


def test_resume_shows_what_changed_in_the_roster_since_the_failed_run(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", ticket_path=ticket_path)

    # pre-seed the card with an old roster (only the builder row — the rest show as "new")
    cli.tracer.Tracer(root / ".lime" / "runs").update_card(
        "deadbeef",
        roster=[{"name": "builder", "runtime": "pi", "model": "openrouter/old/model", "thinking": "high"}],
    )

    roster = []

    def fake_feature_run(run_, ticket, base_branch):
        nonlocal roster
        roster = run_.tracer.card(run_.run_id)["roster"]
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef"]) == 0
    out = capsys.readouterr().out
    assert "was pi \u00b7 openrouter/old/model \u00b7 high" in out
    # the card's roster now has one row per ROLES_FEATURE role
    assert [r["name"] for r in roster] == list(cli.ROLES_FEATURE)
    builder_row = next(r for r in roster if r["name"] == "builder")
    assert builder_row["model"] == "openrouter/deepseek/deepseek-v4.1-flash"


def test_resume_clears_the_agent_map_for_a_budget_exhausted_run(tmp_path, monkeypatch):
    """A budget_exhausted Run resumed into its exhausted session would be re-killed at
    once — stale Sessions, fresh start (unlike a plain `fail`, see the test above)."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(
        root, tmp_path / "worktrees", status="budget_exhausted", ticket_path=ticket_path
    )

    run_dir = root / ".lime" / "runs" / "deadbeef"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "agent_map.json").write_text('{"builder": {"session_id": "stale"}}')

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["agent_map"] = run_.agent_map
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef"]) == 0
    assert captured["agent_map"] == {}  # stale Sessions cleared — a fresh chain gets fresh ones


def test_resume_rejoins_a_pi_phases_persisted_session_after_a_plain_fail(tmp_path, monkeypatch):
    """ticket 76's Done-means clause: `lime run --resume <run-id>` rejoins the session a
    failed pi phase persisted before it died — the resumed phase's first send carries
    resume=True and the same session_id, not a fresh one."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", ticket_path=ticket_path)

    run_dir = root / ".lime" / "runs" / "deadbeef"
    run_dir.mkdir(parents=True, exist_ok=True)
    persisted_session_dir = str(run_dir / "01-builder" / "runtime_sessions")
    (run_dir / "agent_map.json").write_text(
        json.dumps({
            "builder": {
                "session_id": "persisted-session",
                "model": "openrouter/deepseek/deepseek-v4.1-flash",
                "runtime": "pi",
                "session_dir": persisted_session_dir,
            }
        })
    )

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        session_id, resume, session_dir = agents._runtime_session(run_, run_.agent("builder"))
        captured["session_id"] = session_id
        captured["resume"] = resume
        captured["session_dir"] = session_dir
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef"]) == 0
    assert captured["resume"] is True
    assert captured["session_id"] == "persisted-session"
    # The failed phase's own runtime_sessions/ dir travels with the session_id — see
    # test_execute_updates_the_agent_map_for_the_next_phase_to_rejoin's session_dir
    # assertions in tests/test_agents.py for where this actually reaches the adapter.
    assert captured["session_dir"] == persisted_session_dir


def test_resume_blocked_fail_starts_fresh_for_closed_role(tmp_path, monkeypatch):
    """A blocked failure closes this role's session mapping before the exception leaves
    execute(). A resumed plain-fail Run must therefore start a brand-new Session for that
    role — new UUID, resume=False, no old runtime dir — while ordinary-fail maps still
    rejoin (see the test above) and budget-exhausted maps are cleared."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", ticket_path=ticket_path)

    run_dir = root / ".lime" / "runs" / "deadbeef"
    run_dir.mkdir(parents=True, exist_ok=True)
    old_runtime_dir = str(run_dir / "01-builder" / "runtime_sessions")
    (run_dir / "agent_map.json").write_text(
        json.dumps({
            "builder": {
                "session_id": "closed-session",
                "model": "openrouter/deepseek/deepseek-v4.1-flash",
                "runtime": "pi",
                "session_dir": old_runtime_dir,
                "closed": True,
            }
        })
    )

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        session_id, resume, session_dir = agents._runtime_session(run_, run_.agent("builder"))
        captured["session_id"] = session_id
        captured["resume"] = resume
        captured["session_dir"] = session_dir
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef"]) == 0
    assert captured["resume"] is False
    assert captured["session_id"] != "closed-session"
    assert uuid.UUID(captured["session_id"])  # a fresh UUID, not the closed one
    assert captured["session_dir"] is None  # no old runtime directory


def _seed_plan_provenance(tr, run_id: str, phase_id: str, ticket_path, profile=None) -> None:
    """Record a matching PlanProvenance + plan.md so ordinary resume reuses a valid plan.

    The prompt is the real reconstructed planning input (ticket/limits), never a
    placeholder, matching what `phases.stale_plan_reason` now compares."""
    import hashlib

    from lime import config, tracker
    from lime.data_types import PlanProvenance

    ticket_sha = hashlib.sha256(Path(ticket_path).read_bytes()).hexdigest()
    plan_path = tr.run_dir(run_id) / "context_handoff" / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("## Repo facts\nnone\n\n## Step 1\nDo it.\n")
    plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    prompt = cli._expected_plan_prompt(config.load(Path.cwd()), profile, tracker.load(str(ticket_path)))
    prov = PlanProvenance(ticket_sha256=ticket_sha, prompt=prompt, plan_sha256=plan_sha)
    tr.event(
        run_id, "handoff", phase_id=phase_id, name="plan_provenance",
        payload=prov.model_dump(),
    )


def test_resume_loads_the_replay_map_of_prior_successes(tmp_path, monkeypatch):
    """Spec §9: resume restarts the failed Phase — the chain skips what already
    succeeded, driven by the replay map loaded from the trace."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", ticket_path=ticket_path)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    plan = tr.add_phase("deadbeef", seq=1, name="plan", stage="Build", kind="agent", owner="planner")
    tr.set_phase_status(plan, "running")
    tr.set_phase_status(plan, "success")
    tr.record_envelope(
        "deadbeef", plan, agent="planner", output_type="PlanOutput",
        payload_json='{"status": "success"}', valid=True, attempt=1,
    )
    _seed_plan_provenance(tr, "deadbeef", plan, ticket_path)
    push = tr.add_phase("deadbeef", seq=2, name="push_pr", stage="Build", kind="code", owner="git")
    tr.set_phase_status(push, "running")
    tr.set_phase_status(push, "fail", error="rejected")

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["replay"] = run_.replay
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef"]) == 0
    assert set(captured["replay"]) == {"plan"}  # the failed push_pr re-runs for real
    assert captured["replay"]["plan"]["output_type"] == "PlanOutput"


def test_resume_refuses_a_run_with_nothing_left_to_rerun(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", ticket_path=ticket_path)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")

    plan = tr.add_phase("deadbeef", seq=1, name="plan", stage="Build", kind="agent", owner="planner")
    tr.set_phase_status(plan, "running")
    tr.set_phase_status(plan, "success")
    _seed_plan_provenance(tr, "deadbeef", plan, ticket_path)
    review = tr.add_phase("deadbeef", seq=2, name="review", stage="Build", kind="agent", owner="reviewer")
    tr.set_phase_status(review, "running")
    tr.set_phase_status(review, "success")
    tr.event("deadbeef", "error", name="not_accepted", payload={"reason": "step 1 still failed verify"})

    monkeypatch.setattr(cli.feature, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not re-enter the chain")))

    assert main(["run", "--resume", "deadbeef"]) == 1
    err = capsys.readouterr().err
    assert "step 1 still failed verify" in err
    assert "fresh run" in err

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()
    assert row["status"] == "fail"


def test_resume_reports_the_error_and_restores_cwd_when_the_chain_raises(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", ticket_path=ticket_path)

    def fake_feature_run(run_, ticket, base_branch):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef"]) == 1
    assert "boom" in capsys.readouterr().err
    assert Path.cwd() == root


# --- guard: the per-tool-call budget messenger (spec §8) ---


_UNSET = object()


def _guard(monkeypatch, capsys, payload, *, budget_file=None, handoff_dir=None, handoff_path=None,
           writes=_UNSET, runtime=_UNSET):
    for key, value in (
        ("LIME_BUDGET_FILE", budget_file),
        ("LIME_HANDOFF_DIR", handoff_dir),
        ("LIME_HANDOFF_PATH", handoff_path),
    ):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))
    # Clear inherited operator policy by default so the fence is isolated to the test.
    if writes is _UNSET:
        monkeypatch.delenv("LIME_WRITES", raising=False)
    else:
        monkeypatch.setenv("LIME_WRITES", writes)
    # The partial-delivery verb is runtime-selected; never let the operator env decide it.
    if runtime is _UNSET or runtime is None:
        monkeypatch.delenv("LIME_AGENT_RUNTIME", raising=False)
    else:
        monkeypatch.setenv("LIME_AGENT_RUNTIME", runtime)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert main(["guard"]) == 0
    return json.loads(capsys.readouterr().out)


def test_guard_allows_silently_with_no_budget_file(monkeypatch, capsys):
    out = _guard(monkeypatch, capsys, {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert out == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}


def test_guard_allows_silently_under_warn(tmp_path, monkeypatch, capsys):
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "ok"}))
    out = _guard(monkeypatch, capsys, {"tool_name": "Bash"}, budget_file=budget_file)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "additionalContext" not in out["hookSpecificOutput"]


def test_guard_injects_the_warning_exactly_once(tmp_path, monkeypatch, capsys):
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "warn"}))
    handoff_dir = tmp_path / "context_handoff"

    first = _guard(
        monkeypatch, capsys, {"tool_name": "Bash"},
        budget_file=budget_file, handoff_dir=handoff_dir, handoff_path=handoff_dir / "handoff-build-1.md",
    )
    assert first["hookSpecificOutput"]["permissionDecision"] == "allow"
    ctx = first["hookSpecificOutput"]["additionalContext"]
    assert "handoff-build-1.md" in ctx or str(handoff_dir) in ctx
    # The warning must guide the builder to finish the current step and
    # keep a handoff up to date — not to stop or submit partial unconditionally.
    assert "finish" in ctx.lower() or "complete" in ctx.lower() or "prioritize" in ctx.lower()
    assert "reduce" in ctx.lower() or "avoid" in ctx.lower() or "prioritize" in ctx.lower()

    second = _guard(
        monkeypatch, capsys, {"tool_name": "Bash"},
        budget_file=budget_file, handoff_dir=handoff_dir, handoff_path=handoff_dir / "handoff-build-1.md",
    )
    assert "additionalContext" not in second["hookSpecificOutput"]


def test_guard_denies_at_stop_for_a_tool_outside_the_handoff_dir(tmp_path, monkeypatch, capsys):
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "stop"}))
    handoff_dir = tmp_path / "context_handoff"

    out = _guard(
        monkeypatch, capsys, {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        budget_file=budget_file, handoff_dir=handoff_dir,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "handoff" in out["hookSpecificOutput"]["permissionDecisionReason"].lower()


def test_guard_allows_a_write_targeting_the_handoff_dir_at_stop(tmp_path, monkeypatch, capsys):
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "stop"}))
    handoff_dir = tmp_path / "context_handoff"
    handoff_dir.mkdir()

    out = _guard(
        monkeypatch, capsys,
        {"tool_name": "Write", "tool_input": {"file_path": str(handoff_dir / "handoff-build-1.md")}},
        budget_file=budget_file, handoff_dir=handoff_dir,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_guard_denies_a_write_outside_the_handoff_dir_at_stop(tmp_path, monkeypatch, capsys):
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "stop"}))
    handoff_dir = tmp_path / "context_handoff"
    handoff_dir.mkdir()

    out = _guard(
        monkeypatch, capsys,
        {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "elsewhere.py")}},
        budget_file=budget_file, handoff_dir=handoff_dir,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_handles_pi_native_tool_names_and_the_path_field(tmp_path, monkeypatch, capsys):
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "stop"}))
    handoff_dir = tmp_path / "context_handoff"
    handoff_dir.mkdir()

    out = _guard(
        monkeypatch, capsys,
        {"tool_name": "edit", "tool_input": {"path": str(handoff_dir / "handoff-build-1.md")}},
        budget_file=budget_file, handoff_dir=handoff_dir,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_guard_allows_submit_at_stop(tmp_path, monkeypatch, capsys):
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "stop"}))
    handoff_dir = tmp_path / "context_handoff"

    for status in ("success", "fail", "partial"):
        out = _guard(
            monkeypatch, capsys,
            {"tool_name": "submit", "tool_input": {"status": status}},
            budget_file=budget_file, handoff_dir=handoff_dir,
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow", f"submit {status} denied"
        assert "permissionDecisionReason" not in out["hookSpecificOutput"], f"submit {status} has a reason"


@pytest.mark.parametrize("tool", ["bash", "read", "grep", "subagent_create", "context7_docs"])
def test_guard_still_denies_every_other_tool_at_stop(tmp_path, monkeypatch, capsys, tool):
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "stop"}))
    handoff_dir = tmp_path / "context_handoff"

    out = _guard(
        monkeypatch, capsys,
        {"tool_name": tool, "tool_input": {}},
        budget_file=budget_file, handoff_dir=handoff_dir,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_stop_requires_a_truthful_report(tmp_path, monkeypatch, capsys):
    """Stop context must explain that success is conditional on requirements being
    met, and otherwise partial + useful handoff is required. No `status: complete`."""
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "stop"}))
    handoff_dir = tmp_path / "context_handoff"
    handoff_dir.mkdir()

    out = _guard(
        monkeypatch, capsys,
        {"tool_name": "Write", "tool_input": {"file_path": str(handoff_dir / "handoff-build-1.md")}},
        budget_file=budget_file, handoff_dir=handoff_dir, handoff_path=handoff_dir / "handoff-build-1.md",
    )
    # The stop exemption presents the guidance via additionalContext on handoff writes
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    # The stop message is in the denial reason for non-exempt tools
    out2 = _guard(
        monkeypatch, capsys,
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        budget_file=budget_file, handoff_dir=handoff_dir,
    )
    assert out2["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out2["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason
    assert "success" in reason
    # It must acknowledge partial as the default when requirements aren't met
    assert "partial" in reason
    assert "complete" not in reason


def test_guard_time_limit_denial_names_time_ceiling(tmp_path, monkeypatch, capsys):
    """A stop with reason `time_limit` must blame the Session's time ceiling,
    not context-budget exhaustion, and keep the same truthful success/partial guidance."""
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({
        "state": "stop", "reason": "time_limit",
        "context_tokens": 4_321, "warn": 150_000, "stop": 250_000,
    }))
    handoff_dir = tmp_path / "context_handoff"
    handoff_dir.mkdir()

    out = _guard(
        monkeypatch, capsys,
        {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "elsewhere.py")}},
        budget_file=budget_file, handoff_dir=handoff_dir,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "time ceiling" in reason.lower()
    assert "context budget exhausted" not in reason.lower()
    assert "success" in reason
    assert "partial" in reason
    assert "complete" not in reason


def test_guard_time_limit_stop_preserves_finalization_exemptions(tmp_path, monkeypatch, capsys):
    """At a time_limit stop the exemptions are unchanged: native handoff write/edit
    and submit (success/partial) are allowed, while a project edit and bash are denied."""
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "stop", "reason": "time_limit"}))
    handoff_dir = tmp_path / "context_handoff"
    handoff_dir.mkdir()
    note = handoff_dir / "handoff-build-1.md"

    for tool, key in (("Write", "file_path"), ("edit", "path")):
        out = _guard(
            monkeypatch, capsys,
            {"tool_name": tool, "tool_input": {key: str(note)}},
            budget_file=budget_file, handoff_dir=handoff_dir,
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow", f"{tool} handoff denied"

    for status in ("success", "partial"):
        out = _guard(
            monkeypatch, capsys,
            {"tool_name": "submit", "tool_input": {"status": status}},
            budget_file=budget_file, handoff_dir=handoff_dir,
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow", f"submit {status} denied"
        assert "permissionDecisionReason" not in out["hookSpecificOutput"]

    for payload in (
        {"tool_name": "edit", "tool_input": {"path": str(tmp_path / "elsewhere.py")}},
        {"tool_name": "bash", "tool_input": {"command": "ls"}},
    ):
        out = _guard(monkeypatch, capsys, payload, budget_file=budget_file, handoff_dir=handoff_dir)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", payload.get("tool_name")


def test_guard_repeat_denial_names_the_repeated_call(tmp_path, monkeypatch, capsys):
    """A stop with reason `repeat` blames the repeated identical calls, not the time
    ceiling or context budget, and keeps the truthful success/partial guidance."""
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "stop", "reason": "repeat"}))
    handoff_dir = tmp_path / "context_handoff"
    handoff_dir.mkdir()

    out = _guard(
        monkeypatch, capsys,
        {"tool_name": "edit", "tool_input": {"path": str(tmp_path / "elsewhere.py")}},
        budget_file=budget_file, handoff_dir=handoff_dir,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "same tool" in reason.lower()
    assert "time ceiling" not in reason.lower()
    assert "context budget exhausted" not in reason.lower()
    assert "success" in reason
    assert "partial" in reason


def test_guard_repeat_stop_preserves_finalization_exemptions(tmp_path, monkeypatch, capsys):
    """At a repeat stop the exemptions are unchanged: native handoff write/edit and
    submit (success/partial) are allowed, while a project edit and bash are denied."""
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "stop", "reason": "repeat"}))
    handoff_dir = tmp_path / "context_handoff"
    handoff_dir.mkdir()
    note = handoff_dir / "handoff-build-1.md"

    for tool, key in (("Write", "file_path"), ("edit", "path")):
        out = _guard(
            monkeypatch, capsys,
            {"tool_name": tool, "tool_input": {key: str(note)}},
            budget_file=budget_file, handoff_dir=handoff_dir,
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow", f"{tool} handoff denied"

    for status in ("success", "partial"):
        out = _guard(
            monkeypatch, capsys,
            {"tool_name": "submit", "tool_input": {"status": status}},
            budget_file=budget_file, handoff_dir=handoff_dir,
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow", f"submit {status} denied"

    for payload in (
        {"tool_name": "edit", "tool_input": {"path": str(tmp_path / "elsewhere.py")}},
        {"tool_name": "bash", "tool_input": {"command": "ls"}},
    ):
        out = _guard(monkeypatch, capsys, payload, budget_file=budget_file, handoff_dir=handoff_dir)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", payload.get("tool_name")


@pytest.mark.parametrize("payload", [
    {"state": "stop"},
    {"state": "stop", "reason": "budget"},
])
def test_guard_missing_or_unknown_stop_reason_keeps_context_budget_wording(
    tmp_path, monkeypatch, capsys, payload
):
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps(payload))
    handoff_dir = tmp_path / "context_handoff"

    out = _guard(
        monkeypatch, capsys,
        {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        budget_file=budget_file, handoff_dir=handoff_dir,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "context budget exhausted" in reason.lower()
    assert "time ceiling" not in reason.lower()


def test_guard_denies_handoff_path_escape_at_stop(tmp_path, monkeypatch, capsys):
    """Path traversal, sibling-prefix, and resolved symlink escape must be denied."""
    budget_file = tmp_path / "budget.json"
    budget_file.write_text(json.dumps({"state": "stop"}))
    handoff_dir = tmp_path / "context_handoff"
    handoff_dir.mkdir()

    # Path traversal via ../
    out = _guard(
        monkeypatch, capsys,
        {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "context_handoff" / ".." / "secret.txt")}},
        budget_file=budget_file, handoff_dir=handoff_dir,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    # Symlink escape
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = handoff_dir / "link.txt"
    link.symlink_to(outside)
    out2 = _guard(
        monkeypatch, capsys,
        {"tool_name": "Write", "tool_input": {"file_path": str(link)}},
        budget_file=budget_file, handoff_dir=handoff_dir,
    )
    # A resolved symlink targeting outside the handoff dir must be denied
    assert out2["hookSpecificOutput"]["permissionDecision"] == "deny"

    # Sibling prefix like ../handoff-backup must also be denied
    out3 = _guard(
        monkeypatch, capsys,
        {"tool_name": "edit", "tool_input": {"path": str(tmp_path / "handoff-backup" / "note.md")}},
        budget_file=budget_file, handoff_dir=handoff_dir,
    )
    assert out3["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- write fence: the Role's writes boundary at call time (spec §8) ---

_NATIVE_WRITE_TOOLS = [
    ("Write", "file_path"),
    ("Edit", "file_path"),
    ("write", "path"),
    ("edit", "path"),
]


def _fence_symlink_path(tmp_path, handoff_dir):
    outside = tmp_path.parent / f"outside-{tmp_path.name}.py"
    outside.write_text("secret\n")
    link = tmp_path / "src" / "link.py"
    link.symlink_to(outside)
    return str(link)


_FENCE_CASES = {
    # id: (LIME_WRITES JSON, path(tmp, handoff) -> str, expected decision)
    "readonly-deny": ("[]", lambda t, h: str(t / "src" / "app.py"), "deny"),
    "exact-grant": (json.dumps(["src/app.py"]), lambda t, h: str(t / "src" / "app.py"), "allow"),
    "prefix-grant": (json.dumps(["src/"]), lambda t, h: str(t / "src" / "deep" / "app.py"), "allow"),
    "glob-grant": (json.dumps(["src/*.py"]), lambda t, h: str(t / "src" / "app.py"), "allow"),
    "glob-stops-at-separator": (json.dumps(["src/*.py"]), lambda t, h: str(t / "src" / "deep" / "app.py"), "deny"),
    "unlisted-deny": (json.dumps(["docs/"]), lambda t, h: str(t / "src" / "app.py"), "deny"),
    "relative-grant": (json.dumps(["src/app.py"]), lambda t, h: "src/app.py", "allow"),
    "relative-deny": ("[]", lambda t, h: "src/app.py", "deny"),
    "normalized-inside": (json.dumps(["src/app.py"]), lambda t, h: "src/./app.py", "allow"),
    "dotdot-inside": (json.dumps(["src/app.py"]), lambda t, h: "src/../src/app.py", "allow"),
    "dotdot-escape": (json.dumps(["src/"]), lambda t, h: "src/../elsewhere.py", "deny"),
    "sibling-prefix": (json.dumps(["src/"]), lambda t, h: str(t / "srcx" / "app.py"), "deny"),
    "handoff-allow": ("[]", lambda t, h: str(h / "note.md"), "allow"),
    "handoff-escape": ("[]", lambda t, h: str(h / ".." / "escape.py"), "deny"),
    "outside-cwd-broad-glob": (json.dumps(["**"]), lambda t, h: str(t / ".." / "outside.py"), "deny"),
    "symlink-escape": (json.dumps(["**"]), _fence_symlink_path, "deny"),
}


@pytest.mark.parametrize("tool,key", _NATIVE_WRITE_TOOLS)
@pytest.mark.parametrize("case", list(_FENCE_CASES))
def test_guard_write_fence(tmp_path, monkeypatch, capsys, tool, key, case):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    handoff = tmp_path / "context_handoff"
    handoff.mkdir()
    writes, path_for, expected = _FENCE_CASES[case]
    target = path_for(tmp_path, handoff)
    out = _guard(
        monkeypatch, capsys,
        {"tool_name": tool, "tool_input": {key: target}},
        handoff_dir=handoff, writes=writes,
    )
    decision = out["hookSpecificOutput"]["permissionDecision"]
    assert decision == expected, f"{case}/{tool}: {out}"
    if expected == "deny":
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert str(handoff) in reason
        assert "submit" in reason


def test_guard_write_fence_unset_and_other_tools_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    handoff = tmp_path / "context_handoff"
    handoff.mkdir()

    # Unset policy: no new call-time restriction on a codebase write.
    out = _guard(
        monkeypatch, capsys,
        {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "app.py")}},
        handoff_dir=handoff,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    # A read-only policy leaves non-native tools untouched.
    for tool, tool_input in (
        ("Bash", {"command": "ls"}),
        ("Read", {"file_path": str(tmp_path / "app.py")}),
        ("submit", {"status": "success"}),
        ("context7_docs", {}),
        ("MultiEdit", {"file_path": str(tmp_path / "app.py")}),
        ("NotebookEdit", {"notebook_path": str(tmp_path / "n.ipynb")}),
    ):
        out = _guard(
            monkeypatch, capsys, {"tool_name": tool, "tool_input": tool_input},
            handoff_dir=handoff, writes="[]",
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow", tool

    # A native write with no usable target supplies no fence target.
    for payload in (
        {"tool_name": "Write", "tool_input": {}},
        {"tool_name": "Write", "tool_input": {"file_path": 5}},
        {"tool_name": "Write"},
        {"tool_name": "write", "tool_input": "not-an-object"},
        {"tool_name": "edit", "tool_input": {"path": ""}},
    ):
        out = _guard(monkeypatch, capsys, payload, handoff_dir=handoff, writes="[]")
        assert out["hookSpecificOutput"]["permissionDecision"] == "allow", payload


@pytest.mark.parametrize("raw", ["not json", "null", "{}", "3", '"x"', "true", '["ok", 3]', "[]x"])
def test_guard_write_fence_malformed_policy_fails_open(tmp_path, monkeypatch, capsys, raw):
    monkeypatch.chdir(tmp_path)
    handoff = tmp_path / "context_handoff"
    handoff.mkdir()
    out = _guard(
        monkeypatch, capsys,
        {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "app.py")}},
        handoff_dir=handoff, writes=raw,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow", raw


def test_guard_write_fence_internal_error_fails_open(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    handoff = tmp_path / "context_handoff"
    handoff.mkdir()

    def boom(*args, **kwargs):
        raise RuntimeError("permission evaluation exploded")

    monkeypatch.setattr("lime.permissions.permitted", boom)
    out = _guard(
        monkeypatch, capsys,
        {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "app.py")}},
        handoff_dir=handoff, writes=json.dumps(["docs/"]),
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_guard_write_fence_preserves_budget_stop_and_warn(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    handoff = tmp_path / "context_handoff"
    handoff.mkdir()
    repo_write = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "app.py")}}
    handoff_write = {"tool_name": "Write", "tool_input": {"file_path": str(handoff / "note.md")}}
    readonly = "[]"

    # Absent mailbox: the fence still denies a read-only codebase write.
    out = _guard(monkeypatch, capsys, repo_write, handoff_dir=handoff, writes=readonly)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    # ok budget: same denial.
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"state": "ok"}))
    out = _guard(monkeypatch, capsys, repo_write, budget_file=ok, handoff_dir=handoff, writes=readonly)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    # Malformed policy does not disable a valid budget stop.
    stop = tmp_path / "stop.json"
    stop.write_text(json.dumps({"state": "stop"}))
    out = _guard(monkeypatch, capsys, repo_write, budget_file=stop, handoff_dir=handoff, writes="not json")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "context budget exhausted" in out["hookSpecificOutput"]["permissionDecisionReason"].lower()

    # A granted repo write is still denied at stop (a grant never reopens substantive tools).
    out = _guard(
        monkeypatch, capsys, repo_write,
        budget_file=stop, handoff_dir=handoff, writes=json.dumps(["app.py"]),
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"

    # Handoff writes and submit remain allowed at stop.
    out = _guard(monkeypatch, capsys, handoff_write, budget_file=stop, handoff_dir=handoff, writes=readonly)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    out = _guard(
        monkeypatch, capsys, {"tool_name": "submit", "tool_input": {"status": "partial"}},
        budget_file=stop, handoff_dir=handoff, writes=readonly,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    # warn budget: a forbidden write is denied on every call...
    warn = tmp_path / "warn.json"
    warn.write_text(json.dumps({"state": "warn"}))
    for _ in range(2):
        out = _guard(monkeypatch, capsys, repo_write, budget_file=warn, handoff_dir=handoff, writes=readonly)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    # ...while an allowed read still gets the warning exactly once.
    first = _guard(
        monkeypatch, capsys, {"tool_name": "Read", "tool_input": {}},
        budget_file=warn, handoff_dir=handoff, writes=readonly,
    )
    assert first["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "additionalContext" in first["hookSpecificOutput"]
    second = _guard(
        monkeypatch, capsys, {"tool_name": "Read", "tool_input": {}},
        budget_file=warn, handoff_dir=handoff, writes=readonly,
    )
    assert "additionalContext" not in second["hookSpecificOutput"]


# --- deploy: merge -> clean -> Deploy session (ticket 33, spec §10, §12) ---


def test_ci_status_green_on_empty_rollup():
    assert cli._ci_status([]) == "green"


def test_ci_status_green_when_every_check_succeeded():
    assert cli._ci_status([{"conclusion": "SUCCESS"}, {"state": "SUCCESS"}]) == "green"


def test_ci_status_red_when_any_check_failed():
    assert cli._ci_status([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]) == "red"


def test_ci_status_pending_when_a_check_has_no_conclusion_yet():
    assert cli._ci_status([{"conclusion": "SUCCESS"}, {"conclusion": ""}]) == "pending"


def test_ci_status_red_wins_over_pending():
    checks = [{"conclusion": "FAILURE"}, {"conclusion": "IN_PROGRESS"}]
    assert cli._ci_status(checks) == "red"


def test_cli_ci_status_is_the_git_helper_verdict():
    assert cli._ci_status is git_helper.ci_status


def test_resolve_deploy_run_matches_by_ticket_number(tmp_path):
    tr = cli.tracer.Tracer(tmp_path / "runs")
    tr.start_run(
        "r1", workflow="feature", request="x",
        ticket=str(tmp_path / ".lime" / "tickets" / "41-toy.md"),
        branch="lime/feature/41-toy", worktree=str(tmp_path / "wt"),
    )
    con = cli.tracer.connect(tmp_path / "runs" / "lime.db", readonly=True)
    assert cli._resolve_deploy_run(con, "41")["run_id"] == "r1"
    assert cli._resolve_deploy_run(con, "41-toy")["run_id"] == "r1"


def _two_runs_sharing_number_one(root):
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    for run_id, stem in (("r1", "1-b"), ("r2", "01-a")):
        tr.start_run(
            run_id, workflow="feature", request="x",
            ticket=str(root / ".lime" / "tickets" / f"{stem}.md"),
            branch=f"lime/feature/{stem}", worktree=str(root / "wt"),
        )
    tr.con.close()


def test_deploy_refuses_a_number_two_tickets_share(tmp_path, monkeypatch, capsys):
    _two_runs_sharing_number_one(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["deploy", "1"]) == 2
    db = tmp_path.resolve() / ".lime" / "runs" / "lime.db"
    assert capsys.readouterr().err == (
        f"lime deploy: ticket 1 is ambiguous in {db}: 01-a.md, 1-b.md"
        " — name one in full, e.g. `lime deploy 01-a`\n")


def _rename_after_a_discard(root):
    """The review's case: a Run of 7-old-slug discarded, the ticket renamed to 7-better-slug,
    and a new Run of it that passed."""
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    for run_id, stem, status, started in (("old", "7-old-slug", "discarded", "2026-01-01T00:00:00+00:00"),
                                          ("new", "7-better-slug", "success", "2026-01-02T00:00:00+00:00")):
        tr.start_run(
            run_id, workflow="feature", request="x",
            ticket=str(root / ".lime" / "tickets" / f"{stem}.md"),
            branch=f"lime/feature/{stem}", worktree=str(root / "wt" / run_id),
        )
        tr.set_run_status(run_id, status)
        tr.con.execute("UPDATE runs SET started_at=? WHERE run_id=?", (started, run_id))
    tr.con.commit()
    tr.con.close()


def test_deploy_skips_a_discarded_run_of_a_renamed_ticket(tmp_path, monkeypatch, capsys):
    _rename_after_a_discard(tmp_path)
    monkeypatch.chdir(tmp_path)
    viewed = []
    monkeypatch.setattr(cli.git_helper, "pr_view", lambda branch, **kw: viewed.append(branch) or {
        "state": "CLOSED", "statusCheckRollup": [{"conclusion": "SUCCESS"}]})
    assert main(["deploy", "7"]) == 1  # refused at the PR gate: it got past the ticket lookup
    err = capsys.readouterr().err
    assert "ambiguous" not in err
    assert viewed == ["lime/feature/7-better-slug"]
    assert _refusals(tmp_path) == [("new", "pr_not_open", {"ticket": "7-better-slug", "state": "CLOSED"})]
    # the unattended `lime run --deploy` link resolves the same live Run
    monkeypatch.setattr(cli.config, "load", lambda root: {"deploy": []})
    assert cli._deploy_link(tmp_path.resolve(), "7") == "lime deploy 7"
    err = capsys.readouterr().err
    assert "ambiguous" not in err and "not deployed (lime deploy exit 1)" in err
    assert viewed == ["lime/feature/7-better-slug"] * 3  # the CI wait, then the deploy's PR gate
    assert [row[0] for row in _refusals(tmp_path)] == ["new", "new"]


def test_deploy_of_a_non_ascii_digit_is_no_such_ticket(tmp_path, monkeypatch, capsys):
    _two_runs_sharing_number_one(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["deploy", "\u00b2"]) == 1
    assert "no Run matching ticket '\u00b2'" in capsys.readouterr().err


def test_run_refuses_a_number_two_ticket_files_share(tmp_path, monkeypatch, capsys):
    tickets = tmp_path / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    for name in ("1-b.md", "01-a.md"):
        (tickets / name).write_text("# 1 — x\nWorkflow: feature\nStatus: open\n\n## Where\n- `a.py`\n")
    monkeypatch.chdir(tmp_path)
    assert main(["run", "1"]) == 2
    assert capsys.readouterr().err == (
        "lime run: ticket 1 is ambiguous in .lime/tickets/: 01-a.md, 1-b.md"
        " — name one in full, e.g. `lime run 01-a`\n")


def test_resolve_deploy_run_ignores_non_run_kinds_and_unmatched_tickets(tmp_path):
    tr = cli.tracer.Tracer(tmp_path / "runs")
    tr.start_run(
        "res1", workflow="feature", request="x", kind="research",
        ticket=str(tmp_path / ".lime" / "tickets" / "41-toy.md"),
    )
    con = cli.tracer.connect(tmp_path / "runs" / "lime.db", readonly=True)
    assert cli._resolve_deploy_run(con, "41") is None
    assert cli._resolve_deploy_run(con, "99") is None


def test_deploy_prompt_numbers_the_configured_commands():
    prompt = cli._deploy_prompt(["vercel deploy --prod", "curl example.com/warm"])
    assert "1. vercel deploy --prod" in prompt
    assert "2. curl example.com/warm" in prompt


def test_deploy_prompt_tells_the_session_to_pull_the_merge_first():
    # project_root sits on the base branch; `gh pr merge` never touches that checkout, so
    # without a pull the deploy commands would run against pre-merge code.
    assert "git pull" in cli._deploy_prompt(["vercel deploy --prod"])


def test_deploy_refuses_without_a_trace_database(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    assert main(["deploy", "41"]) == 1
    assert "no trace database" in capsys.readouterr().err


def test_deploy_refuses_when_no_run_matches_the_ticket(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    cli.tracer.Tracer(root / ".lime" / "runs")
    assert main(["deploy", "41"]) == 1
    assert "no Run matching" in capsys.readouterr().err


@pytest.mark.parametrize("status", ["fail", "needs_rescope"])
def test_deploy_refuses_a_run_that_is_not_success(tmp_path, monkeypatch, capsys, status):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", ticket_path=ticket_path, status=status)

    def fail_if_called(*a, **kw):
        raise AssertionError("a non-success Run must refuse before touching the PR")

    monkeypatch.setattr(cli.git_helper, "pr_view", fail_if_called)

    assert main(["deploy", "41"]) == 1
    assert "not 'success'" in capsys.readouterr().err


def test_deploy_refuses_when_the_pr_is_not_open(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    monkeypatch.setattr(
        cli.git_helper, "pr_view", lambda branch: {"state": "CLOSED", "statusCheckRollup": []}
    )

    def fail_if_called(*a, **kw):
        raise AssertionError("a closed PR must refuse before merging")

    monkeypatch.setattr(cli.git_helper, "merge_pr", fail_if_called)
    _forbid_gate(monkeypatch)

    assert main(["deploy", "41"]) == 1
    err = capsys.readouterr().err
    assert "CLOSED" in err and "OPEN" in err
    assert _refusals(root) == [("deadbeef", "pr_not_open", {"ticket": "41-toy", "state": "CLOSED"})]


def test_deploy_records_a_hand_merged_pr(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    monkeypatch.setattr(
        cli.git_helper, "pr_view", lambda branch: {"state": "MERGED", "statusCheckRollup": []}
    )
    _fail_if_called(monkeypatch, (cli.git_helper, "merge_pr"), "a hand-merged PR must refuse before merging")
    _forbid_gate(monkeypatch)

    assert main(["deploy", "41"]) == 1
    err = capsys.readouterr().err
    assert "MERGED" in err and "OPEN" in err
    assert _refusals(root) == [("deadbeef", "hand_merge", {"ticket": "41-toy", "state": "MERGED"})]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_deploy_refusal_output_survives_an_unwritable_ledger(
    tmp_path, monkeypatch, capsys, state
):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    monkeypatch.setattr(
        cli.git_helper, "pr_view", lambda branch: {"state": state, "statusCheckRollup": []}
    )
    _fail_if_called(monkeypatch, (cli.git_helper, "merge_pr"), "a refusing PR must refuse before merging")
    _forbid_gate(monkeypatch)

    assert main(["deploy", "41"]) == 1
    first = capsys.readouterr()
    _freeze_ledger(root)
    assert main(["deploy", "41"]) == 1
    second = capsys.readouterr()
    assert (second.out, second.err) == (first.out, first.err)
    assert len(_refusals(root)) == 1


def test_deploy_refuses_when_ci_is_red(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "FAILURE"}]},
    )

    def fail_if_called(*a, **kw):
        raise AssertionError("red CI must refuse before merging")

    monkeypatch.setattr(cli.git_helper, "merge_pr", fail_if_called)
    _forbid_gate(monkeypatch)

    assert main(["deploy", "41"]) == 1
    assert "CI is red" in capsys.readouterr().err
    assert _refusals(root) == [("deadbeef", "ci_red", {"ticket": "41-toy"})]


def test_deploy_refuses_when_ci_is_still_running(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": ""}]},
    )

    def fail_if_called(*a, **kw):
        raise AssertionError("pending CI must refuse before merging")

    monkeypatch.setattr(cli.git_helper, "merge_pr", fail_if_called)
    _forbid_gate(monkeypatch)

    assert main(["deploy", "41"]) == 1
    assert "CI is pending" in capsys.readouterr().err
    assert _refusals(root) == [("deadbeef", "ci_pending", {"ticket": "41-toy"})]


def test_deploy_merges_tears_down_and_marks_deployed_with_no_deploy_commands(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    ticket_path.unlink()  # delete-on-close reached main with the merge (ticket 61)

    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    merged = []
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: merged.append(branch))
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")
    teardown_calls = []

    def fake_teardown(repo_root, run_id, branch, *, command=None, env=None, base=None):
        teardown_calls.append((repo_root, run_id, branch, command, env))
        return []

    monkeypatch.setattr(cli.worktree, "teardown", fake_teardown)
    monkeypatch.setattr(
        cli.config, "load",
        lambda root: {"commands": {"teardown": "rm -rf .venv"}, "deploy": []},
    )

    def fail_if_called(*a, **kw):
        raise AssertionError("no deploy session when commands.deploy is empty")

    monkeypatch.setattr(cli.subprocess, "run", fail_if_called)

    assert main(["deploy", "41"]) == 0
    assert merged == ["lime/feature/41-toy"]
    assert teardown_calls[0][1:3] == ("deadbeef", "lime/feature/41-toy")
    assert teardown_calls[0][3] == "rm -rf .venv"

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()
    assert row["status"] == "deployed"
    assert "deployed" in capsys.readouterr().out


def test_deploy_opens_a_beating_deploy_row_before_the_merge_and_closes_it(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    ticket_path.unlink()

    monkeypatch.setattr(cli.tracer, "HEARTBEAT_S", 0.01)
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    db = root / ".lime" / "runs" / "lime.db"
    seen: dict = {}

    def fake_merge(branch):
        con = cli.tracer.connect(db, readonly=True)
        row = None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            row = con.execute(
                "SELECT pid, ended_at, heartbeat_at FROM processes"
                " WHERE run_id='deadbeef' AND kind='deploy'"
            ).fetchone()
            if row is not None and row["heartbeat_at"] is not None:
                break
            time.sleep(0.01)
        seen["row"] = (row["pid"], row["ended_at"])
        seen["verdict"] = cli.tracer.liveness(con, "deadbeef", datetime.now(UTC), "deploy")
        con.close()

    monkeypatch.setattr(cli.git_helper, "merge_pr", fake_merge)
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    assert main(["deploy", "41"]) == 0
    assert seen["row"] == (os.getpid(), None)
    assert seen["verdict"] == "live"

    con = cli.tracer.connect(db, readonly=True)
    rows = con.execute(
        "SELECT ended_at FROM processes WHERE run_id='deadbeef' AND kind='deploy'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ended_at"] is not None
    assert con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()["status"] == "deployed"
    con.close()


def test_deploy_closes_its_row_when_it_refuses_after_opening(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )

    def boom(branch):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.git_helper, "merge_pr", boom)
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    assert main(["deploy", "41"]) == 1
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    assert con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()["status"] == "success"
    rows = con.execute(
        "SELECT ended_at FROM processes WHERE run_id='deadbeef' AND kind='deploy'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ended_at"] is not None
    con.close()


def test_deploy_without_its_process_row_refuses_to_merge(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )

    def no_row(*a, **kw):
        raise sqlite3.OperationalError("ledger refused")

    monkeypatch.setattr(cli.tracer.Tracer, "start_process", no_row)

    def fail_if_merged(*a, **kw):
        raise AssertionError("merge must not run without the deploy process row")

    monkeypatch.setattr(cli.git_helper, "merge_pr", fail_if_merged)
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    assert main(["deploy", "41"]) == 1
    assert "refusing to deploy" in capsys.readouterr().err
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    rows = con.execute(
        "SELECT id FROM processes WHERE run_id='deadbeef' AND kind='deploy'"
    ).fetchall()
    con.close()
    assert rows == []


def test_deploy_refused_at_the_pr_gate_opens_no_row(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    monkeypatch.setattr(
        cli.git_helper, "pr_view", lambda branch: {"state": "CLOSED", "statusCheckRollup": []}
    )

    def fail_if_called(*a, **kw):
        raise AssertionError("a closed PR must refuse before merging")

    monkeypatch.setattr(cli.git_helper, "merge_pr", fail_if_called)
    _forbid_gate(monkeypatch)

    assert main(["deploy", "41"]) == 1
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    rows = con.execute(
        "SELECT id FROM processes WHERE run_id='deadbeef' AND kind='deploy'"
    ).fetchall()
    con.close()
    assert rows == []


def test_only_deploy_opens_a_deploy_process_row():
    """`_deploy_process` is the single `start_process(kind="deploy")` caller (ticket 252)."""
    cli_path = _REPO_ROOT / "lime" / "cli.py"
    deploy_fn = _ast_func_source(cli_path, "_deploy_process")
    deploy_calls = []
    for path in sorted((_REPO_ROOT / "lime").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else None
            )
            if called != "start_process":
                continue
            kind_kw = [kw for kw in node.keywords if kw.arg == "kind"]
            if not kind_kw:
                continue
            try:
                kind_value = ast.literal_eval(kind_kw[0].value)
            except ValueError:
                continue  # a symbolic kind (e.g. lime/batch.py's KIND) is never "deploy"
            if kind_value != "deploy":
                continue
            deploy_calls.append((path, node))
    assert len(deploy_calls) == 1
    path, node = deploy_calls[0]
    assert path == cli_path
    assert deploy_fn.lineno <= node.lineno <= (deploy_fn.end_lineno or deploy_fn.lineno)


# --- deploy: finish a deploy that died after its merge (ticket 253) ---


class _Died(BaseException):
    """the deploy process died here — nothing in `_cmd_deploy` catches it"""


def _half_done_deploy(monkeypatch, root: Path, die_at: str) -> Counter:
    """A deploy that dies at `die_at` on its first run, after that step's side effect.

    Every tail step is idempotent, so the second run must finish the deploy. Returns
    the counter of merge/gate/ff/teardown calls the fakes made.
    """
    _toy_project(root, track_ticket=False)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, root.parent / "worktrees", status="success", ticket_path=ticket_path)

    calls: Counter = Counter()
    pr = {"state": "OPEN"}
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": pr["state"], "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    monkeypatch.setattr(
        cli, "_check_deploy_merged_tree", lambda *a, **kw: calls.update(["gate"]) or 0
    )

    first = {"done": False}

    def merge(branch):
        calls.update(["merge"])
        pr["state"] = "MERGED"
        if die_at == "merge" and not first["done"]:
            first["done"] = True
            raise _Died()

    monkeypatch.setattr(cli.git_helper, "merge_pr", merge)

    def fast_forward(base):
        calls.update(["ff"])
        if die_at == "fast_forward" and not first["done"]:
            first["done"] = True
            raise _Died()
        return "abc1234"

    monkeypatch.setattr(cli.git_helper, "fast_forward", fast_forward)

    def teardown(*a, **kw):
        if die_at == "unlink" and not first["done"]:
            first["done"] = True
            raise _Died()
        calls.update(["teardown"])
        return []

    monkeypatch.setattr(cli.worktree, "teardown", teardown)
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")
    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: [])
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})
    return calls


def _deploy_rows(root: Path, run_id: str = "deadbeef") -> list:
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    try:
        return con.execute(
            "SELECT ended_at FROM processes WHERE run_id=? AND kind='deploy'", (run_id,)
        ).fetchall()
    finally:
        con.close()


def _run_status(root: Path, run_id: str = "deadbeef") -> str:
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    try:
        return con.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()["status"]
    finally:
        con.close()


@pytest.mark.parametrize("die_at", ["merge", "fast_forward", "unlink"])
def test_deploy_rerun_finishes_a_deploy_that_died_after_its_merge(
    tmp_path, monkeypatch, capsys, die_at
):
    root = tmp_path / "project"
    calls = _half_done_deploy(monkeypatch, root, die_at)
    copy = root / ".lime" / "tickets" / "41-toy.md"

    with pytest.raises(_Died):
        main(["deploy", "41"])

    assert _run_status(root) == "success"
    rows = _deploy_rows(root)
    assert len(rows) == 1
    if die_at == "unlink":
        assert not copy.exists()

    capsys.readouterr()  # drop the failed first run's output
    assert main(["deploy", "41"]) == 0
    out = capsys.readouterr().out

    assert calls["merge"] == 1 and calls["gate"] == 1  # the finishing run skips both
    assert calls["ff"] >= 1 and calls["teardown"] >= 1
    assert _run_status(root) == "deployed"
    assert not copy.exists()
    assert _refusals(root) == []
    assert "was merged by an earlier deploy of deadbeef" in out
    assert "merged lime/feature/41-toy" not in out
    rows = _deploy_rows(root)
    assert len(rows) == 2
    assert all(row["ended_at"] is not None for row in rows)


def test_deploy_finishing_run_that_dies_again_is_finished_by_the_next(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    calls = _half_done_deploy(monkeypatch, root, "merge")

    with pytest.raises(_Died):
        main(["deploy", "41"])

    # The finishing run's teardown dies once — it must still leave its own deploy row.
    hits = {"n": 0}

    def boom_teardown(*a, **kw):
        hits["n"] += 1
        if hits["n"] == 1:
            raise _Died()
        calls.update(["teardown"])
        return []

    monkeypatch.setattr(cli.worktree, "teardown", boom_teardown)

    with pytest.raises(_Died):
        main(["deploy", "41"])
    assert _run_status(root) == "success"
    assert len(_deploy_rows(root)) == 2

    assert main(["deploy", "41"]) == 0
    assert _run_status(root) == "deployed"
    assert calls["merge"] == 1
    assert len(_deploy_rows(root)) == 3


def test_deploy_refuses_a_merged_pr_without_an_earlier_deploy_row(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)

    # Proof must be a `kind='deploy'` row for *this* Run — a run-row and another Run's
    # deploy row are not proof that lime merged deadbeef's PR.
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_process("deadbeef", kind="run", name="lime", pid=os.getpid(), command="lime run 41")
    # A different ticket stem so `_resolve_deploy_run("41")` still lands on deadbeef.
    tr.start_run(
        "cafef00d", workflow="feature", request="do X",
        ticket=str(root / ".lime" / "tickets" / "99-other.md"),
        branch="lime/feature/99-other", worktree=str(tmp_path / "worktrees" / "cafef00d"),
    )
    tr.start_process("cafef00d", kind="deploy", name="lime", pid=os.getpid(), command="lime deploy 41")
    tr.con.close()

    monkeypatch.setattr(
        cli.git_helper, "pr_view", lambda branch: {"state": "MERGED", "statusCheckRollup": []}
    )
    _fail_if_called(monkeypatch, (cli.git_helper, "merge_pr"), "a hand-merged PR must refuse before merging")
    _fail_if_called(monkeypatch, (cli.git_helper, "fast_forward"), "a refused deploy must not fast-forward")
    _fail_if_called(monkeypatch, (cli.worktree, "teardown"), "a refused deploy must not tear down")
    _forbid_gate(monkeypatch)

    assert main(["deploy", "41"]) == 1
    assert _refusals(root) == [
        ("deadbeef", "hand_merge", {"ticket": "41-toy", "state": "MERGED"})
    ]
    assert _deploy_rows(root) == []
    assert _run_status(root) == "success"


def test_earlier_deploy_row_reads_no_proof_from_a_ledger_without_processes(tmp_path):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE runs (run_id TEXT)")
    assert cli._earlier_deploy_row(con, "deadbeef") is False
    con.close()

    root = tmp_path / "project"
    _toy_project(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    con = tr.con
    assert cli._earlier_deploy_row(con, "deadbeef") is False
    tr.start_process("deadbeef", kind="deploy", name="lime", pid=os.getpid(), command="lime deploy 41")
    assert cli._earlier_deploy_row(con, "deadbeef") is True
    assert cli._earlier_deploy_row(con, "cafef00d") is False
    con.close()


def test_deploy_rerun_after_teardown_finds_every_step_already_done(
    tmp_path, monkeypatch, capsys
):
    root, origin, _ = _deploy_gate_project(tmp_path, monkeypatch, advance_default=False)
    branch = "lime/feature/41-toy"
    wt = tmp_path / "worktrees" / "project" / "deadbeef"
    # The Run's delete-on-close: removing the tracked copy makes the "survived the merge"
    # check pass on the second run.
    _git(wt, "rm", "-q", ".lime/tickets/41-toy.md")
    _git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "close ticket")
    _git(wt, "push", "-q", "origin", branch)

    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.worktree, "CC_PROJECTS", tmp_path / "cc")

    calls: Counter = Counter()
    pr = {"state": "OPEN"}
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": pr["state"], "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    monkeypatch.setattr(
        cli, "_check_deploy_merged_tree", lambda *a, **kw: calls.update(["gate"]) or 0
    )

    def merge(branch):
        _merge_on_origin(origin, branch, tmp_path / "scratch-merge")
        pr["state"] = "MERGED"
        calls.update(["merge"])

    monkeypatch.setattr(cli.git_helper, "merge_pr", merge)
    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: [])
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    real_teardown = cli.worktree.teardown
    problems = []

    def wrap_teardown(*a, **kw):
        result = real_teardown(*a, **kw)
        problems.append(result)
        if len(problems) == 1:
            raise _Died()
        return result

    monkeypatch.setattr(cli.worktree, "teardown", wrap_teardown)

    with pytest.raises(_Died):
        main(["deploy", "41"])
    assert _run_status(root) == "success"
    assert _rev(root, "main") == _rev(origin, "main")

    capsys.readouterr()
    assert main(["deploy", "41"]) == 0

    assert problems == [[], []]
    assert _run_status(root) == "deployed"
    assert _rev(root, "main") == _rev(origin, "main")
    assert not wt.exists()
    assert _out(root, "branch", "--list", "lime/*") == ""
    assert calls["gate"] == 1 and calls["merge"] == 1


def test_deploy_runs_the_interactive_session_and_records_tokens(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    ticket_path.unlink()  # delete-on-close reached main with the merge (ticket 61)

    monkeypatch.setattr(
        cli.git_helper, "pr_view", lambda branch: {"state": "OPEN", "statusCheckRollup": []}
    )
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: None)
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])
    monkeypatch.setattr(
        cli.config, "load",
        lambda root: {"commands": {}, "deploy": ["vercel deploy --prod"]},
    )

    session_dir = tmp_path / "cc-session"
    monkeypatch.setattr(cli.worktree, "session_dir", lambda path: session_dir)

    captured = {}

    class Done:
        returncode = 0

    def fake_run(cmd, cwd=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        session_id = cmd[cmd.index("--session-id") + 1]
        session_dir.mkdir(parents=True, exist_ok=True)
        usage = {
            "input_tokens": 100,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 5,
        }
        turn = json.dumps({"type": "assistant", "message": {"id": "m1", "usage": usage}})
        # the same content block repeats with an identical id — must not double-count
        (session_dir / f"{session_id}.jsonl").write_text(turn + "\n" + turn + "\n")
        return Done()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert main(["deploy", "41"]) == 0

    assert "vercel deploy --prod" in " ".join(captured["cmd"])
    assert captured["cwd"] == root
    assert "--strict-mcp-config" in captured["cmd"]
    assert cli.DEPLOY_MODEL in captured["cmd"]

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    run_row = con.execute(
        "SELECT status, total_tokens FROM runs WHERE run_id='deadbeef'"
    ).fetchone()
    assert run_row["status"] == "deployed"
    assert run_row["total_tokens"] == 125  # 100 + 20 + 5, counted once
    session_row = con.execute(
        "SELECT role, runtime, model, budget_warn, budget_stop FROM sessions WHERE run_id='deadbeef'"
    ).fetchone()
    assert session_row["role"] == "orchestrator"
    assert session_row["runtime"] == "claude_code"
    assert session_row["model"] == cli.DEPLOY_MODEL
    # the run's config carried no budget key → the uniform defaults (claude_code window unknown)
    assert (session_row["budget_warn"], session_row["budget_stop"]) == (150_000, 250_000)
    phase_row = con.execute(
        "SELECT kind, owner, status, stage FROM phases WHERE run_id='deadbeef' AND name='deploy'"
    ).fetchone()
    assert (phase_row["kind"], phase_row["owner"], phase_row["status"], phase_row["stage"]) == (
        "deploy", "orchestrator", "success", "Ship",
    )


def test_deploy_warns_but_still_marks_deployed_when_the_session_exits_nonzero(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    ticket_path.unlink()  # delete-on-close reached main with the merge (ticket 61)

    monkeypatch.setattr(
        cli.git_helper, "pr_view", lambda branch: {"state": "OPEN", "statusCheckRollup": []}
    )
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: None)
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])
    monkeypatch.setattr(
        cli.config, "load", lambda root: {"commands": {}, "deploy": ["vercel deploy --prod"]}
    )
    monkeypatch.setattr(cli.worktree, "session_dir", lambda path: tmp_path / "cc-session")

    class Crashed:
        returncode = 1

    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, cwd=None: Crashed())

    assert main(["deploy", "41"]) == 0  # already merged and cleaned — that stands regardless
    err = capsys.readouterr().err
    assert "exited 1" in err
    assert "lime trace deadbeef" in err

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    run_row = con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()
    assert run_row["status"] == "deployed"
    phase_row = con.execute(
        "SELECT status FROM phases WHERE run_id='deadbeef' AND name='deploy'"
    ).fetchone()
    assert phase_row["status"] == "fail"


def test_deploy_fast_forwards_the_local_default_branch_after_the_merge(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)

    # Simulate the Run's branch, then return to main.
    _git(root, "checkout", "-q", "-b", "lime/feature/41-toy")
    (root / ".lime" / "tickets" / "41-toy.md").unlink()  # delete-on-close on the branch (ticket 61)
    (root / "app.py").write_text("v2\n")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "feature")
    _git(root, "push", "-q", "origin", "lime/feature/41-toy")
    _git(root, "checkout", "-q", "main")

    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=root / ".lime" / "tickets" / "41-toy.md")

    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    monkeypatch.setattr(
        cli.git_helper, "merge_pr",
        lambda branch: _git(root, "push", "-q", "origin", f"{branch}:main"),
    )
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    assert main(["deploy", "41"]) == 0
    assert (root / "app.py").read_text() == "v2\n"
    out = capsys.readouterr().out
    assert "fast-forward" in out


def test_deploy_fast_forwards_while_the_next_ticket_is_still_pending(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.worktree, "CC_PROJECTS", tmp_path / "cc-projects")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    # Write ticket A (untracked — the new contract says "write a file, don't commit it")
    tickets_dir = root / ".lime" / "tickets"
    (tickets_dir / "42-a.md").write_text(TICKET_TEXT.replace("# 41", "# 42"))

    # Fake the feature chain — this is where it fails today (refused as untracked)
    monkeypatch.setattr(cli.feature, "run", _fake_chain("v2\n"))
    assert main(["run", "42"]) == 0

    # Write ticket B (also untracked — pending on the engineer's side)
    (tickets_dir / "43-b.md").write_text(TICKET_TEXT.replace("# 41", "# 43"))

    # Patch config.load for deploy (skip the template's interactive vercel deploy)
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    monkeypatch.setattr(
        cli.git_helper, "merge_pr",
        lambda branch: _merge_on_origin(origin, branch, tmp_path / "merger"),
    )

    assert main(["deploy", "42"]) == 0

    # Assertions
    assert _rev(root, "main") == _rev(root, "origin/main"), "main must fast-forward to origin/main"
    assert (root / "app.py").read_text() == "v2\n"
    assert "could not fast-forward" not in capsys.readouterr().err
    assert not (root / ".lime/tickets/42-a.md").exists(), "closed ticket copy removed"
    assert (root / ".lime/tickets/43-b.md").exists(), "pending ticket B untouched"
    # B is untracked (??)
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--", ".lime/tickets/43-b.md"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert status.startswith("??"), f"B should be untracked, got {status!r}"
    # Exactly one commit titled "ticket: 42-a" in the whole graph
    log = subprocess.run(
        ["git", "-C", str(root), "log", "--all", "--format=%s"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    assert [s for s in log if "ticket: 42-a" in s] == ["ticket: 42-a"], \
        f"expected exactly one 'ticket: 42-a', got {log}"
    # Branch is cleaned up
    branches = subprocess.run(
        ["git", "-C", str(root), "branch", "--list", "lime/feature/42-a"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branches == "", f"branch should be deleted, got {branches!r}"


def test_deploy_warns_but_still_deploys_when_the_local_branch_cannot_fast_forward(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)

    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: None)
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: (_ for _ in ()).throw(RuntimeError("not a fast-forward")))
    teardown_calls = []

    def fake_teardown(repo_root, run_id, branch, *, command=None, env=None, base=None):
        teardown_calls.append((repo_root, run_id, branch, command, env))
        return []

    monkeypatch.setattr(cli.worktree, "teardown", fake_teardown)
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    assert main(["deploy", "41"]) == 0
    err = capsys.readouterr().err
    assert "could not fast-forward" in err
    assert len(teardown_calls) == 1
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()
    assert row["status"] == "deployed"


def test_deploy_fast_forwards_between_the_merge_and_the_teardown(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)
    ticket_path.unlink()  # delete-on-close reached main with the merge (ticket 61)

    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    calls = []
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: calls.append("merge"))
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: calls.append("ff") or "abc1234")
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: calls.append("teardown") or [])
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    assert main(["deploy", "41"]) == 0
    assert calls == ["merge", "ff", "teardown"]


# --- deploy: ticket copy cleanup (ticket 59) ---


def test_deploy_removes_the_closed_ticket_s_untracked_copy_after_the_fast_forward(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)

    # Seed the run with ticket_path=root/.lime/tickets/42-a.md
    ticket_path = root / ".lime" / "tickets" / "42-a.md"
    ticket_path.write_text(TICKET_TEXT.replace("# 41", "# 42"))
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)

    # Write untracked 42-a.md (same name as the run's ticket) and 43-b.md
    (root / ".lime" / "tickets" / "42-a.md").write_text("closed copy")
    (root / ".lime" / "tickets" / "43-b.md").write_text("pending copy")

    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: None)
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    assert main(["deploy", "42"]) == 0

    assert not (root / ".lime" / "tickets" / "42-a.md").exists()
    assert (root / ".lime" / "tickets" / "43-b.md").exists()
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--", ".lime/tickets/43-b.md"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert status.startswith("??")
    out = capsys.readouterr().out
    assert "removed .lime/tickets/42-a.md" in out


def test_deploy_keeps_the_ticket_copy_when_the_fast_forward_is_refused(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)

    # Seed the run with a ticket path that is NOT committed by _toy_project
    ticket_path = root / ".lime" / "tickets" / "42-a.md"
    ticket_path.write_text(TICKET_TEXT.replace("# 41", "# 42"))
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)

    # Write an untracked copy of the ticket (42-a.md was never committed)
    (root / ".lime" / "tickets" / "42-a.md").write_text("leftover copy")

    def fail_if_called(*a, **kw):
        raise AssertionError("no reconcile after a refused fast-forward")

    monkeypatch.setattr(cli.git_helper, "merged_prs", fail_if_called)

    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: None)
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: (_ for _ in ()).throw(RuntimeError("not a fast-forward")))
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    assert main(["deploy", "42"]) == 0
    # Ticket copy still exists because ff was refused
    assert (root / ".lime" / "tickets" / "42-a.md").read_text() == "leftover copy"
    # Run is still marked deployed
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()
    assert row["status"] == "deployed"


# --- deploy: stale-ticket reconcile + --check (ticket 61) ---


def _advance_origin_main(origin: Path, scratch: Path, n: int = 1) -> None:
    """Push n commits to origin/main from a scratch clone — local main stays put."""
    _git(scratch.parent, "clone", "-q", str(origin), str(scratch))
    _git(scratch, "config", "user.email", "t@t")
    _git(scratch, "config", "user.name", "t")
    for i in range(n):
        (scratch / "app.py").write_text(f"origin {i}\n")
        _git(scratch, "commit", "-qam", f"origin {i}")
    _git(scratch, "push", "-q", "origin", "main")


def test_pr_ticket_stem_takes_the_segment_after_lime_workflow():
    assert cli._pr_ticket_stem("lime/bug-fix/54-x") == "54-x"
    assert cli._pr_ticket_stem("lime/install") is None
    assert cli._pr_ticket_stem("main") is None


def test_stale_tickets_matches_ticket_stems_to_merged_head_branches(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    tickets = root / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    (tickets / "54-old.md").write_text("old ticket")
    (tickets / "60-pending.md").write_text("pending ticket")
    merged = [
        {"number": 16, "headRefName": "lime/bug-fix/54-old"},
        {"number": 2, "headRefName": "lime/install"},
        {"number": 99, "headRefName": "lime/feature/99-gone"},
    ]
    found = cli._stale_tickets(root, merged)
    assert found == [(Path(".lime/tickets/54-old.md"), 16)]


def test_deploy_warns_about_stale_tickets_left_by_earlier_merged_prs(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)

    # Remove the deployed ticket (41-toy.md) as delete-on-close would
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    ticket_path.unlink()

    # Write a stale ticket (54-old.md) and commit it — mirrors tracked leftovers
    (root / ".lime" / "tickets" / "54-old.md").write_text("stale tracked ticket\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add stale tracked ticket")

    # Write an untracked stale ticket (60-pending.md) — extra noise
    (root / ".lime" / "tickets" / "60-pending.md").write_text("pending ticket\n")

    monkeypatch.setattr(
        cli.git_helper, "merged_prs",
        lambda: [{"number": 16, "headRefName": "lime/bug-fix/54-old"},
                 {"number": 3, "headRefName": "lime/feature/41-toy"}],
    )

    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)

    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: None)
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    assert main(["deploy", "41"]) == 0
    err = capsys.readouterr().err
    assert "stale ticket: .lime/tickets/54-old.md (PR #16 merged)" in err
    assert "60-pending" not in err
    assert "41-toy" not in err

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()
    assert row["status"] == "deployed"
    assert (root / ".lime" / "tickets" / "54-old.md").exists()


def test_deploy_fails_when_the_deployed_ticket_survives_the_merge(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)

    monkeypatch.setattr(
        cli.git_helper, "merged_prs",
        lambda: [{"number": 3, "headRefName": "lime/feature/41-toy"}],
    )
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: None)
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")
    teardown_calls = []

    def fake_teardown(repo_root, run_id, branch, *, command=None, env=None, base=None):
        teardown_calls.append((repo_root, run_id, branch, command, env))
        return []

    monkeypatch.setattr(cli.worktree, "teardown", fake_teardown)
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    assert main(["deploy", "41"]) == 1
    err = capsys.readouterr().err
    assert ".lime/tickets/41-toy.md" in err
    assert "survived" in err
    # 41-toy appears once in err (only in the ticket path, not as a stale-ticket line)
    assert err.count("41-toy.md") == 1
    assert len(teardown_calls) == 1

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()
    assert row["status"] == "deployed"


def test_deploy_skips_the_stale_check_when_gh_is_unavailable(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _stub_gate(monkeypatch)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    ticket_path.unlink()  # delete-on-close reached main

    _seed_failed_run(root, tmp_path / "worktrees", status="success", ticket_path=ticket_path)

    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: (_ for _ in ()).throw(RuntimeError("gh not found")))
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: None)
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])
    monkeypatch.setattr(cli.config, "load", lambda root: {"commands": {}, "deploy": []})

    assert main(["deploy", "41"]) == 0
    err = capsys.readouterr().err
    assert "stale-ticket check skipped: gh unavailable" in err


def test_deploy_without_a_ticket_or_check_refuses(capsys):
    assert main(["deploy"]) == 1
    err = capsys.readouterr().err
    assert "ticket number is required" in err


def test_deploy_check_reports_up_to_date(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)

    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")
    monkeypatch.setattr(cli.git_helper, "sync_state", lambda branch: (0, 0))

    assert main(["deploy", "--check"]) == 0
    out, err = capsys.readouterr()
    assert "up to date" in out
    assert "no stale tickets" in out


def test_deploy_check_reports_fast_forwardable_without_moving_main(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)

    _advance_origin_main(origin, tmp_path / "scratch", 1)
    before = _rev(root, "main")

    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")

    assert main(["deploy", "--check"]) == 0
    out, err = capsys.readouterr()
    assert "fast-forwardable (1 behind)" in out
    assert _rev(root, "main") == before
    assert (root / "app.py").read_text() == "v1\n"


def test_deploy_check_reports_diverged(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)

    # Local commit on main
    (root / "app.py").write_text("local\n")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "local")
    _advance_origin_main(origin, tmp_path / "scratch", 2)

    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")

    assert main(["deploy", "--check"]) == 1
    out, err = capsys.readouterr()
    assert "diverged (1 ahead / 2 behind)" in out


def test_deploy_check_treats_ahead_only_as_diverged(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)

    # Local commit on main, origin untouched
    (root / "app.py").write_text("local\n")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "local")

    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")

    assert main(["deploy", "--check"]) == 1
    out, err = capsys.readouterr()
    assert "diverged (1 ahead / 0 behind)" in out


def test_deploy_check_fails_on_stale_tickets_even_when_up_to_date(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)

    # Commit a stale ticket and push it — still up to date
    (root / ".lime" / "tickets" / "54-old.md").write_text("stale\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add stale")
    _git(root, "push", "-q", "origin", "main")

    monkeypatch.setattr(cli.git_helper, "merged_prs",
        lambda: [{"number": 16, "headRefName": "lime/bug-fix/54-old"}],
    )
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")

    assert main(["deploy", "--check"]) == 1
    out, err = capsys.readouterr()
    assert "up to date" in out
    assert "stale ticket: .lime/tickets/54-old.md (PR #16 merged)" in out


def test_deploy_check_leaves_the_repo_untouched(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)

    # Stale ticket — commit and push so main stays up to date
    (root / ".lime" / "tickets" / "54-old.md").write_text("stale\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "stale")
    _git(root, "push", "-q", "origin", "main")

    # Local commit on main (diverges)
    (root / "app.py").write_text("local\n")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "local")

    # Advance origin by 1 (now diverged: 1 ahead / 1 behind)
    _advance_origin_main(origin, tmp_path / "scratch", 1)

    monkeypatch.setattr(cli.git_helper, "merged_prs",
        lambda: [{"number": 16, "headRefName": "lime/bug-fix/54-old"}],
    )
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")

    def fail_if_called(*a, **kw):
        raise AssertionError("--check must not write anything")

    monkeypatch.setattr(cli.git_helper, "merge_pr", fail_if_called)
    monkeypatch.setattr(cli.git_helper, "fast_forward", fail_if_called)
    monkeypatch.setattr(cli.worktree, "teardown", fail_if_called)

    before = (
        _rev(root, "main"),
        _rev(root, "HEAD"),
        subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                        capture_output=True, text=True, check=True).stdout,
        subprocess.run(["git", "-C", str(root), "branch", "--show-current"],
                        capture_output=True, text=True, check=True).stdout.strip(),
        sorted(p.name for p in (root / ".lime" / "tickets").glob("*.md")),
        (root / "app.py").read_text(),
    )

    assert main(["deploy", "--check"]) == 1

    after = (
        _rev(root, "main"),
        _rev(root, "HEAD"),
        subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                        capture_output=True, text=True, check=True).stdout,
        subprocess.run(["git", "-C", str(root), "branch", "--show-current"],
                        capture_output=True, text=True, check=True).stdout.strip(),
        sorted(p.name for p in (root / ".lime" / "tickets").glob("*.md")),
        (root / "app.py").read_text(),
    )

    assert before == after
    assert not (root / ".lime" / "runs").exists()


def test_deploy_check_reports_ff_state_when_gh_is_unavailable(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)

    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: (_ for _ in ()).throw(RuntimeError("gh not found")))
    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")

    assert main(["deploy", "--check"]) == 0
    out, err = capsys.readouterr()
    assert "up to date" in out
    assert "stale-ticket check skipped: gh unavailable" in out


def test_deploy_check_fails_without_an_origin(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)

    monkeypatch.setattr(cli.worktree, "default_branch", lambda root: "main")

    assert main(["deploy", "--check"]) == 1
    err = capsys.readouterr().err
    assert err  # non-empty error about no origin


# --- deploy: pre-merge merged-tree gate + base movement (ticket 141) ---


def _out(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _origin_main(origin: Path) -> str:
    return _out(origin, "rev-parse", "main")


def _checkout_snapshot(root: Path) -> dict:
    """Original checkout + Run branch state — the gate must leave every field identical."""
    return {
        "head": _rev(root, "HEAD"),
        "branch": _out(root, "branch", "--show-current"),
        "status": _out(root, "status", "--porcelain"),
        "staged": _out(root, "diff", "--cached", "--name-only"),
        "app": (root / "app.py").read_text(),
        "run_branch": _out(root, "rev-parse", "refs/heads/lime/feature/41-toy"),
        "worktrees": _out(root, "worktree", "list", "--porcelain"),
    }


def _push_default_file(origin: Path, scratch: Path, name: str) -> None:
    """Advance origin/main from a scratch clone by adding one default-only file."""
    _git(scratch.parent, "clone", "-q", str(origin), str(scratch))
    _git(scratch, "config", "user.email", "t@t")
    _git(scratch, "config", "user.name", "t")
    (scratch / name).write_text("default\n")
    _git(scratch, "add", "-A")
    _git(scratch, "commit", "-q", "-m", f"add {name}")
    _git(scratch, "push", "-q", "origin", "main")


def _push_default_merge(origin: Path, scratch: Path, topic: str) -> None:
    """Advance origin/main with one first-parent merge commit whose topic carries 2 commits."""
    _git(scratch.parent, "clone", "-q", str(origin), str(scratch))
    _git(scratch, "config", "user.email", "t@t")
    _git(scratch, "config", "user.name", "t")
    _git(scratch, "checkout", "-q", "-b", topic)
    for n in (1, 2):
        (scratch / f"{topic}-{n}.txt").write_text(f"{topic} {n}\n")
        _git(scratch, "add", "-A")
        _git(scratch, "commit", "-q", "-m", f"{topic} {n}")
    _git(scratch, "checkout", "-q", "main")
    _git(scratch, "merge", "--no-ff", "-q", "-m", f"Merge {topic}", topic)
    _git(scratch, "push", "-q", "origin", "main")


def _seed_gated_run(root: Path, tmp_path: Path, *, base_sha, status="success", run_id="deadbeef",
                    port=None, db_name=None):
    """A real Run trace row pinned at `base_sha`, with its branch named like a Run's."""
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run(
        run_id, workflow="feature", request="do X", ticket=str(ticket_path),
        branch="lime/feature/41-toy", worktree=str(tmp_path / "worktrees" / run_id),
        base_sha=base_sha, port=port, db_name=db_name,
    )
    tr.set_run_status(run_id, status)
    return ticket_path


def _deploy_gate_project(tmp_path, monkeypatch, *, advance_default=True):
    """Bare origin + a real launch worktree + a real Run trace row for gate tests.

    The ticket is left untracked on the default branch (a real launch shape) and the
    Run is born with real `cli.worktree.create`, so `base_sha` is the actual birth pin
    whose parent is origin/main's tip. The Run branch then adds pr-only.txt. With
    advance_default, origin/main moves on (a default-only commit), leaving the pin on a
    sibling history. Returns (root, origin, base_sha).
    """
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin, track_ticket=False)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    worktree_path = cli.worktree.create(
        root, "deadbeef", "lime/feature/41-toy",
        ticket=ticket_path, base=tmp_path / "worktrees",
    )
    base_sha = _rev(worktree_path, "HEAD")
    (worktree_path / "pr-only.txt").write_text("pr\n")
    _git(worktree_path, "add", "-A")
    _git(worktree_path, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "pr work")
    _git(worktree_path, "push", "-q", "-u", "origin", "lime/feature/41-toy")
    if advance_default:
        _push_default_file(origin, tmp_path / "scratch-default", "default-only.txt")
    _seed_gated_run(root, tmp_path, base_sha=base_sha).unlink()  # delete-on-close reached default
    return root, origin, base_sha


def _deploy_conflict_project(tmp_path, monkeypatch):
    """Like `_deploy_gate_project` but origin/main and the Run branch touch the same file."""
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)
    base_sha = _rev(root, "main")
    _git(root, "checkout", "-q", "-b", "lime/feature/41-toy")
    (root / "app.py").write_text("branch\n")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "branch change")
    _git(root, "push", "-q", "-u", "origin", "lime/feature/41-toy")
    _git(root, "checkout", "-q", "main")
    scratch = tmp_path / "scratch-conflict"
    _git(scratch.parent, "clone", "-q", str(origin), str(scratch))
    _git(scratch, "config", "user.email", "t@t")
    _git(scratch, "config", "user.name", "t")
    (scratch / "app.py").write_text("default\n")
    _git(scratch, "commit", "-qam", "default change")
    _git(scratch, "push", "-q", "origin", "main")
    _seed_gated_run(root, tmp_path, base_sha=base_sha).unlink()
    return root


def _mock_open_pr(monkeypatch):
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch: {"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )


def _gate_phase_spy(monkeypatch):
    """Record the phases the gate runs, delegating to the real quality runner."""
    phases = []
    real_run_check = cli.quality.run_check

    def spy(adapter, name):
        phases.append(name)
        return real_run_check(adapter, name)

    monkeypatch.setattr(cli.quality, "run_check", spy)
    return phases


def _run_base_sha(root: Path, run_id="deadbeef") -> str | None:
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    return con.execute(
        "SELECT base_sha FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()["base_sha"]


def _launch_repo(tmp_path: Path) -> tuple[Path, str]:
    """A real Git repo with one default commit; returns (root, default_sha)."""
    root = tmp_path / "pin-project"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "app.py").write_text("v1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root, _rev(root, "HEAD")


def _launch_commit(
    root: Path, *, stem="41-toy", run_id="deadbeef", role="orchestrator", extra=None,
    note="",
) -> str:
    """A launch-shaped commit exactly as `worktree.create` writes it; returns its SHA."""
    ticket = root / ".lime" / "tickets" / f"{stem}.md"
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text(f"# {stem}\n{note}\n")
    if extra:
        (root / extra).write_text("extra\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
         "-m", f"ticket: {stem}\n\nLime-Run: {run_id}\nLime-Role: {role}")
    return _rev(root, "HEAD")


def _predicate_case(case: str, root: Path, default: str) -> str:
    """Build the negative history named by `case`; returns the pin to inspect."""
    if case == "nonlaunch":
        (root / "app.py").write_text("changed\n")
        _git(root, "commit", "-qam", "ordinary work")
        return _rev(root, "HEAD")
    if case == "other_run":
        return _launch_commit(root, run_id="cafebabe")
    if case == "wrong_ticket":
        return _launch_commit(root, stem="99-other")
    if case == "wrong_role":
        return _launch_commit(root, role="builder")
    if case == "extra_file":
        return _launch_commit(root, extra="app.py")
    if case == "merge":
        _git(root, "checkout", "-q", "-b", "side", default)
        (root / "side.txt").write_text("side\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "side work")
        _git(root, "checkout", "-q", "main")
        (root / "main.txt").write_text("main\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "main work")
        _git(root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        return _rev(root, "HEAD")
    raise AssertionError(case)


def _fail_if_called(monkeypatch, target, message):
    def fail(*a, **kw):
        raise AssertionError(message)

    monkeypatch.setattr(target[0], target[1], fail)


@pytest.mark.parametrize("red", [False, True])
def test_deploy_tests_merged_tree_before_merge(tmp_path, monkeypatch, capsys, red):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    before = _checkout_snapshot(root)
    # a real ticket pin, and the default tip moved on independently after launch
    assert base_sha != _origin_main(origin)
    assert _out(root, "log", "-1", "--format=%s", base_sha) == "ticket: 41-toy"
    setup_cmd = "printf setup > .lime-setup-marker"
    if red:
        test_cmd = "echo GATE-FAIL-TAIL; exit 7"
    else:
        test_cmd = (
            "test -f .lime-setup-marker && test -f default-only.txt && test -f pr-only.txt"
            " && echo GATE-MERGED-OK"
        )
    monkeypatch.setattr(
        cli.config, "load",
        lambda r: {
            "commands": {"setup": setup_cmd, "test": test_cmd, "teardown": "true"},
            "deploy": [],
        },
    )
    _mock_open_pr(monkeypatch)
    phases = _gate_phase_spy(monkeypatch)
    calls = {}

    def fake_merge(branch):
        calls["branch"] = branch
        calls["state_at_merge"] = _checkout_snapshot(root)

    monkeypatch.setattr(cli.git_helper, "merge_pr", fake_merge)
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: [])
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])

    assert main(["deploy", "41"]) == (1 if red else 0)
    out, err = capsys.readouterr()
    assert "base unmoved" not in out  # the default moved, so the merged tree is tested
    assert phases == ["setup", "test", "teardown"]  # configured phases run in order
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    status = con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()["status"]

    if red:
        assert "merged-tree test failed" in err
        assert "exited 7" in err
        assert "GATE-FAIL-TAIL" in err
        match = re.search(r"output: (\S+)", err)
        assert match
        log = Path(match.group(1))
        assert log.exists() and "GATE-FAIL-TAIL" in log.read_text()
        assert "branch" not in calls  # merge never ran
        assert status == "success"    # Run stays success, ready for a later deploy
        assert _refusals(root) == [
            ("deadbeef", "merged_tree_failed", {"ticket": "41-toy", "check": "test", "returncode": 7})
        ]
    else:
        assert calls["branch"] == "lime/feature/41-toy"
        # merge happens only after cleanup: original checkout + no temp worktree left
        assert calls["state_at_merge"] == before
        assert status == "deployed"
        assert _refusals(root) == []


def test_deploy_base_unmoved_skips_commands(tmp_path, monkeypatch, capsys):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch, advance_default=False)
    # the pin is a real launch commit whose parent is the fetched origin default tip
    default_sha = _origin_main(origin)
    assert base_sha != default_sha
    assert _rev(root, f"{base_sha}^") == default_sha
    assert _out(root, "log", "-1", "--format=%s", base_sha) == "ticket: 41-toy"
    body = _out(root, "log", "-1", "--format=%B", base_sha)
    assert "Lime-Run: deadbeef" in body
    assert "Lime-Role: orchestrator" in body
    monkeypatch.setattr(
        cli.config, "load",
        lambda r: {
            "commands": {"setup": "false", "test": "false", "teardown": "false"},
            "deploy": [],
        },
    )
    _fail_if_called(monkeypatch, (cli.quality, "run_check"), "unmoved base must not run commands")
    _fail_if_called(monkeypatch, (cli.git_helper, "merged_worktree"), "unmoved base must not build a worktree")
    _fail_if_called(monkeypatch, (cli, "_allocate_check_port"), "unmoved base must not allocate a port")
    _mock_open_pr(monkeypatch)
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: None)
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: [])
    teardowns = []
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: teardowns.append(kw) or [])
    before = _checkout_snapshot(root)

    assert main(["deploy", "41"]) == 0
    out = capsys.readouterr().out
    assert "lime deploy: base unmoved" in out
    assert len(teardowns) == 1  # skipping the check does not skip normal deployment cleanup
    assert _checkout_snapshot(root) == before


@pytest.mark.parametrize(
    "case,expected",
    [
        ("equal", True),
        ("single", True),
        ("multiple", True),
        ("nonlaunch", False),
        ("other_run", False),
        ("wrong_ticket", False),
        ("wrong_role", False),
        ("extra_file", False),
        ("merge", False),
    ],
)
def test_deploy_base_unmoved_requires_only_own_launch_commits(tmp_path, case, expected):
    root, default = _launch_repo(tmp_path)
    if case == "equal":
        base_sha = default
    elif case == "single":
        base_sha = _launch_commit(root)
    elif case == "multiple":
        _launch_commit(root)
        base_sha = _launch_commit(root, note="second")
    else:
        base_sha = _predicate_case(case, root, default)
    refs_before = _out(root, "show-ref")
    head_before = _rev(root, "HEAD")

    result = cli._deploy_base_unmoved(
        root, "deadbeef", base_sha, default, "lime/feature/41-toy"
    )

    assert result is expected
    assert _out(root, "show-ref") == refs_before  # read-only: no ref moved
    assert _rev(root, "HEAD") == head_before


@pytest.mark.parametrize("pin", [None, ""])
def test_deploy_base_unmoved_rejects_null_pin(tmp_path, pin):
    root, default = _launch_repo(tmp_path)
    assert cli._deploy_base_unmoved(
        root, "deadbeef", pin, default, "lime/feature/41-toy"
    ) is False


def test_deploy_base_unmoved_rejects_unknown_branch_shape(tmp_path):
    root, default = _launch_repo(tmp_path)
    base_sha = _launch_commit(root)
    assert cli._deploy_base_unmoved(
        root, "deadbeef", base_sha, default, "lime/install"
    ) is False


def test_deploy_nonancestor_pin_runs_full_check(tmp_path, monkeypatch, capsys):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    default_sha = _origin_main(origin)
    # a resolvable pin on a sibling history: the default tip is not its ancestor
    birth_parent = _rev(root, f"{base_sha}^")
    _git(root, "checkout", "-q", "-b", "sibling", birth_parent)
    (root / "sibling.txt").write_text("s\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "sibling work")
    sibling_sha = _rev(root, "HEAD")
    _git(root, "checkout", "-q", "main")
    assert sibling_sha != default_sha
    assert cli._deploy_base_unmoved(
        root, "deadbeef", sibling_sha, default_sha, "lime/feature/41-toy"
    ) is False

    phases = _gate_phase_spy(monkeypatch)
    marker = tmp_path / "setup-marker"
    commands = {
        "setup": f"touch {shlex.quote(str(marker))}",
        "test": "true",
        "teardown": "true",
    }

    rc = cli._check_deploy_merged_tree(
        root, "deadbeef", sibling_sha, "lime/feature/41-toy", commands
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "base unmoved" not in out
    assert phases == ["setup", "test", "teardown"]
    assert marker.exists()


@pytest.mark.parametrize("case", ["unavailable_pin", "inspection_failure"])
def test_deploy_pin_inspection_failure_runs_full_check(tmp_path, monkeypatch, capsys, case):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    default_sha = _origin_main(origin)
    birth_parent = _rev(root, f"{base_sha}^")
    _git(root, "checkout", "-q", "-b", "sibling", birth_parent)
    (root / "sibling.txt").write_text("s\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "sibling work")
    pin = _rev(root, "HEAD")
    _git(root, "checkout", "-q", "main")
    if case == "unavailable_pin":
        pin = "0" * 40
    else:
        # patch only the new inspection boundary: a failed read is never a skip
        monkeypatch.setattr(cli, "_deploy_git_read", lambda *a, **kw: None)

    phases = _gate_phase_spy(monkeypatch)
    marker = tmp_path / "setup-marker"
    commands = {
        "setup": f"touch {shlex.quote(str(marker))}",
        "test": "true",
        "teardown": "true",
    }
    before = _checkout_snapshot(root)
    pin_before = _run_base_sha(root)

    assert cli._check_deploy_merged_tree(
        root, "deadbeef", pin, "lime/feature/41-toy", commands
    ) == 0
    out = capsys.readouterr().out
    assert "base unmoved" not in out
    assert phases == ["setup", "test", "teardown"]
    assert marker.exists()

    # a repeated read-only decision recomputes and leaves pin + checkout snapshot intact
    phases.clear()
    assert cli._check_deploy_merged_tree(
        root, "deadbeef", pin, "lime/feature/41-toy", commands
    ) == 0
    assert _run_base_sha(root) == pin_before
    assert _checkout_snapshot(root) == before


def test_deploy_conflict_refuses_before_commands_or_merge(tmp_path, monkeypatch, capsys):
    root = _deploy_conflict_project(tmp_path, monkeypatch)
    before = _checkout_snapshot(root)
    monkeypatch.setattr(
        cli.config, "load",
        lambda r: {"commands": {"setup": "false", "test": "false"}, "deploy": []},
    )
    _fail_if_called(monkeypatch, (cli.quality, "run_check"), "conflict must refuse before commands")
    _fail_if_called(monkeypatch, (cli.git_helper, "merge_pr"), "conflict must refuse before merge")
    _fail_if_called(monkeypatch, (cli.worktree, "teardown"), "conflict must refuse before teardown")
    _mock_open_pr(monkeypatch)

    assert main(["deploy", "41"]) == 1
    err = capsys.readouterr().err
    assert "conflict" in err.lower()
    assert "app.py" in err
    assert _checkout_snapshot(root) == before
    assert "lime-deploy-" not in _out(root, "worktree", "list", "--porcelain")
    assert _refusals(root) == [
        ("deadbeef", "merge_conflict", {"ticket": "41-toy", "paths": ["app.py"]})
    ]


def test_deploy_setup_failure_refuses_before_test(tmp_path, monkeypatch, capsys):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli.config, "load",
        lambda r: {
            "commands": {"setup": "echo SETUP-BOOM; exit 3", "test": "echo SHOULD-NOT-RUN; exit 9"},
            "deploy": [],
        },
    )
    test_calls = []
    real_run_check = cli.quality.run_check

    def spy(adapter, name):
        if name == "test":
            test_calls.append(name)
        return real_run_check(adapter, name)

    monkeypatch.setattr(cli.quality, "run_check", spy)
    _mock_open_pr(monkeypatch)
    _fail_if_called(monkeypatch, (cli.git_helper, "merge_pr"), "setup failure must refuse before merge")
    _fail_if_called(monkeypatch, (cli.worktree, "teardown"), "setup failure must refuse before teardown")

    assert main(["deploy", "41"]) == 1
    err = capsys.readouterr().err
    assert "merged-tree setup failed" in err
    assert "SETUP-BOOM" in err
    assert test_calls == []
    match = re.search(r"output: (\S+)", err)
    assert match and Path(match.group(1)).exists()
    assert _refusals(root) == [
        ("deadbeef", "merged_tree_failed", {"ticket": "41-toy", "check": "setup", "returncode": 3})
    ]


def test_deploy_fetch_failure_refuses(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)  # no origin at all
    monkeypatch.chdir(root)
    _seed_gated_run(root, tmp_path, base_sha=None)
    monkeypatch.setattr(cli.config, "load", lambda r: {"commands": {"test": "true"}, "deploy": []})
    _mock_open_pr(monkeypatch)
    _fail_if_called(monkeypatch, (cli.git_helper, "merge_pr"), "fetch failure must refuse before merge")
    _fail_if_called(monkeypatch, (cli.git_helper, "fast_forward"), "fetch failure must refuse before fast-forward")
    _fail_if_called(monkeypatch, (cli.worktree, "teardown"), "fetch failure must refuse before teardown")

    assert main(["deploy", "41"]) == 1
    assert "cannot resolve the base tip" in capsys.readouterr().err


def test_deploy_missing_test_refuses(tmp_path, monkeypatch, capsys):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.config, "load", lambda r: {"commands": {}, "deploy": []})
    _mock_open_pr(monkeypatch)
    _fail_if_called(monkeypatch, (cli.quality, "run_check"), "no test command must refuse")
    _fail_if_called(monkeypatch, (cli.git_helper, "merge_pr"), "no test command must refuse before merge")

    assert main(["deploy", "41"]) == 1
    assert "commands.test is unset" in capsys.readouterr().err


@pytest.mark.parametrize("extra,expected", [({}, 600), ({"test_timeout": 1500}, 1500)])
def test_deploy_merged_tree_check_uses_the_test_timeout(tmp_path, monkeypatch, extra, expected):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    recorded = []
    real = cli.quality._run

    def spy(spec, run):
        recorded.append((spec.name, spec.timeout_seconds))
        return real(spec, run)

    monkeypatch.setattr(cli.quality, "_run", spy)
    rc = cli._check_deploy_merged_tree(
        root, "deadbeef", base_sha, "lime/feature/41-toy", {"test": "true", **extra}
    )
    assert rc == 0
    assert ("test", expected) in recorded


def test_deploy_invalid_test_timeout_refuses(tmp_path, monkeypatch, capsys):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli.config, "load",
        lambda r: {"commands": {"test": "true", "test_timeout": 0}, "deploy": []},
    )
    _mock_open_pr(monkeypatch)
    _fail_if_called(monkeypatch, (cli.quality, "run_check"), "bad test_timeout must refuse")
    _fail_if_called(monkeypatch, (cli.git_helper, "merge_pr"), "bad test_timeout must refuse before merge")

    assert main(["deploy", "41"]) == 1
    assert "commands.test_timeout" in capsys.readouterr().err


def test_deploy_missing_pr_ref_refuses(tmp_path, monkeypatch, capsys):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    _git(root, "push", "-q", "origin", "--delete", "lime/feature/41-toy")
    monkeypatch.setattr(cli.config, "load", lambda r: {"commands": {"test": "true"}, "deploy": []})
    _mock_open_pr(monkeypatch)
    _fail_if_called(monkeypatch, (cli.git_helper, "merge_pr"), "missing PR ref must refuse before merge")

    assert main(["deploy", "41"]) == 1
    assert "cannot resolve the PR head" in capsys.readouterr().err


def test_deploy_legacy_unpinned_run_tests_instead_of_skipping(tmp_path, monkeypatch, capsys):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    # A legacy Run has a NULL pin, so conservatively test the merged tree with the real runner.
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.con.execute("UPDATE runs SET base_sha=NULL WHERE run_id='deadbeef'")
    tr.con.commit()
    monkeypatch.setattr(
        cli.config, "load",
        lambda r: {"commands": {"test": "echo GATE-LEGACY-RAN; exit 5"}, "deploy": []},
    )
    _mock_open_pr(monkeypatch)
    _fail_if_called(monkeypatch, (cli.git_helper, "merge_pr"), "a failed gate must refuse before merge")
    _fail_if_called(monkeypatch, (cli.worktree, "teardown"), "a failed gate must refuse before teardown")

    assert main(["deploy", "41"]) == 1
    err = capsys.readouterr().err
    assert "merged-tree test failed" in err
    assert "GATE-LEGACY-RAN" in err  # a real quality invocation ran, not a mocked result


def test_deploy_check_reports_each_open_run_base(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: [])

    base_a = _origin_main(origin)
    _push_default_merge(origin, tmp_path / "m1", "topicA")
    base_b = _origin_main(origin)
    _push_default_merge(origin, tmp_path / "m2", "topicB")
    tip = _origin_main(origin)

    # a diverged (never-merged) side commit
    _git(root, "checkout", "-q", "-b", "side", "main")
    (root / "side.txt").write_text("side\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "side")
    side = _rev(root, "HEAD")
    _git(root, "checkout", "-q", "main")
    _git(root, "branch", "-D", "side")

    tr = cli.tracer.Tracer(root / ".lime" / "runs")

    def run_row(run_id, base_sha, status, kind="run"):
        tr.start_run(
            run_id, workflow="feature", request="x", kind=kind,
            ticket=str(root / ".lime" / "tickets" / f"{run_id}-toy.md"),
            branch=f"lime/feature/{run_id}-toy", base_sha=base_sha,
        )
        if status == "deployed":
            tr.set_run_status(run_id, "success")
        if status == "needs_rescope":
            _plant_legacy_status(tr, run_id, status)
        else:
            tr.set_run_status(run_id, status)

    run_row("aaaa1111", base_a, "success")          # behind 2 merges
    run_row("bbbb2222", base_b, "fail")             # behind 1 merge
    run_row("cccc3333", tip, "success")             # equal
    run_row("dddd4444", side, "success")            # diverged
    run_row("eeee5555", None, "needs_rescope")      # NULL pin
    run_row("ffff6666", "f" * 40, "success")        # unresolvable
    run_row("gggg7777", base_a, "success", kind="scout")  # not a run
    run_row("hhhh8888", base_a, "deployed")         # closed

    before = (
        _rev(root, "main"),
        _out(root, "status", "--porcelain"),
        _out(root, "worktree", "list", "--porcelain"),
        {r["run_id"]: r["status"] for r in tr.con.execute("SELECT run_id, status FROM runs")},
    )

    assert main(["deploy", "--check"]) == 0
    out, _ = capsys.readouterr()
    assert "aaaa1111 (aaaa1111-toy) base moved: 2 merges since base" in out
    assert "bbbb2222 (bbbb2222-toy) base moved: 1 merges since base" in out
    assert "cccc3333" not in out and "dddd4444" not in out and "eeee5555" not in out
    assert "ffff6666 cannot inspect base" in out
    assert "gggg7777" not in out and "hhhh8888" not in out

    after = (
        _rev(root, "main"),
        _out(root, "status", "--porcelain"),
        _out(root, "worktree", "list", "--porcelain"),
        {r["run_id"]: r["status"] for r in tr.con.execute("SELECT run_id, status FROM runs")},
    )
    assert before == after  # no DB writes, no checkout modification


def _seed_pre_144_ledger(root: Path) -> Path:
    """A `runs` table as it shipped before `base_sha` (ticket 210). Raw sqlite3, never `Tracer`."""
    db = root / ".lime" / "runs" / "lime.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    sql = re.sub(r"^\s*base_sha\s+TEXT,\n", "", cli.tracer._SCHEMA, flags=re.M)
    assert "base_sha" not in sql
    con = sqlite3.connect(db)
    con.executescript(sql)
    rows = [
        ("aaaa1111", "success", 1, "run"),
        ("bbbb2222", "fail", 2, "run"),
        ("cccc3333", "deployed", 3, "run"),
        ("dddd4444", "discarded", 4, "run"),
        ("eeee5555", "success", 5, "scout"),
    ]
    for run_id, status, seq, kind in rows:
        con.execute(
            "INSERT INTO runs (run_id, workflow, status, started_at, ticket, branch, kind)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, "feature", status, f"2020-01-0{seq}T00:00:00Z",
                str(root / ".lime" / "tickets" / f"{run_id}-toy.md"),
                f"lime/feature/{run_id}-toy", kind,
            ),
        )
    con.commit()
    con.close()
    return db


def test_deploy_check_on_a_pre_144_ledger_lists_open_runs_twice(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: [])
    db = _seed_pre_144_ledger(root)

    calls = []
    real_base_movement = cli.git_helper.base_movement

    def spy(repo_root, base_sha, default_sha):
        calls.append(base_sha)
        return real_base_movement(repo_root, base_sha, default_sha)

    monkeypatch.setattr(cli.git_helper, "base_movement", spy)

    for _ in range(2):
        calls.clear()
        assert main(["deploy", "--check"]) == 0
        out = capsys.readouterr().out
        assert "is up to date" in out
        assert "no stale tickets" in out
        assert calls == [None, None]  # the two open kind='run' rows, both unpinned
        # legacy open Runs are listed in the output, with their ids and tickets
        assert "aaaa1111 (aaaa1111-toy) has no recorded base" in out
        assert "bbbb2222 (bbbb2222-toy) has no recorded base" in out
        assert "cccc3333" not in out and "dddd4444" not in out and "eeee5555" not in out

    check = sqlite3.connect(db)
    cols = {r[1] for r in check.execute("PRAGMA table_info(runs)")}
    assert "base_sha" not in cols  # deploy --check wrote nothing
    check.close()


def test_read_only_readers_work_on_a_pre_144_ledger(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)
    db = _seed_pre_144_ledger(root)

    con = cli.tracer.connect(db, readonly=True)
    row = cli._resolve_deploy_run(con, "aaaa1111")
    assert row["run_id"] == "aaaa1111"
    assert row["base_sha"] is None
    con.close()

    assert {r["run_id"] for r, _ in cli.overlap._read_local_rows(root)} == {
        "aaaa1111", "bbbb2222"
    }

    assert main(["runs"]) == 0
    assert main(["trace", "aaaa1111"]) == 0

    # Last, because its read-write `Tracer` migrates the ledger.
    assert main(["run", "--resume", "dddd4444"]) == 1
    assert "only fail or budget_exhausted" in capsys.readouterr().err


@pytest.mark.parametrize(
    "rel", ["lime/skill/lime/SKILL.md", ".claude/skills/lime/SKILL.md"]
)
def test_skill_routes_cover_merged_tree_refusal_and_base_movement(rel):
    text = (_REPO_ROOT / rel).read_text()
    assert "moved Run bases" in text
    assert "merged-tree conflict" in text
    assert "merged-tree setup/test failure" in text
    assert "never `gh pr merge`" in text
    assert "docs_merge_" in text
    assert "documenter's `writes:`" in text


# --- ticket next: print the next free ticket number ---


def test_ticket_next_prints_one_integer_from_git_not_from_the_tickets_dir(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)

    _git(root, "branch", "lime/feature/72-x")
    _git(root, "push", "-q", "origin", "lime/feature/72-x")

    assert main(["ticket", "next"]) == 0
    assert capsys.readouterr().out.strip() == "73"

    # Even after removing local tickets, the answer is still from git
    (root / ".lime" / "tickets" / "41-toy.md").unlink()
    assert main(["ticket", "next"]) == 0
    assert capsys.readouterr().out.strip() == "73"


def test_ticket_next_refuses_outside_a_git_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["ticket", "next"]) == 1
    assert "not a git repository" in capsys.readouterr().err


def test_run_refuses_a_number_another_run_reserved_on_origin(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)

    # Write a ticket file with number 41
    tickets = root / ".lime" / "tickets"
    (tickets / "41-other.md").write_text(TICKET_TEXT.replace("# 41 — Toy Ticket", "# 41 — Other"))

    # Reserve 41 on origin as if another Run won
    from lime import numbering
    numbering.reserve(root, 41, "41-toy", "run-a")

    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    def fail_if_called(*a, **kw):
        raise AssertionError("must not create worktree when reservation fails")

    monkeypatch.setattr(cli.worktree, "create", fail_if_called)

    assert main(["run", "41-other"]) == 1
    err = capsys.readouterr().err
    assert f"{numbering.RESERVATION_NS}/41" in err
    assert "41-toy" in err

    # No branch was created
    branches = subprocess.run(
        ["git", "-C", str(root), "branch", "--list", "lime/feature/41-other"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert branches == ""


def test_run_reserves_the_number_on_origin_before_the_worktree_is_born(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)

    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.worktree, "CC_PROJECTS", tmp_path / "cc-projects")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    from lime import numbering

    reservation_checked = []

    original_create = cli.worktree.create

    def create_with_assert(repo_root, run_id, branch, *, setup=None, env=None, base=None, ticket=None):
        # Assert the reservation already exists on origin
        ls = subprocess.run(
            ["git", "-C", str(repo_root), "ls-remote", "origin", f"{numbering.RESERVATION_NS}/41"],
            capture_output=True, text=True,
        )
        assert ls.stdout.strip(), f"{numbering.RESERVATION_NS}/41 must exist before worktree.create"
        reservation_checked.append(True)
        return original_create(repo_root, run_id, branch, setup=setup, env=env, base=base, ticket=ticket)

    monkeypatch.setattr(cli.worktree, "create", create_with_assert)

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["cwd"] = Path.cwd()
        captured["ticket"] = ticket.slug
        run_.tracer.set_run_status(run_.run_id, "success")
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "41"]) == 0
    assert len(reservation_checked) == 1
    assert captured["ticket"] == "41-toy"


# --- ticket close / reopen: set the header Status: line ---


_TICKET_BODY = (
    "# 10 — A task\n"
    "Type: task\n"
    "Status: open\n"
    "Blocked by:\n"
    "\n"
    "## Ask\n"
    "Status: open is an example line\n"
    "\n"
    "## Where\n"
    "`x`\n"
)


def _registered_ticket(tmp_path, monkeypatch, body=_TICKET_BODY):
    work = tmp_path / "work"
    tickets = work / "alpha" / ".lime" / "tickets"
    tickets.mkdir(parents=True)
    ticket = tickets / "10-a-task.md"
    ticket.write_text(body)
    registry = tmp_path / "projects"
    registry.write_text("# reg\n" + str(work / "alpha") + "\n")
    monkeypatch.setenv("LIME_PROJECTS", str(registry))
    monkeypatch.delenv("LIME_AGENT_RUNTIME", raising=False)
    return ticket


def test_ticket_close_then_reopen_changes_only_the_header_status_line(tmp_path, monkeypatch, capsys):
    ticket = _registered_ticket(tmp_path, monkeypatch)
    before = ticket.read_bytes()

    assert main(["ticket", "close", "alpha", "10"]) == 0
    assert capsys.readouterr().out == "alpha · 10-a-task · closed\n"
    after_close = before.replace(b"Status: open\n", b"Status: closed\n", 1)
    assert ticket.read_bytes() == after_close
    # The body's example `Status: open` line is untouched.
    assert b"Status: open is an example line" in ticket.read_bytes()

    # Re-running close on already-closed bytes writes nothing, same output.
    assert main(["ticket", "close", "alpha", "10"]) == 0
    assert capsys.readouterr().out == "alpha · 10-a-task · closed\n"
    assert ticket.read_bytes() == after_close

    # reopen resolves "010" to the same ticket and restores the bytes.
    assert main(["ticket", "reopen", "alpha", "010"]) == 0
    assert ticket.read_bytes() == before

    assert list(ticket.parent.glob("*.tmp")) == []


def test_ticket_close_json_reports_project_ticket_status(tmp_path, monkeypatch, capsys):
    _registered_ticket(tmp_path, monkeypatch)

    assert main(["ticket", "close", "alpha", "10", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "project": "alpha", "ticket": "10-a-task", "status": "closed"
    }

    assert main(["ticket", "reopen", "alpha", "10", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "open"


@pytest.mark.parametrize(
    "argv, fragment",
    [
        (["ticket", "close", "nope", "10"], 'project "nope" is not registered in'),
        (["ticket", "close", "alpha", "99"], "no ticket 99 in"),
        (["ticket", "reopen", "alpha", "11"], "has no Status: line above its first ## heading"),
    ],
)
def test_ticket_close_refusals_exit_2_one_line_untouched(tmp_path, monkeypatch, capsys, argv, fragment):
    ticket = _registered_ticket(tmp_path, monkeypatch)
    (ticket.parent / "11-no-status.md").write_text(
        "# 11 — N\nType: task\n\n## Ask\nStatus: open\n"
    )
    tickets_dir = ticket.parent
    before = {p.name: p.read_bytes() for p in tickets_dir.iterdir()}

    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.strip().splitlines()) == 1
    assert fragment in captured.err
    assert {p.name: p.read_bytes() for p in tickets_dir.iterdir()} == before


@pytest.mark.parametrize("verb", ["close", "reopen"])
def test_ticket_close_and_reopen_are_owner_only(tmp_path, monkeypatch, capsys, verb):
    ticket = _registered_ticket(tmp_path, monkeypatch)
    monkeypatch.setenv("LIME_AGENT_RUNTIME", "pi")
    before = ticket.read_bytes()

    assert main(["ticket", verb, "alpha", "10"]) == 2
    captured = capsys.readouterr()
    assert captured.err == f"lime ticket {verb}: owner-only; refused inside a Role session\n"
    assert ticket.read_bytes() == before


# --- research: on-demand researcher Session (ticket 37, spec §14) ---


def _research_usage() -> UsageBreakdown:
    return UsageBreakdown(input=100, output=50, total=150, cost=CostBreakdown(total=0.01))


def _research_stub_adapter(reply_json: dict, write: dict[str, str] | None = None):
    """One adapter reply, same shape as test_workflows_phases.py's stub_adapter."""
    calls = []

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        calls.append(req)
        for rel_path, content in (write or {}).items():
            p = Path(rel_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return AgentResult(
            status="ok", text="", session_id=req.session_id,
            usage=_research_usage(), cost=0.01, context_tokens=150, context_window=100_000,
            envelope=reply_json,
        )

    adapter.calls = calls
    return adapter


def _research_cfg() -> dict:
    return {
        "defaults": {"runtime": "pi", "model": "openrouter/x/y", "thinking": "low"},
        "agents": [
            {"name": "researcher", "prompt_engineering": {}, "tools": ["read", "write"], "writes": []},
        ],
        "profiles": {
            "api-research": {"agents": [{"name": "researcher", "model": "openrouter/y/flash"}]},
        },
        "commands": {},
    }


def _research_reply(artifact: str) -> dict:
    return {
        "status": "success",
        "summary": "answered",
        "artifacts": [artifact],
        "sources": [{"url": "https://example.com/docs", "note": "settled it"}],
    }


def _research_fail_reply(artifact: str) -> dict:
    # a brief + source are still declared so the gates (artifacts_exist,
    # sources_cited) pass cleanly — this reply tests envelope.status handling,
    # not the gates
    return {
        "status": "fail",
        "summary": "no sources found",
        "artifacts": [artifact],
        "sources": [{"url": "https://example.com/docs", "note": "irrelevant"}],
    }


def test_research_creates_its_own_run_row_and_prints_the_brief_path(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "deadbeef")
    monkeypatch.setattr(cli.config, "load", lambda root: _research_cfg())
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    brief_path = root / ".lime" / "runs" / "deadbeef" / "context_handoff" / "research-x.md"
    adapter = _research_stub_adapter(
        _research_reply(str(brief_path)), write={str(brief_path): "# brief\n"}
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["research", "what does X do?"]) == 0
    out = capsys.readouterr().out
    assert str(brief_path) in out

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT kind, workflow, status, request FROM runs WHERE run_id='deadbeef'").fetchone()
    assert row["kind"] == "research"
    assert row["workflow"] == ""
    assert row["status"] == "success"
    assert row["request"] == "what does X do?"
    phase = con.execute(
        "SELECT owner, kind, status FROM phases WHERE run_id='deadbeef' AND name='research'"
    ).fetchone()
    assert (phase["owner"], phase["kind"], phase["status"]) == ("researcher", "agent", "success")
    assert_table_stages(con, "deadbeef")


def test_research_joins_the_parent_run_when_lime_run_id_is_set(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "load", lambda root: _research_cfg())
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("R1", workflow="feature", request="build the thing", kind="run")
    monkeypatch.setenv("LIME_RUN_ID", "R1")

    brief_path = root / ".lime" / "runs" / "R1" / "context_handoff" / "research-x.md"
    adapter = _research_stub_adapter(
        _research_reply(str(brief_path)), write={str(brief_path): "# brief\n"}
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["research", "what does X do?"]) == 0
    assert str(brief_path) in capsys.readouterr().out

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT kind, workflow, status FROM runs WHERE run_id='R1'").fetchone()
    # joining a Run never touches the row the parent chain already owns
    assert (row["kind"], row["workflow"], row["status"]) == ("run", "feature", "running")
    phase = con.execute(
        "SELECT owner, status FROM phases WHERE run_id='R1' AND name='research'"
    ).fetchone()
    assert (phase["owner"], phase["status"]) == ("researcher", "success")


def test_research_joins_via_lime_runs_dir_from_inside_a_worktree(tmp_path, monkeypatch, capsys):
    """The planner's bash tool runs with cwd inside the Run's worktree, which has
    no `.lime/runs/` of its own (gitignored, never checked out) — `LIME_RUNS_DIR`
    (set by `lime run` alongside `LIME_RUN_ID`) is what lets it find the
    project's real trace db instead of starting a disconnected one (spec §14)."""
    root = tmp_path / "project"
    _toy_project(root)
    worktree_root = tmp_path / "worktree"
    worktree_root.mkdir()

    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("R1", workflow="feature", request="build the thing", kind="run")

    monkeypatch.chdir(worktree_root)  # simulates the planner's bash tool cwd
    monkeypatch.setattr(cli.config, "load", lambda root: _research_cfg())
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    monkeypatch.setenv("LIME_RUN_ID", "R1")
    monkeypatch.setenv("LIME_RUNS_DIR", str(root / ".lime" / "runs"))

    brief_path = root / ".lime" / "runs" / "R1" / "context_handoff" / "research-x.md"
    adapter = _research_stub_adapter(
        _research_reply(str(brief_path)), write={str(brief_path): "# brief\n"}
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["research", "what does X do?"]) == 0
    assert str(brief_path) in capsys.readouterr().out
    assert not (worktree_root / ".lime").exists()  # never spawned a disconnected trace db here

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    rows = con.execute("SELECT run_id FROM runs").fetchall()
    assert [r["run_id"] for r in rows] == ["R1"]  # joined, never a second row
    phase = con.execute(
        "SELECT status FROM phases WHERE run_id='R1' AND name='research'"
    ).fetchone()
    assert phase["status"] == "success"


def test_research_marks_a_standalone_run_failed_when_the_agent_errors(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "deadbeef")
    monkeypatch.setattr(cli.config, "load", lambda root: _research_cfg())
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    brief_path = root / ".lime" / "runs" / "deadbeef" / "context_handoff" / "research-x.md"
    adapter = _research_stub_adapter(
        _research_fail_reply(str(brief_path)), write={str(brief_path): "# brief\n"}
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["research", "what does X do?"]) == 1
    assert "no sources found" in capsys.readouterr().err

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()
    assert row["status"] == "fail"


def test_research_reports_a_failure_on_the_joined_run_without_touching_its_status(
    tmp_path, monkeypatch, capsys
):
    """Joining must never settle the shared row (§14: the parent chain owns that) —
    not even when the research phase itself fails."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "load", lambda root: _research_cfg())
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("R1", workflow="feature", request="build the thing", kind="run")
    parent_pid = 999999  # a decoy — real liveness is beside the point of this assertion
    tr.start_process(run_id="R1", kind="run", name="lime", pid=parent_pid, command="lime run x")
    monkeypatch.setenv("LIME_RUN_ID", "R1")
    monkeypatch.setenv("LIME_RUNS_DIR", str(root / ".lime" / "runs"))

    brief_path = root / ".lime" / "runs" / "R1" / "context_handoff" / "research-x.md"
    adapter = _research_stub_adapter(
        _research_fail_reply(str(brief_path)), write={str(brief_path): "# brief\n"}
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["research", "what does X do?"]) == 1
    assert "no sources found" in capsys.readouterr().err

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT status FROM runs WHERE run_id='R1'").fetchone()
    assert row["status"] == "running"  # a failed research phase must not fail the parent chain's Run
    phase = con.execute(
        "SELECT status FROM phases WHERE run_id='R1' AND name='research'"
    ).fetchone()
    assert phase["status"] == "fail"
    # the parent's own process row is untouched; only research's own registration was closed
    processes = con.execute(
        "SELECT pid, ended_at FROM processes WHERE run_id='R1' AND kind='run' ORDER BY id"
    ).fetchall()
    by_pid = {p["pid"]: p["ended_at"] for p in processes}
    assert by_pid[parent_pid] is None
    research_pid = next(pid for pid in by_pid if pid != parent_pid)
    assert by_pid[research_pid] is not None


def test_research_refuses_when_the_roster_does_not_validate(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "load", lambda root: _research_cfg())
    monkeypatch.setattr(
        cli.config, "validate_before_spawn", lambda *a, **kw: ["researcher: no model resolves"]
    )

    assert main(["research", "what does X do?"]) == 1
    err = capsys.readouterr().err
    assert "FAIL  researcher: no model resolves" in err
    assert not (root / ".lime" / "runs" / "lime.db").exists()


def test_research_passes_profile_and_chain_tests_false_to_validation(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "deadbeef")
    monkeypatch.setattr(cli.config, "load", lambda root: _research_cfg())

    captured = {}

    def fake_validate(raw, roles, profile=None, chain_tests=True, root=None):
        captured["roles"] = roles
        captured["profile"] = profile
        captured["chain_tests"] = chain_tests
        return []

    monkeypatch.setattr(cli.config, "validate_before_spawn", fake_validate)

    brief_path = root / ".lime" / "runs" / "deadbeef" / "context_handoff" / "research-x.md"
    adapter = _research_stub_adapter(
        _research_reply(str(brief_path)), write={str(brief_path): "# brief\n"}
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["research", "what does X do?", "--profile", "api-research"]) == 0
    assert captured["roles"] == ["researcher"]
    assert captured["profile"] == "api-research"
    assert captured["chain_tests"] is False

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT profile FROM runs WHERE run_id='deadbeef'").fetchone()
    assert row["profile"] == "api-research"


# --- prototype: the designer loop, Upstream, HITL, main working tree (ticket 38, spec §14) ---


def _prototype_cfg() -> dict:
    return {
        "defaults": {"runtime": "pi", "model": "openrouter/x/y", "thinking": "low"},
        "agents": [
            {"name": "scout", "prompt_engineering": {}, "tools": ["read", "write"], "writes": []},
            {
                "name": "designer", "prompt_engineering": {}, "tools": ["read", "write"],
                "writes": ["**/proto-<slug>/**"],
            },
        ],
        "commands": {},
    }


def _prototype_usage() -> UsageBreakdown:
    return UsageBreakdown(input=100, output=50, total=150, cost=CostBreakdown(total=0.01))


def _prototype_stub_adapter(
    scout_reply: dict, designer_reply: dict,
    scout_write: dict[str, str] | None = None, designer_write: dict[str, str] | None = None,
):
    """One stub adapter serving both on-demand Roles the loop calls, differentiated by the
    rendered system prompt (each Role's own `# <Name> Agent` heading, same shape as
    `_research_stub_adapter`). Each Role only ever writes its own files — a scout call
    writing the designer's route file would trip the fence for the wrong reason."""
    calls = []

    def adapter(req, on_event=None, on_spawn=None, on_exit=None):
        calls.append(req)
        is_scout = "# Scout Agent" in req.system_prompt
        for rel_path, content in ((scout_write if is_scout else designer_write) or {}).items():
            p = Path(rel_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            if on_event:
                on_event({"type": "tool_execution_start", "toolCallId": "w1", "toolName": "write", "args": {"path": rel_path, "content": content}})
                on_event({"type": "tool_execution_end", "toolCallId": "w1", "toolName": "write"})
        reply = scout_reply if is_scout else designer_reply
        if on_event:
            on_event({"type": "turn", "model": "openrouter/x/y", "usage": {"input": 100, "output": 50}, "context_tokens": 150, "cost": 0.01})
        return AgentResult(
            status="ok", text="", session_id=req.session_id,
            usage=_prototype_usage(), cost=0.01, context_tokens=150, context_window=100_000,
            envelope=reply,
        )

    adapter.calls = calls
    return adapter


def _scout_reply(brief_path: str) -> dict:
    return {
        "status": "success",
        "summary": "found the router and tokens",
        "findings": [{"file": "app/layout.tsx", "note": "shared layout"}],
        "artifacts": [brief_path],
    }


def _design_reply(route_dir="app/proto-hero/", url_path="/proto-hero", variant_ids=("a", "b")) -> dict:
    return {
        "status": "success",
        "summary": "variants built",
        "route_dir": route_dir,
        "url_path": url_path,
        "variants": [{"id": v, "note": f"variant {v}"} for v in variant_ids],
        "notes_path": "",
        "artifacts": [],
    }


def _lock_reply(notes_path: str, route_dir="app/proto-hero/", url_path="/proto-hero") -> dict:
    return {
        "status": "success",
        "summary": "locked variant b",
        "route_dir": route_dir,
        "url_path": url_path,
        "variants": [{"id": "b", "note": "the winner"}],
        "notes_path": notes_path,
        "artifacts": [],
    }


def _prototype_setup(root: Path, monkeypatch) -> None:
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "load", lambda root=Path("."): _prototype_cfg())
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])


def test_prototype_start_scouts_then_designs_inside_the_fence(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _prototype_setup(root, monkeypatch)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "beef1234")

    brief_path = root / ".lime" / "runs" / "beef1234" / "context_handoff" / "scout_findings.md"
    route_file = root / "app" / "proto-hero" / "page.tsx"
    adapter = _prototype_stub_adapter(
        _scout_reply(str(brief_path)), _design_reply(),
        scout_write={str(brief_path): "# brief\n"},
        designer_write={str(route_file): "export default function P() {}\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["prototype", "hero", "make it pop"]) == 0
    out = capsys.readouterr().out
    assert "beef1234" in out and "/proto-hero" in out and "a, b" in out

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute(
        "SELECT kind, ticket, status, port, branch, worktree FROM runs WHERE run_id='beef1234'"
    ).fetchone()
    assert (row["kind"], row["ticket"], row["status"]) == ("prototype", "hero", "running")
    assert row["port"] is None and row["branch"] is None and row["worktree"] is None
    names = [
        p["name"] for p in
        con.execute("SELECT name FROM phases WHERE run_id='beef1234' ORDER BY seq").fetchall()
    ]
    assert names == ["scout", "design"]


def test_prototype_continue_reuses_the_run_and_the_designer_session(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _prototype_setup(root, monkeypatch)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "beef1234")

    brief_path = root / ".lime" / "runs" / "beef1234" / "context_handoff" / "scout_findings.md"
    route_file = root / "app" / "proto-hero" / "page.tsx"
    start_adapter = _prototype_stub_adapter(
        _scout_reply(str(brief_path)), _design_reply(),
        scout_write={str(brief_path): "# brief\n"},
        designer_write={str(route_file): "v1\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", start_adapter)
    assert main(["prototype", "hero", "make it pop"]) == 0
    designer_session_id = next(
        r.session_id for r in start_adapter.calls if "# Designer Agent" in r.system_prompt
    )

    continue_adapter = _prototype_stub_adapter(
        _scout_reply(str(brief_path)), _design_reply(), designer_write={str(route_file): "v2\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", continue_adapter)
    assert main(["prototype", "hero", "make b bolder", "--continue"]) == 0

    [call] = continue_adapter.calls
    assert call.session_id != designer_session_id
    assert call.resume is False

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    assert con.execute(
        "SELECT status FROM runs WHERE run_id='beef1234'"
    ).fetchone()["status"] == "running"
    names = [
        p["name"] for p in
        con.execute("SELECT name FROM phases WHERE run_id='beef1234' ORDER BY seq").fetchall()
    ]
    assert names == ["scout", "design", "design_continue"]
    assert_table_stages(con, "beef1234")


def test_prototype_lock_writes_notes_and_marks_success(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _prototype_setup(root, monkeypatch)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "beef1234")

    brief_path = root / ".lime" / "runs" / "beef1234" / "context_handoff" / "scout_findings.md"
    route_file = root / "app" / "proto-hero" / "page.tsx"
    start_adapter = _prototype_stub_adapter(
        _scout_reply(str(brief_path)), _design_reply(),
        scout_write={str(brief_path): "# brief\n"},
        designer_write={str(route_file): "v1\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", start_adapter)
    assert main(["prototype", "hero", "make it pop"]) == 0

    notes_path = root / "app" / "proto-hero" / "NOTES.md"
    lock_adapter = _prototype_stub_adapter(
        _scout_reply(str(brief_path)), _lock_reply(str(notes_path)),
        designer_write={str(notes_path): "## Fixed\nlayout\n## Free\ncopy\n## Open\nanimations\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", lock_adapter)
    assert main(["prototype", "hero", "ship variant b", "--lock"]) == 0

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    assert con.execute(
        "SELECT status FROM runs WHERE run_id='beef1234'"
    ).fetchone()["status"] == "success"
    assert_table_stages(con, "beef1234")


def test_prototype_write_outside_the_fence_aborts_the_phase(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _prototype_setup(root, monkeypatch)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "beef1234")

    brief_path = root / ".lime" / "runs" / "beef1234" / "context_handoff" / "scout_findings.md"
    rogue_file = root / "outside.txt"
    adapter = _prototype_stub_adapter(
        _scout_reply(str(brief_path)), _design_reply(),
        scout_write={str(brief_path): "# brief\n"},
        designer_write={str(rogue_file): "not allowed\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["prototype", "hero", "make it pop"]) == 1
    assert "lime prototype:" in capsys.readouterr().err
    assert not rogue_file.exists()  # untracked write outside the fence, rolled back

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    assert con.execute(
        "SELECT status FROM runs WHERE run_id='beef1234'"
    ).fetchone()["status"] == "fail"


def test_prototype_start_refuses_when_one_is_already_open(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _prototype_setup(root, monkeypatch)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("p1", workflow="", request="x", kind="prototype", ticket="hero")

    assert main(["prototype", "hero", "again"]) == 1
    assert "already has an open prototype" in capsys.readouterr().err


def test_prototype_continue_refuses_without_an_open_prototype(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _prototype_setup(root, monkeypatch)

    assert main(["prototype", "hero", "feedback", "--continue"]) == 1
    assert "no open prototype" in capsys.readouterr().err


def test_prototype_continue_refuses_once_locked(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _prototype_setup(root, monkeypatch)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("p1", workflow="", request="x", kind="prototype", ticket="hero")
    tr.set_run_status("p1", "success")

    assert main(["prototype", "hero", "feedback", "--continue"]) == 1
    assert "'success'" in capsys.readouterr().err


def test_prototype_start_is_allowed_again_after_a_prior_lock(tmp_path, monkeypatch, capsys):
    """A locked (`success`) row closes ITS loop, not the slug — nothing in the spec makes
    a slug unique for all time, and a second independent prototype at the same route name
    is a reasonable thing to want (17-designer-role.md never claims otherwise)."""
    root = tmp_path / "project"
    _prototype_setup(root, monkeypatch)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("old", workflow="", request="x", kind="prototype", ticket="hero")
    tr.set_run_status("old", "success")
    monkeypatch.setattr(cli, "new_id", lambda n=8: "beef1234")

    brief_path = root / ".lime" / "runs" / "beef1234" / "context_handoff" / "scout_findings.md"
    route_file = root / "app" / "proto-hero" / "page.tsx"
    adapter = _prototype_stub_adapter(
        _scout_reply(str(brief_path)), _design_reply(),
        scout_write={str(brief_path): "# brief\n"}, designer_write={str(route_file): "v1\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["prototype", "hero", "redo it"]) == 0
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    rows = con.execute(
        "SELECT run_id, status FROM runs WHERE ticket='hero' ORDER BY started_at"
    ).fetchall()
    assert [r["run_id"] for r in rows] == ["old", "beef1234"]
    assert rows[-1]["status"] == "running"


def test_prototype_continue_resumes_a_budget_exhausted_prototype(tmp_path, monkeypatch, capsys):
    """Same resumability `lime run --resume` gives a budget-exhausted Run — and it must not
    crash: `budget_exhausted` only transitions to `running` or `discarded` (tracer.py), so a
    later `run.finish()`/`close_run()` from this call needs the row flipped to `running` first."""
    root = tmp_path / "project"
    _prototype_setup(root, monkeypatch)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("p1", workflow="", request="x", kind="prototype", ticket="hero")
    tr.set_run_status("p1", "budget_exhausted")

    brief_path = root / ".lime" / "runs" / "p1" / "context_handoff" / "scout_findings.md"
    route_file = root / "app" / "proto-hero" / "page.tsx"
    adapter = _prototype_stub_adapter(
        _scout_reply(str(brief_path)), _design_reply(), designer_write={str(route_file): "v1\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["prototype", "hero", "pick up where you left off", "--continue"]) == 0
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    assert con.execute("SELECT status FROM runs WHERE run_id='p1'").fetchone()["status"] == "running"


def test_fence_designer_to_slug_leaves_unrestricted_writes_alone():
    """`writes: None` means unrestricted (permissions.py) — substituting `<slug>` into `[]`
    would silently turn an unrestricted designer read-only."""
    roster = {"designer": AgentConfig(name="designer", model="m", writes=None)}
    run = SimpleNamespace(agents=roster, agent=lambda name: roster[name])
    cli._fence_designer_to_slug(run, "hero")
    assert run.agents["designer"].writes is None


def test_prototype_ref_flag_becomes_an_attachment(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _prototype_setup(root, monkeypatch)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "beef1234")
    ref = root / "mock.png"
    ref.write_bytes(b"fake")

    brief_path = root / ".lime" / "runs" / "beef1234" / "context_handoff" / "scout_findings.md"
    route_file = root / "app" / "proto-hero" / "page.tsx"
    adapter = _prototype_stub_adapter(
        _scout_reply(str(brief_path)), _design_reply(),
        scout_write={str(brief_path): "# brief\n"},
        designer_write={str(route_file): "v1\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["prototype", "hero", "match this", "--ref", str(ref)]) == 0
    design_call = next(r for r in adapter.calls if "# Designer Agent" in r.system_prompt)
    assert design_call.attachments == [str(ref.resolve())]


def test_runs_rm_discards_a_prototype_without_any_worktree_teardown(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("p1", workflow="", request="x", kind="prototype", ticket="hero")

    assert main(["runs", "rm", "p1"]) == 0
    assert "discarded" in capsys.readouterr().out
    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    assert con.execute("SELECT status FROM runs WHERE run_id='p1'").fetchone()["status"] == "discarded"


# --- scout: on-demand scout Session for the Orchestrator (ADR 0014) ---


def _scout_cfg() -> dict:
    return {
        "defaults": {"runtime": "pi", "model": "openrouter/x/y", "thinking": "low"},
        "agents": [{"name": "scout", "prompt_engineering": {}, "tools": ["read", "write"], "writes": []}],
        "profiles": {"quality": {"defaults": {"thinking": "high"}}},
        "commands": {},
    }


def _scout_reply(artifact: str) -> dict:
    return {
        "status": "success",
        "summary": "found it",
        "findings": [{"file": "app.py", "note": "the entry point"}],
        "artifacts": [artifact],
    }


def test_bare_scout_is_traceless_and_prints_the_findings_inline(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "deadbeef")
    monkeypatch.setattr(cli.config, "load", lambda root: _scout_cfg())
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    # Mock mkdtemp so we can predict the temp runs dir
    tmp_runs = tmp_path / "tmp_runs"
    tmp_runs.mkdir()
    monkeypatch.setattr(cli.tempfile, "mkdtemp", lambda prefix="": str(tmp_runs))

    findings_path = tmp_runs / "deadbeef" / "context_handoff" / "scout_findings.md"
    adapter = _research_stub_adapter(
        _scout_reply(str(findings_path)),
        write={str(findings_path): "- app.py — the entry point\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["scout", "where is the entry point?"]) == 0
    out = capsys.readouterr().out
    assert "- app.py — the entry point" in out
    assert "where is the entry point?" in adapter.calls[0].prompt

    # The project's .lime/runs/lime.db must remain absent (no trace left behind).
    project_db = root / ".lime" / "runs" / "lime.db"
    assert not project_db.exists()


def test_scout_joins_the_parent_run_when_lime_run_id_is_set(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "deadbeef")
    monkeypatch.setattr(cli.config, "load", lambda root: _scout_cfg())
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run("R1", workflow="feature", request="build the thing", kind="run")
    monkeypatch.setenv("LIME_RUN_ID", "R1")

    findings = root / ".lime" / "runs" / "R1" / "context_handoff" / "scout_findings.md"
    adapter = _research_stub_adapter(_scout_reply(str(findings)), write={str(findings): "- app.py\n"})
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["scout", "where?"]) == 0
    assert "- app.py" in capsys.readouterr().out

    con = cli.tracer.connect(root / ".lime" / "runs" / "lime.db", readonly=True)
    row = con.execute("SELECT kind, workflow, status FROM runs WHERE run_id='R1'").fetchone()
    # Parent remains kind=run, status=running and gains the successful scout phase
    assert (row["kind"], row["workflow"], row["status"]) == ("run", "feature", "running")
    phase = con.execute("SELECT owner, status, kind FROM phases WHERE run_id='R1' AND name='scout'").fetchone()
    assert phase is not None
    assert (phase["owner"], phase["status"]) == ("scout", "success")


def test_scout_falls_back_to_the_envelope_when_the_findings_file_is_missing(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "deadbeef")
    monkeypatch.setattr(cli.config, "load", lambda root: _scout_cfg())
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    monkeypatch.setattr(cli.gates, "artifacts_exist", lambda *a, **kw: cli.gates.GateReport())

    # The test doesn't write the artifact file — the fallback reads from envelope.findings.
    adapter = _research_stub_adapter(_scout_reply(str(tmp_path / "missing/scout_findings.md")))
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["scout", "where?"]) == 0
    assert "app.py — the entry point" in capsys.readouterr().out

    # Also leaves no project trace (transient path).
    project_db = root / ".lime" / "runs" / "lime.db"
    assert not project_db.exists()

def test_bare_scout_applies_scout_print_lines_before_cleanup(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "deadbeef")
    monkeypatch.setattr(cli.config, "load", lambda root: _scout_cfg())
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    # Mock mkdtemp so we can predict the temp runs dir
    tmp_runs = tmp_path / "tmp_runs"
    tmp_runs.mkdir()
    monkeypatch.setattr(cli.tempfile, "mkdtemp", lambda prefix="": str(tmp_runs))

    findings_path = str(tmp_runs / "deadbeef" / "context_handoff" / "scout_findings.md")
    lines = [f"line {i}" for i in range(cli.SCOUT_PRINT_LINES + 10)]
    adapter = _research_stub_adapter(
        _scout_reply(findings_path),
        write={findings_path: "\n".join(lines)},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["scout", "where?"]) == 0
    out = capsys.readouterr().out
    for i in range(cli.SCOUT_PRINT_LINES):
        assert f"line {i}" in out
    assert f"line {cli.SCOUT_PRINT_LINES}" not in out
    assert f"… {10} more lines" in out

    # No project trace
    project_db = root / ".lime" / "runs" / "lime.db"
    assert not project_db.exists()


def test_scout_profile_uses_the_resolved_roster_scout(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "new_id", lambda n=8: "deadbeef")
    monkeypatch.setattr(cli.config, "load", lambda root: _scout_cfg())
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    # Mock mkdtemp so we can predict the temp runs dir
    tmp_runs = tmp_path / "tmp_runs"
    tmp_runs.mkdir()
    monkeypatch.setattr(cli.tempfile, "mkdtemp", lambda prefix="": str(tmp_runs))

    findings_path = str(tmp_runs / "deadbeef" / "context_handoff" / "scout_findings.md")
    adapter = _research_stub_adapter(
        _scout_reply(findings_path),
        write={findings_path: "- app.py — the entry point\n"},
    )
    monkeypatch.setitem(agents.ADAPTERS, "pi", adapter)

    assert main(["scout", "where?", "--profile", "quality"]) == 0

    # Assert the AgentRequest carries the profile's effect: quality profile sets
    # defaults.thinking to "high" so the scout's thinking should be "high"
    req = adapter.calls[0]
    assert req.thinking == "high"

    # No project trace
    project_db = root / ".lime" / "runs" / "lime.db"
    assert not project_db.exists()


def test_scout_path_prints_the_packaged_pi_extension(tmp_path, monkeypatch, capsys):
    # Outside a configured project
    monkeypatch.chdir(tmp_path)
    assert main(["scout", "--path"]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert path.is_absolute()
    assert path.exists()
    assert path.name == "scout.ts"
    assert path.parent.name == "harness_engineering"


def test_scout_without_question_or_path_is_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["scout"]) == 1
    err = capsys.readouterr().err
    assert "question is required" in err or "a question" in err


# --- ticket 132: explicit stale-plan decision (`--replan`) ---


def _seed_valid_plan_run(root: Path, tmp_path: Path, *, run_id: str = "deadbeef", profile=None, workflow="feature"):
    """A failed Run whose plan and scout both succeeded with matching provenance."""
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    worktree_path = _seed_failed_run(
        root, tmp_path / "worktrees", ticket_path=ticket_path, profile=profile, workflow=workflow
    )
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    scout = tr.add_phase(run_id, seq=1, name="scout", stage="Build", kind="agent", owner="scout")
    tr.set_phase_status(scout, "running")
    tr.set_phase_status(scout, "success")
    plan = tr.add_phase(run_id, seq=2, name="plan", stage="Build", kind="agent", owner="planner")
    tr.set_phase_status(plan, "running")
    tr.set_phase_status(plan, "success")
    tr.record_envelope(
        run_id, plan, agent="planner", output_type="PlanOutput",
        payload_json='{"status": "success"}', valid=True, attempt=1,
    )
    _seed_plan_provenance(tr, run_id, plan, ticket_path, profile=profile)
    failed = tr.add_phase(run_id, seq=3, name="build_1", stage="Build", kind="agent", owner="builder")
    tr.set_phase_status(failed, "running")
    tr.set_phase_status(failed, "fail", error="red verify")
    return tr, plan, ticket_path, worktree_path


@pytest.mark.parametrize(
    "edit", ["ask", "where", "done", "out_of_scope"]
)
def test_resume_changed_ticket_requires_explicit_replan(tmp_path, monkeypatch, capsys, edit):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **k: [])
    monkeypatch.setattr(
        cli.session, "ensure",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create a session")),
    )
    monkeypatch.setattr(
        cli.feature, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not dispatch the chain")),
    )
    tr, _plan, ticket_path, _wt = _seed_valid_plan_run(root, tmp_path)
    edits = {
        "ask": ("Do X.", "Do Y."),
        "where": ("app.py", "other.py"),
        "done": ("It works.", "It works well."),
        "out_of_scope": ("n/a", "nothing"),
    }
    old, new = edits[edit]
    ticket_path.write_text(ticket_path.read_text().replace(old, new))

    code = main(["run", "--resume", "deadbeef"])

    assert code == 1
    err = capsys.readouterr().err
    assert "--replan" in err
    assert "cached plan" in err
    assert tr.con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()["status"] == "fail"


def test_resume_changed_or_legacy_plan_requires_replan(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **k: [])
    monkeypatch.setattr(cli.session, "ensure", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(cli.feature, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    tr, _plan, ticket_path, _wt = _seed_valid_plan_run(root, tmp_path)

    # changed plan.md
    plan_path = tr.run_dir("deadbeef") / "context_handoff" / "plan.md"
    plan_path.write_text(plan_path.read_text() + "\nchanged\n")
    assert main(["run", "--resume", "deadbeef"]) == 1
    assert "--replan" in capsys.readouterr().err

    # legacy plan: no provenance at all
    plan_path.write_text("## Step 1\nDo it.\n")
    tr.con.execute(
        "DELETE FROM events WHERE run_id='deadbeef' AND name='plan_provenance'"
    )
    tr.con.commit()
    assert main(["run", "--resume", "deadbeef"]) == 1
    err = capsys.readouterr().err
    assert "--replan" in err
    assert "no provenance" in err


@pytest.mark.parametrize(
    "case", ["ticket", "plan_changed", "plan_missing", "no_provenance", "version"]
)
def test_resume_stale_plan_prints_each_reason_unchanged(tmp_path, monkeypatch, capsys, case):
    """Every stale-plan reason refuses the resume with the same text, before any Run
    state, phase, session or worktree mutation."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **k: [])
    monkeypatch.setattr(
        cli.session, "ensure",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create a session")),
    )
    monkeypatch.setattr(
        cli.feature, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not dispatch the chain")),
    )
    tr, plan, ticket_path, wt = _seed_valid_plan_run(root, tmp_path)
    plan_path = tr.run_dir("deadbeef") / "context_handoff" / "plan.md"

    if case == "ticket":
        ticket_path.write_text(ticket_path.read_text().replace("Do X.", "Do Y."))
    elif case == "plan_changed":
        plan_path.write_text(plan_path.read_text() + "\nchanged\n")
    elif case == "plan_missing":
        plan_path.unlink()
    elif case == "no_provenance":
        tr.con.execute(
            "DELETE FROM events WHERE run_id='deadbeef' AND name='plan_provenance'"
        )
        tr.con.commit()
    elif case == "version":
        prov = cli.phases.load_plan_provenance(tr, "deadbeef", plan)
        tr.event(
            "deadbeef", "handoff", phase_id=plan, name="plan_provenance",
            payload={**prov.model_dump(), "version": 1},
        )

    events_before = tr.con.execute(
        "SELECT count(*) AS n FROM events WHERE run_id='deadbeef'"
    ).fetchone()["n"]
    phases_before = tr.con.execute(
        "SELECT count(*) AS n FROM phases WHERE run_id='deadbeef'"
    ).fetchone()["n"]
    wt_before = sorted(wt.iterdir())

    assert main(["run", "--resume", "deadbeef"]) == 1

    err = capsys.readouterr().err
    reason = {
        "ticket": "the ticket changed since it was planned",
        "plan_changed": "the cached plan.md changed since it was planned",
        "plan_missing": "the cached plan.md is missing",
        "no_provenance": (
            "the cached plan has no provenance (recorded before provenance existed)"
        ),
        "version": "the cached plan's provenance version 1 is unsupported",
    }[case]
    assert f"lime run: deadbeef has a cached plan that cannot be reused — {reason}.\n" in err
    assert "Re-plan explicitly with: lime run --resume deadbeef --replan" in err
    assert tr.con.execute(
        "SELECT status FROM runs WHERE run_id='deadbeef'"
    ).fetchone()["status"] == "fail"
    assert tr.con.execute(
        "SELECT count(*) AS n FROM events WHERE run_id='deadbeef'"
    ).fetchone()["n"] == events_before
    assert tr.con.execute(
        "SELECT count(*) AS n FROM phases WHERE run_id='deadbeef'"
    ).fetchone()["n"] == phases_before
    assert tr.con.execute(
        "SELECT count(*) AS n FROM events WHERE run_id='deadbeef' AND name='replan_decision'"
    ).fetchone()["n"] == 0
    assert sorted(wt.iterdir()) == wt_before


def test_resume_stale_plan_reports_an_unreadable_ticket(tmp_path, monkeypatch):
    """`_resume_stale_plan` gathers cli-side inputs only: an unreadable ticket is
    reported, and a Run with no replay-eligible plan has nothing to be stale."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    tr, _plan, ticket_path, _wt = _seed_valid_plan_run(root, tmp_path)

    result = cli._resume_stale_plan(tr, "deadbeef", str(tmp_path / "gone.md"), "p")
    assert result.startswith("the ticket is unreadable: ")

    _seed_failed_run(root, tmp_path / "worktrees", run_id="feedface", ticket_path=ticket_path)
    assert cli._resume_stale_plan(tr, "feedface", str(ticket_path), "p") is None


def test_unchanged_ticket_keeps_mechanics_resume(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **k: [])
    tr, _plan, _ticket, _wt = _seed_valid_plan_run(root, tmp_path)
    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["replay"] = dict(run_.replay)
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef"]) == 0
    assert set(captured["replay"]) == {"scout", "plan"}
    assert captured["replay"]["plan"]["output_type"] == "PlanOutput"


def test_resume_replan_records_decision_and_invalidates_stale_plan_and_scout(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **k: [])
    tr, _plan, ticket_path, _wt = _seed_valid_plan_run(root, tmp_path)
    ticket_path.write_text(ticket_path.read_text().replace("Do X.", "Do Y."))
    plan_path = tr.run_dir("deadbeef") / "context_handoff" / "plan.md"
    plan_before = plan_path.read_bytes()
    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["replay"] = dict(run_.replay)
        captured["agent_map"] = dict(run_.agent_map)
        captured["status"] = run_.tracer.con.execute(
            "SELECT status FROM runs WHERE run_id='deadbeef'"
        ).fetchone()["status"]
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    code = main(["run", "--resume", "deadbeef", "--replan"])

    assert code == 0
    assert captured["status"] == "running"
    assert "plan" not in captured["replay"]
    assert "scout" not in captured["replay"]
    assert captured["agent_map"] == {}
    decision = tr.con.execute(
        "SELECT 1 FROM events WHERE run_id='deadbeef' AND name='replan_decision'"
    ).fetchone()
    assert decision is not None
    # old plan/handoff bytes preserved until the planner deliberately replaces them
    assert plan_path.read_bytes() == plan_before
    assert (tr.run_dir("deadbeef") / "context_handoff" / "needs_rescope.json").exists() is False


def test_replan_requires_resume_flag(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    assert main(["run", "--replan"]) == 1
    assert "--resume" in capsys.readouterr().err


def _seed_reviewed_run(root: Path, tmp_path: Path, *, run_id: str = "deadbeef"):
    """A failed Run whose every phase succeeded through review; returns (tr, ticket, worktree)."""
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    worktree_path = _seed_failed_run(root, tmp_path / "worktrees", run_id=run_id, ticket_path=ticket_path)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    plan = None
    names = [
        ("scout", "scout"), ("plan", "planner"), ("build_1", "builder"), ("verify_1", "verifier"),
        ("build_2", "builder"), ("verify_2", "verifier"), ("review", "reviewer"),
        ("revise_1", "builder"), ("verify_2_retry3", "verifier"), ("review_retry1", "reviewer"),
    ]
    for seq, (name, owner) in enumerate(names, start=1):
        pid = tr.add_phase(run_id, seq=seq, name=name, stage="Build", kind="agent", owner=owner)
        tr.set_phase_status(pid, "running")
        tr.set_phase_status(pid, "success")
        if name == "plan":
            plan = pid
            tr.record_envelope(
                run_id, pid, agent="planner", output_type="PlanOutput",
                payload_json='{"status": "success"}', valid=True, attempt=1,
            )
            _seed_plan_provenance(tr, run_id, pid, ticket_path)
    return tr, ticket_path, worktree_path


def _rereview_events(tr, run_id="deadbeef"):
    return tr.con.execute(
        "SELECT name FROM events WHERE run_id=? AND name IN ('rereview_decision','phase_invalidated')",
        (run_id,),
    ).fetchall()


def _forbid_dispatch(monkeypatch):
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **k: [])
    monkeypatch.setattr(cli.session, "ensure", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(cli.feature, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))


def test_plain_resume_of_a_reviewed_run_is_blocked_and_rereview_reopens_it(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **k: [])
    tr, _ticket, _wt = _seed_reviewed_run(root, tmp_path)
    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["replay"] = dict(run_.replay)
        captured["agent_map"] = dict(run_.agent_map)
        captured["status"] = run_.tracer.con.execute(
            "SELECT status FROM runs WHERE run_id='deadbeef'"
        ).fetchone()["status"]
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef"]) == 1
    assert "no phase left to re-run" in capsys.readouterr().err

    map_path = tr.run_dir("deadbeef") / "agent_map.json"
    map_path.write_text('{"builder": {"session_id": "stale"}}')

    assert main(["run", "--resume", "deadbeef", "--rereview"]) == 0

    assert set(captured["replay"]) == {"scout", "plan", "build_1", "verify_1", "build_2"}
    assert captured["agent_map"] == {}
    assert captured["status"] == "running"
    assert not map_path.exists()
    rows = tr.con.execute(
        "SELECT payload_json FROM events WHERE run_id='deadbeef' AND name='rereview_decision'"
    ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"])["verify"] == "verify_2"
    assert "re-reviewing" in capsys.readouterr().out


def test_rereview_requires_resume_flag(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    assert main(["run", "--rereview"]) == 1
    assert "--resume" in capsys.readouterr().err


def test_rereview_refuses_replan(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _forbid_dispatch(monkeypatch)
    tr, _ticket, _wt = _seed_reviewed_run(root, tmp_path)

    assert main(["run", "--resume", "deadbeef", "--rereview", "--replan"]) == 1

    assert "cannot be combined" in capsys.readouterr().err
    assert _rereview_events(tr) == []


def test_rereview_refuses_a_run_that_never_reached_review(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _forbid_dispatch(monkeypatch)
    tr, _plan, _ticket, _wt = _seed_valid_plan_run(root, tmp_path)

    assert main(["run", "--resume", "deadbeef", "--rereview"]) == 1

    assert "never reached review" in capsys.readouterr().err
    assert tr.con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()["status"] == "fail"
    assert _rereview_events(tr) == []


def test_rereview_refuses_after_the_code_commit(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _forbid_dispatch(monkeypatch)
    tr, _ticket, _wt = _seed_reviewed_run(root, tmp_path)
    commit = tr.add_phase("deadbeef", seq=11, name="commit_code", stage="Build", kind="python", owner="python")
    tr.set_phase_status(commit, "running")
    tr.set_phase_status(commit, "success")

    assert main(["run", "--resume", "deadbeef", "--rereview"]) == 1

    assert "already committed" in capsys.readouterr().err
    assert _rereview_events(tr) == []


def test_rereview_refuses_a_stale_plan(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    _forbid_dispatch(monkeypatch)
    tr, ticket_path, _wt = _seed_reviewed_run(root, tmp_path)
    ticket_path.write_text(ticket_path.read_text().replace("Do X.", "Do Y."))

    assert main(["run", "--resume", "deadbeef", "--rereview"]) == 1

    assert "--replan" in capsys.readouterr().err
    assert _rereview_events(tr) == []


def test_rereview_state_has_a_closed_writer_set():
    lime_dir = _REPO_ROOT / "lime"
    allowed = {"agents.py", "runner.py", "cli.py", "phases.py"}
    for path in sorted(lime_dir.rglob("*.py")):
        if "skill" in path.relative_to(lime_dir).parts:
            continue
        text = path.read_text()
        tree = ast.parse(text)
        touches = "agent_map.json" in text or any(
            isinstance(n, ast.Attribute) and n.attr == "_agent_map_path" for n in ast.walk(tree)
        )
        if touches:
            assert path.name in allowed, f"{path} touches agent_map state outside the writer set"

    cli_tree = ast.parse((lime_dir / "cli.py").read_text())
    resume = next(
        n for n in ast.walk(cli_tree) if isinstance(n, ast.FunctionDef) and n.name == "_cmd_run_resume"
    )
    inside = {id(n) for n in ast.walk(resume)}
    for node in ast.walk(cli_tree):
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "unlink"
            and any(isinstance(c, ast.Constant) and c.value == "agent_map.json" for c in ast.walk(node.func.value))
        ):
            assert id(node) in inside, "agent_map.json unlinked outside _cmd_run_resume"

    for path in sorted(lime_dir.rglob("*.py")):
        if path.name == "tracer.py" or "skill" in path.relative_to(lime_dir).parts:
            continue
        tree = cli_tree if path.name == "cli.py" else ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"rereview", "rereview_target"}
            ):
                assert path.name == "cli.py" and id(node) in inside, (
                    f"{path}: {node.func.attr} called outside cli._cmd_run_resume"
                )


# --- pinned birth identity: launch + resume (ticket 144) ---


def _birth_head(worktree_path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _git_worktree(tmp_path: Path, name: str = "deadbeef") -> Path:
    wt = tmp_path / "worktrees" / name
    wt.mkdir(parents=True)
    _git(wt, "init", "-q", "-b", "main")
    (wt / "app.py").write_text("v1\n")
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "birth")
    return wt


def test_run_persists_the_birth_head_as_the_pin(tmp_path, monkeypatch):
    """Launch resolves full HEAD in the new worktree and stores it as the pin; the PR
    target stays the default branch name."""
    root = tmp_path / "project"
    _toy_project(root)  # the ticket is tracked and identical -> no ticket commit
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["cwd"] = Path.cwd()
        captured["base_branch"] = base_branch
        captured["in_memory_pin"] = run_.base_sha
        row = run_.tracer.con.execute(
            "SELECT base_sha, worktree FROM runs WHERE run_id=?", (run_.run_id,)
        ).fetchone()
        captured["stored_pin"] = row["base_sha"]
        captured["birth_head"] = _birth_head(Path(row["worktree"]))
        run_.tracer.set_run_status(run_.run_id, "success")
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "41"]) == 0
    assert captured["base_branch"] == "main"  # PR target name
    assert captured["birth_head"] == captured["in_memory_pin"] == captured["stored_pin"]
    assert len(captured["in_memory_pin"]) == 40
    assert captured["cwd"] == Path(captured["cwd"])  # inside the worktree
    assert Path.cwd() == root  # restored


def _seed_pinned_run(root, tmp_path, ticket_path, *, base_sha, run_id="deadbeef", status="fail"):
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    wt = _git_worktree(tmp_path, run_id)
    tr.start_run(
        run_id, workflow="feature", request="do X", ticket=str(ticket_path),
        branch="lime/feature/41-toy", worktree=str(wt), profile=None, base_sha=base_sha,
    )
    tr.set_run_status(run_id, status)
    return tr, wt


def test_resume_uses_the_stored_pin_after_own_commits_and_branch_tip_movement(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    wt = _git_worktree(tmp_path)
    pin = _birth_head(wt)
    tr.start_run(
        "deadbeef", workflow="feature", request="x", ticket=str(ticket_path),
        branch="lime/feature/41-toy", worktree=str(wt), profile=None, base_sha=pin,
    )
    tr.set_run_status("deadbeef", "fail")

    # own commits inside the worktree, then movement of the project's default branch
    (wt / "own.py").write_text("o\n")
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "own")
    (root / "more.txt").write_text("m\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "move main")

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["base_branch"] = base_branch
        captured["pin"] = run_.base_sha
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)
    assert main(["run", "--resume", "deadbeef"]) == 0
    assert captured["pin"] == pin
    assert captured["base_branch"] == "main"


OLD_RUNS_SCHEMA = """
CREATE TABLE runs (
    run_id       TEXT PRIMARY KEY,
    workflow     TEXT NOT NULL,
    request      TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    engineer     TEXT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost   REAL NOT NULL DEFAULT 0,
    archived     INTEGER NOT NULL DEFAULT 0,
    ticket       TEXT,
    branch       TEXT,
    worktree     TEXT,
    port         INTEGER,
    db_name      TEXT,
    profile      TEXT,
    kind         TEXT NOT NULL DEFAULT 'run'
);
"""


def test_resume_of_a_pre_column_database_uses_legacy_behavior(tmp_path, monkeypatch):
    import sqlite3

    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    wt = _git_worktree(tmp_path)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"

    runs_dir = root / ".lime" / "runs"
    runs_dir.mkdir(parents=True)
    con = sqlite3.connect(runs_dir / "lime.db")
    con.executescript(OLD_RUNS_SCHEMA)
    con.execute(
        "INSERT INTO runs (run_id, workflow, request, status, engineer, started_at,"
        " ticket, branch, worktree, profile, kind) VALUES"
        " ('deadbeef', 'feature', 'x', 'fail', 'e', '2020-01-01T00:00:00Z', ?, ?, ?,"
        " NULL, 'run')",
        (str(ticket_path), "lime/feature/41-toy", str(wt)),
    )
    con.commit()
    con.close()

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["base_branch"] = base_branch
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)
    assert main(["run", "--resume", "deadbeef"]) == 0
    assert captured["base_branch"] == "main"  # no pin -> legacy behavior


def _orphan_pin(wt: Path) -> str:
    """A commit on a safe side branch that is NOT an ancestor of the worktree's HEAD."""
    _git(wt, "switch", "-q", "-c", "recreated")
    (wt / "recreated.py").write_text("r\n")
    _git(wt, "add", "-A")
    _git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "recreated")
    orphan = _birth_head(wt)
    _git(wt, "switch", "-q", "main")
    return orphan


@pytest.mark.parametrize("flags", [[], ["--replan"]])
def test_resume_invalid_pin_stops_before_spawning_or_mutating(tmp_path, monkeypatch, capsys, flags):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"

    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    wt = _git_worktree(tmp_path)
    orphan = _orphan_pin(wt)
    tr.start_run(
        "deadbeef", workflow="feature", request="x", ticket=str(ticket_path),
        branch="lime/feature/41-toy", worktree=str(wt), profile=None, base_sha=orphan,
    )
    tr.set_run_status("deadbeef", "fail")
    run_dir = root / ".lime" / "runs" / "deadbeef"
    (run_dir / "context_handoff").mkdir(parents=True, exist_ok=True)
    (run_dir / "context_handoff" / "plan.md").write_text("plan\n")
    (run_dir / "agent_map.json").write_text('{"builder": {"session_id": "keep"}}')

    spawned = []
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: ["should not run"])
    monkeypatch.setattr(cli.feature, "run", lambda *a, **k: spawned.append(1) or 0)

    assert main(["run", "--resume", "deadbeef", *flags]) == 1
    assert "rebased/merged past its base" in capsys.readouterr().err
    assert spawned == []
    row = tr.con.execute(
        "SELECT status, base_sha FROM runs WHERE run_id='deadbeef'"
    ).fetchone()
    assert row["status"] == "fail"
    assert row["base_sha"] == orphan  # pin untouched
    assert (run_dir / "context_handoff" / "plan.md").read_text() == "plan\n"
    assert (run_dir / "agent_map.json").read_text() == '{"builder": {"session_id": "keep"}}'
    assert tr.con.execute(
        "SELECT 1 FROM events WHERE run_id='deadbeef' AND name='replan_decision'"
    ).fetchone() is None


# --- ticket 129: resolved builder limits bind plan identity ---


def _limits_raw():
    """A minimal config with named `slow` profile and explicit commit/budget blocks."""
    return {
        "defaults": {"runtime": "pi", "model": "openrouter/x/y", "thinking": "medium"},
        "agents": [
            {"name": "scout", "prompt_engineering": {}, "tools": ["read", "write"], "writes": []},
            {"name": "planner", "prompt_engineering": {}, "tools": ["read", "write"], "writes": []},
            {"name": "builder", "prompt_engineering": {}, "tools": ["read", "edit", "write"]},
            {"name": "reviewer", "prompt_engineering": {}, "tools": ["read", "write"], "writes": []},
            {"name": "documenter", "prompt_engineering": {}, "tools": ["read", "edit", "write"],
             "writes": ["AGENTS.md"]},
        ],
        "commands": {},
        "budget": {"warn": 150000, "stop": 250000},
        "commit": {"max_added_lines": 1500, "uncounted": []},
        "profiles": {"slow": {"agents": [{"name": "builder", "timeout_s": 2400}]}},
    }


def _write_lime_config(root, raw):
    import yaml

    (root / ".lime").mkdir(parents=True, exist_ok=True)
    (root / ".lime" / "config.yaml").write_text(yaml.safe_dump(raw))


def _mutated_limits(field: str) -> dict:
    raw = _limits_raw()
    if field == "timeout":  # named-profile patch
        raw["profiles"]["slow"]["agents"][0]["timeout_s"] = 9999
    elif field == "warn":
        raw["budget"]["warn"] = 140000
    elif field == "stop":
        raw["budget"]["stop"] = 240000
    elif field == "cap":
        raw["commit"]["max_added_lines"] = 1400
    elif field == "uncounted":
        raw["commit"]["uncounted"] = ["gen/"]
    return raw


@pytest.mark.parametrize("field", ["timeout", "warn", "stop", "cap", "uncounted"])
def test_resume_changed_builder_limits_requires_replan(tmp_path, monkeypatch, capsys, field):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **k: [])
    monkeypatch.setattr(
        cli.session, "ensure",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create a session")),
    )
    ran = []
    monkeypatch.setattr(cli.feature, "run", lambda *a, **k: ran.append(True) or 0)

    _write_lime_config(root, _limits_raw())
    tr, _plan, _ticket_path, _wt = _seed_valid_plan_run(root, tmp_path, profile="slow")
    plan_path = tr.run_dir("deadbeef") / "context_handoff" / "plan.md"
    plan_before = plan_path.read_bytes()

    _write_lime_config(root, _mutated_limits(field))
    code = main(["run", "--resume", "deadbeef"])

    assert code == 1
    err = capsys.readouterr().err
    assert "--replan" in err
    assert "limits" in err and "changed" in err
    assert (
        "has a cached plan that cannot be reused — the planning inputs or resolved"
        " limits changed since it was planned." in err
    )
    assert ran == []  # refusal precedes session creation and chain dispatch
    assert tr.con.execute("SELECT status FROM runs WHERE run_id='deadbeef'").fetchone()["status"] == "fail"
    assert plan_path.read_bytes() == plan_before  # untouched before any mutation


def test_resume_unchanged_limits_still_replays(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **k: [])
    _write_lime_config(root, _limits_raw())
    tr, _plan, _ticket_path, _wt = _seed_valid_plan_run(root, tmp_path, profile="slow")
    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["replay"] = dict(run_.replay)
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef"]) == 0
    assert "plan" in captured["replay"]


def test_resume_bug_fix_plan_reconstruction_replays(tmp_path, monkeypatch):
    """The bug-fix plan addendum is reconstructed on resume, so that workflow's valid
    cached plan is not perpetually stale."""
    root = tmp_path / "project"
    _toy_project(root)
    (root / ".lime" / "tickets" / "41-toy.md").write_text(
        TICKET_TEXT.replace("Workflow: feature", "Workflow: bug-fix")
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **k: [])
    _write_lime_config(root, _limits_raw())
    tr, _plan, _ticket_path, _wt = _seed_valid_plan_run(root, tmp_path, workflow="bug-fix")
    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["replay"] = dict(run_.replay)
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)
    assert main(["run", "--resume", "deadbeef"]) == 0
    assert "plan" in captured["replay"]


def test_resume_replan_after_limit_change_invalidates_builds(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **k: [])
    _write_lime_config(root, _limits_raw())
    tr, _plan, _ticket_path, _wt = _seed_valid_plan_run(root, tmp_path)
    # A successful old build downstream of the plan, plus a stale runtime session map.
    build = tr.add_phase("deadbeef", seq=4, name="build_1", stage="Build", kind="agent", owner="builder")
    tr.set_phase_status(build, "running")
    tr.set_phase_status(build, "success")
    tr.record_envelope(
        "deadbeef", build, agent="builder", output_type="BuildOutput",
        payload_json=json.dumps({"status": "success", "summary": "old build", "changed_files": []}),
        valid=True, attempt=1,
    )
    agent_map_path = tr.run_dir("deadbeef") / "agent_map.json"
    agent_map_path.write_text(json.dumps({"builder": {"session_id": "old", "model": "m", "runtime": "pi"}}))
    # A later interruption keeps the run resumable and build_1 replayable before replan.
    verify = tr.add_phase("deadbeef", seq=5, name="verify_1", stage="Build", kind="code", owner="git")
    tr.set_phase_status(verify, "running")
    tr.set_phase_status(verify, "fail")
    assert "build_1" in tr.replay_map("deadbeef")

    _write_lime_config(root, _mutated_limits("cap"))
    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["replay"] = dict(run_.replay)
        captured["agent_map"] = dict(run_.agent_map)
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef", "--replan"]) == 0
    assert "build_1" not in captured["replay"], "stale build cannot execute from replay"
    assert "plan" not in captured["replay"]
    assert captured["agent_map"] == {}, "stale runtime agent mappings must be cleared"


# --- run: overlap guard at single launch (ticket 160) ---


def _overlap_project(tmp_path, *, where="`app.py`", sibling_where="`app.py`", blocked_by=None):
    """A `trunk`-default origin with one unmerged Run branch and a local 41 ticket."""
    origin = Origin(tmp_path)
    origin.publish_run("lime/feature/70-sibling", 70, sibling_where)
    origin.write_ticket("41-toy.md", ticket_text(41, where, blocked_by=blocked_by))
    return origin


def test_run_overlap_refuses_before_reservation(tmp_path, monkeypatch, capsys):
    origin = _overlap_project(tmp_path)
    monkeypatch.chdir(origin.root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")

    def fail_if_called(*a, **kw):
        raise AssertionError("overlap must refuse before reserve/create/session")

    monkeypatch.setattr(cli.numbering, "reserve", fail_if_called)
    monkeypatch.setattr(cli.worktree, "create", fail_if_called)
    monkeypatch.setattr(cli.session, "ensure", fail_if_called)

    assert main(["run", "41"]) == 2
    err = capsys.readouterr().err
    assert "lime/feature/70-sibling" in err
    assert "app.py" in err
    assert "--allow-overlap" in err


def test_run_disjoint_where_launches(tmp_path, monkeypatch):
    origin = _overlap_project(tmp_path, sibling_where="`unrelated.py`")
    monkeypatch.chdir(origin.root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    reached = []
    monkeypatch.setattr(cli.feature, "run", lambda run_, ticket, base: reached.append(ticket.slug) or 0)

    assert main(["run", "41"]) == 0
    assert reached == ["41-toy"]


def test_run_allow_overlap_launches(tmp_path, monkeypatch):
    origin = _overlap_project(tmp_path)
    monkeypatch.chdir(origin.root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    reached = []
    monkeypatch.setattr(cli.feature, "run", lambda run_, ticket, base: reached.append(ticket.slug) or 0)

    assert main(["run", "41", "--allow-overlap"]) == 0
    assert reached == ["41-toy"]


def test_run_blocked_by_sibling_overlap_exempt(tmp_path, monkeypatch):
    # The blocker names the remote branch's ticket; it is absent locally but the
    # overlap exemption still applies (blocker refusal is unchanged).
    origin = _overlap_project(tmp_path, blocked_by=".lime/tickets/70-sibling.md")
    monkeypatch.chdir(origin.root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    reached = []
    monkeypatch.setattr(cli.feature, "run", lambda run_, ticket, base: reached.append(ticket.slug) or 0)

    assert main(["run", "41"]) == 0
    assert reached == ["41-toy"]


def test_run_no_origin_still_runs_the_guard(tmp_path, monkeypatch):
    # No origin: the guard runs but returns empty, so the launch proceeds unchanged.
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    calls = []
    real = cli.overlap.live_overlap

    def spy(project_root, ticket):
        calls.append(ticket.slug)
        return real(project_root, ticket)

    monkeypatch.setattr(cli.overlap, "live_overlap", spy)
    monkeypatch.setattr(cli.feature, "run", lambda *a, **kw: 0)

    assert main(["run", "41"]) == 0
    assert calls == ["41-toy"]


def test_run_allow_overlap_rejected_for_a_batch(capsys):
    assert main(["run", "41", "42", "--allow-overlap"]) == 2
    assert "--allow-overlap" in capsys.readouterr().err


def test_run_allow_overlap_rejected_for_resume(capsys):
    assert main(["run", "--resume", "abc", "--allow-overlap"]) == 2
    assert "--allow-overlap" in capsys.readouterr().err


# --- run: overlap holds in a batch (ticket 160) ---


def _patch_overlap(monkeypatch, conflicts):
    """Stub the overlap detector by ticket slug; keep fake-Popen tests away from Git."""
    def fake(project_root, ticket):
        return dict(conflicts.get(ticket.slug, {}))

    monkeypatch.setattr(cli.overlap, "live_overlap", fake)


class _ReadyPopen:
    """Records launches and reports an instant clean exit."""

    launched: list[str] = []

    def __init__(self, cmd, cwd=None):
        self.returncode = 0
        _ReadyPopen.launched.append(cmd[-1])

    def poll(self):
        return self.returncode


def test_pick_ready_skips_a_held_candidate_and_keeps_it(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)  # 41, 42
    _patch_overlap(monkeypatch, {"41-toy": {"lime/feature/70-x": ["app.py"]}})
    pending = ["41", "42"]
    assert cli._pick_ready(root, pending) == 1
    assert pending == ["41", "42"]


def test_pick_ready_returns_none_when_all_held(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)
    _patch_overlap(
        monkeypatch,
        {"41-toy": {"lime/feature/70-x": ["app.py"]}, "42-toy": {"lime/feature/71-y": ["b.py"]}},
    )
    pending = ["41", "42"]
    assert cli._pick_ready(root, pending) is None
    assert pending == ["41", "42"]


def test_pick_ready_reports_skips_and_hold_reasons(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=3)  # 41..44
    tickets = root / ".lime" / "tickets"
    (tickets / "99-blocker.md").write_text("# 99 — Blocker\n")
    (tickets / "42-toy.md").write_text(
        TICKET_TEXT.replace("# 41", "# 42").replace(
            "Status: open", "Status: open\nBlocked by: .lime/tickets/99-blocker.md"
        )
    )
    _patch_overlap(monkeypatch, {"43-toy": {"Run ab12cd34 (lime/feature/x)": ["app43.py"]}})

    pending = ["nope", "42", "43", "44"]
    skips: list[int] = []
    holds: list[tuple[int, str]] = []
    idx = cli._pick_ready(
        root,
        pending,
        on_skip=lambda i: skips.append(i),
        on_hold=lambda i, reason: holds.append((i, reason)),
    )
    assert idx == 2
    assert skips == [0]
    assert holds == [
        (0, "blocked 99"),
        (1, "overlap Run ab12cd34 (lime/feature/x)"),
    ]


def test_pick_ready_records_one_hold_per_holder_map(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=0)
    _patch_overlap(monkeypatch, {"41-toy": {"lime/feature/70-x": ["app.py"]}})
    recorded: set = set()

    assert cli._pick_ready(root, ["41"], recorded=recorded) is None
    assert cli._pick_ready(root, ["41"], recorded=recorded) is None
    assert _refusals(root) == [
        (cli.tracer.NO_RUN, "overlap_held",
         {"ticket": "41-toy", "holders": {"lime/feature/70-x": ["app.py"]}})
    ]

    # a distinct holder map is a new event, not a dedupe of the first
    _patch_overlap(monkeypatch, {"41-toy": {"lime/feature/71-y": ["app.py"]}})
    assert cli._pick_ready(root, ["41"], recorded=recorded) is None
    assert len(_refusals(root)) == 2


def test_run_batch_launches_only_disjoint_tickets(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)
    monkeypatch.chdir(root)
    _patch_overlap(monkeypatch, {"41-toy": {"lime/feature/70-x": ["app.py"]}})
    _ReadyPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _ReadyPopen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42"]) == 1
    assert _ReadyPopen.launched == ["42"]
    assert "41: held: overlap with lime/feature/70-x" in capsys.readouterr().err


def test_run_batch_records_a_held_ticket_once(tmp_path, monkeypatch, capsys):
    """41 stays pending across the passes that run 42, but records one hold, not one per poll."""
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)
    monkeypatch.chdir(root)
    _patch_overlap(monkeypatch, {"41-toy": {"lime/feature/70-x": ["app.py"]}})
    _ReadyPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _ReadyPopen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42"]) == 1
    assert _ReadyPopen.launched == ["42"]
    held = [r for r in _refusals(root) if r[1] == "overlap_held"]
    assert held == [
        (cli.tracer.NO_RUN, "overlap_held",
         {"ticket": "41-toy", "holders": {"lime/feature/70-x": ["app.py"]}})
    ]


def test_run_batch_terminates_nonzero_when_everything_is_held(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)
    monkeypatch.chdir(root)
    _patch_overlap(
        monkeypatch,
        {"41-toy": {"lime/feature/70-x": ["app.py"]}, "42-toy": {"lime/feature/71-y": ["b.py"]}},
    )
    _ReadyPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _ReadyPopen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42"]) == 1
    assert _ReadyPopen.launched == []
    err = capsys.readouterr().err
    assert "41: held: overlap with lime/feature/70-x" in err
    assert "42: held: overlap with lime/feature/71-y" in err


def test_run_batch_prints_every_conflicting_branch(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)  # 41, 42
    monkeypatch.chdir(root)
    _patch_overlap(
        monkeypatch,
        {"41-toy": {"lime/feature/70-x": ["app.py"], "lime/feature/71-y": ["src/"]}},
    )
    _ReadyPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _ReadyPopen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42"]) == 1
    assert _ReadyPopen.launched == ["42"]  # the disjoint ticket still ran
    err = capsys.readouterr().err
    assert "lime/feature/70-x" in err and "app.py" in err
    assert "lime/feature/71-y" in err and "src/" in err


def test_run_batch_reports_mixed_blocked_and_held(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)
    tickets = root / ".lime" / "tickets"
    (tickets / "99-blocker.md").write_text("# 99 — Blocker\n")
    (tickets / "42-toy.md").write_text(
        TICKET_TEXT.replace("# 41", "# 42").replace(
            "Status: open", "Status: open\nBlocked by: .lime/tickets/99-blocker.md"
        )
    )
    monkeypatch.chdir(root)
    _patch_overlap(monkeypatch, {"41-toy": {"lime/feature/70-x": ["app.py"]}})
    _ReadyPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _ReadyPopen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42"]) == 1
    assert _ReadyPopen.launched == []
    out, err = capsys.readouterr()
    assert "41: held: overlap with lime/feature/70-x" in err
    assert "still blocked" in err and "42" in err
    assert "--deploy report" not in out


def test_run_batch_overlap_clears_on_a_later_check(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)  # 41, 42
    monkeypatch.chdir(root)
    calls = {"n": 0}

    def fake(project_root, ticket):
        if ticket.slug == "42-toy":
            calls["n"] += 1
            return {"lime/feature/70-x": ["app.py"]} if calls["n"] == 1 else {}
        return {}

    monkeypatch.setattr(cli.overlap, "live_overlap", fake)
    _ReadyPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _ReadyPopen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42"]) == 0
    assert _ReadyPopen.launched == ["41", "42"]


@pytest.mark.parametrize("persistent", [False, True], ids=["transient", "persistent"])
def test_run_batch_overlap_error_while_child_active(
    tmp_path, monkeypatch, capsys, persistent
):
    assert cli.OVERLAP_CHECK_CEILING_S == 300
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)  # 41, 42
    monkeypatch.chdir(root)
    clock = {"t": 0.0}
    calls: list[float] = []
    exited_at: list[float] = []

    class _SlowPopen:
        launched: list[str] = []

        def __init__(self, cmd, cwd=None):
            self.returncode = None
            self._polls = 0
            _SlowPopen.launched.append(cmd[-1])

        def poll(self):
            self._polls += 1
            if self._polls >= 5:
                self.returncode = 0
                exited_at.append(clock["t"])
            return self.returncode

    def fake(project_root, ticket):
        if ticket.slug == "42-toy":
            calls.append(clock["t"])
            if persistent or len(calls) <= 2:
                raise cli.overlap.OverlapError(f"cannot lock ref #{len(calls)}")
        return {}

    step = cli.OVERLAP_CHECK_CEILING_S / 3

    def fake_sleep(seconds):
        clock["t"] += step

    monkeypatch.setattr(cli, "_batch_clock", lambda: clock["t"])
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    monkeypatch.setattr(cli.overlap, "live_overlap", fake)
    _SlowPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _SlowPopen)

    code = main(["run", "41", "42"])
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if "overlap check failed" in line]
    _, children = _batch_ledger(root)
    if not persistent:
        assert code == 0
        assert _SlowPopen.launched == ["41", "42"]
        assert calls == [0.0, 100.0, 200.0]
        assert len(lines) == 1
        assert "cannot lock ref #1" in lines[0] and "300 s" in lines[0]
        assert "#2" not in err
        assert "giving up" not in err
        assert children == [
            (0, "41-toy", "started", None),
            (1, "42-toy", "started", None),
        ]
    else:
        assert code == 1
        assert _SlowPopen.launched == ["41"]  # 42 never spawned
        assert calls == [0.0, 100.0, 200.0, 300.0]
        # 41 outlived the ceiling: drained, not abandoned
        assert exited_at[0] > cli.OVERLAP_CHECK_CEILING_S
        assert len(lines) == 2
        assert "cannot lock ref #1" in lines[0]
        assert "giving up on: 42" in lines[1]
        assert "last error: cannot lock ref #4" in lines[1]
        assert "#2" not in err and "#3" not in err  # no line per pass
        assert children == [
            (0, "41-toy", "started", None),
            (1, "42-toy", "dropped", "overlap check failed"),
        ]


def test_run_batch_overlap_error_with_no_active_child_drops_after_the_ceiling(
    tmp_path, monkeypatch, capsys
):
    assert cli.OVERLAP_CHECK_CEILING_S == 300
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)  # 41, 42
    monkeypatch.chdir(root)
    clock = {"t": 0.0}
    calls: list[float] = []

    def fake(project_root, ticket):
        calls.append(clock["t"])
        raise cli.overlap.OverlapError(f"cannot lock ref #{len(calls)}")

    step = cli.OVERLAP_CHECK_CEILING_S / 3

    def fake_sleep(seconds):
        clock["t"] += step

    monkeypatch.setattr(cli, "_batch_clock", lambda: clock["t"])
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    monkeypatch.setattr(cli.overlap, "live_overlap", fake)
    _ReadyPopen.launched = []
    monkeypatch.setattr(cli.subprocess, "Popen", _ReadyPopen)

    assert main(["run", "41", "42"]) == 1
    assert _ReadyPopen.launched == []
    assert calls == [0.0, 100.0, 200.0, 300.0]
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if "overlap check failed" in line]
    assert len(lines) == 2
    assert "cannot lock ref #1" in lines[0]
    assert "giving up on: 41, 42" in lines[1]
    assert "last error: cannot lock ref #4" in lines[1]
    assert "still blocked" not in err and "held:" not in err
    _, children = _batch_ledger(root)
    assert children == [
        (0, "41-toy", "dropped", "overlap check failed"),
        (1, "42-toy", "dropped", "overlap check failed"),
    ]


# --- run: unpushed local Run discovery + batch spawn-to-row race (ticket 167) ---


class _ScriptedChildren:
    """Fake child spawner intercepting only `lime run` launches, so real Git (used by
    the detector) is untouched. Records launch order; each child polls a callback."""

    def __init__(self, poll_fn):
        self.poll_fn = poll_fn
        self.launched: list[str] = []
        self.instances: list[_ScriptedChild] = []

    def __call__(self, cmd, cwd=None):
        ticket = cmd[-1]
        self.launched.append(ticket)
        child = _ScriptedChild(ticket, self.poll_fn)
        self.instances.append(child)
        return child


class _ScriptedChild:
    def __init__(self, ticket, poll_fn):
        self.ticket = ticket
        self.poll_fn = poll_fn
        self.polls = 0
        self.returncode = None

    def poll(self):
        self.polls += 1
        code = self.poll_fn(self, self.polls)
        if code is not None:
            self.returncode = code
        return self.returncode


@pytest.mark.parametrize("state", ["running", "stopped-red"])
def test_run_unpushed_overlap_refuses_before_reservation(
    tmp_path, monkeypatch, capsys, state
):
    origin = Origin(tmp_path)
    where = "- `src/a.py`\n- `src/b.py`\n"
    run = unpushed_run(
        origin.root, 70, where, run_id="deadbeef",
        status="running" if state == "running" else "fail",
        process=None if state == "running" else "dead",
    )
    origin.write_ticket("41-toy.md", ticket_text(41, where))
    monkeypatch.chdir(origin.root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")

    def fail_if_called(*a, **kw):
        raise AssertionError("overlap must refuse before reserve/create/session")

    monkeypatch.setattr(cli.numbering, "reserve", fail_if_called)
    monkeypatch.setattr(cli.worktree, "create", fail_if_called)
    monkeypatch.setattr(cli.session, "ensure", fail_if_called)

    assert main(["run", "41"]) == 2
    assert main(["run", "41"]) == 2  # repeated refusal recomputes; no new effects
    err = capsys.readouterr().err
    assert run.run_id in err
    assert "src/a.py" in err and "src/b.py" in err
    assert "--allow-overlap" in err
    refusals = _refusals(origin.root)
    assert len(refusals) == 2  # one event per refused invocation, never merged
    for run_id_, name, payload in refusals:
        assert run_id_ == cli.tracer.NO_RUN
        assert name == "overlap_refused"
        assert payload["ticket"] == "41-toy"
        (holder, paths), = payload["holders"].items()
        assert run.run_id in holder
        assert sorted(paths) == ["src/a.py", "src/b.py"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_run_overlap_refusal_output_survives_an_unwritable_ledger(
    tmp_path, monkeypatch, capsys
):
    origin = Origin(tmp_path)
    where = "- `src/a.py`\n- `src/b.py`\n"
    unpushed_run(origin.root, 70, where, run_id="deadbeef", status="running", process=None)
    origin.write_ticket("41-toy.md", ticket_text(41, where))
    monkeypatch.chdir(origin.root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")

    assert main(["run", "41"]) == 2
    first = capsys.readouterr()
    _freeze_ledger(origin.root)
    assert main(["run", "41"]) == 2
    second = capsys.readouterr()
    assert (second.out, second.err) == (first.out, first.err)
    assert len(_refusals(origin.root)) == 1


def test_run_unpushed_overlap_allow_override_launches(tmp_path, monkeypatch):
    origin = Origin(tmp_path)
    unpushed_run(origin.root, 70, "`src/a.py`", run_id="deadbeef", status="fail", process="dead")
    origin.write_ticket("41-toy.md", ticket_text(41, "`src/a.py`"))
    monkeypatch.chdir(origin.root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    reached = []
    monkeypatch.setattr(
        cli.feature, "run", lambda run_, ticket, base: reached.append(ticket.slug) or 0
    )

    assert main(["run", "41", "--allow-overlap"]) == 0
    assert reached == ["41-toy"]


def test_run_unpushed_overlap_blocked_by_carried_sibling_exempt(tmp_path, monkeypatch):
    origin = Origin(tmp_path)
    run = unpushed_run(origin.root, 70, "`src/a.py`", run_id="deadbeef", status="fail", process="dead")
    run.ticket.unlink()  # absent from root: only the Run's branch carries it
    origin.write_ticket(
        "41-toy.md", ticket_text(41, "`src/a.py`", blocked_by=".lime/tickets/70-toy.md")
    )
    monkeypatch.chdir(origin.root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    reached = []
    monkeypatch.setattr(
        cli.feature, "run", lambda run_, ticket, base: reached.append(ticket.slug) or 0
    )

    assert main(["run", "41"]) == 0
    assert reached == ["41-toy"]


def test_run_unpushed_overlap_existing_blocker_still_refused(tmp_path, monkeypatch, capsys):
    origin = Origin(tmp_path)
    unpushed_run(origin.root, 70, "`src/a.py`", run_id="deadbeef", status="fail", process="dead")
    origin.write_ticket("99-ghost.md", "# 99 — Ghost\n")
    origin.write_ticket(
        "41-toy.md", ticket_text(41, "`src/a.py`", blocked_by=".lime/tickets/99-ghost.md")
    )
    monkeypatch.chdir(origin.root)

    assert main(["run", "41"]) == 1
    assert "blocked by" in capsys.readouterr().err


@pytest.mark.parametrize("state", ["running", "stopped-red"])
def test_run_batch_holds_unpushed_run_and_starts_disjoint(
    tmp_path, monkeypatch, capsys, state
):
    origin = Origin(tmp_path)
    run = unpushed_run(
        origin.root, 70, "`shared.py`", run_id="deadbeef",
        status="running" if state == "running" else "fail",
        process=None if state == "running" else "dead",
    )
    origin.write_ticket("41-toy.md", ticket_text(41, "`shared.py`"))
    origin.write_ticket("42-toy.md", ticket_text(42, "`disjoint.py`"))
    monkeypatch.chdir(origin.root)
    children = _ScriptedChildren(lambda child, polls: 0)
    monkeypatch.setattr(cli, "_spawn_run", children)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42"]) == 1
    assert children.launched == ["42"]
    err = capsys.readouterr().err
    assert run.run_id in err
    assert "shared.py" in err


def test_run_batch_holds_child_before_trace_registration(tmp_path, monkeypatch, capsys):
    origin = Origin(tmp_path)
    origin.write_ticket("41-toy.md", ticket_text(41, "`shared.py`"))
    origin.write_ticket("42-toy.md", ticket_text(42, "`shared.py`"))
    origin.write_ticket("43-toy.md", ticket_text(43, "`disjoint.py`"))
    monkeypatch.chdir(origin.root)

    def poll_fn(child, polls):
        if child.ticket == "41":
            assert "42" not in children.launched  # overlapping work never concurrent
            if polls == 2:
                # a later scheduler tick registers first's local Run/branch, then fails it
                unpushed_run(
                    origin.root, 41, "`shared.py`", run_id="redrun",
                    status="fail", process="dead",
                )
            return 1 if polls >= 2 else None
        if child.ticket == "43":
            return 0 if polls >= 3 else None
        return 0

    children = _ScriptedChildren(poll_fn)
    monkeypatch.setattr(cli, "_spawn_run", children)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42", "43"]) == 1
    assert children.launched == ["41", "43"]
    err = capsys.readouterr().err
    assert "redrun" in err and "shared.py" in err


def test_run_batch_hold_clears_after_discard_while_other_active(
    tmp_path, monkeypatch, capsys
):
    origin = Origin(tmp_path)
    origin.write_ticket("41-toy.md", ticket_text(41, "`shared.py`"))
    origin.write_ticket("42-toy.md", ticket_text(42, "`shared.py`"))
    origin.write_ticket("43-toy.md", ticket_text(43, "`disjoint.py`"))
    monkeypatch.chdir(origin.root)

    def poll_fn(child, polls):
        if child.ticket == "41":
            assert "42" not in children.launched  # held by 41's active snapshot
            if polls == 2:
                # 41's persisted Run is discarded, so the local hold releases once it exits
                unpushed_run(
                    origin.root, 41, "`shared.py`", run_id="gonerun",
                    status="discarded", process="dead",
                )
            return 0 if polls >= 2 else None
        if child.ticket == "43":
            return 0 if polls >= 4 else None  # stays alive to keep the scheduler running
        return 0

    children = _ScriptedChildren(poll_fn)
    monkeypatch.setattr(cli, "_spawn_run", children)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42", "43"]) == 0
    assert children.launched == ["41", "43", "42"]


def test_run_batch_holds_on_a_reaped_child_whose_deploy_was_refused(
    tmp_path, monkeypatch, capsys
):
    origin = Origin(tmp_path)
    (origin.root / ".lime" / "config.yaml").write_text(
        'auto_deploy: staging\ndeploy: []\nruns:\n  ports: 3100-3102\ncommands:\n  test: "true"\n'
    )
    origin.write_ticket("265-card.md", ticket_text(265, "- `src/card.vue`\n- `src/cash.ts`\n"))
    origin.write_ticket("266-next.md", ticket_text(266, "`src/cash.ts`"))
    monkeypatch.chdir(origin.root)

    def poll_fn(child, polls):
        if child.ticket == "265":
            assert "266" not in children.launched
            run = unpushed_run(
                origin.root, 265, "- `src/card.vue`\n- `src/cash.ts`\n",
                ticket_name="265-card.md", run_id="run265", status="success", process="dead",
            )
            close_local_ticket(run)  # commit_docs
            origin.push_branch(run.branch)  # push_pr
        return 0

    children = _ScriptedChildren(poll_fn)
    monkeypatch.setattr(cli, "_spawn_run", children)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "_deploy_link", lambda root, ticket_arg: f"lime deploy {ticket_arg}")

    assert main(["run", "265", "266", "--deploy"]) == 1
    assert children.launched == ["265"]
    captured = capsys.readouterr()
    assert "266: held: overlap with Run run265 (lime/feature/265-card) (src/cash.ts)" in captured.err
    assert "0 deployed · 1 failed · 1 held" in captured.out


def test_run_batch_active_ticket_snapshot_survives_ticket_removal(
    tmp_path, monkeypatch
):
    origin = Origin(tmp_path)
    origin.write_ticket("41-toy.md", ticket_text(41, "`shared.py`"))
    origin.write_ticket("42-toy.md", ticket_text(42, "`shared.py`"))
    monkeypatch.chdir(origin.root)
    ticket41 = origin.root / ".lime" / "tickets" / "41-toy.md"

    def poll_fn(child, polls):
        if child is children.instances[0]:
            if polls == 1:
                ticket41.unlink()  # root child ticket removed after spawn
            assert "42" not in children.launched
            return 0 if polls >= 3 else None
        return 0

    children = _ScriptedChildren(poll_fn)
    monkeypatch.setattr(cli, "_spawn_run", children)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42"]) == 0
    assert children.launched == ["41", "42"]


def test_run_batch_duplicate_pending_not_concurrent(tmp_path, monkeypatch):
    origin = Origin(tmp_path)
    origin.write_ticket("41-toy.md", ticket_text(41, "`shared.py`"))
    monkeypatch.chdir(origin.root)

    def poll_fn(child, polls):
        if child is children.instances[0] and polls < 3:
            assert children.launched == ["41"]  # its own snapshot holds the duplicate
        return 0 if polls >= 3 else None

    children = _ScriptedChildren(poll_fn)
    monkeypatch.setattr(cli, "_spawn_run", children)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "41"]) == 0
    assert children.launched == ["41", "41"]


def test_runs_rm_releases_unpushed_overlap(tmp_path, monkeypatch, capsys):
    origin = Origin(tmp_path)
    unpushed_run(origin.root, 70, "`src/a.py`", run_id="deadbeef", status="fail", process="dead")
    origin.write_ticket("41-toy.md", ticket_text(41, "`src/a.py`"))
    monkeypatch.chdir(origin.root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.worktree, "CC_PROJECTS", tmp_path / "cc")
    monkeypatch.setattr(cli.config, "load", lambda root=Path("."): {"commands": {}})
    candidate = tracker.parse(
        (origin.root / ".lime" / "tickets" / "41-toy.md").read_text(), "41-toy.md"
    )

    assert cli.overlap.live_overlap(origin.root, candidate) != {}
    assert main(["runs", "rm", "deadbeef"]) == 0
    assert cli.overlap.live_overlap(origin.root, candidate) == {}


def test_run_cookbook_skill_text_parity():
    repo = Path(__file__).resolve().parents[1]
    source = (repo / "lime" / "skill" / "lime" / "cookbooks" / "run.md").read_text()
    stamped = (repo / ".claude" / "skills" / "lime" / "cookbooks" / "run.md").read_text()
    assert source == stamped
    for phrase in ("local Runs", "Run id", "red Run", "single fresh ticket"):
        assert phrase in source


# --- deploy merged-tree check: its own Run env + unconditional check teardown (ticket 164) ---


def _ast_func_source(path: Path, name: str):
    """The `ast.FunctionDef` for `name` in `path` — source inspection for ownership guards."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def _run_check_calls(func_node, attr_name: str):
    """Calls to `name(...)` (a bare Name) or `obj.name(...)` (an Attribute) within a function."""
    calls = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == attr_name) or (
            isinstance(func, ast.Name) and func.id == attr_name
        ):
            calls.append(node)
    return calls


def _set_deploy_run_resources(root: Path, *, run_id="deadbeef", port=5100, db_name="lime_deadbeef"):
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.con.execute(
        "UPDATE runs SET port=?, db_name=? WHERE run_id=?", (port, db_name, run_id)
    )
    tr.con.commit()


def _stub_post_merge(monkeypatch, *, merge_sink=None):
    """Every post-check deploy step mocked except the gate and its real shell commands."""
    _mock_open_pr(monkeypatch)
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: merge_sink.append(branch))
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: [])
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])


@pytest.mark.parametrize("conflict", [False, True])
def test_deploy_merged_tree_has_own_run_env(tmp_path, monkeypatch, capsys, conflict):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    _set_deploy_run_resources(root)
    # another live Run/process holds a distinct port (status='running', no closing process row)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    tr.start_run(
        "cafebabe", workflow="feature", request="y", kind="run",
        ticket=str(root / ".lime" / "tickets" / "cafebabe-toy.md"),
        branch="lime/feature/cafebabe-toy", port=5200, db_name="lime_cafebabe",
    )

    rec = tmp_path / "check-env.txt"
    q = shlex.quote(str(rec))
    test_cmd = (
        'test -n "$LIME_DB_NAME" && test -n "$LIME_PORT" && test -n "$LIME_RUN_ID"'
        " && test -f default-only.txt && test -f pr-only.txt"
        f' && printf "%s\\n%s\\n%s\\n" "$LIME_RUN_ID" "$LIME_DB_NAME" "$LIME_PORT" > {q}'
    )
    monkeypatch.setattr(
        cli.config, "load", lambda r: {"commands": {"test": test_cmd}, "deploy": []}
    )
    merge_state = {}
    _mock_open_pr(monkeypatch)
    monkeypatch.setattr(
        cli.git_helper, "merge_pr",
        lambda branch: merge_state.setdefault("state", _checkout_snapshot(root)),
    )
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: [])
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])
    before = _checkout_snapshot(root)

    if conflict:
        monkeypatch.setenv("LIME_RUN_ID", "inherited-id")
        monkeypatch.setenv("LIME_DB_NAME", "inherited-db")
        monkeypatch.setenv("LIME_PORT", "9999")

    assert main(["deploy", "41"]) == 0
    run_id, db_name, port = rec.read_text().splitlines()
    assert run_id and db_name and port
    assert run_id != "deadbeef" and run_id != "inherited-id"
    assert db_name == f"lime_{run_id}"
    assert db_name != "lime_deadbeef" and db_name != "inherited-db"
    assert port.isdigit() and port not in {"5100", "5200", "9999"}
    # merged tree still had both sides (the command only passed if both files existed)
    assert merge_state["state"] == before
    if conflict:
        # explicit env wins in the subprocess; the parent process env is untouched
        assert os.environ["LIME_RUN_ID"] == "inherited-id"
        assert os.environ["LIME_DB_NAME"] == "inherited-db"
        assert os.environ["LIME_PORT"] == "9999"


@pytest.mark.parametrize("phase", ["pass", "setup", "test"])
def test_deploy_check_teardown_uses_same_env_once(tmp_path, monkeypatch, capsys, phase):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    rec = tmp_path / "phases.txt"
    q = shlex.quote(str(rec))

    def rec_cmd(name, *, fail=False, cwd_alive=False):
        args = '"$LIME_RUN_ID" "$LIME_DB_NAME" "$LIME_PORT"'
        fields = f'"{name} %s %s %s\\n"'
        if cwd_alive:
            fields = f'"{name} %s %s %s cwd_alive=%s\\n"'
            args += ' "$([ -d "$PWD" ] && echo yes || echo no)"'
        cmd = f"printf {fields} {args} >> {q}"
        if fail:
            cmd += "; exit 9"
        return cmd

    commands = {
        "setup": rec_cmd("setup", fail=(phase == "setup")),
        "test": rec_cmd("test", fail=(phase == "test")),
        "teardown": rec_cmd("teardown", cwd_alive=True),
    }
    monkeypatch.setattr(cli.config, "load", lambda r: {"commands": commands, "deploy": []})
    merged = []
    _stub_post_merge(monkeypatch, merge_sink=merged)

    assert main(["deploy", "41"]) == (0 if phase == "pass" else 1)
    lines = rec.read_text().splitlines()
    names = [line.split()[0] for line in lines]
    expected = ["setup", "teardown"] if phase == "setup" else ["setup", "test", "teardown"]
    assert names == expected
    assert names.count("teardown") == 1
    teardown_line = [line for line in lines if line.startswith("teardown")][0]
    assert "cwd_alive=yes" in teardown_line  # cleanup ran while the merged cwd still existed
    envs = {tuple(line.split()[1:4]) for line in lines}
    assert len(envs) == 1  # one frozen env across every phase
    rid, db, port = next(iter(envs))
    assert rid != "deadbeef" and db == f"lime_{rid}" and db != "lime_deadbeef"
    assert merged == (["lime/feature/41-toy"] if phase == "pass" else [])


@pytest.mark.parametrize("phase", ["green", "setup", "test"])
def test_deploy_check_teardown_failure_warns_without_changing_verdict(tmp_path, monkeypatch, capsys, phase):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    rec = tmp_path / "env.txt"
    q = shlex.quote(str(rec))
    setup = f'printf "setup\\n" >> {q}' + ("; exit 4" if phase == "setup" else "")
    test = f'printf "test\\n" >> {q}' + ("; exit 5" if phase == "test" else "")
    teardown = f'printf "%s %s %s\\n" "$LIME_RUN_ID" "$LIME_DB_NAME" "$LIME_PORT" >> {q}; exit 6'
    commands = {"setup": setup, "test": test, "teardown": teardown}
    before = _checkout_snapshot(root)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    before_row = dict(
        tr.con.execute("SELECT * FROM runs WHERE run_id='deadbeef'").fetchone()
    )

    rc = cli._check_deploy_merged_tree(root, "deadbeef", base_sha, "lime/feature/41-toy", commands)

    assert rc == (0 if phase == "green" else 1)
    err = capsys.readouterr().err
    rid, db, _port = rec.read_text().strip().splitlines()[-1].split()
    assert db == f"lime_{rid}"
    assert f"LIME_DB_NAME={db}" in err
    assert "teardown failed" in err and "exited 6" in err
    assert _checkout_snapshot(root) == before  # temp tree gone, checkout untouched
    after_row = dict(
        tr.con.execute("SELECT * FROM runs WHERE run_id='deadbeef'").fetchone()
    )
    assert after_row == before_row  # Run identity/status untouched by the gate


def test_deploy_check_without_teardown_is_silent(tmp_path, monkeypatch, capsys):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    marker = tmp_path / "setup-ran"
    commands = {"setup": f"touch {shlex.quote(str(marker))}", "test": "true"}

    rc = cli._check_deploy_merged_tree(root, "deadbeef", base_sha, "lime/feature/41-toy", commands)

    assert rc == 0
    err = capsys.readouterr().err
    assert "teardown" not in err
    assert marker.exists()
    check_root = root / ".lime" / "runs" / "deadbeef" / "context_handoff" / "deploy-check"
    dirs = list(check_root.iterdir())
    assert len(dirs) == 1
    assert {p.name for p in (dirs[0] / "quality").iterdir()} == {"00_setup", "00_test"}


def test_deploy_check_port_excludes_run_resources(tmp_path, monkeypatch, capsys):
    # deterministic seam: parent port, live-held port, configured-but-unused port, then a free one
    candidates = iter([5100, 5200, 5300, 5400])
    assert cli._allocate_check_port(
        excluded={5100, 5200, 5300}, candidate=lambda: next(candidates)
    ) == 5400
    # exhaustion: a bounded loop refuses rather than reusing an excluded port
    exhausted = iter([5100] * 200)
    assert cli._allocate_check_port(excluded={5100}, candidate=lambda: next(exhausted)) is None

    def boom():
        raise OSError("no sockets")

    assert cli._allocate_check_port(excluded=set(), candidate=boom) is None
    # the real probe closes its socket: the port is bindable the instant it returns
    port = cli._probe_free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))

    # allocation failure refuses before setup runs
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    marker = tmp_path / "setup-ran"
    commands = {"setup": f"touch {shlex.quote(str(marker))}", "test": "true"}
    monkeypatch.setattr(cli, "_allocate_check_port", lambda **kw: None)
    rc = cli._check_deploy_merged_tree(root, "deadbeef", base_sha, "lime/feature/41-toy", commands)
    assert rc == 1
    assert "could not allocate" in capsys.readouterr().err
    assert not marker.exists()


def test_deploy_check_retry_and_interrupt_cleanup(tmp_path, monkeypatch, capsys):
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    rec = tmp_path / "ids.txt"
    q = shlex.quote(str(rec))
    commands = {
        "test": f'printf "%s %s\\n" "$LIME_RUN_ID" "$LIME_DB_NAME" >> {q}',
        "teardown": f'printf "teardown %s\\n" "$LIME_RUN_ID" >> {q}',
    }
    before = _checkout_snapshot(root)
    tr = cli.tracer.Tracer(root / ".lime" / "runs")
    before_row = dict(tr.con.execute("SELECT * FROM runs WHERE run_id='deadbeef'").fetchone())

    assert cli._check_deploy_merged_tree(root, "deadbeef", base_sha, "lime/feature/41-toy", commands) == 0
    assert cli._check_deploy_merged_tree(root, "deadbeef", base_sha, "lime/feature/41-toy", commands) == 0
    ids = [
        tuple(line.split()) for line in rec.read_text().splitlines()
        if not line.startswith("teardown")
    ]
    assert len(ids) == 2
    assert ids[0][0] != ids[1][0] and ids[0][1] != ids[1][1]
    assert all(db == f"lime_{rid}" for rid, db in ids)
    check_root = root / ".lime" / "runs" / "deadbeef" / "context_handoff" / "deploy-check"
    assert len({p.name for p in check_root.iterdir()}) == 2  # a fresh log dir per invocation

    # KeyboardInterrupt in the test body: one real teardown runs, the tree is removed first
    rec2 = tmp_path / "interrupt.txt"
    q2 = shlex.quote(str(rec2))
    commands2 = {
        "test": "true",
        "teardown": f'printf "teardown %s\\n" "$LIME_RUN_ID" >> {q2}',
    }
    real_run_check = cli.quality.run_check

    def interrupt_test(adapter, name):
        if name == "test":
            raise KeyboardInterrupt
        return real_run_check(adapter, name)

    monkeypatch.setattr(cli.quality, "run_check", interrupt_test)
    with pytest.raises(KeyboardInterrupt):
        cli._check_deploy_merged_tree(root, "deadbeef", base_sha, "lime/feature/41-toy", commands2)
    assert len(rec2.read_text().splitlines()) == 1
    assert rec2.read_text().splitlines()[0].startswith("teardown ")
    assert _checkout_snapshot(root) == before
    after_row = dict(tr.con.execute("SELECT * FROM runs WHERE run_id='deadbeef'").fetchone())
    assert after_row == before_row


def test_deploy_check_cleanup_ownership_guard():
    func = _ast_func_source(_REPO_ROOT / "lime" / "cli.py", "_check_deploy_merged_tree")
    attrs = [node.attr for node in ast.walk(func) if isinstance(node, ast.Attribute)]
    # never the full Run teardown (worktree + branches + session dir)
    assert "teardown" not in attrs
    # never a destructive branch op or Run row/status write, and no remote-branch writer:
    # the lease push lives in `_publish_docs_merge`, the PR merge in `_cmd_deploy`
    assert not (
        set(attrs)
        & {"delete_branch", "set_run_status", "start_run", "start_session", "add_phase",
           "_git_at", "_git", "push_lease", "push", "merge_pr"}
    )
    # never mutate the global process environment
    assert "environ" not in attrs
    # the only db access is a read
    for call in _run_check_calls(func, "execute"):
        assert call.args, "execute without SQL"
        assert "select" in ast.literal_eval(call.args[0]).lower()
    # still uses the merged-worktree context manager
    assert any(
        isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Attribute)
        and item.context_expr.func.attr == "merged_worktree"
        for with_node in ast.walk(func)
        if isinstance(with_node, ast.With)
        for item in with_node.items
    )


def test_deploy_original_run_teardown_guard(tmp_path, monkeypatch, capsys):
    cli_path = _REPO_ROOT / "lime" / "cli.py"
    # full Run teardown stays only at its existing lifecycle boundaries
    tree = ast.parse(cli_path.read_text())
    callers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "teardown"
                    and isinstance(inner.func.value, ast.Name)
                    and inner.func.value.id == "worktree"
                ):
                    callers.add(node.name)
    assert callers == {"_run_ticket", "_cmd_runs_rm", "_cmd_deploy"}
    # deploy's original post-merge teardown keeps the deploying Run's recorded env
    deploy = _ast_func_source(cli_path, "_cmd_deploy")
    teardowns = _run_check_calls(deploy, "teardown")
    assert len(teardowns) == 1
    env_kw = [kw for kw in teardowns[0].keywords if kw.arg == "env"][0]
    unparsed = ast.unparse(env_kw.value)
    assert "run_id" in unparsed and "row['port']" in unparsed and "row['db_name']" in unparsed
    runs_rm = _ast_func_source(cli_path, "_cmd_runs_rm")
    assert any(
        "args.run_id" in ast.unparse(call)
        and "row['port']" in ast.unparse(call)
        and "row['db_name']" in ast.unparse(call)
        for call in _run_check_calls(runs_rm, "_run_env")
    )

    # behavioral: exactly one original teardown, with the deploying Run's env, after the check
    root, origin, base_sha = _deploy_gate_project(tmp_path, monkeypatch)
    _set_deploy_run_resources(root)
    monkeypatch.setattr(
        cli.config, "load", lambda r: {"commands": {"test": "true"}, "deploy": []}
    )
    seen = []
    _mock_open_pr(monkeypatch)
    monkeypatch.setattr(cli.git_helper, "merge_pr", lambda branch: None)
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: [])
    monkeypatch.setattr(
        cli.worktree, "teardown", lambda *a, **kw: seen.append(kw) or []
    )

    assert main(["deploy", "41"]) == 0
    assert len(seen) == 1
    assert seen[0]["env"] == {
        "LIME_RUN_ID": "deadbeef", "LIME_PORT": "5100", "LIME_DB_NAME": "lime_deadbeef",
    }


# --- Where lint: launch refuses atomically, check aggregates (ticket 173) ---


def _where_ticket_file(root: Path, name: str, text: str) -> Path:
    tickets = root / ".lime" / "tickets"
    tickets.mkdir(parents=True, exist_ok=True)
    path = tickets / name
    path.write_text(text)
    return path


def _raise(message: str):
    def fail(*a, **kw):
        raise AssertionError(message)
    return fail


@pytest.mark.parametrize("allow_overlap", [False, True])
@pytest.mark.parametrize(
    "where, detail",
    [("app.py", None), ("`src/a.py` `src/b.py`", ("Where line 1", "src/b.py"))],
)
def test_run_where_refuses_before_any_launch_effect(
    tmp_path, monkeypatch, capsys, allow_overlap, where, detail
):
    root = tmp_path / "project"
    root.mkdir()
    ticket_file = _where_ticket_file(root, "41-toy.md", ticket_text(41, where))
    monkeypatch.chdir(root)
    before = ticket_file.read_bytes()

    monkeypatch.setattr(cli.overlap, "live_overlap", _raise("no overlap discovery"))
    monkeypatch.setattr(cli.numbering, "reserve", _raise("no reservation"))
    monkeypatch.setattr(cli.worktree, "create", _raise("no worktree"))
    monkeypatch.setattr(cli.session, "ensure", _raise("no session"))
    monkeypatch.setattr(cli.feature, "run", _raise("no chain"))

    argv = ["run", "41"] + (["--allow-overlap"] if allow_overlap else [])
    assert main(argv) == 2
    assert main(argv) == 2  # rerun recomputes diagnostics; still no new effects
    err = capsys.readouterr().err
    assert "41-toy.md" in err
    assert "use one backticked path per line" in err
    if detail:
        assert detail[0] in err and detail[1] in err
    assert ticket_file.read_bytes() == before
    assert not (root / ".lime" / "runs").exists()


def test_run_where_symbols_pass_and_unrelated_faults_do_not_block(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _toy_project(root)
    _where_ticket_file(
        root, "42-toy.md",
        ticket_text(42, "\n".join([
            "`app.py`",
            "`app.py` then `_cmd_run`",
            "`app.py` then `commands.setup`",
            "`app.py` then `lime_<run_id>`",
        ])),
    )
    _where_ticket_file(root, "80-bad.md", ticket_text(80, "app.py"))  # unrequested, faulty
    _where_ticket_file(root, "99-blocker.md", "# 99 — Blocker\n")
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    reached = []
    monkeypatch.setattr(
        cli.feature, "run", lambda run_, ticket, base: reached.append(ticket.slug) or 0
    )

    assert main(["run", "42"]) == 0
    assert reached == ["42-toy"]


def test_run_where_flags_an_existing_root_basename(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    (root / "commands.setup").write_text("setup\n")
    _where_ticket_file(root, "42-toy.md", ticket_text(42, "`app.py` then `commands.setup`"))
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.numbering, "reserve", _raise("no reservation"))
    monkeypatch.setattr(cli.worktree, "create", _raise("no worktree"))

    assert main(["run", "42"]) == 2
    err = capsys.readouterr().err
    assert "commands.setup" in err
    assert "Where line 1" in err
    assert "use one backticked path per line" in err


@pytest.mark.parametrize(
    "where, detail",
    [("app.py", None), ("`src/a.py` `src/b.py`", ("Where line 1", "src/b.py"))],
)
def test_run_batch_where_fault_in_later_ticket_launches_nothing(
    tmp_path, monkeypatch, capsys, where, detail
):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)  # 41 valid, 42
    _where_ticket_file(root, "42-toy.md", ticket_text(42, where))
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "_pick_ready", _raise("no scheduling"))
    monkeypatch.setattr(cli.overlap, "live_overlap", _raise("no overlap discovery"))
    monkeypatch.setattr(cli.subprocess, "Popen", _raise("no child spawn"))
    monkeypatch.setattr(cli.numbering, "reserve", _raise("no reservation"))

    assert main(["run", "41", "42"]) == 2
    err = capsys.readouterr().err
    assert "42-toy.md" in err
    assert "use one backticked path per line" in err
    if detail:
        assert detail[0] in err and detail[1] in err


def test_run_batch_where_duplicate_faulty_args_refuse_idempotently(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)
    _where_ticket_file(root, "42-toy.md", ticket_text(42, "app.py"))
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "_pick_ready", _raise("no scheduling"))
    monkeypatch.setattr(cli.subprocess, "Popen", _raise("no child spawn"))

    assert main(["run", "42", "42"]) == 2
    assert "42-toy.md" in capsys.readouterr().err


def test_run_batch_where_fault_beats_a_blocker(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)
    _where_ticket_file(root, "99-blocker.md", "# 99 — Blocker\n")
    _where_ticket_file(
        root, "42-toy.md",
        ticket_text(42, "app.py", blocked_by=".lime/tickets/99-blocker.md"),
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli, "_pick_ready", _raise("no scheduling"))
    monkeypatch.setattr(cli.subprocess, "Popen", _raise("no child spawn"))

    assert main(["run", "41", "42"]) == 2
    err = capsys.readouterr().err
    assert "42-toy.md" in err
    assert "use one backticked path per line" in err


def test_run_batch_where_ignores_unrequested_faulty_ticket(tmp_path, monkeypatch):
    root = tmp_path / "project"
    _batch_project(root, extra_tickets=1)  # 41, 42 valid
    _where_ticket_file(root, "43-bad.md", ticket_text(43, "app.py"))
    monkeypatch.chdir(root)
    launched = []

    class _Popen:
        def __init__(self, cmd, cwd=None):
            launched.append(cmd[-1])
            self.returncode = 0

        def poll(self):
            return self.returncode

    _patch_overlap(monkeypatch, {})
    monkeypatch.setattr(cli.subprocess, "Popen", _Popen)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    assert main(["run", "41", "42"]) == 0
    assert sorted(launched) == ["41", "42"]

def test_check_reports_all_where_faults(green_environment, capsys):
    root = Path.cwd()  # green_environment chdir'd to its tmp_path
    _where_ticket_file(root, "41-ok.md", ticket_text(41, "`app.py`"))
    _where_ticket_file(root, "42-zero.md", ticket_text(42, "app.py"))
    _where_ticket_file(
        root, "43-hidden.md",
        ticket_text(43, "`src/a.py` then `src/b.py` and `src/c.py`"),
    )
    _where_ticket_file(
        root, "44-both.md",
        ticket_text(44, "`_run_ticket` then `src/new.py`").replace(
            "Status: open", "Status: claimed (dev, 2024-01-01)"
        ),
    )
    _where_ticket_file(
        root, "notes.md",
        ticket_text(0, "`app.py` then `src/z.py`").replace(
            "Workflow: feature", "Type: task"
        ),
    )
    maps = root / ".lime" / "maps"
    maps.mkdir(parents=True, exist_ok=True)
    (maps / "plain-map.md").write_text("# plain map\n\nStatus: open\n")

    assert main(["check"]) == 1
    out = capsys.readouterr().out
    where_lines = [l for l in out.splitlines() if "use one backticked path per line" in l]
    assert len(where_lines) == 6  # one zero + two hidden + two both + one nonnumeric-hidden
    blob = "\n".join(where_lines)
    for name in ("42-zero.md", "43-hidden.md", "44-both.md", "notes.md"):
        assert name in blob
    for token in ("src/b.py", "src/c.py", "src/new.py", "src/z.py"):
        assert token in blob
    assert "Where line 1" in blob
    assert "41-ok.md" not in blob  # a valid file gains no Where diagnostic
    assert "plain-map.md" not in blob  # maps are not linted for Where paths


def test_check_where_valid_symbols_green(green_environment, capsys):
    root = Path.cwd()
    _where_ticket_file(
        root, "41-ok.md",
        ticket_text(41, "\n".join([
            "`app.py`",
            "`app.py` then `_cmd_run`",
            "`app.py` then `commands.setup`",
            "`app.py` then `lime_<run_id>`",
        ])),
    )
    assert main(["check"]) == 0
    assert "green" in capsys.readouterr().out


def test_where_lint_rechecks_after_ticket_repair(tmp_path, monkeypatch, capsys):
    root = tmp_path / "project"
    _toy_project(root)
    ticket_file = _where_ticket_file(
        root, "42-toy.md", ticket_text(42, "`src/a.py` `src/b.py`")
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.worktree, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(
        cli.config, "validate_before_spawn", lambda *a, **kw: ["downstream gate reached"]
    )

    assert main(["run", "42"]) == 2  # Where fault refuses before config
    capsys.readouterr()

    # Repair only the synthetic fixture: one backticked path per line, then rerun.
    ticket_file.write_text(ticket_text(42, "`src/a.py`\n`src/b.py`"))
    assert main(["run", "42"]) == 1  # Where lint now passes; the downstream gate refuses
    assert "downstream gate reached" in capsys.readouterr().err


# --- ticket 174: the guard's partial-delivery verb follows the adapter runtime ---

_PI_TRUTHFUL = (
    "Report truthfully. Use status: success only when every applicable requirement "
    "(including required verification and reporting/artifacts) is already met; "
    "otherwise write a useful handoff and submit status: partial, identifying "
    "done and remaining/unverified work. Never claim checks ran or passed when "
    "they did not."
)
_CC_TRUTHFUL = _PI_TRUTHFUL.replace("submit status: partial", "report status: partial")

_STOP_REASON_PREFIX = {
    None: "context budget exhausted — no more substantive work. ",
    "time_limit": (
        "the Session's time ceiling has been reached and substantive tools are closed — "
        "only handoff-directory writes/edits and final reporting remain. "
    ),
    "repeat": (
        "this Session called the same tool with identical arguments three times in a row, "
        "so substantive tools are closed — only handoff-directory writes/edits and final "
        "reporting remain. "
    ),
}


@pytest.mark.parametrize("runtime", ["claude_code", "pi", None], ids=["cc", "pi", "unset"])
@pytest.mark.parametrize("reason", [None, "time_limit", "repeat"], ids=["missing", "time_limit", "repeat"])
def test_guard_stop_delivery_wording_by_runtime(tmp_path, monkeypatch, capsys, runtime, reason):
    """Only a claude_code runtime switches the partial verb to `report`; pi and an unset
    marker keep the literal pi wording. The deny applies and exemptions are unchanged."""
    budget_file = tmp_path / "budget.json"
    payload = {"state": "stop"}
    if reason is not None:
        payload["reason"] = reason
    budget_file.write_text(json.dumps(payload))
    handoff_dir = tmp_path / "context_handoff"
    handoff_dir.mkdir()

    out = _guard(
        monkeypatch, capsys, {"tool_name": "Bash", "tool_input": {"command": "ls"}},
        budget_file=budget_file, handoff_dir=handoff_dir, runtime=runtime,
    )
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    truthful = _CC_TRUTHFUL if runtime == "claude_code" else _PI_TRUTHFUL
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == _STOP_REASON_PREFIX[reason] + truthful
    if runtime == "claude_code":
        assert "submit" not in out["hookSpecificOutput"]["permissionDecisionReason"]

    # the stop exemptions still pass for every runtime
    exempt = _guard(
        monkeypatch, capsys,
        {"tool_name": "Write", "tool_input": {"file_path": str(handoff_dir / "handoff-build-1.md")}},
        budget_file=budget_file, handoff_dir=handoff_dir, runtime=runtime,
    )
    assert exempt["hookSpecificOutput"]["permissionDecision"] == "allow"


@pytest.mark.parametrize("closed,expect_fresh", [(True, True), (False, False)], ids=["closed", "open"])
def test_resume_cc_role_closed_starts_fresh_open_rejoins(tmp_path, monkeypatch, closed, expect_fresh):
    """A plain-fail resume of a closed cc role chooses a fresh UUID/dir; an ordinary open
    cc map still rejoins its persisted id — matching the pi resume behavior."""
    root = tmp_path / "project"
    _toy_project(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(cli.config, "validate_before_spawn", lambda *a, **kw: [])
    raw = yaml.safe_load(config.TEMPLATE.read_text())
    builder = next(a for a in raw["agents"] if a["name"] == "builder")
    builder["runtime"] = "claude_code"
    builder["model"] = "sonnet"
    (root / ".lime" / "config.yaml").write_text(yaml.safe_dump(raw))
    ticket_path = root / ".lime" / "tickets" / "41-toy.md"
    _seed_failed_run(root, tmp_path / "worktrees", ticket_path=ticket_path)

    run_dir = root / ".lime" / "runs" / "deadbeef"
    run_dir.mkdir(parents=True, exist_ok=True)
    old_runtime_dir = str(run_dir / "01-builder" / "runtime_sessions")
    entry = {
        "session_id": "cc-persisted", "model": "sonnet",
        "runtime": "claude_code", "session_dir": old_runtime_dir,
    }
    if closed:
        entry["closed"] = True
    (run_dir / "agent_map.json").write_text(json.dumps({"builder": entry}))

    captured = {}

    def fake_feature_run(run_, ticket, base_branch):
        captured["sid"], captured["resume"], captured["sdir"] = agents._runtime_session(
            run_, run_.agent("builder")
        )
        return 0

    monkeypatch.setattr(cli.feature, "run", fake_feature_run)

    assert main(["run", "--resume", "deadbeef"]) == 0
    if expect_fresh:
        assert captured["resume"] is False
        assert captured["sid"] != "cc-persisted"
        assert uuid.UUID(captured["sid"])
        assert captured["sdir"] is None
    else:
        assert captured["resume"] is True
        assert captured["sid"] == "cc-persisted"
        assert captured["sdir"] == old_runtime_dir


# --- ticket 275: docs-only deploy merge ---


def _commit_merge(root: Path, pr_sha: str, default_sha: str) -> str:
    """A merge commit with `pr_sha` first, `default_sha` second — the tested docs merge."""
    return _out(
        root, "commit-tree", f"{pr_sha}^{{tree}}", "-p", pr_sha, "-p", default_sha, "-m", "docs merge"
    )


def _publish_repo(tmp_path, monkeypatch):
    """A real origin with a pushed Run branch and a seeded success Run.

    Returns (root, origin, pr_sha, default_sha). The local Run branch stays at `pr_sha`,
    so the writers invariant (only origin moves) can be asserted.
    """
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)
    default_sha = _rev(root, "main")
    _git(root, "checkout", "-q", "-b", "lime/feature/41-toy")
    (root / "pr-only.txt").write_text("pr\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "pr work")
    _git(root, "push", "-q", "-u", "origin", "lime/feature/41-toy")
    _git(root, "checkout", "-q", "main")
    pr_sha = _rev(root, "refs/heads/lime/feature/41-toy")
    _seed_gated_run(root, tmp_path, base_sha=default_sha)
    return root, origin, pr_sha, default_sha


def test_publish_docs_merge_pushes_waits_for_head_and_passes_green(tmp_path, monkeypatch):
    root, origin, pr_sha, default_sha = _publish_repo(tmp_path, monkeypatch)
    merge_sha = _commit_merge(root, pr_sha, default_sha)
    sleeps = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))
    views = iter([
        {"headRefOid": pr_sha},
        {"headRefOid": pr_sha},
        {"headRefOid": merge_sha, "statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    ])
    monkeypatch.setattr(cli.git_helper, "pr_view", lambda branch, *, timeout=None: next(views))

    assert cli._publish_docs_merge(
        root, "deadbeef", "lime/feature/41-toy", pr_sha, merge_sha,
        had_checks=True, ticket="41-toy",
    ) is True
    assert sleeps == [5, 5]
    assert _out(origin, "rev-parse", "lime/feature/41-toy") == merge_sha
    assert _rev(root, "refs/heads/lime/feature/41-toy") == pr_sha
    assert _refusals(root) == []


def test_publish_docs_merge_push_rejected_records_its_reason(tmp_path, monkeypatch):
    root, origin, pr_sha, default_sha = _publish_repo(tmp_path, monkeypatch)
    merge_sha = _commit_merge(root, pr_sha, default_sha)
    sleeps = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))
    _fail_if_called(monkeypatch, (cli.git_helper, "pr_view"), "pr_view must not run after a rejected push")

    # a real lease rejection: the remote branch is at pr_sha, not the expected default tip
    assert cli._publish_docs_merge(
        root, "deadbeef", "lime/feature/41-toy", default_sha, merge_sha,
        had_checks=True, ticket="41-toy",
    ) is False
    assert _refusals(root) == [
        ("deadbeef", "docs_merge_push_rejected", {"ticket": "41-toy", "sha": merge_sha})
    ]
    assert sleeps == []
    assert _out(origin, "rev-parse", "lime/feature/41-toy") == pr_sha


def test_publish_docs_merge_head_missing_after_two_minutes(tmp_path, monkeypatch):
    root, origin, pr_sha, default_sha = _publish_repo(tmp_path, monkeypatch)
    merge_sha = _commit_merge(root, pr_sha, default_sha)
    clock = {"t": 0.0}
    sleeps = []
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock["t"])

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    calls = {"n": 0}

    def fake(branch, *, timeout=None):
        calls["n"] += 1
        assert timeout is not None and timeout > 0  # every gh call is deadline-bounded
        if calls["n"] % 2 == 1:
            return {"headRefOid": pr_sha}  # the old head
        raise RuntimeError("gh boom")      # a gh error counts as not yet shown

    monkeypatch.setattr(cli.git_helper, "pr_view", fake)

    assert cli._publish_docs_merge(
        root, "deadbeef", "lime/feature/41-toy", pr_sha, merge_sha,
        had_checks=True, ticket="41-toy",
    ) is False
    assert clock["t"] == 120
    assert sum(sleeps) == 120
    assert _refusals(root) == [
        ("deadbeef", "docs_merge_head_missing", {"ticket": "41-toy", "sha": merge_sha})
    ]


@pytest.mark.parametrize("checks,had_checks,expected", [
    ([{"conclusion": "FAILURE"}], True, "docs_merge_ci_red"),
    ([{"conclusion": None, "status": "IN_PROGRESS"}], True, "docs_merge_ci_pending"),
    ([], True, "docs_merge_ci_pending"),
    ([], False, None),
])
def test_publish_docs_merge_ci_on_pushed_head(tmp_path, monkeypatch, checks, had_checks, expected):
    root, origin, pr_sha, default_sha = _publish_repo(tmp_path, monkeypatch)
    merge_sha = _commit_merge(root, pr_sha, default_sha)
    sleeps = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch, *, timeout=None: {"headRefOid": merge_sha, "statusCheckRollup": checks},
    )

    result = cli._publish_docs_merge(
        root, "deadbeef", "lime/feature/41-toy", pr_sha, merge_sha,
        had_checks=had_checks, ticket="41-toy",
    )
    if expected is None:
        assert result is True
        assert _refusals(root) == []
    else:
        assert result is False
        assert _refusals(root) == [
            ("deadbeef", expected, {"ticket": "41-toy", "sha": merge_sha})
        ]
    assert sleeps == []


def test_docs_merge_branch_writers_guard():
    push_lease_funcs = set()
    push_callers = set()
    for path in (_REPO_ROOT / "lime").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Attribute) and inner.attr == "push_lease":
                    push_lease_funcs.add(node.name)
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "push"
                    and isinstance(inner.func.value, ast.Name)
                    and inner.func.value.id == "git_helper"
                ):
                    push_callers.add(path.relative_to(_REPO_ROOT).as_posix())
    # the lease push onto the origin PR branch lives only in `_publish_docs_merge`
    assert push_lease_funcs == {"_publish_docs_merge"}
    # the ordinary push (a Run branch) still lives only in phases.push_pr
    assert push_callers == {"lime/workflows/phases.py"}

    func = _ast_func_source(_REPO_ROOT / "lime" / "cli.py", "_publish_docs_merge")
    names = {n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
    assert not (names & {
        "merge_pr", "teardown", "delete_branch", "fast_forward", "set_run_status",
        "Tracer", "execute", "_git_at", "_git",
    })


def _docs_entry(term: str) -> str:
    return f"- **{term}** — short text.\n"


def _docs_glossary(*entries: str) -> str:
    return "# Glossary\n" + "".join(entries)


def _deploy_docs_conflict_project(tmp_path, monkeypatch, shape):
    """A real origin whose main and Run branch conflict in CONTEXT.md (ticket 275).

    The base glossary (`Notch, Workspace`) lands on main; the Run branch adds `Terminal`;
    a scratch clone pushes `Run line, Run canvas` onto origin/main. Shape `"265"` also
    conflicts on `app.py`; `"rewritten"` rewrites the `Notch` line on both sides instead
    of an insert-only hunk. Returns (root, origin).
    """
    root = tmp_path / "project"
    origin = tmp_path / "origin.git"
    _toy_project_with_origin(root, origin)
    monkeypatch.chdir(root)
    (root / "CONTEXT.md").write_text(
        _docs_glossary(_docs_entry("Notch"), _docs_entry("Workspace"))
    )
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base glossary")
    _git(root, "push", "-q", "origin", "main")
    base_sha = _rev(root, "main")
    _git(root, "checkout", "-q", "-b", "lime/feature/41-toy")
    if shape == "rewritten":
        (root / "CONTEXT.md").write_text(
            _docs_glossary(_docs_entry("Notch on branch"), _docs_entry("Workspace"))
        )
    else:
        (root / "CONTEXT.md").write_text(
            _docs_glossary(
                _docs_entry("Notch"), _docs_entry("Terminal"), _docs_entry("Workspace")
            )
        )
    if shape == "265":
        (root / "app.py").write_text("branch\n")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "branch change")
    _git(root, "push", "-q", "-u", "origin", "lime/feature/41-toy")
    _git(root, "checkout", "-q", "main")
    scratch = tmp_path / "scratch-docs"
    _git(scratch.parent, "clone", "-q", str(origin), str(scratch))
    _git(scratch, "config", "user.email", "t@t")
    _git(scratch, "config", "user.name", "t")
    if shape == "rewritten":
        (scratch / "CONTEXT.md").write_text(
            _docs_glossary(_docs_entry("Notch on main"), _docs_entry("Workspace"))
        )
    else:
        (scratch / "CONTEXT.md").write_text(
            _docs_glossary(
                _docs_entry("Notch"), _docs_entry("Run line"),
                _docs_entry("Run canvas"), _docs_entry("Workspace"),
            )
        )
    if shape == "265":
        (scratch / "app.py").write_text("default\n")
    _git(scratch, "commit", "-qam", "default change")
    _git(scratch, "push", "-q", "origin", "main")
    _seed_gated_run(root, tmp_path, base_sha=base_sha).unlink()
    return root, origin


def _docs_raw(writes):
    entry = {"name": "documenter"}
    if writes is not None:
        entry["writes"] = writes
    return {
        "commands": {
            "test": "grep -q 'Run canvas' CONTEXT.md && grep -q Terminal CONTEXT.md"
                    " && ! grep -q '<<<<<<<' CONTEXT.md",
        },
        "deploy": [],
        "agents": [entry],
    }


def _stub_docs_pr(monkeypatch, origin):
    """`pr_view` reads the live origin tip as `headRefOid`, always with green checks."""

    def fake_pr_view(branch, *, timeout=None):
        head = _out(origin, "rev-parse", f"refs/heads/{branch}")
        return {
            "state": "OPEN",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            "headRefOid": head,
        }

    monkeypatch.setattr(cli.git_helper, "pr_view", fake_pr_view)


def _stub_docs_deploy(monkeypatch, merges):
    monkeypatch.setattr(
        cli.git_helper, "merge_pr",
        lambda branch, head_sha=None: merges.append((branch, head_sha)),
    )
    monkeypatch.setattr(cli.git_helper, "fast_forward", lambda base: "abc1234")
    monkeypatch.setattr(cli.git_helper, "uncommitted_state", lambda path: None)
    monkeypatch.setattr(cli.git_helper, "merged_prs", lambda: [])
    monkeypatch.setattr(cli.worktree, "teardown", lambda *a, **kw: [])
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)


@pytest.mark.parametrize("writes", [
    ["AGENTS.md", "CONTEXT.md", "docs/adr/"],
    ["*.md"],
])
def test_deploy_merges_263_docs_conflict_keeping_both_glossary_lines(
    tmp_path, monkeypatch, capsys, writes
):
    root, origin = _deploy_docs_conflict_project(tmp_path, monkeypatch, "263")
    before = _checkout_snapshot(root)
    pr_sha = _rev(root, "refs/heads/lime/feature/41-toy")
    default_sha = _origin_main(origin)
    monkeypatch.setattr(cli.config, "load", lambda r: _docs_raw(writes))
    merges = []
    _stub_docs_pr(monkeypatch, origin)
    _stub_docs_deploy(monkeypatch, merges)

    assert main(["deploy", "41"]) == 0
    out = capsys.readouterr().out
    assert "resolved a docs-only conflict" in out
    assert len(merges) == 1
    branch, merge_sha = merges[0]
    assert branch == "lime/feature/41-toy" and merge_sha is not None
    assert _out(origin, "rev-parse", "refs/heads/lime/feature/41-toy") == merge_sha
    parents = _out(root, "rev-list", "--parents", "-n1", merge_sha).split()
    assert parents == [merge_sha, pr_sha, default_sha]
    assert _out(root, "show", f"{merge_sha}:CONTEXT.md") == _docs_glossary(
        _docs_entry("Notch"), _docs_entry("Run line"), _docs_entry("Run canvas"),
        _docs_entry("Terminal"), _docs_entry("Workspace"),
    ).rstrip("\n")
    assert _checkout_snapshot(root) == before
    assert "lime-deploy-" not in _out(root, "worktree", "list", "--porcelain")
    assert _refusals(root) == []


@pytest.mark.parametrize("shape,writes,paths", [
    ("265", ["AGENTS.md", "CONTEXT.md", "docs/adr/"], ["CONTEXT.md", "app.py"]),
    ("rewritten", ["AGENTS.md", "CONTEXT.md", "docs/adr/"], ["CONTEXT.md"]),
    ("263", None, ["CONTEXT.md"]),
    ("263", ["AGENTS.md", "docs/adr/"], ["CONTEXT.md"]),
])
def test_deploy_docs_conflict_refusals_push_nothing(
    tmp_path, monkeypatch, capsys, shape, writes, paths
):
    root, origin = _deploy_docs_conflict_project(tmp_path, monkeypatch, shape)
    monkeypatch.setattr(cli.config, "load", lambda r: _docs_raw(writes))
    _stub_docs_pr(monkeypatch, origin)
    _fail_if_called(monkeypatch, (cli.quality, "run_check"), "a conflicted tree must refuse before commands")
    _fail_if_called(monkeypatch, (cli.git_helper, "merge_pr"), "a conflicted tree must refuse before merge")
    _fail_if_called(monkeypatch, (cli.worktree, "teardown"), "a conflicted tree must refuse before teardown")
    before_refs = _out(origin, "for-each-ref")

    assert main(["deploy", "41"]) == 1
    assert _out(origin, "for-each-ref") == before_refs
    assert _refusals(root) == [
        ("deadbeef", "merge_conflict", {"ticket": "41-toy", "paths": paths})
    ]


def test_deploy_rerun_after_docs_merge_ci_pending_uses_unchanged_gate(
    tmp_path, monkeypatch, capsys
):
    root, origin = _deploy_docs_conflict_project(tmp_path, monkeypatch, "263")
    monkeypatch.setattr(
        cli.config, "load",
        lambda r: _docs_raw(["AGENTS.md", "CONTEXT.md", "docs/adr/"]),
    )
    pr_sha = _rev(root, "refs/heads/lime/feature/41-toy")
    merges = []
    _stub_docs_deploy(monkeypatch, merges)
    state = {"ci": "pending"}

    def fake_pr_view(branch, *, timeout=None):
        head = _out(origin, "rev-parse", f"refs/heads/{branch}")
        if head == pr_sha or state["ci"] == "green":
            checks = [{"conclusion": "SUCCESS"}]
        else:
            checks = [{"conclusion": None, "status": "IN_PROGRESS"}]
        return {"state": "OPEN", "statusCheckRollup": checks, "headRefOid": head}

    monkeypatch.setattr(cli.git_helper, "pr_view", fake_pr_view)

    assert main(["deploy", "41"]) == 1
    merge_sha = _out(origin, "rev-parse", "refs/heads/lime/feature/41-toy")
    assert merge_sha != pr_sha  # the first deploy pushed the tested merge
    assert _refusals(root) == [
        ("deadbeef", "docs_merge_ci_pending", {"ticket": "41-toy", "sha": merge_sha})
    ]
    assert merges == []

    state["ci"] = "green"
    assert main(["deploy", "41"]) == 0
    assert merges == [("lime/feature/41-toy", None)]
    assert _out(origin, "rev-parse", "refs/heads/lime/feature/41-toy") == merge_sha
    assert _refusals(root) == [
        ("deadbeef", "docs_merge_ci_pending", {"ticket": "41-toy", "sha": merge_sha})
    ]


@pytest.mark.parametrize("second", ["green", "red", "pending"])
def test_deploy_link_waits_once_more_for_ci_on_a_pushed_docs_merge(
    tmp_path, monkeypatch, capsys, second
):
    """Ticket 276: a deploy refused only for `docs_merge_ci_pending` waits once more for
    that CI — outside the launch lock — and deploys again; a second `pending` holds."""
    root, origin = _deploy_docs_conflict_project(tmp_path, monkeypatch, "263")
    monkeypatch.setattr(
        cli.config, "load",
        lambda r: _docs_raw(["AGENTS.md", "CONTEXT.md", "docs/adr/"]),
    )
    pr_sha = _rev(root, "refs/heads/lime/feature/41-toy")
    merges = []
    _stub_docs_deploy(monkeypatch, merges)
    if second == "pending":
        monkeypatch.setattr(cli, "DEPLOY_CI_CEILING_S", 60)
        monkeypatch.setattr(cli, "DEPLOY_CI_POLL_S", 30)
    state = {"second": False, "polls": 0}

    def fake_pr_view(branch, *, timeout=None):
        head = _out(origin, "rev-parse", f"refs/heads/{branch}")
        if head == pr_sha:
            checks = [{"conclusion": "SUCCESS"}]
        elif not state["second"]:
            checks = [{"conclusion": None, "status": "IN_PROGRESS"}]
        else:
            state["polls"] += 1
            if state["polls"] == 1 or second == "pending":
                checks = [{"conclusion": None, "status": "IN_PROGRESS"}]
            elif second == "green":
                checks = [{"conclusion": "SUCCESS"}]
            else:
                checks = [{"conclusion": "FAILURE"}]
        return {"state": "OPEN", "statusCheckRollup": checks, "headRefOid": head}

    monkeypatch.setattr(cli.git_helper, "pr_view", fake_pr_view)

    real_await = cli._await_ci
    waits: list[tuple[dict, bool]] = []

    def spy_await(root_, arg, **kw):
        if kw.get("checks_expected"):
            state["second"] = True
        waits.append((kw, _launch_lock_held(root_)))
        return real_await(root_, arg, **kw)

    monkeypatch.setattr(cli, "_await_ci", spy_await)

    real_deploy = cli._cmd_deploy
    deploy_locks: list[bool] = []

    def spy_deploy(args):
        deploy_locks.append(_launch_lock_held(root))
        return real_deploy(args)

    monkeypatch.setattr(cli, "_cmd_deploy", spy_deploy)

    result = cli._deploy_link(root, "41")
    merge_sha = _out(origin, "rev-parse", "refs/heads/lime/feature/41-toy")
    pending_row = (
        "deadbeef", "docs_merge_ci_pending", {"ticket": "41-toy", "sha": merge_sha}
    )
    out, err = capsys.readouterr()

    assert waits == [({}, False), ({"checks_expected": True}, False)]
    assert deploy_locks and all(deploy_locks)
    assert _refusals(root)[0] == pending_row

    if second == "green":
        assert result is None
        assert merges == [("lime/feature/41-toy", None)]
        assert len(deploy_locks) == 2
        assert "waiting once more" in out
        assert _refusals(root) == [pending_row]
    elif second == "red":
        assert result == "lime deploy 41"
        assert merges == []
        assert len(deploy_locks) == 2
        assert _refusals(root) == [
            pending_row, ("deadbeef", "ci_red", {"ticket": "41-toy"})
        ]
        assert "not deployed (lime deploy exit 1)" in err
    else:
        assert result == "lime deploy 41"
        assert merges == []
        assert len(deploy_locks) == 1
        assert _refusals(root) == [pending_row]
        assert "still pending" in err


def _deploy_link_rewait_setup(tmp_path, monkeypatch):
    """A success Run `deadbeef` on `lime/feature/41-toy`, green CI, no `deploy:` commands."""
    root = tmp_path / "project"
    root.mkdir()
    _success_run(root, tmp_path / "worktrees")
    monkeypatch.setattr(cli.config, "load", lambda r: {"deploy": []})
    monkeypatch.setattr(
        cli.git_helper, "pr_view",
        lambda branch, **kw: {"statusCheckRollup": [{"conclusion": "SUCCESS"}]},
    )
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    real_await = cli._await_ci
    waits: list[dict] = []

    def spy_await(root_, arg, **kw):
        waits.append(kw)
        return real_await(root_, arg, **kw)

    monkeypatch.setattr(cli, "_await_ci", spy_await)
    return root, waits


@pytest.mark.parametrize("case", [
    "ci_red",
    "pr_not_open",
    "docs_merge_ci_red",
    "docs_merge_head_missing",
    "docs_merge_push_rejected",
    "merge_conflict",
    "no_row",
    "stale_pending",
    "other_run_pending",
    "crash_after_pending",
])
def test_deploy_link_never_waits_again_on_any_other_refusal(
    tmp_path, monkeypatch, capsys, case
):
    """Ticket 276: only exactly one new `docs_merge_ci_pending` for this Run re-waits."""
    root, waits = _deploy_link_rewait_setup(tmp_path, monkeypatch)
    if case == "stale_pending":
        cli._record_refusal(
            root, "41-toy", "docs_merge_ci_pending", run_id="deadbeef", sha="old"
        )
    deploys: list[str] = []

    def fake_cmd_deploy(args):
        deploys.append(args.ticket)
        if case in {
            "ci_red", "pr_not_open", "docs_merge_ci_red", "docs_merge_head_missing",
            "docs_merge_push_rejected", "merge_conflict",
        }:
            cli._record_refusal(root, "41-toy", case, run_id="deadbeef")
        elif case == "other_run_pending":
            cli._record_refusal(
                root, "41-toy", "docs_merge_ci_pending", run_id="other", sha="abc"
            )
        elif case == "crash_after_pending":
            cli._record_refusal(
                root, "41-toy", "docs_merge_ci_pending", run_id="deadbeef", sha="abc"
            )
            raise RuntimeError("deploy crashed")
        return 1

    monkeypatch.setattr(cli, "_cmd_deploy", fake_cmd_deploy)

    assert cli._deploy_link(root, "41") == "lime deploy 41"
    assert len(waits) == 1
    assert deploys == ["41"]


def test_deploy_link_waits_at_most_once_more(tmp_path, monkeypatch):
    """Ticket 276: a second `docs_merge_ci_pending` holds the link — never a third wait."""
    root, waits = _deploy_link_rewait_setup(tmp_path, monkeypatch)
    deploys: list[str] = []

    def fake_cmd_deploy(args):
        deploys.append(args.ticket)
        cli._record_refusal(
            root, "41-toy", "docs_merge_ci_pending", run_id="deadbeef", sha="abc"
        )
        return 1

    monkeypatch.setattr(cli, "_cmd_deploy", fake_cmd_deploy)

    assert cli._deploy_link(root, "41") == "lime deploy 41"
    assert len(waits) == 2
    assert waits[1] == {"checks_expected": True}
    assert deploys == ["41", "41"]
