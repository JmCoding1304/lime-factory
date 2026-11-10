"""Ticket 235 — `lime focus summarize`: one short "what to do" per report item.

This module reads the report (`lime.status`) and, for each summarizable item,
asks the `summarizer` Role for at most two plain sentences. It writes only the
project's `lime.db` — a `summaries` row per item plus one Run-less `sessions`
row and `turn` events per call. A summarizer Session has no Run: it carries
`tracer.NO_RUN`, so `lime cost` counts its turns and nothing lists it as a Run.
"""

from __future__ import annotations

import re
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml
from pydantic import ValidationError

from lime import agents, budget, config, home, prompts, status, tracer
from lime.data_types import AgentConfig, AgentRequest

MAX_SENTENCES = 2
MAX_CHARS = 280
_ABBREV = re.compile(r"(?:\b[A-Za-z]\.){2,}$")

DEFAULT_SUMMARIZER = AgentConfig(
    name="summarizer",
    color="",
    runtime="claude_code",
    model="claude-haiku-5-5",
    thinking="low",
    tools=[],
    writes=[],
    timeout_s=120,
)


def _is_abbrev(prefix: str) -> bool:
    """A `.` ending an initialism (`P.I.`, `U.S.`, `e.g.`, `i.e.`) is not a
    sentence end."""
    return _ABBREV.search(prefix) is not None


def trim(text: str) -> str:
    """The first two sentences of `text`, collapsed to single spaces, capped at
    `MAX_CHARS` (cut at a word boundary and marked with `…`). `""` for blank."""
    text = " ".join(text.split())
    if not text:
        return ""
    ends: list[int] = []
    for i, ch in enumerate(text):
        if ch not in ".!?":
            continue
        if i + 1 < len(text) and text[i + 1] != " ":
            continue
        if ch == "." and _is_abbrev(text[: i + 1]):
            continue
        ends.append(i)
    if len(ends) >= MAX_SENTENCES:
        text = text[: ends[MAX_SENTENCES - 1] + 1]
    if len(text) > MAX_CHARS:
        cut = text.rfind(" ", 0, MAX_CHARS - 1)
        if cut < 0:
            cut = MAX_CHARS - 1
        text = text[:cut].rstrip(",;:") + "…"
    return text


def age(since: str | None, now: datetime) -> str:
    """How long the item has waited: `N days (since YYYY-MM-DD)`, or
    `less than a day`, or `an unknown time` when `since` is unusable."""
    if not since:
        return "an unknown time"
    try:
        dt = status._parse_dt(since)
    except (ValueError, TypeError):
        return "an unknown time"
    days = (now - dt).days
    if days >= 1:
        word = "day" if days == 1 else "days"
        return f"{days} {word} (since {dt.date().isoformat()})"
    return "less than a day"


def summarizer(root: Path) -> AgentConfig:
    """The project's `summarizer` Role, tools and writes forced to `[]`; the
    default Role when none is configured or the config cannot be read."""
    try:
        roster = config.roster(config.load(root))
    except (OSError, ValueError, yaml.YAMLError, ValidationError):
        return DEFAULT_SUMMARIZER
    for agent in roster:
        if agent.name == "summarizer":
            return agent.model_copy(update={"tools": [], "writes": []})
    return DEFAULT_SUMMARIZER


def _forwarder(
    tr: tracer.Tracer, session_id: str, phase_id: str, agent_name: str
) -> Callable[[dict], None]:
    """Feed the adapter's `turn` events into the ledger as `turn` events on
    `NO_RUN`, with the same payload shape as `agents._event_forwarder`."""
    turn_no = 0

    def forward(event: dict) -> None:
        nonlocal turn_no
        if event.get("type") != "turn":
            return
        turn_no += 1
        usage = event.get("usage") or {}
        tr.event(
            tracer.NO_RUN,
            "turn",
            phase_id=phase_id,
            name=session_id,
            tokens=event.get("context_tokens", 0),
            payload={
                "turn_no": turn_no,
                "model": event.get("model"),
                "agent": agent_name,
                "input": usage.get("input", usage.get("input_tokens", 0)),
                "output": usage.get("output", usage.get("output_tokens", 0)),
                "cache_read": usage.get("cacheRead", usage.get("cache_read_input_tokens", 0)),
                "cache_write": usage.get("cacheWrite", usage.get("cache_creation_input_tokens", 0)),
                "cost": event.get("cost", 0.0),
                "context_tokens": event.get("context_tokens", 0),
            },
        )

    return forward


