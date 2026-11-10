"""Adapter dispatch (spec §4), envelope extraction (pi via `submit` tool, ticket 74; claude_code still parses text until its MCP follow-up), and one agent call
end to end: render prompts -> adapter run -> typed parse -> gates -> permissions -> envelope.

Parse failures, gate violations, and transient pi stream errors re-prompt the
SAME session with a correction — context intact, bounded retries. Once
in-place retries are exhausted, one stream-fresh session (stream_fresh) is
spent before the phase fails; a partial envelope continuation (continuation)
accounts for the other independent allowance. At most three sessions per Phase.
Agent proposes, code disposes.

A text-only pi reply containing the dropped-tag `<function=` or `<tool_call>`
marker (a tool-call leak) without an accepted submit, rejection, or structured
toolCall part also triggers the stream-fresh recovery path directly, skipping
same-session JSON corrections. The fresh session reuses the stream_fresh
allowance and does not increment continuation. At most one leak recovery per
Phase; a second leak after the allowance is exhausted fails with a clear
`tool_call_leak` reason.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import signal
import time
import uuid
from pathlib import Path
from typing import Callable, Literal, TypeVar, get_args, get_origin

from pydantic import ValidationError

from . import agent_cc, agent_pi, budget, config, permissions, prompts, rules
from .mask import env_secrets, mask
from .data_types import (
    BASH_DEADLINE_S,
    AgentCall,
    AgentConfig,
    AgentRequest,
    AgentResult,
    EnvelopeBase,
    Phase,
    Runtime,
    UsageBreakdown,
)

ADAPTERS: dict[Runtime, Callable] = {"pi": agent_pi.run, "claude_code": agent_cc.run}

E = TypeVar("E", bound=EnvelopeBase)

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)

JSON_FIX_ATTEMPTS = 2  # continue-with-correction attempts for malformed JSON

STREAM_RETRIES = 2          # same-session re-sends after a transient provider/stream error (ticket 76)
STREAM_RETRY_DELAY_S = 15.0  # backoff before the first re-send; doubles each time (15 s, 30 s)
# Stream-retry backoff's own sleep, bound once at import: tests observe the backoff by patching
# this seam, never the shared `time.sleep` that subprocess's timed-wait poll loop also calls.
_sleep = time.sleep
NUDGE_TIMEOUT_S = 300      # the one nudge send after the Session's ceiling: write the handoff, submit partial (ticket 82)
REPEAT_LIMIT = 3           # identical tool calls in a row that stop the Session like the time ceiling (ticket 173)

GUARD_DIR = Path(__file__).resolve().parent.parent / "guard"

# Denylist: everything pi reports as an errored final message is retried unless
# the reason says the request itself is wrong (the cheap mistake is two wasted
# re-sends; the expensive one is orphaning 44 turns — 278e4fd2). HTTP codes are
# word-bound so `140000 tokens` never matches `400`.
NON_TRANSIENT_ERROR = re.compile(
    r"\b40[0134]\b|\b413\b|\b422\b"
    r"|unauthori[sz]ed|forbidden|authentication|invalid.?api.?key|api key|no auth credentials"
    r"|insufficient_quota|quota|billing|out of budget|available balance|usage limit"
    r"|context.?(window|length|size)|maximum context|prompt is too long|request_too_large"
    r"|too many tokens|token limit|reduce the length"
    r"|bad.?request|invalid.?request|invalid_request|invalid.?param|no endpoints found"
    r"|not.?found|does not exist|unsupported|unknown model",
    re.I,
)


def transient_error(result: AgentResult) -> bool:
    """status='error' carrying the runtime's own reason, with nothing in it that says the request is wrong.
    `timeout` is the Spine's own ceiling (ticket 82) — nudged in `send()`, never retried.
    claude_code never fills `error`, so only pi phases retry."""
    return result.status == "error" and bool(result.error) and not NON_TRANSIENT_ERROR.search(result.error)


def leaked_tool_call(result: AgentResult, runtime: Runtime) -> bool:
    """ADR 0016: an otherwise successful pi reply with no accepted submit, no rejection
    and no structured tool call whose text carries a dropped-tag tool-call marker.
    pi-only — a claude_code reply is text by design, never a leak. Never a transient error."""
    return (
        runtime == "pi"
        and result.envelope is None
        and not result.submit_error
        and not result.has_tool_calls
        and ("<function=" in result.text or "<tool_call>" in result.text)
    )


class GateFailure(RuntimeError):
    """Every gate-retry attempt was spent and violations still remain."""


class BudgetExhausted(RuntimeError):
    """Both partials in a Phase were measured exhaustion (time or token) after its one
    continuation (spec §9) — the Run ends `budget_exhausted`."""


# Why a partial Session is being continued. Internal only — never a schema field:
# `timeout` (this Session was nudged at its ceiling), `budget` (this Session's own
# measurements reached token warn/stop), or `blocked` (no ceiling was reached).
PartialReason = Literal["timeout", "budget", "blocked"]


def _context_window(agent: AgentConfig) -> int:
    """The window used to size the budget thresholds before any turn lands.

    pi's catalog lookup (`agent_pi.context_window`) is exact, so its value sizes
    warn/stop/backstop directly. Claude Code's real number only appears in the
    stream's final `result` event — far too late for a live per-turn check — and
    adapter-reported telemetry is deliberately NOT trusted as a policy input, so
    no alias guess stands in: the window is unknown (0) and `budget.thresholds`
    falls back to its uniform defaults (warn 150k, stop 250k, backstop 270k).
    The reported number still lands in AgentResult.context_window and the trace,
    unaffected — it just never moves the stop.
    """
    if agent.runtime == "pi":
        return agent_pi.context_window(agent.model)
    return 0


def _guard_entry(runtime: Runtime) -> str:
    """pi: an extension path for `-e`. claude_code: the hook command itself (spec §8)."""
    if runtime == "pi":
        return str((GUARD_DIR / "guard.ts").resolve())
    return "lime guard"


def run(
    req: AgentRequest,
    on_event: Callable[[dict], None] | None = None,
    on_spawn: Callable[[int], None] | None = None,
    on_exit: Callable[[int], None] | None = None,
) -> AgentResult:
    return ADAPTERS[req.runtime](req, on_event, on_spawn, on_exit)


def _repair_escapes(candidate: str) -> str:
    """Walk the string: keep valid JSON escapes, double illegal ones.

    A backslash followed by one of ``"``, ``\\``, ``/``, ``b``, ``f``, ``n``,
    ``r``, ``t``, ``u`` is a valid JSON escape — kept as-is. A backslash
    followed by any other character (e.g. ``\\w``) is an invalid escape —
    the backslash is doubled so that JSON interprets it as a literal
    backslash followed by that character.

    A legal ``\\`` pair is consumed as a unit so the second backslash is
    *not* mistaken for the start of a second escape. This preserves valid
    JSON like ``\\w`` (literal backslash + ``w``) while fixing the
    ``\\w``-style defect.

    Reproduces 9268bb42: a fenced BuildOutput whose ``summary`` quotes a
    regex containing ``\\w`` — an invalid JSON escape that would otherwise
    make the entire candidate undecodable.
    """
    legal: set[str] = set('"\\/bfnrtu')
    result: list[str] = []
    i = 0
    while i < len(candidate):
        if candidate[i] == "\\" and i + 1 < len(candidate):
            nxt = candidate[i + 1]
            if nxt == "\\":
                # valid \\ pair — keep both, consume both
                result.append("\\\\")
                i += 2
                continue
            if nxt in legal:
                # valid escape like \n, \t, \", \/
                result.append("\\" + nxt)
                i += 2
                continue
            # illegal escape like \w — double the backslash
            result.append("\\\\")
            i += 1
            continue
        result.append(candidate[i])
        i += 1
    return "".join(result)


def _validation_detail(error: ValidationError) -> str:
    """Format pydantic errors for correction messages (same string extract_envelope uses)."""
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in error.errors()
    )


def extract_envelope(text: str, cls: type[E]) -> E:
    """Last fenced block first, then the outermost bare `{...}`. Three outcomes:

    1. A candidate decodes as JSON and passes pydantic validation → returned.
    2. No candidate decodes → ``{cls.__name__} JSON does not decode — <msg>``.
    3. A candidate decodes but fails validation → pydantic field errors.
    4. None of the above → ``no {cls.__name__} envelope in agent text``.

    An invalid JSON escape (``\\w`` etc.) is repaired via ``_repair_escapes``
    before giving up on a candidate.
    """
    if not text.strip():
        raise ValueError(f"empty reply — no text at all, so no {cls.__name__} envelope")
    bare = text[text.find("{") : text.rfind("}") + 1]
    invalid: ValidationError | None = None
    undecodable: tuple[json.JSONDecodeError, str] | None = None
    fenced_candidates = _FENCE.findall(text)
    for candidate in (*reversed(fenced_candidates), bare):
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as err:
            # Only remember the decode error for candidates that look like a
            # real JSON attempt — a fenced block, or a bare span whose first
            # non-whitespace char after ``{`` is ``"`` (``{"..`` or
            # ``{\n  "status"..``).  Random text like ``{foo}`` that
            # happens to have braces is not a JSON attempt.
            is_real_attempt = bool(fenced_candidates) or (
                candidate.startswith('{')
                and candidate[1:].lstrip().startswith('"')
            )
            try:
                payload = json.loads(_repair_escapes(candidate))
            except json.JSONDecodeError:
                if is_real_attempt:
                    undecodable = undecodable or (err, candidate)
                continue
        try:
            return cls.model_validate(payload)
        except ValidationError as error:
            invalid = error
    if invalid is not None:
        raise ValueError(f"{cls.__name__} envelope invalid — {_validation_detail(invalid)}")
    if undecodable is not None:
        err, candidate = undecodable
        raise ValueError(
            f"{cls.__name__} JSON does not decode — {err.msg} at line {err.lineno}"
            f" column {err.colno}, near: {candidate[max(0, err.pos - 40): err.pos + 20]!r}"
        )
    raise ValueError(f"no {cls.__name__} envelope in agent text")


class SubmitRejected(ValueError):
    """pi refused a `submit` call at the schema (ticket 78). The model DID call the tool, so the
    correction relays pi's own validation text verbatim — never "no submit call"."""


