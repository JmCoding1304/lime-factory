"""One deterministic limits line, rendered differently for the planner and the builder.

The planner sees the *builder's* resolved limits — the Session's wall-clock ceiling,
token warn/stop and the Run's cumulative added-lines sizing budget — so a plan is sized
against the same numbers that will be measured against it (ticket 193). The builder sees
only its own Session ceilings and never sizes code against a line count. This module
renders those numbers once, identically, for phase execution and for the CLI's read-only
stale-plan decision.

The planner line stays byte-identical to the historical `Builder limits:` line, so a
pre-193 Run's recorded plan identity (ADR 0025) still matches and its plan replays with
no forced re-plan. Only the build/revise prompts change, and they are not plan identity.

Pure resolution: it reuses `agents._context_window`, `budget.thresholds` and
`config.resolve_commit_settings` and duplicates none of their algorithms (ticket 129).
`agents` never imports this helper, so there is no cycle. No elapsed time, remaining
lines, live usage, git statistics or trace data belongs in the line — those are dynamic
and would make equivalent configurations render differently.
"""

from __future__ import annotations

import json

from . import agents, budget, config
from .data_types import AgentConfig

PREFIX = "Builder limits: "


def builder_line(agent: AgentConfig, raw: dict) -> str:
    """The one deterministic `Builder limits: …` line for a resolved builder.

    Numbers are the builder's own Session ceilings: the resolved Session timeout and
    the effective warn/stop (agent override beats run, capped by the model window).
    It never carries `max_added_lines` or `uncounted` — the builder never sizes code
    against a line count."""
    window = agents._context_window(agent)
    warn, stop, _ = budget.thresholds(agent.budget, (raw or {}).get("budget"), window)
    return (
        f"{PREFIX}timeout_s={agent.timeout_s}; token_warn={warn};"
        f" token_stop={stop}"
    )


def planner_line(agent: AgentConfig, raw: dict) -> str:
    """The `Builder limits: …` line the planner sees: the builder's clock/token
    ceilings plus the Run's added-lines sizing budget.

    Byte-identical to the historical `line()`: `builder_line` plus the Run's cumulative
    `max_added_lines`. Uncounted patterns are JSON-rendered sorted and deduplicated so
    equivalent configurations render identically; an explicit zero budget and an empty
    pattern list are preserved."""
    _, uncounted, max_added_lines = config.resolve_commit_settings(raw or {})
    patterns = json.dumps(sorted(set(uncounted)), separators=(",", ":"))
    return (
        f"{builder_line(agent, raw)}; max_added_lines={max_added_lines};"
        f" uncounted={patterns}"
    )


def append_builder(prompt: str, agent: AgentConfig, raw: dict) -> str:
    """Append exactly one builder-limits line to a raw build/revise input."""
    return f"{prompt}\n\n{builder_line(agent, raw)}"


def append_planner(prompt: str, agent: AgentConfig, raw: dict) -> str:
    """Append exactly one planner-limits line to a raw planning input."""
    return f"{prompt}\n\n{planner_line(agent, raw)}"