def _nudge_prompt(agent: AgentConfig) -> str:
    """The one reporting nudge after a Session's ceiling (ADR 0011/0022). The
    summarizer has no tools, so the latched stop is structural: after this send
    the only remaining outcome is the summary or a failure."""
    return (
        f"Time is up: this Session's {agent.timeout_s}s ceiling has passed and your last"
        " model call was stopped. This session and everything you did before it are intact"
        " — do not start new work. Reply now with ONLY the two requested sentences on what"
        " the owner should do. You have"
        f" {agents.NUDGE_TIMEOUT_S}s."
    )


def _ask(
    tr: tracer.Tracer,
    agent: AgentConfig,
    system: str,
    prompt: str,
    root: Path,
    key: str,
) -> tuple[str | None, str | None]:
    """One item, no Run. Returns `(summary, error)` — exactly one is set.

    Recovery follows the same ladder every Role's Session does: a timeout spends
    the ceiling's one reporting nudge before it can fail (ADR 0011/0022), and a
    transient pi stream error retries in place (ADR 0008) then once in a fresh
    Session (ADR 0009) before it can fail.
    """
    phase_id = f"summary:{key}"

    # The resolved thresholds after per-agent overrides, persisted on the Session
    # row at creation like every other Session (ticket 251).
    try:
        run_budget = (config.load(root) or {}).get("budget")
    except (OSError, ValueError, yaml.YAMLError, ValidationError):
        run_budget = None
    warn, stop, _kill_at = budget.thresholds(
        agent.budget, run_budget, agents._context_window(agent)
    )

    def open_session(index: int) -> tuple[str, Path, Callable[[dict], None]]:
        tracer_sid = tr.start_summary_session(
            phase_id, role=agent.name, runtime=agent.runtime, model=agent.model,
            color=agent.color, budget_warn=warn, budget_stop=stop,
        )
        return str(uuid.uuid4()), Path("session") / str(index), _forwarder(
            tr, tracer_sid, phase_id, agent.name
        )

    with tempfile.TemporaryDirectory(prefix="lime-summary-") as scratch:
        base = Path(scratch)

        def call(
            runtime_sid: str,
            subdir: Path,
            text: str,
            forward: Callable[[dict], None],
            *,
            resume: bool,
            timeout_s: int,
        ):
            session_dir = base / subdir
            session_dir.mkdir(parents=True, exist_ok=True)
            req = AgentRequest(
                runtime=agent.runtime,
                model=agent.model,
                thinking=agent.thinking,
                system_prompt=system,
                prompt=text,
                session_id=runtime_sid,
                resume=resume,
                session_dir=str(session_dir),
                raw_output_path=str(session_dir / "raw_output.jsonl"),
                cwd=str(root),
                tools=[],
                writes=[],
                timeout_s=timeout_s,
            )
            return agents.ADAPTERS[agent.runtime](req, on_event=forward)

        runtime_sid, subdir, forward = open_session(0)

        def nudge() -> AgentResult:
            """Spend the current Session's one reporting nudge after a ceiling hit.

            Reads the live `runtime_sid`/`subdir`/`forward`, so it always nudges the
            Session currently in play (the fresh one after ADR 0009). The caller
            decides whether the item fails; a nudge never does so on its own.
            """
            return call(runtime_sid, subdir, _nudge_prompt(agent), forward,
                        resume=True, timeout_s=agents.NUDGE_TIMEOUT_S)

        try:
            result = call(runtime_sid, subdir, prompt, forward,
                          resume=False, timeout_s=agent.timeout_s)
            # Ceiling: latch the stop and spend the one reporting nudge BEFORE any
            # failure is raised, exactly as a Role's Session does (ADR 0011/0022).
            if result.status == "timeout":
                result = nudge()
                if result.status != "ok":
                    return None, f"timeout after {agent.timeout_s}s"
            # Transient provider/stream errors retry in place (ADR 0008), then once
            # via a fresh Session (ADR 0009). pi-only — claude_code never reports them.
            attempt = 0
            while (agent.runtime == "pi" and agents.transient_error(result)
                   and attempt < agents.STREAM_RETRIES):
                attempt += 1
                time.sleep(agents.STREAM_RETRY_DELAY_S * 2 ** (attempt - 1))
                result = call(runtime_sid, subdir, agents._stream_retry_prompt(result.error),
                              forward, resume=True, timeout_s=agent.timeout_s)
                # A retry can itself hit the ceiling: it must spend the Session's one
                # reporting nudge before failure, never fail silently (ADR 0011/0022).
                if result.status == "timeout":
                    result = nudge()
                    if result.status != "ok":
                        return None, f"timeout after {agent.timeout_s}s"
            if agent.runtime == "pi" and agents.transient_error(result):
                runtime_sid, subdir, forward = open_session(1)
                result = call(runtime_sid, subdir, prompt, forward,
                              resume=False, timeout_s=agent.timeout_s)
                if result.status == "timeout":
                    result = nudge()
                    if result.status != "ok":
                        return None, f"timeout after {agent.timeout_s}s"
        except Exception as exc:  # noqa: BLE001 — one bad item never aborts the batch
            return None, f"{type(exc).__name__}: {exc}"
    if result.status == "timeout":
        return None, f"timeout after {agent.timeout_s}s"
    if result.status != "ok":
        return None, result.error or f"{result.status}: {result.stderr_tail[-200:]}"
    text = trim(result.text)
    if not text:
        return None, "empty answer"
    return text, None


