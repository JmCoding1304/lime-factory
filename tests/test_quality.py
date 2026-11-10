"""Deterministic test/lint/typecheck/build blocks over `config.yaml commands:` (spec §7)."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lime import quality
from lime.data_types import Phase, PhaseParams, QualityCheckResult
from lime.tracer import Tracer


class FakeConsole:
    def note(self, message: str) -> None:
        pass


def fake_run(tmp_path, commands: dict) -> SimpleNamespace:
    tracer = Tracer(tmp_path / "runs")
    tracer.start_run("R1", workflow="feature", request="x")
    phase_id = tracer.add_phase("R1", seq=1, name="verify", stage="Build", kind="code", owner="git", description="Run the checks")
    phase = Phase(
        phase_id=phase_id, run_id="R1", seq=1,
        params=PhaseParams(name="verify", stage="Build", kind="code", owner="git", description="Run the checks"),
    )
    return SimpleNamespace(
        repo_root=tmp_path, run_id="R1", tracer=tracer, console=FakeConsole(),
        phases=[phase], commands=commands, context_handoff_dir=tmp_path / "handoff",
    )


def test_run_check_returns_none_for_an_unconfigured_command(tmp_path):
    run = fake_run(tmp_path, {})
    assert quality.run_check(run, "lint") is None


def test_run_check_runs_the_configured_shell_line(tmp_path):
    run = fake_run(tmp_path, {"lint": "echo linting && exit 0"})
    result = quality.run_check(run, "lint")
    assert result.passed
    assert result.returncode == 0
    assert "linting" in (result.output_tail)


def test_run_check_captures_a_failing_command(tmp_path):
    run = fake_run(tmp_path, {"typecheck": "echo type error && exit 1"})
    result = quality.run_check(run, "typecheck")
    assert not result.passed
    assert "type error" in result.output_tail


def test_run_quality_skips_absent_keys_and_collects_every_failure(tmp_path):
    run = fake_run(tmp_path, {"test": "exit 1", "build": "exit 1"})
    result = quality.run_quality(run)
    assert not result.passed
    assert [c.name for c in result.checks] == ["test", "build"]  # lint/typecheck absent, skipped
    assert len(result.failures) == 2


def test_run_check_writes_the_command_log_artifact(tmp_path):
    run = fake_run(tmp_path, {"lint": "echo hi && exit 0"})
    result = quality.run_check(run, "lint")
    assert Path(result.output_artifact).read_text().startswith("$ echo hi")


def test_run_check_explicit_environment_is_scoped(tmp_path, monkeypatch):
    """A Run carrying `command_env` gets it verbatim; ordinary Runs still get operator_env()."""
    monkeypatch.setenv("LIME_DB_NAME", "parent-db")

    def run_dir(name):
        path = tmp_path / name
        path.mkdir()
        return path

    scoped = fake_run(
        run_dir("scoped"),
        {"test": 'test "$LIME_DB_NAME" = "lime_check" && test "$LIME_PORT" = "5555"'},
    )
    scoped.command_env = {
        **os.environ, "LIME_RUN_ID": "check", "LIME_PORT": "5555", "LIME_DB_NAME": "lime_check",
    }
    assert quality.run_check(scoped, "test").passed
    assert os.environ["LIME_DB_NAME"] == "parent-db"  # global env untouched

    # absent attribute: falls back to the engineer's own environment
    plain = fake_run(run_dir("plain"), {"test": 'test "$LIME_DB_NAME" = "parent-db"'})
    assert not hasattr(plain, "command_env")
    assert quality.run_check(plain, "test").passed

    # explicit None: same fallback
    noned = fake_run(run_dir("noned"), {"test": 'test "$LIME_DB_NAME" = "parent-db"'})
    noned.command_env = None
    assert quality.run_check(noned, "test").passed


# --- commands.test_timeout (ticket 272) ---


def _spy_run(recorded):
    def spy(spec, run):
        recorded.append((spec.name, spec.timeout_seconds))
        return QualityCheckResult(
            name=spec.name, command=spec.command, returncode=0, passed=True,
            duration_seconds=0.0, output_artifact="", output_tail="",
        )
    return spy


def test_run_check_test_timeout_defaults_to_600(tmp_path, monkeypatch):
    run = fake_run(tmp_path, {"test": "true"})
    recorded = []
    monkeypatch.setattr(quality, "_run", _spy_run(recorded))
    assert quality.run_check(run, "test").passed
    assert recorded == [("test", 600)]
    assert recorded[0][1] == quality.TIMEOUTS["test"]


def test_run_quality_uses_the_configured_test_timeout_only_for_test(tmp_path, monkeypatch):
    run = fake_run(
        tmp_path,
        {"test": "true", "lint": "true", "build": "true", "test_timeout": 1500},
    )
    recorded = []
    monkeypatch.setattr(quality, "_run", _spy_run(recorded))
    result = quality.run_quality(run)
    assert recorded == [("test", 1500), ("lint", 120), ("build", 600)]
    assert [c.name for c in result.checks] == ["test", "lint", "build"]


def test_run_check_configured_test_timeout_expires(tmp_path):
    run = fake_run(tmp_path, {"test": "exec sleep 5", "test_timeout": 1})
    result = quality.run_check(run, "test")
    assert result.returncode == 124
    assert not result.passed
    assert "Timed out after 1s." in result.output_tail


@pytest.mark.parametrize("bad", [0, -5, "soon", True, 1.5, None])
def test_invalid_test_timeout_is_refused(bad):
    assert "commands.test_timeout" in quality.test_timeout_failure({"test_timeout": bad})
    with pytest.raises(ValueError, match="commands.test_timeout"):
        quality.check_timeout({"test": "x", "test_timeout": bad}, "test")
    assert quality.test_timeout_failure({}) is None
    assert quality.test_timeout_failure({"test_timeout": 1500}) is None
    assert quality.check_timeout({"test_timeout": 0}, "lint") == 120