def _envelope_from_submit(payload: dict | None, cls: type[E], rejected: str = "") -> E:
    """pi roles (ticket 74): the envelope is the args of the last accepted `submit` call; text is never parsed.
    `rejected` is pi's text for a call it refused (ticket 78) — consulted only when no call was accepted."""
    if payload is None:
        if rejected:
            raise SubmitRejected(rejected)
        raise ValueError(f"no `submit` call — a {cls.__name__} is delivered by calling the `submit` tool, not as text")
    try:
        return cls.model_validate(payload)
    except ValidationError as error:
        raise ValueError(f"{cls.__name__} envelope invalid — {_validation_detail(error)}") from error


def _envelope_validator(output_type: type[EnvelopeBase]) -> Callable[[dict], bool]:
    """pi (ticket 250): a non-raising form of `_envelope_from_submit` — the `tool_result`'s `ok`
    must report lime's own `model_validate` verdict for a `submit`'s args, so an envelope lime
    would reject can never carry a successful result. The adapter still delivers the args; the
    correction trail owns the rejection."""
    def valid(args: dict) -> bool:
        try:
            _envelope_from_submit(args, output_type)
            return True
        except ValueError:
            return False
    return valid


def submit_schema(output_type: type[EnvelopeBase]) -> dict:
    """The role's envelope schema as pi's `submit` tool receives it (ticket 79): pydantic's JSON Schema with
    every `$ref` replaced by its `$defs` entry and `$defs` dropped. pi-ai resolves local refs itself, but
    OpenRouter's translation to Gemini's function-declaration format does not — `findings` arrived typed as
    strings (gemini-3.8-flash 0/4 valid with `$ref`, 4/4 with the same schema inlined)."""
    schema = output_type.model_json_schema()
    defs = schema.pop("$defs", {})
    return _inline(schema, defs)