def summarize_project(
    root: Path,
    items: list[dict],
    now: datetime,
    *,
    on_failure: Callable[[str, str], None] | None = None,
) -> dict[str, int]:
    """Summarize every changed, summarizable item; return counts written / failed /
    unchanged. A re-run with nothing changed claims nothing and calls no agent.
    `on_failure(key, error)` is called once per failed item (the one place a
    failure is reported)."""
    root = Path(root)
    counts = {"written": 0, "failed": 0, "unchanged": 0}
    agent = summarizer(root)
    system = prompts.render(prompts.resolve_path(agent, "system", root), {})
    template = prompts.resolve_path(agent, "user", root)
    tr = tracer.Tracer(root / ".lime" / "runs")
    try:
        for item in items:
            if not status.summarizable(item):
                continue
            key = status.summary_key(item)
            source = status.summary_source(item, root)
            fp = status.fingerprint(source)
            if not tr.claim_summary(key, fp):
                counts["unchanged"] += 1
                continue
            prompt = prompts.render(
                template,
                {
                    "today": now.date().isoformat(),
                    "age": age(item["since"], now),
                    "source": source,
                },
            )
            summary, error = _ask(tr, agent, system, prompt, root, key)
            tr.finish_summary(key, fp, summary=summary, error=error)
            if summary:
                counts["written"] += 1
            else:
                counts["failed"] += 1
                if on_failure is not None:
                    on_failure(key, error)
    finally:
        tr.con.close()
    return counts


def command(args, registry: Path) -> int:
    """Dispatch `lime focus summarize [--project P]`: write a short "what to do"
    for every changed, summarizable item. Not owner-only; never touches focus.md."""
    name = getattr(args, "project", None)
    names = [project_name for project_name, _ in status.registered(registry)]
    if name is not None and name not in names:
        print(
            f'lime focus summarize: project "{name}" is not registered in {registry}',
            file=sys.stderr,
        )
        return 2
    data = status.report(registry, project=name, gh=True)
    now = home._now()
    written = failed = unchanged = 0
    project_failures = 0
    for project_name, root in status.registered(registry):
        if name is not None and project_name != name:
            continue
        items = [
            item
            for item in data["items"]
            if item["project"] == project_name and status.summarizable(item)
        ]
        if not items:
            continue

        def on_failure(key: str, error: str, project_name: str = project_name) -> None:
            first = (error or "").splitlines()[0] if error else ""
            print(f"failed {project_name} {key}: {first}", file=sys.stderr)

        try:
            counts = summarize_project(root, items, now, on_failure=on_failure)
        except Exception as exc:  # noqa: BLE001 — one bad project never aborts the batch
            print(f"lime focus summarize: {project_name}: {exc}", file=sys.stderr)
            project_failures += 1
            continue
        written += counts["written"]
        failed += counts["failed"]
        unchanged += counts["unchanged"]
    print(f"summaries: {written} written, {failed} failed, {unchanged} unchanged")
    return 1 if failed or project_failures else 0
