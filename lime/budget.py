"""Context budget: measurement, thresholds, `budget.json`, and the handoff note (spec §9).

Numbers absolute and uniform: warn 150k, stop `min(250k, window - 2·RESERVE)`. An
agent's own `budget:` override (AgentConfig.budget) beats the Run's `config.yaml
budget:` key, which beats these defaults. The backstop — the Spine's own kill
point — is always `window - RESERVE`, even when `stop` sits lower because the
config capped it; it exists for the model that keeps burning tokens through
repeated Guard denials instead of wrapping up. One RESERVE (`{RESERVE:,}`) of
wrap-up room always separates them (before: both 180k for claude_code’s 200k
estimate → a reviewer killed on turn one, run 322ff312).

This module is pure: no subprocess, no tracer, no adapter. `lime guard` (cli.py)
and `agents.execute()` are the two callers; each owns its own file I/O and
process control, this owns only the numbers and the note's shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

WARN = 150_000
STOP = 250_000
RESERVE = 20_000  # margin below the model's real window (spec §9)

BudgetState = Literal["ok", "warn", "stop"]

HANDOFF_SECTIONS = ("Done", "Left", "Decisions", "Don't redo", "Open")


def stop_threshold(window: int, stop: int = STOP) -> int:
    """`min(stop, window - 2·RESERVE)` — a small real window caps the configured default, and the cap stays one RESERVE under the backstop so the Guard's `stop` (tools closed, handoff/partial still allowed) lands before the Spine's kill (ticket 83)."""
    return min(stop, window - 2 * RESERVE) if window else min(stop, STOP)


def backstop(window: int) -> int:
    """The Spine's own kill point. An unknown window (0) never triggers it early."""
    return window - RESERVE if window else STOP + RESERVE


def thresholds(
    agent_budget: dict[str, int] | None, run_budget: dict[str, int] | None, window: int
) -> tuple[int, int, int]:
    """(warn, stop, backstop) for one agent call — agent overrides run, both fall back to defaults."""
    merged = {**(run_budget or {}), **(agent_budget or {})}
    warn = merged.get("warn", WARN)
    stop = stop_threshold(window, merged.get("stop", STOP))
    return warn, stop, backstop(window)


def evaluate(context_tokens: int, warn: int, stop: int) -> BudgetState:
    if context_tokens >= stop:
        return "stop"
    if context_tokens >= warn:
        return "warn"
    return "ok"


def write(
    budget_file: Path, *, state: BudgetState, context_tokens: int, warn: int, stop: int,
    reason: str | None = None,
) -> None:
    """The Spine's mailbox to the Guard — one small file, fully overwritten every turn.

    `reason` names *why* the mailbox stopped (e.g. `time_limit`) so the Guard can
    pick its wording. It is serialized only when present; a full replacement
    without one removes any previous reason (legacy callers stay reason-less)."""
    payload = {"state": state, "context_tokens": context_tokens, "warn": warn, "stop": stop}
    if reason is not None:
        payload["reason"] = reason
    budget_file.write_text(json.dumps(payload))


def read(budget_file: Path) -> dict:
    """`ok` for a missing or unreadable file — no budget.json yet means nothing to warn about."""
    try:
        return json.loads(budget_file.read_text())
    except (OSError, ValueError):
        return {"state": "ok"}


def git_facts(repo_root, continuation: int) -> str:
    """Spine-appended facts a fresh Session gets alongside the note (spec §9)."""
    from . import git_helper  # here, not at module top: budget.py stays import-light for `lime guard`

    return (
        f"## Git facts\ncontinuation: {continuation}\n\n"
        f"### git status --short\n{git_helper.status_short() or '(clean)'}\n\n"
        f"### git diff --stat\n{git_helper.diff_stat('HEAD') or '(no diff)'}"
    )


def code_only_note(repo_root, continuation: int, cause: str = "backstop kill") -> str:
    """The backstop kill's note: no agent prose survived, so this is facts only."""
    body = "\n\n".join(f"## {s}\n({cause} — Session ended before writing a handoff)" for s in HANDOFF_SECTIONS)
    return f"{body}\n\n{git_facts(repo_root, continuation)}"