def _inline(node, defs: dict):
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            if not ref.startswith("#/$defs/"):
                raise ValueError(f"submit schema: unsupported $ref {ref!r}")
            target = defs[ref.removeprefix("#/$defs/")]
            siblings = {k: v for k, v in node.items() if k != "$ref"}  # pydantic puts description/default beside $ref
            return _inline({**target, **siblings}, defs)
        return {k: _inline(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def _correction(runtime: Runtime, error: ValueError, fields: str) -> str:
    if runtime == "pi" and isinstance(error, SubmitRejected):
        return (f"Your `submit` call was rejected by the tool's schema check. pi's report:\n{error}\n\n"
                f"Fix the arguments and call `submit` again with these fields: {fields}."
                " Do not write the JSON as text — only the `submit` call is read.")
    if runtime == "pi":
        return (f"Your report was not accepted ({error}). Call the `submit` tool now with these fields: {fields}."
                " Do not write the JSON as text — only the `submit` call is read.")
    return (f"Your response was not valid JSON for the required structure ({error}). Respond again with ONLY a"
            f" JSON object with these fields: {fields}. No prose, no code fences.")


def _stream_retry_prompt(reason: str) -> str:
    """pi wording only: cc never reaches it — see `transient_error`. The original prompt is not
    re-sent: pi persisted it in the session before the model call."""
    return (f"Your previous model call was cut off by a transient provider error ({reason}); this session"
            " and everything you did before it are intact. Continue from where you stopped — do not start"
            " over or redo finished work. If the work was already complete, call `submit` now with your Report.")


def _timeout_nudge_prompt(runtime: Runtime, timeout_s: int, nudge_s: int, handoff_path: str,
                          repeated: str = "") -> str:
    """The one send after the Session's ceiling (ticket 82): the same ask the Guard makes at budget stop, with a clock on it.

    `repeated` names the tool whose identical calls tripped the repeat stop (ticket 173);
    only the opening sentence changes — the ask itself is identical."""
    if runtime == "pi":
        finish = 'call `submit` with status: success'
        partial_verb = "submit"
    else:
        finish = 'respond with ONLY your Report JSON with status: success'
        partial_verb = "report"
    if repeated:
        opener = (f"Stopped: you called `{repeated}` {REPEAT_LIMIT} times in a row with identical arguments,"
                  " so your last model call was stopped;")
    else:
        opener = (f"Time is up: this Session's {timeout_s}s ceiling has passed and your last model call"
                  " was stopped;")
    return (opener +
            " the Guard has closed substantive tools — only handoff-directory writes/edits and"
            " final reporting remain. This session and everything you did before it are intact"
            " — do not start new work."
            f" Write your handoff note to {handoff_path} now (<=80 lines: Done/Left/Decisions/Don't redo/Open,"
            f" no code or diffs), then {finish} only when every applicable completion requirement"
            " (including required verification and reporting/artifacts) is already met; otherwise"
            f' write a useful handoff and {partial_verb} status: partial, identifying done and remaining/unverified'
            " work. Never claim checks ran or passed when they did not. You have"
            f" {nudge_s}s.")


# --- one agent call, start to finish ---


def execute(run_, phase: Phase, call: AgentCall) -> EnvelopeBase:
    """One agent call: render prompts -> adapter run -> typed parse -> gates -> envelope."""
    agent = run_.agent(phase.params.owner)
    root = run_.repo_root
    session_dir = run_.tracer.session_dir(run_.run_id, phase.seq, agent.name)
    (session_dir / "prompts").mkdir(parents=True, exist_ok=True)

    # The whole Report contract, computed once from the resolved runtime and the call's
    # own output type and reused verbatim for every continuation in this Phase.
    report_block = _report_block(agent.runtime, call.output_type)
    variables = {
        "prompt": call.prompt,
        "previous_envelope": (
            call.previous.model_dump_json(indent=2) if call.previous else "(none)"
        ),
        "context_handoff_dir": str(run_.context_handoff_dir),
        "never": run_.never,
        "rules": rules.rules_block(root),
        "report": report_block,
    }
    system_text = prompts.render(prompts.resolve_path(agent, "system", root), variables)
    user_text = prompts.render(prompts.resolve_path(agent, "user", root), variables)
    prompts.save(session_dir / "prompts", "system.md", system_text)
    prompts.save(session_dir / "prompts", "user.md", user_text)

    # Budget bookkeeping for this Phase — measurement, thresholds, and the one
    # continuation spec §9 allows before the Run ends `budget_exhausted`.
    window = _context_window(agent)
    warn, stop, kill_at = budget.thresholds(agent.budget, run_.cfg.get("budget"), window)

    tracer_session_id = run_.tracer.start_session(
        run_.run_id,
        phase.phase_id,
        seq=phase.seq,
        role=agent.name,
        runtime=agent.runtime,
        model=agent.model,
        color=agent.color or None,
        budget_warn=warn,
        budget_stop=stop,
    )
    session_id, resume, resumed_session_dir = _runtime_session(run_, agent)
    # A resumed phase is a NEW Run.phase() (runner.py: self._seq += 1) → a new
    # <NN>-<role>/ dir. pi's --session-id only names the session; --session-dir
    # names the folder pi looks in for that session's file. Passing the old
    # session_id with THIS phase's (fresh, empty) dir would make pi start a new
    # file under the old id instead of rejoining — so on resume the request's
    # session_dir is the failed phase's own runtime_sessions/, persisted in
    # agent_map.json by _remember_session() below.
    runtime_session_dir = (
        resumed_session_dir if resume and resumed_session_dir
        else str((session_dir / "runtime_sessions").resolve())
    )

    def _remember_session() -> None:
        """agent_map.json before any send (ticket 76): a phase that dies mid-stream still leaves its runtime
        session id AND the runtime_sessions/ dir that session id's file lives in — so a later
        --resume rejoins the exact folder pi wrote to, not a new phase's empty one.

        A rejoined cc Session also keeps the cumulative cost it already reported
        (`cost_reported`, ticket 175), so the next send books only the honest delta.
        A fresh UUID, a changed runtime/model, or a closed-entry replacement starts the
        baseline at zero — legacy entries without the key default to zero."""
        entry = run_.agent_map.get(agent.name)
        rejoined = (
            agent.runtime == "claude_code"
            and resume
            and entry is not None
            and entry.get("session_id") == session_id
            and entry.get("model") == agent.model
            and entry.get("runtime") == agent.runtime
            and not entry.get("closed", False)
        )
        remembered = {
            "session_id": session_id, "model": agent.model, "runtime": agent.runtime,
            "session_dir": runtime_session_dir,
        }
        if agent.runtime == "claude_code":
            remembered["cost_reported"] = entry.get("cost_reported", 0.0) if rejoined else 0.0
        run_.save_agent_map(agent.name, remembered)
    _remember_session()

    resolved = config.resolve_tools(agent.runtime, agent.tools)
    tools = resolved.tools or None
    # pi: the roster's harness_engineering + extension words + submit.ts + the Guard.
    # claude_code: only `lime guard` — harness_engineering would become hook shell commands there,
    # and the template promises it is inert on claude_code (audit C1).
    guards: list[str] = []
    if agent.runtime == "pi":
        # pi's cwd is root; an entry only the installed package has goes by absolute path (validation already refused the rest)
        guards += [config.resolve_extension(e, root) or e for e in agent.harness_engineering]
        guards += resolved.extensions
        guards.append(config.SUBMIT_EXTENSION)  # every pi role finishes by calling `submit` (ticket 74)
        if tools:  # pi's --tools allowlist hides extension tools; no allowlist = every tool, submit included
            tools = [*tools, config.SUBMIT_TOOL]
    guards.append(_guard_entry(agent.runtime))

    run_.tracer.event(
        run_.run_id,
        "agent_start",
        phase_id=phase.phase_id,
        name=agent.name,
        payload={
            "model": agent.model,
            "thinking": agent.thinking,
            "color": agent.color,
            "session_id": session_id,
            "runtime": agent.runtime,
            "purpose": agent.purpose,
            "tools": agent.tools,
            "harness_engineering": agent.harness_engineering,
        },
    )
    run_.console.agent_started(agent.name, agent.model, session_id)

    # Budget bookkeeping for this Phase — measurement, thresholds, and the one
    # continuation spec §9 allows before the Run ends `budget_exhausted`.
    budget_file = run_.run_dir / "budget.json"
    handoff_dir = run_.context_handoff_dir
    handoff_path = handoff_dir / f"handoff-{phase.params.name}-1.md"
    continuation = 0
    stream_fresh = 0  # independent fresh-session allowance for stream failure recovery
    session_index = 0  # monotonic ordinal across all sessions (stream_fresh + continuation)
    session_opening_prompt = user_text  # current session's opening prompt, updated only on session open
    current_pid = 0
    killed = False
    last_state: budget.BudgetState = "ok"
    # Session-local stop reason, paired with `last_state`. None on a fresh
    # Session (and after a context-budget stop); "time_limit" once the ceiling
    # has latched — so subsequent same-Session writes keep naming the cause.
    last_reason: str | None = None
    # Repeat stop (ticket 173): the last tool call's identity (tool name, canonical args)
    # and how many times in a row it has repeated. Session-local like the budget latch —
    # `_reset_budget` zeroes both on every fresh Session.
    repeat_call: tuple[str, str] | None = None
    repeat_count = 0
    # Time ceiling (ticket 82): ONE deadline per Session — every send in it (first prompt, JSON-fix
    # corrections, gate corrections, stream retries) draws from this clock, so retries never extend it.
    # _continue_session / _new_session re-arm it: a fresh Session gets a fresh clock the way it gets a fresh window,
    # and two independent allowances (stream_fresh + continuation) allow at most three sessions.
    phase_armed_at = run_.clock()  # immutable Phase origin
    armed_at = phase_armed_at      # current-Session origin, reset at session re-arm
    deadline = armed_at + agent.timeout_s
    nudged = False  # the one nudge send is spent; every later send in this Session is overtime
    # Session-local evidence that THIS Session actually reached the token warn/stop
    # threshold, from real occupancy measurements — never summary text, elapsed time,
    # total spend, or a time-induced stop. Sticky: a later turn below warn keeps it.
    # Only a fresh Session (_reset_budget) clears it.
    token_threshold_reached = False
    # The first partial's measured cause, kept Phase-local so the continuation (which
    # resets Session measurements) cannot erase it; a second partial is classified
    # from its own current Session.
    first_partial_reason: PartialReason | None = None
    # ticket 170: a pi Session ENDS the moment its submit is accepted. These track that
    # boundary locally so a later parse/gate correction opens a NEW session instead of
    # resuming the closed runtime id. `accepted_session_id` names the closed session;
    # `accepted_report_raw` is the first accepted submit's raw args for the reopen prompt.
    accepted_session_id: str | None = None
    accepted_report_raw: dict | None = None
    # A timeout send that still delivered an envelope: measured time causality for
    # partial classification, kept separate from `nudged` (no nudge actually happened).
    timed_out_with_envelope = False

    def remaining() -> int:
        return max(1, math.ceil(deadline - run_.clock()))  # the floor keeps the adapter's Timer legal

    def _on_spawn(pid: int) -> None:
        nonlocal current_pid
        current_pid = pid
        run_.tracer.start_process(
            run_.run_id, kind="agent", name=agent.name, pid=pid,
            command=f"{agent.runtime} {agent.name} {agent.model}",
        )

    def _track_budget(event: dict) -> None:
        """Fed every assistant event (spec §9): write budget.json, trace a state
        transition, and — past the backstop — kill the process outright."""
        nonlocal last_state, last_reason, killed, token_threshold_reached
        if event.get("type") != "turn":
            return
        tokens = event.get("context_tokens", 0)
        measured = budget.evaluate(tokens, warn, stop)
        if measured != "ok":  # a real measurement reached warn/stop, independent of the latch
            token_threshold_reached = True
        # Once stop is latched, subsequent turns must stay stop regardless of occupancy
        if last_state == "stop":
            state = "stop"
        else:
            state = measured
        budget.write(budget_file, state=state, context_tokens=tokens, warn=warn, stop=stop,
                     reason=last_reason)
        if state != last_state and state != "ok":
            run_.tracer.event(
                run_.run_id, f"budget_{state}", phase_id=phase.phase_id, name=agent.name,
                payload={"context_tokens": tokens, "warn": warn, "stop": stop},
            )
        last_state = state
        if tokens >= kill_at and current_pid and not killed:
            killed = True
            with contextlib.suppress(ProcessLookupError):
                os.kill(current_pid, signal.SIGTERM)

    def _reset_budget() -> None:
        """Centralised fresh-Session budget init: set latch to ok, publish an ok
        mailbox with zero initial occupancy, clear the guard_warned marker, and reset
        Session-only bookkeeping."""
        nonlocal last_state, last_reason, token_threshold_reached, repeat_call, repeat_count
        last_state = "ok"
        last_reason = None
        token_threshold_reached = False
        repeat_call = None
        repeat_count = 0
        budget.write(budget_file, state="ok", context_tokens=0, warn=warn, stop=stop, reason=None)
        warned_marker = budget_file.with_name("guard_warned")
        if warned_marker.exists():
            warned_marker.unlink()

    def _count_call(tool: str, args) -> None:
        """Count consecutive identical tool calls (ticket 173) and trace the stop once.

        Frozen once the limit is reached: later calls in the same Session — including the
        nudge — never re-fire the event, and only `_reset_budget` (a fresh Session) zeroes it."""
        nonlocal repeat_call, repeat_count
        if repeat_count >= REPEAT_LIMIT:
            return
        key = (tool, json.dumps(args, sort_keys=True, default=str))
        if key == repeat_call:
            repeat_count += 1
        else:
            repeat_call = key
            repeat_count = 1
        if repeat_count >= REPEAT_LIMIT:
            run_.tracer.event(
                run_.run_id, "repeat_stop", phase_id=phase.phase_id, name=agent.name,
                payload={"tool": tool, "count": REPEAT_LIMIT, "args": _clip_args(args),
                         "session_id": session_id},
            )

    # Parse retries and gate corrections re-enter the SAME runtime session, so
    # the last send is the one whose context occupancy is current — while
    # spend is the opposite: every send costs, so usage accumulates across all.
    latest: AgentResult | None = None
    spent = UsageBreakdown()

    def _retry_phase(attempt: int, reason: str, delay: float) -> None:
        """The retry as its own attempt on the same phase, using the transition the tracer
        already documents, plus a structured event and the console line."""
        run_.tracer.set_phase_status(phase.phase_id, "fail", error=f"{agent.name}: {reason}")
        run_.tracer.set_phase_status(phase.phase_id, "running", error="")  # fail -> running: attempt += 1
        run_.tracer.event(
            run_.run_id, "log", phase_id=phase.phase_id, name="stream_retry",
            payload={"agent": agent.name, "attempt": attempt, "max": STREAM_RETRIES, "delay_s": delay,
                     "error": reason, "session_id": session_id},
        )
        run_.console.retry(agent.name, attempt, STREAM_RETRIES, f"transient stream error: {reason}")

    def _timeout_kill(result: AgentResult) -> None:
        """Overtime ran out too: keep any handoff the agent managed, else leave a code-only note; fail the phase.

        Precondition (ticket 170): the result carries NO envelope. A delivered envelope
        is normalized to `ok` in `_send_once` before `send` can nudge, so a timeout that
        already holds the report never reaches here — no code-only note, no timeout_kill trace."""
        assert result.envelope is None, "_timeout_kill reached with a delivered envelope"
        repeat = repeat_count >= REPEAT_LIMIT
        if not handoff_path.exists():
            cause = "repeat stop kill" if repeat else "time ceiling kill"
            handoff_path.write_text(budget.code_only_note(root, continuation, cause=cause))
        now = run_.clock()
        run_.tracer.event(run_.run_id, "timeout_kill", phase_id=phase.phase_id, name=agent.name,
                          payload={"timeout_s": agent.timeout_s, "nudge_s": NUDGE_TIMEOUT_S,
                                   "elapsed_s": round(now - armed_at),
                                   "phase_elapsed_s": round(now - phase_armed_at),
                                   "session_id": session_id,
                                   "context_tokens": result.context_tokens,
                                   **({"reason": "repeat"} if repeat else {})})
        if repeat:
            raise RuntimeError(
                f"{agent.name}: no envelope after the same `{repeat_call[0]}` call repeated"
                f" {REPEAT_LIMIT} times in a row + the {NUDGE_TIMEOUT_S}s nudge window"
            )
        raise RuntimeError(f"{agent.name}: no envelope within the {agent.timeout_s}s time ceiling"
                           f" + the {NUDGE_TIMEOUT_S}s nudge window")

    def _nudge() -> AgentResult:
        """The ceiling hit: one send into the SAME session under the short timer (ticket 82)."""
        nonlocal nudged, last_state, last_reason
        nudged = True
        # Latch the stop and publish the mailbox BEFORE tracing/logging and,
        # above all, before the nudge send — so every tool call during the
        # finalization window is already denied except the Guard's exemptions.
        # Occupancy is the just-returned timeout result's measurement (`latest`
        # is assigned by `_send_once` before we get here), never a cumulative
        # total or a fabricated threshold count.
        last_state = "stop"
        cause = "repeat" if repeat_count >= REPEAT_LIMIT else "time_limit"
        last_reason = cause
        budget.write(budget_file, state="stop",
                     context_tokens=latest.context_tokens if latest else 0,
                     warn=warn, stop=stop, reason=cause)
        now = run_.clock()
        run_.tracer.event(run_.run_id, "timeout_nudge", phase_id=phase.phase_id, name=agent.name,
                          payload={"timeout_s": agent.timeout_s, "nudge_s": NUDGE_TIMEOUT_S,
                                   "elapsed_s": round(now - armed_at),
                                   "phase_elapsed_s": round(now - phase_armed_at),
                                   "session_id": session_id, "reason": cause})
        run_.console.timeout_nudge(agent.name, agent.timeout_s, NUDGE_TIMEOUT_S)
        repeated = repeat_call[0] if cause == "repeat" else ""
        return _send_once(_timeout_nudge_prompt(agent.runtime, agent.timeout_s, NUDGE_TIMEOUT_S,
                                                str(handoff_path), repeated=repeated))

    def _note_acceptance(request_session_id: str, envelope=None) -> None:
        """Record the accepted pi submit for a send and persist closure immediately.

        Acceptance is the stream's `submit_accepted` notification or, for stub/result-only
        adapters, a non-None result envelope. The close is bound to the request's session
        identity: a stale notification for a session that is no longer current neither
        moves the boundary nor closes a fresh replacement (ticket 170). Persistence
        failures and KeyboardInterrupt propagate (the adapter's own cleanup reaps the child)."""
        nonlocal accepted_session_id, accepted_report_raw
        entry = run_.agent_map.get(agent.name)
        if entry is None or entry.get("session_id") != request_session_id:
            return  # not the current session — never close a replacement or move the boundary
        accepted_session_id = request_session_id
        if isinstance(envelope, dict):
            accepted_report_raw = envelope
        run_.close_agent_session(agent.name)

    def _reopen_after_acceptance(correction: str) -> str:
        """A pi Session is closed at submit acceptance, so a later parse/gate correction
        must open a NEW Session (ticket 170). The new opening prompt keeps the original
        task, the accepted report and the correction; neither `stream_fresh` nor
        `continuation` is spent — the acceptance boundary is not a recovery allowance."""
        nonlocal accepted_session_id, accepted_report_raw, session_opening_prompt
        old_session_id = session_id
        opening = session_opening_prompt
        report = accepted_report_raw
        accepted_session_id = None
        accepted_report_raw = None
        _new_session()
        run_.tracer.event(
            run_.run_id, "log", phase_id=phase.phase_id, name="accepted_session_reopen",
            payload={"agent": agent.name, "old_session_id": old_session_id,
                     "new_session_id": session_id, "continuation": continuation,
                     "session_index": session_index, "reason": "correction after accepted submit"},
        )
        parts = [opening]
        if report is not None:
            parts += ["---", "## Accepted report (the previous Session closed at this submit)",
                      json.dumps(report, indent=2)]
        if correction and correction != opening:
            parts += ["---", "## Correction", correction]
        prompt_text = "\n\n".join(parts)
        session_opening_prompt = prompt_text
        prompts.save(session_dir / "prompts", "system.md", system_text)
        prompts.save(session_dir / "prompts", "user.md", prompt_text)
        return prompt_text

    def _send_once(prompt_text: str) -> AgentResult:
        nonlocal latest, resume, token_threshold_reached, timed_out_with_envelope
        request_session_id = session_id  # bind callbacks to THIS request's identity (ticket 170)
        schema_path = ""
        if agent.runtime == "pi":
            schema_path = str((session_dir / "submit_schema.json").resolve())
            Path(schema_path).write_text(json.dumps(submit_schema(call.output_type), indent=2))
        req = AgentRequest(
            runtime=agent.runtime,
            model=agent.model,
            thinking=agent.thinking,
            system_prompt=system_text,
            prompt=prompt_text,
            session_id=session_id,
            resume=resume,
            session_dir=runtime_session_dir,
            raw_output_path=str((session_dir / "raw_output.jsonl").resolve()),
            cwd=str(root),
            tools=tools,
            writes=agent.writes,
            guards=guards,
            attachments=call.attachments,
            max_turns=call.max_turns,
            timeout_s=NUDGE_TIMEOUT_S if nudged else remaining(),
            budget_file=str(budget_file),
            handoff_dir=str(handoff_dir),
            handoff_path=str(handoff_path),
            tool_choice=agent.tool_choice,
            submit_schema=schema_path,
            # pi (ticket 250): the `submit` tool_result's `ok` is lime's model_validate verdict
            envelope_validator=_envelope_validator(call.output_type) if agent.runtime == "pi" else None,
            # Claude Code's Bash stops each call itself; only the pi builder needs the cap (ticket 198)
            bash_deadline_s=BASH_DEADLINE_S if agent.name == "builder" and agent.runtime == "pi" else None,
            should_stop=lambda: repeat_count >= REPEAT_LIMIT and not nudged,
        )
        forward = _event_forwarder(run_, phase, agent.name, tracer_session_id,
                                          ledger=ledger, on_tool_call=_count_call)

        def on_event(event: dict) -> None:
            forward(event)
            _track_budget(event)
            if agent.runtime == "pi" and event.get("type") == "submit_accepted":
                # ticket 170: the stream owns acceptance recognition; close the matching
                # map entry NOW — before parsing, gates, permissions or timeout can throw.
                _note_acceptance(request_session_id)

        try:
            result = run(req, on_event=on_event, on_spawn=_on_spawn,
                          on_exit=lambda pid: run_.tracer.end_process(run_.run_id, pid))
        except UnicodeDecodeError as _exc:
            result = AgentResult(
                status="error",
                error=f"invalid UTF-8 in pi provider stream: {_exc}",
                session_id=session_id,
                usage=UsageBreakdown(),
            )
        if agent.runtime == "pi" and result.envelope is not None:
            # result-only delivery (stub adapters / a stream that did not surface the
            # notification): close idempotently, still before parse/gates/permissions.
            _note_acceptance(request_session_id, result.envelope)
        if agent.runtime == "pi" and result.status == "timeout" and result.envelope is not None:
            # an accepted envelope is delivery even when the timer fired: normalize to `ok`
            # before `send` can nudge or `_timeout_kill` can discard it (ticket 170).
            result = result.model_copy(update={"status": "ok", "error": ""})
            timed_out_with_envelope = True
        if agent.runtime == "claude_code":
            # cc reports the run-cumulative cost per session: book only this send's
            # honest increment and persist the cumulative report as the new baseline,
            # bound to the request's identity so a replaced map entry is never charged
            # or overwritten (ticket 175). Delta is max-zero, never a high-water mark.
            entry = run_.agent_map.get(agent.name)
            if entry is None or entry.get("session_id") != request_session_id:
                raise RuntimeError(
                    f"{agent.name}: claude_code send for session {request_session_id!r}"
                    " returned after its agent_map entry was replaced — refusing to book"
                    " stale spend or overwrite the replacement"
                )
            baseline = entry.get("cost_reported", 0.0)
            delta = max(0.0, result.cost - baseline)
            run_.add_usage(result.usage.total, delta)
            # Copy the usage so the shared/stub AgentResult keeps its raw adapter
            # telemetry; only the cost total merged into `spent` is the increment.
            billed = result.usage.model_copy(
                update={"cost": result.usage.cost.model_copy(update={"total": delta})}
            )
            spent.merge(billed)
            run_.save_agent_map(agent.name, {**entry, "cost_reported": result.cost})
        else:
            run_.add_usage(result.usage.total, result.cost)
            spent.merge(result.usage)
        run_.tracer.touch_session(
            tracer_session_id,
            context_tokens=result.context_tokens,
            context_window=result.context_window,
        )
        resume = True  # every send after the first continues the same runtime session
        latest = result
        # The final AgentResult carries a real occupancy measurement too; when no turn
        # event exposed a threshold crossing, this is the measurement to evaluate.
        if result.context_tokens and budget.evaluate(result.context_tokens, warn, stop) != "ok":
            token_threshold_reached = True
        if killed:
            handoff_path.write_text(budget.code_only_note(root, continuation))
            run_.tracer.event(
                run_.run_id, "budget_kill", phase_id=phase.phase_id, name=agent.name,
                payload={"context_tokens": result.context_tokens},
            )
            raise BudgetExhausted(f"{agent.name}: backstop kill at ~{result.context_tokens} tokens")
        return result

    def _send_in_fresh_session(original: str) -> AgentResult:
        """One fresh-session recovery (ADR 0009 stream outage, ADR 0016 tool-call leak):
        snapshot the opening prompt, the interrupted correction and the handoff note, open a
        new Session, save its prompts, send once. Never copies leaked assistant text. Callers
        own the stream_fresh allowance, events, console label and failure reasons."""
        opening = session_opening_prompt
        correction = original if original != opening else ""
        note_text = ""
        note_path = ""
        try:
            note_text = handoff_path.read_text()
            note_path = str(handoff_path)
        except (OSError, UnicodeDecodeError):
            pass
        _new_session()
        parts = [opening]
        if correction:
            parts += ["---", "## Interrupted correction", correction]
        if note_text and note_path:
            parts += ["---", f"## Handoff note (from {note_path})", note_text]
        recovery_prompt = "\n\n".join(parts) if len(parts) > 1 else parts[0]
        prompts.save(session_dir / "prompts", "system.md", system_text)
        prompts.save(session_dir / "prompts", "user.md", recovery_prompt)
        return _send_once(recovery_prompt)

    def send(prompt_text: str) -> AgentResult:
        nonlocal stream_fresh, session_opening_prompt
        original = prompt_text
        if agent.runtime == "pi" and accepted_session_id is not None and session_id == accepted_session_id:
            # the referenced Session was closed at submit acceptance; a correction needs a fresh one
            prompt_text = _reopen_after_acceptance(original)
        for stream_attempt in range(1, STREAM_RETRIES + 2):
            result = _send_once(prompt_text)
            if result.status == "ok":
                # Tool-call leak detection: otherwise successful pi reply with no accepted submit,
                # no rejection, no structured tool call, but text contains the marker
                if leaked_tool_call(result, agent.runtime):
                    # Leak detected — intercept before parse/correction
                    return _handle_tool_call_leak(result, original)
                return result
            if result.status == "timeout":  # the Spine's own ceiling — never a stream retry
                if nudged:
                    _timeout_kill(result)
                result = _nudge()
                if result.status == "ok":
                    # Tool-call leak detected after nudge
                    if leaked_tool_call(result, agent.runtime):
                        return _handle_tool_call_leak(result, original)
                    return result
                if result.status == "timeout":
                    _timeout_kill(result)
                break  # error/denied during the nudge: the tail below decides (fresh session or fail)
            if not transient_error(result) or stream_attempt > STREAM_RETRIES:
                break
            delay = STREAM_RETRY_DELAY_S * 2 ** (stream_attempt - 1)
            _retry_phase(stream_attempt, result.error, delay)
            _sleep(delay)
            prompt_text = _stream_retry_prompt(result.error)
        reason = result.error or result.stderr_tail
        detail = f": {reason}" if reason else ""
        # In-place retries exhausted and the stream_fresh allowance is still unspent:
        # launch a brand-new session with context-preserving recovery prompt.
        if transient_error(result) and stream_fresh == 0:
            old_session_id = session_id
            stream_fresh = 1
            # Emit the fresh event before initialization can fail
            run_.tracer.event(
                run_.run_id, "log", phase_id=phase.phase_id, name="stream_retry_fresh",
                payload={"agent": agent.name, "error": reason,
                         "attempts": STREAM_RETRIES + 1,
                         "old_session_id": old_session_id, "continuation": continuation,
                         "stream_fresh": stream_fresh},
            )
            run_.console.fresh_retry(agent.name, reason)
            fresh = _send_in_fresh_session(original)
            if fresh.status == "ok":
                # Tool-call leak detected on fresh session reply
                if leaked_tool_call(fresh, agent.runtime):
                    return _handle_tool_call_leak(fresh, original)
                return fresh
            worktree_path = str(run_.worktree) if run_.worktree else str(root)
            raise RuntimeError(
                f"{agent.name}: adapter returned status={fresh.status!r}"
                f" after {STREAM_RETRIES + 1} attempts + 1 fresh-session retry: {reason}"
                f" [worktree: {worktree_path}]"
            )
        if transient_error(result):
            worktree_path = str(run_.worktree) if run_.worktree else str(root)
            raise RuntimeError(
                f"{agent.name}: adapter returned status={result.status!r}"
                f" after {STREAM_RETRIES + 1} attempts: {reason}"
                f" [worktree: {worktree_path}]"
            )
        raise RuntimeError(f"{agent.name}: adapter returned status={result.status!r}{detail}")

    def _handle_tool_call_leak(leak_result: AgentResult, original: str) -> AgentResult:
        """Intercept a tool-call leak and attempt recovery via stream-fresh session.

        The leak is detected in send() before parse/correction. On the first leak,
        reuse the stream_fresh allowance: skip same-session JSON corrections, spend
        stream_fresh once, open through _new_session(). Returns the fresh result
        or fails with a clear tool_call_leak reason.
        """
        nonlocal stream_fresh, continuation, session_opening_prompt, session_id
        old_session_id = session_id
        run_.tracer.event(
            run_.run_id, "log", phase_id=phase.phase_id, name="tool_call_leak",
            payload={"agent": agent.name, "old_session_id": old_session_id,
                     "stream_fresh": stream_fresh, "continuation": continuation,
                     "recovery_possible": stream_fresh == 0},
        )
        if stream_fresh > 0:
            # Allowance already consumed — fail boundedly
            raise RuntimeError(
                f"{agent.name}: tool_call_leak — a text-only pi reply contained"
                f" `<function=` or `<tool_call>` without a structured tool call"
                f" or accepted submit; stream_fresh exhausted, no recovery"
                f" [session: {old_session_id}]"
            )
        stream_fresh = 1
        run_.console.fresh_retry(agent.name, "tool_call_leak")
        fresh = _send_in_fresh_session(original)
        if fresh.status == "ok":
            # Check if the fresh reply also leaks — exhaust allowance
            if leaked_tool_call(fresh, agent.runtime):
                run_.tracer.event(
                    run_.run_id, "log", phase_id=phase.phase_id, name="tool_call_leak",
                    payload={"agent": agent.name, "old_session_id": fresh.session_id,
                             "stream_fresh": 1, "continuation": continuation,
                             "recovery_possible": False},
                )
                raise RuntimeError(
                    f"{agent.name}: tool_call_leak — fresh session also leaked"
                    f" a text-only tool call without structured call or submit"
                    f" [session: {fresh.session_id}]"
                )
            return fresh
        worktree_path = str(run_.worktree) if run_.worktree else str(root)
        raise RuntimeError(
            f"{agent.name}: tool_call_leak recovery returned status={fresh.status!r}"
            f" — {fresh.error}"
            f" [worktree: {worktree_path}]"
        )

    def _new_session() -> None:
        """Initialize a replacement session: increment session_index, assign new UUID/dirs, start tracer row.
        Does not increment continuation — that is _continue_session's role."""
        nonlocal session_id, resume, tracer_session_id, session_index, session_dir, runtime_session_dir, deadline, nudged, last_state, armed_at, session_opening_prompt, timed_out_with_envelope
        session_index += 1
        armed_at = run_.clock()
        deadline = armed_at + agent.timeout_s
        nudged = False
        timed_out_with_envelope = False
        session_id, resume = str(uuid.uuid4()), False
        session_dir = run_.tracer.session_dir(run_.run_id, phase.seq, agent.name, session_index)
        runtime_session_dir = str((session_dir / "runtime_sessions").resolve())
        (session_dir / "prompts").mkdir(parents=True, exist_ok=True)
        (session_dir / "runtime_sessions").mkdir(parents=True, exist_ok=True)
        tracer_session_id = run_.tracer.start_session(
            run_.run_id, phase.phase_id, seq=phase.seq, role=agent.name,
            runtime=agent.runtime, model=agent.model,
            continuation=continuation, session_index=session_index,
            color=agent.color or None,
            budget_warn=warn, budget_stop=stop,
        )
        _reset_budget()
        _remember_session()

    def _continue_session(reason: PartialReason, error: str = "") -> None:
        """Fresh Session via the continuation allowance — the only incrementer of continuation."""
        nonlocal continuation, session_opening_prompt
        continuation += 1
        _new_session()
        run_.tracer.event(
            run_.run_id, "continuation", phase_id=phase.phase_id, name=agent.name,
            payload={"continuation": continuation, "reason": reason, "error": error},
        )

    def _continuation_prompt(note_text: str, previous_envelope: EnvelopeBase, reason: PartialReason) -> str:
        if reason == "timeout" and repeat_count >= REPEAT_LIMIT:
            situation = (
                f"This is a continuation: the previous Session for this Phase was stopped for"
                f" repeating the same `{repeat_call[0]}` call — this Session has a fresh"
                f" {agent.timeout_s}s; do not repeat that call, and finish what the note lists under Left."
            )
        elif reason == "timeout":
            situation = (
                f"This is a continuation: the previous Session for this Phase stopped at its"
                f" {agent.timeout_s}s time ceiling — this Session has a fresh {agent.timeout_s}s;"
                f" finish what the note lists under Left."
            )
        elif reason == "budget":
            situation = (
                "This is a continuation: the previous Session for this Phase reached its"
                " context warning threshold (or the context stop) — this Session starts with a"
                " fresh context window; finish what the note lists under Left."
            )
        else:
            situation = (
                "This is a continuation: the previous Session for this Phase ended blocked —"
                " no time or token ceiling was reached. This is a blocked continuation."
                " Do not redo checks the previous Session already completed. If the outside"
                " prerequisite named below is still unavailable, report `fail` naming that"
                " outside blocker instead of another partial; if it can be resolved, finish"
                " the work and report success."
            )
        parts = [
            call.prompt,
            "---",
            situation,
            "## Previous Envelope",
            previous_envelope.model_dump_json(indent=2),
        ]
        if reason == "blocked":
            parts += [
                "## Newest Envelope notes (authoritative — not the handoff)",
                previous_envelope.notes_for_next_agent or "(no notes supplied)",
                "## Handoff note (may be stale)",
            ]
        else:
            parts.append("## Handoff note")
        parts += [
            note_text or "(none written)",
            budget.git_facts(root, continuation),
            report_block,
        ]
        return "\n\n".join(parts)

    # What the tree looked like before this agent got its hands on it. Every
    # send in this phase — first prompt, JSON retries, gate corrections,
    # continuation — is measured against this one baseline.
    tree_before = permissions.snapshot(root)
    protected_files = permissions.load_trust(root)["protected_files"]
    # worktree = the agent's alone → every diff is its doing;
    # main tree = shared → only what its tool calls named is judged (ticket 54)
    ledger = permissions.Ledger(root) if run_.worktree is None else None

    def check_permissions() -> list[str]:
        """Compare the tree against `tree_before`, rolling back anything unpermitted.

        Runs on every exit from the block below — a phase that errors out, runs
        out of parse or gate retries, still gets its writes checked and any
        breach rolled back. `PermissionBreach` here always wins over whatever
        exception was already in flight (spec §8: it "aborts the Phase").
        """
        try:
            return permissions.enforce(root, agent, tree_before, protected_files,
                                          ledger=ledger)
        except permissions.PermissionBreach as breach:
            run_.tracer.event(
                run_.run_id,
                "error",
                phase_id=phase.phase_id,
                name="permission_breach",
                payload={
                    "agent": agent.name,
                    "error": str(breach),
                    "writes": agent.writes,
                    "protected_files": protected_files,
                },
            )
            raise

    def _classify_partial() -> PartialReason:
        """Why THIS Session's partial is being continued: a repeat stop rides the one nudge,
        so it too is `timeout` (a time nudge, or an accepted timeout-with-envelope, measured
        time causality without pretending a nudge) wins, then a measured token threshold,
        else blocked (no ceiling reached)."""
        if nudged or timed_out_with_envelope:
            return "timeout"
        if token_threshold_reached:
            return "budget"
        return "blocked"

    def _terminate_second_partial(envelope: EnvelopeBase, second_reason: PartialReason) -> None:
        """The one continuation is spent and a second partial arrived. Exhaust the Run
        only when BOTH causes were measured ceilings; otherwise fail honestly as blocked,
        naming the newest Envelope's notes and closing this role's session mapping."""
        worktree_path = str(run_.worktree) if run_.worktree else str(root)
        handoff_note_path = str(handoff_path)
        if {first_partial_reason, second_reason} <= {"timeout", "budget"}:
            # Measured exhaustion (time and/or token) on both partials → budget_exhausted.
            left_content = _extract_left_section(handoff_path)
            note_fallback = (
                f"Report notes: {envelope.notes_for_next_agent}"
                if envelope.notes_for_next_agent else "(no notes)"
            ) if left_content is None else ""
            detail = f" [worktree: {worktree_path}, handoff: {handoff_note_path}"
            if left_content is not None:
                detail += f", remaining: {left_content}"
            else:
                detail += f", {note_fallback}"
            detail += "]"
            raise BudgetExhausted(f"{agent.name}: a second partial Envelope in one Phase.{detail}")
        # At least one partial was blocked: never budget_exhausted, never the generic
        # `reported status='partial'`. The newest Envelope's notes are authoritative,
        # whatever a possibly stale handoff `Left` says; paths are supplementary.
        notes = envelope.notes_for_next_agent or envelope.summary or "no notes supplied"
        run_.close_agent_session(agent.name)
        raise RuntimeError(
            f"{agent.name}: blocked: {notes}"
            f" [worktree: {worktree_path}, handoff: {handoff_note_path}]"
        )

    def _handle_partials(envelope: EnvelopeBase, attempt: int) -> tuple[EnvelopeBase, int]:
        """The shared partial path: persist the partial, continue once for ANY cause
        (timeout, budget, blocked), and drain until a nonpartial Envelope arrives.
        A second partial terminates the Phase."""
        nonlocal first_partial_reason, session_opening_prompt
        while envelope.status == "partial":
            _persist_envelope(
                run_, phase, agent.name, call, envelope, attempt, valid=True,
                continuation=continuation, session_index=session_index,
            )
            reason = _classify_partial()
            if continuation:
                _terminate_second_partial(envelope, reason)
            # Save the first cause Phase-locally BEFORE the continuation resets this
            # Session's measurements, so a later reset cannot erase it.
            first_partial_reason = reason
            try:
                note_text = handoff_path.read_text()
            except (OSError, UnicodeDecodeError):
                note_text = ""
            prompt_text = _continuation_prompt(note_text, envelope, reason)
            _continue_session(reason)
            prompts.save(session_dir / "prompts", "system.md", system_text)
            prompts.save(session_dir / "prompts", "user.md", prompt_text)
            session_opening_prompt = prompt_text
            result = send(prompt_text)
            envelope, attempt = _parse_with_retries(
                run_, phase, call, result, send, continuation, lambda: session_index
            )
        return envelope, attempt

    def _close_stopped_session() -> None:
        """A failure or catchable interrupt on a stopped/nudged Session leaves it
        unusable: close THIS role's mapping so a later --resume starts fresh.

        Only when the in-memory Session latch says stop (a measured token stop, a time
        ceiling, or the one nudge) AND the mapping still names THIS execution's current
        session_id — never a replacement, never an ordinary/warn-only failure. Repeated
        calls are harmless; `close_agent_session` preserves id/model/runtime/dir."""
        if not (last_state == "stop" or nudged):
            return
        entry = run_.agent_map.get(agent.name)
        if entry is not None and entry.get("session_id") == session_id:
            run_.close_agent_session(agent.name)

    outcome: Literal["ok", "fail"] = "fail"  # the end event's status; `ok` only on measured success
    try:
        _reset_budget()
        result = send(user_text)
        envelope, attempt = _parse_with_retries(run_, phase, call, result, send, continuation, lambda: session_index)
        envelope, attempt = _handle_partials(envelope, attempt)

        # claim gates — for claude_code, or a pi reply with no accepted submit, violations
        # flow back into the SAME session; a pi accepted submit has closed its Session, so
        # its correction reopens a fresh one (ticket 170).
        for gate_attempt in range(1, max(1, phase.params.retries + 1) + 1):
            violations: list[str] = []
            for gate in call.gates:
                report = gate(envelope, run_)
                found = report.violations
                run_.tracer.record_gate(
                    run_.run_id,
                    phase.phase_id,
                    attempt=gate_attempt,
                    gate=gate.__name__,
                    passed=not found,
                    violations=found,
                    checks=[c.model_dump() for c in report.checks],
                )
                run_.tracer.event(
                    run_.run_id,
                    "gate_fail" if found else "gate_pass",
                    phase_id=phase.phase_id,
                    name=gate.__name__,
                    payload={
                        "attempt": gate_attempt,
                        "violations": found,
                        "checks": [c.model_dump() for c in report.checks],
                    },
                )
                run_.console.gate_result(gate.__name__, report)
                violations.extend(found)
            if not violations:
                break
            if gate_attempt > phase.params.retries:
                raise GateFailure(
                    f"{agent.name} failed gates after {gate_attempt} attempt(s):\n- "
                    + "\n- ".join(violations)
                )
            run_.console.retry(
                agent.name,
                gate_attempt,
                phase.params.retries,
                f"{len(violations)} gate violation(s)",
            )
            gate_suffix = " then call `submit` again with your corrected Report." if agent.runtime == "pi" else " then re-emit ONLY your Report JSON."
            correction = (
                "Your previous response failed validation:\n- "
                + "\n- ".join(violations)
                + "\n\nFix these problems" + gate_suffix
            )
            result = send(correction)
            envelope, attempt = _parse_with_retries(run_, phase, call, result, send, continuation, lambda: session_index)
            envelope, attempt = _handle_partials(envelope, attempt)

        # Permission is checked after every send is done, and before the envelope is
        # accepted: an agent does not get to report success on a phase in which it
        # wrote somewhere it was not allowed to. Kept inside the protected region so a
        # breach here still runs the stopped-session cleanup before it propagates.
        touched = check_permissions()
        if touched:
            run_.tracer.event(
                run_.run_id,
                "log",
                phase_id=phase.phase_id,
                name="paths_touched",
                payload={"agent": agent.name, "paths": touched},
            )

        _persist_envelope(run_, phase, agent.name, call, envelope, attempt, valid=True, continuation=continuation, session_index=session_index)
        run_.console.envelope_summary(envelope)
        run_.tracer.event(
            run_.run_id,
            "handoff",
            phase_id=phase.phase_id,
            name=agent.name,
            payload={"artifacts": envelope.artifacts, "summary": envelope.summary},
        )
        run_.close_agent_session(agent.name)
        run_.console.agent_finished(agent.name, spent.total, spent.cost.total)
    except BudgetExhausted:
        _close_stopped_session()
        check_permissions()
        # A non-fatal Phase's exhaustion is that Phase's failure, not the Run's.
        if phase.params.fatal:
            run_.close_run(ok=False, status="budget_exhausted")
        raise
    except permissions.PermissionBreach:
        # already rolled back and traced by the raising check; only ensure a stopped
        # session is not left open, then propagate the breach unchanged.
        _close_stopped_session()
        raise
    except BaseException:
        _close_stopped_session()  # a stopped failure/interrupt leaves this session unusable
        check_permissions()  # a breach found here replaces whatever is propagating
        raise
    else:
        # The final envelope-status rejection lives OUTSIDE the exception handlers
        # (so a delivered failure keeps its existing cleanup behavior), but inside
        # the finalizer's scope: a delivered `fail` never becomes an `ok` end event.
        if envelope.status != "success":
            raise RuntimeError(f"{agent.name} reported status={envelope.status!r}: {envelope.summary}")
        outcome = "ok"
    finally:
        # EXACTLY ONE end event per execute, whatever the outcome (ticket 175): the
        # finalizer runs on success, reported failure, error, denial, exhaustion,
        # breach and catchable interrupt alike. `outcome` stays `fail` unless the typed
        # success, all gates and permissions, and normal finalization completed. `latest`
        # is the last accounted result, or absent on an early adapter exception.
        run_.tracer.event(
            run_.run_id,
            "agent_end",
            phase_id=phase.phase_id,
            name=agent.name,
            tokens=spent.total,
            payload={
                "status": outcome,
                "cost": spent.cost.total,
                "usage": spent.model_dump(),
                "context_tokens": latest.context_tokens if latest is not None else 0,
                "context_window": latest.context_window if latest is not None else 0,
            },
        )
    return envelope


# --- internals ---


def _runtime_session(run_, agent: AgentConfig) -> tuple[str, bool, str | None]:
    """Rejoin the agent's existing runtime session only when the phase it belonged to
    is still unfinished — i.e. the entry is not closed. A closed, matching entry starts
    fresh (new UUID, resume=False, no session dir). Missing ``closed`` is treated as
    live for backward compatibility with existing ticket-76 maps.

    Returns (session_id, resume, session_dir)."""
    entry = run_.agent_map.get(agent.name)
    if entry and entry.get("model") == agent.model and entry.get("runtime") == agent.runtime:
        closed = entry.get("closed", False)
        if not closed:
            return entry["session_id"], True, entry.get("session_dir")
    return str(uuid.uuid4()), False, None  # cc requires a UUID; pi accepts any string


_ARG_CLIP = 400  # chars per string arg — a Write body must not land whole in events.jsonl
_RESULT_TAIL = 2048  # bytes of a tool's output kept in the trace — the last the model saw


def _result_tail(text: str) -> str:
    """Mask the WHOLE output first, then keep its last 2 KiB — a cut first could leave half a secret."""
    data = mask(text, env_secrets()).encode("utf-8")
    return data[-_RESULT_TAIL:].decode("utf-8", errors="ignore")


def _clip_args(args) -> dict:
    """Copy of a tool's args with every long string cut; dashboards poll events.jsonl at 500ms.
    Strings are masked before the cut, so a clip never leaves half a secret."""
    secrets = env_secrets()
    def clip(v):
        if isinstance(v, str):
            v = mask(v, secrets)
            if len(v) > _ARG_CLIP:
                return v[:_ARG_CLIP] + f"…[+{len(v) - _ARG_CLIP} chars]"
            return v
        if isinstance(v, dict):
            return {k: clip(x) for k, x in v.items()}
        if isinstance(v, list):
            return [clip(x) for x in v]
        return v
    return clip(args) if isinstance(args, dict) else {}


def _event_forwarder(
    run_, phase: Phase, agent_name: str, tracer_session_id: str,
    ledger: permissions.Ledger | None = None,
    on_tool_call: Callable[[str, object], None] | None = None,
) -> Callable[[dict], None]:
    """Feed: `turn` events for spend tracking (spec §11), `tool_call` and `tool_result` events
    for the dashboard. The pending-call map is per send: a result whose call this send never
    recorded (an orphan, a duplicate, or a stale id from an earlier send) is dropped."""
    turn_no = 0
    seen_tool_ids: set[str] = set()
    pending: dict[str, tuple[str, float]] = {}  # call id → (tool, run_.clock() at receipt)

    def tool_call(tool, call_id, args) -> None:
        # the raw args feed the repeat counter (ticket 173) before any clipping
        if on_tool_call is not None:
            on_tool_call(tool, args)
        if ledger is not None:
            ledger.record(tool, args)
        pending[call_id] = (tool, run_.clock())
        run_.tracer.event(
            run_.run_id, "tool_call", phase_id=phase.phase_id, name=tool,
            payload={"tool": tool, "tool_call_id": call_id,
                     "args": _clip_args(args), "agent": agent_name},
        )

    def forward(event: dict) -> None:
        nonlocal turn_no
        kind = event.get("type")
        if kind == "tool_execution_start":  # pi — start only, so the count matches exactly
            tool_call(str(event.get("toolName") or ""), str(event.get("toolCallId") or ""),
                      event.get("args"))
            return
        if kind == "assistant":  # claude_code — one message repeats per content block
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") != "tool_use":
                    continue
                block_id = str(block.get("id") or "")
                if block_id in seen_tool_ids:
                    continue
                seen_tool_ids.add(block_id)
                tool_call(str(block.get("name") or ""), block_id, block.get("input"))
            return
        if kind == "tool_result":  # the adapter's normalized result — never a call, never counted
            call_id = str(event.get("tool_call_id") or "")
            started = pending.pop(call_id, None)
            if started is None:
                return  # no call recorded in this send: orphan or duplicate
            tool, at = started
            output = str(event.get("output") or "")
            run_.tracer.event(
                run_.run_id, "tool_result", phase_id=phase.phase_id, name=tool,
                payload={"tool_call_id": call_id, "tool": tool, "ok": bool(event.get("ok")),
                         "duration_ms": max(0, round((run_.clock() - at) * 1000)),
                         "out_bytes": len(output.encode("utf-8")), "tail": _result_tail(output)},
            )
            return
        if kind != "turn":
            return
        turn_no += 1
        usage = event.get("usage") or {}
        run_.tracer.event(
            run_.run_id,
            "turn",
            phase_id=phase.phase_id,
            name=tracer_session_id,  # lime cost joins events.name = sessions.session_id
            tokens=event.get("context_tokens", 0),
            payload={
                "turn_no": turn_no,
                "model": event.get("model"),
                "agent": agent_name,
                "input": usage.get("input", usage.get("input_tokens", 0)),
                "output": usage.get("output", usage.get("output_tokens", 0)),
                "cache_read": usage.get("cacheRead", usage.get("cache_read_input_tokens", 0)),
                "cache_write": usage.get(
                    "cacheWrite", usage.get("cache_creation_input_tokens", 0)
                ),
                "cost": event.get("cost", 0.0),
                "context_tokens": event.get("context_tokens", 0),
            },
        )

    return forward


def _field_hints(cls: type[EnvelopeBase]) -> str:
    """`status (one of: 'success', 'fail', 'partial'), summary, ...` -- every Literal
    field lists its allowed values, read from the model so they cannot drift."""
    hints = []
    for name, field in cls.model_fields.items():
        if get_origin(field.annotation) is Literal:
            choices = ", ".join(repr(value) for value in get_args(field.annotation))
            hints.append(f"{name} (one of: {choices})")
        else:
            hints.append(name)
    return ", ".join(hints)


def _example_value(node: dict, name: str = "") -> object:
    """One illustrative value for an already-inlined schema node.

    Objects keep every property; arrays keep one representative item (so a nested
    model array shows a real object, never a bare string). A scalar's own default is
    honored, then `const`, then a valid enum value (preferring `success`), then a
    type-appropriate placeholder. Deterministic and pure — the schema is only read."""
    if "default" in node:
        return node["default"]
    if "const" in node:
        return node["const"]
    if "enum" in node:
        choices = list(node["enum"])
        return "success" if "success" in choices else choices[0]
    kind = node.get("type")
    if kind == "object" or "properties" in node:
        return {prop: _example_value(sub, prop) for prop, sub in node.get("properties", {}).items()}
    if kind == "array":
        return [_example_value(node.get("items", {}), name)]
    if kind == "boolean":
        return True
    if kind in ("integer", "number"):
        return 0
    if kind == "null":
        return None
    return f"<{name}>" if name else "<value>"


def _report_block(runtime: Runtime, output_type: type[EnvelopeBase]) -> str:
    """The runtime-aware Report contract shared by a first prompt and every continuation.

    One source of truth: the type's inlined `submit_schema` (shape and nested refs) and
    `_field_hints` (every top-level field and Literal choice). The illustrative envelope
    is generated from that schema, so it cannot drift from the model and carries exactly
    one JSON object. pi finishes by calling `submit`; claude_code's final message IS the
    JSON. No cache and no persisted state — the same inputs always yield the same block."""
    schema = submit_schema(output_type)
    example = json.dumps(_example_value(schema), indent=2)
    if runtime == "pi":
        delivery = (
            f"Finish by calling the `submit` tool with a `{output_type.__name__}` Report — do not"
            " write the JSON as text; only the `submit` call is read."
        )
    else:
        delivery = (
            f"Deliver your `{output_type.__name__}` Report as your final message: that JSON"
            " envelope is the entire final response — no surrounding prose and no code fences in"
            " that response."
        )
    return (
        "## Report\n\n"
        f"{delivery}\n\n"
        f"Fields: {_field_hints(output_type)}.\n\n"
        "Example only — a shape to fill in, not completed work:\n\n"
        f"```json\n{example}\n```"
    )


def _extract_left_section(handoff_path: Path) -> str | None:
    """Extract the remaining work from the handoff's `## Left` section up to the
    next section heading. Returns None when the file is missing, unreadable,
    contains invalid encoding, or has no Left section."""
    try:
        text = handoff_path.read_text()
    except (OSError, FileNotFoundError, UnicodeDecodeError, ValueError):
        return None
    # Find ## Left section
    left_marker = "## Left"
    idx = text.find(left_marker)
    if idx == -1:
        return None
    # Content starts after the marker line
    remainder = text[idx + len(left_marker):].lstrip("\n\r")
    # Find next section heading (## ...)
    next_heading = None
    for m in re.finditer(r"^##\s", remainder, re.MULTILINE):
        next_heading = m.start()
        break
    content = remainder[:next_heading].strip() if next_heading is not None else remainder.strip()
    if not content:
        return None
    # Keep it bounded
    if len(content) > 500:
        content = content[:500] + "..."
    return content


def _parse_with_retries(
    run_,
    phase: Phase,
    call: AgentCall,
    result: AgentResult,
    send: Callable[[str], AgentResult],
    continuation: int = 0,
    session_index=None,  # int, or a zero-arg callable re-read after each correction (ticket 170)
) -> tuple[EnvelopeBase, int]:
    """Parse the final response against the declared output type; on failure,
    continue the SAME session with a correction (bounded).

    `session_index` may be a callable so a correction that reopens a closed pi Session
    (ticket 170) records each envelope under the CURRENT session's ordinal, not the one
    that was current when the loop started."""
    runtime = run_.agent(phase.params.owner).runtime
    for attempt in range(1, JSON_FIX_ATTEMPTS + 2):
        try:
            envelope = _envelope_from_submit(result.envelope, call.output_type, result.submit_error) if runtime == "pi" else extract_envelope(result.text, call.output_type)
            return envelope, attempt
        except ValueError as error:
            _persist_envelope(
                run_, phase, phase.params.owner, call, None, attempt,
                valid=False, raw=json.dumps(result.envelope) if result.envelope is not None else (result.submit_error or result.text), continuation=continuation, session_index=session_index() if callable(session_index) else session_index,
            )
            if attempt > JSON_FIX_ATTEMPTS:
                raise RuntimeError(
                    f"{phase.params.owner} never produced valid "
                    f"{call.output_type.__name__} JSON: {error}"
                    f" — {attempt} replies, the last at {result.context_tokens} context tokens"
                ) from error
            run_.console.retry(
                phase.params.owner,
                attempt,
                JSON_FIX_ATTEMPTS,
                f"invalid {call.output_type.__name__} JSON: {error}",
            )
            fields = _field_hints(call.output_type)
            result = send(_correction(runtime, error, fields))
    raise AssertionError("unreachable")  # the loop above always returns or raises


def _persist_envelope(
    run_,
    phase: Phase,
    agent_name: str,
    call: AgentCall,
    envelope: EnvelopeBase | None,
    attempt: int,
    valid: bool,
    raw: str = "",
    continuation: int = 0,
    session_index: int | None = None,
) -> None:
    payload_json = (
        envelope.model_dump_json(indent=2) if envelope else json.dumps({"raw": raw[-2000:]})
    )
    run_.tracer.record_envelope(
        run_.run_id,
        phase.phase_id,
        agent=agent_name,
        output_type=call.output_type.__name__,
        payload_json=payload_json,
        valid=valid,
        attempt=attempt,
    )
    if envelope:
        agent = run_.agent(agent_name)
        record = {
            "agent_name": agent_name,
            "purpose": agent.purpose,
            "output_type": call.output_type.__name__,
            "attempt": attempt,
            **envelope.model_dump(),
        }
        ordinal = session_index if session_index is not None else continuation
        session_dir = run_.tracer.session_dir(run_.run_id, phase.seq, agent_name, ordinal)
        (session_dir / "envelope.json").write_text(json.dumps(record, indent=2))
