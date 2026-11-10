"""Deterministic test, lint, typecheck, and build blocks.

A known command is not a judgement call. Anything whose invocation you can
write down belongs here as code — it runs in milliseconds, costs nothing, and
returns the same answer every time. Agents are for the parts that need reading
and deciding.

lime (ADR 0001): commands are not hard-coded per repo; they come from the
target project's `config.yaml commands:` — shell lines, run verbatim with the
operator's environment. The verify step (§7) is `run_quality` over
test → lint → typecheck → build, absent keys skipped.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .data_types import QualityCheckResult, QualityCheckSpec, QualityResult
from .utils import now_iso, operator_env

# How much of a failing command's output rides back inside the envelope. Enough
# for a builder to act on without opening the artifact; bounded so a runaway
# stack trace can't swamp the next agent's context.
TAIL_CHARS = 4_000

ORDER = ("test", "lint", "typecheck", "build")  # §7: verify runs these in order
# Default caps; `commands.test_timeout` overrides "test" (ticket 272). Others:
# QualityCheckSpec default.
TIMEOUTS = {"test": 600, "build": 600}


def test_timeout_failure(commands: dict) -> str | None:
    """None when commands.test_timeout is absent or a whole number ≥ 1; else the refusal."""
    commands = commands or {}
    if "test_timeout" not in commands:
        return None
    value = commands["test_timeout"]
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return None
    return (
        f"commands.test_timeout {value!r} must be a whole number of seconds, at least 1"
    )


def check_timeout(commands: dict, name: str) -> int:
    """The seconds `name` may run: the configured test_timeout for "test", else the default."""
    if name == "test" and "test_timeout" in (commands or {}):
        failure = test_timeout_failure(commands)
        if failure:
            raise ValueError(failure)
        return commands["test_timeout"]
    return TIMEOUTS.get(name, 120)


def _check_dir(run, name: str) -> Path:
    seq = run.phases[-1].seq if run.phases else 0
    path = run.context_handoff_dir / "quality" / f"{seq:02d}_{name}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run(spec: QualityCheckSpec, run) -> QualityCheckResult:
    phase = run.phases[-1] if run.phases else None
    output_dir = _check_dir(run, spec.name)
    output_artifact = output_dir / "command.log"
    # A Run whose subprocess identity differs from the process environment (the deploy
    # gate's throwaway check) carries the full env it wants; every ordinary Run has no
    # such attribute and gets the engineer's own shell environment, exactly as before.
    explicit = getattr(run, "command_env", None)
    env = dict(explicit) if explicit else operator_env()

    run.console.note(f"quality {spec.name}: {spec.command}")
    started_at = now_iso()
    clock = time.monotonic()
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            spec.command,
            shell=True,  # the config line is a shell command, `&&` and `$LIME_*` included
            cwd=run.repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=spec.timeout_seconds,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as error:
        returncode = 124
        stdout = _text(error.stdout)
        stderr = _text(error.stderr) + f"\nTimed out after {spec.timeout_seconds}s."
    except OSError as error:
        # A missing shell lands here with the real message — no pre-flight
        # probe needed, and none wanted.
        returncode = 127
        stderr = str(error)

    duration = time.monotonic() - clock
    output_artifact.write_text(
        f"$ {spec.command}\nexit: {returncode}\nduration_seconds: {duration:.3f}\n"
        f"\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n"
    )
    passed = returncode == 0
    run.tracer.event(
        run.run_id,
        "tool_call",
        phase_id=phase.phase_id if phase else None,
        name=f"quality:{spec.name}",
        payload={
            "command": spec.command,
            "returncode": returncode,
            "passed": passed,
            "output_artifact": str(output_artifact),
            "started_at": started_at,
            "ended_at": now_iso(),
        },
    )
    run.console.note(
        f"quality {spec.name}: {'passed' if passed else 'failed'} "
        f"(exit {returncode}, {duration:.1f}s)"
    )
    return QualityCheckResult(
        name=spec.name,
        command=spec.command,
        returncode=returncode,
        passed=passed,
        duration_seconds=duration,
        output_artifact=str(output_artifact),
        output_tail=(stdout + stderr)[-TAIL_CHARS:],
    )


def _text(data) -> str:
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return data or ""


def run_check(run, name: str) -> QualityCheckResult | None:
    """One named block from config. None when the project defines no such command."""
    commands = run.commands or {}
    command = commands.get(name)
    if not command:
        return None
    spec = QualityCheckSpec(
        name=name, command=command, timeout_seconds=check_timeout(commands, name)
    )
    return _run(spec, run)


def run_quality(run) -> QualityResult:
    """Run every configured block and collect ALL failures — one pass tells you everything.

    Ordering contract for the caller: a failing block does NOT fail the phase.
    The runner did its job; the CODE is what failed. Hand this result to the
    builder and let the bounded repair loop decide the run's fate.
    """
    checks = [c for name in ORDER if (c := run_check(run, name)) is not None]
    # A failure is the command, its exit code, and what it actually printed —
    # everything a builder needs to repair without opening a log or being told
    # what the error "means" by a parser that guessed.
    failures = [
        f"{check.name}: `{check.command}` exited {check.returncode}\n{check.output_tail}".rstrip()
        for check in checks
        if not check.passed
    ]
    return QualityResult(
        passed=not failures,
        checks=checks,
        failures=failures,
        artifacts=[check.output_artifact for check in checks],
    )
