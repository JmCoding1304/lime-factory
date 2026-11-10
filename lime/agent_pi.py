"""pi adapter (spec §4, assets/02-runtime-adapters.md)."""

import contextlib
import io
import json
import logging
import os
import signal
import threading
from subprocess import DEVNULL, PIPE, Popen, TimeoutExpired
from typing import Callable

from .config import SUBMIT_TOOL, pi_model_entry
from .data_types import AgentRequest, AgentResult, UsageBreakdown

logger = logging.getLogger(__name__)

SIGINT_GRACE = 5.0  # SIGTERM first would discard the in-flight turn
SIGTERM_GRACE = 5.0


def context_window(model: str) -> int:
    """The catalog's contextWindow; 0 when pi does not know the model (the preflight has already warned/refused)."""
    return int((pi_model_entry(model) or {}).get("contextWindow") or 0)


def build_command(req: AgentRequest) -> list[str]:
    provider, _, model_id = req.model.partition("/")
    cmd = [
        "pi", "-p", "--mode", "json",
        "--provider", provider, "--model", model_id, "--thinking", req.thinking,
        "--session-id", req.session_id, "--session-dir", req.session_dir,
        "--system-prompt", req.system_prompt,
        "--no-context-files", "--no-skills", "--no-prompt-templates",
        "--no-extensions", "--no-approve",
    ]
    if req.tools:
        cmd += ["--tools", ",".join(req.tools)]
    elif req.tools is not None:  # [] = no tools (None = runtime default, unchanged)
        cmd += ["--no-tools"]
    for guard in req.guards:
        cmd += ["-e", guard]
    # native, vision-capable — pi reads an `@<path>` arg as a file reference (17-designer-role.md)
    attachments = [f"@{path}" for path in req.attachments]
    return cmd + ["--", *attachments, req.prompt]


def _result_text(event: dict) -> str:
    """The text pi put in a tool result: `result.content[*].text` joined — pi's own validation
    message for a rejected call (ticket 78). Falls back to the fixture-style `reason`/`result` string."""
    result = event.get("result")
    if isinstance(result, dict):
        parts = [p.get("text", "") for p in result.get("content") or []
                 if isinstance(p, dict) and p.get("type") == "text"]
        if any(parts):
            return "\n".join(parts)
    return str(event.get("reason") or result or "")


def _tool_output(event: dict) -> str:
    """The text of a tool result: `result.content[*].text` joined, or a `result` string.
    A dict result never `str()`s — its `terminate`/`details` fields are not output."""
    result = event.get("result")
    if isinstance(result, dict):
        return "\n".join(
            p.get("text", "") for p in result.get("content") or []
            if isinstance(p, dict) and p.get("type") == "text"
        )
    if isinstance(result, str):
        return result
    return ""


class _Stream:
    """Folds parsed stdout events into result state; emits one `turn` per model answer and
    one `tool_result` per `tool_execution_end`.

    An errored final message (``stopReason: "error"``) is recorded as
    ``self.error`` even though pi may exit 0 after auto-retry — the runtime's
    own reason is the reason the agent never answered (9948f34e).
    ``turns`` counts ``turn_start`` — one model call — for the ``max_turns`` cap.

    ``envelope`` captures the FIRST accepted ``submit`` call's args (ticket 170);
    the process is stopped at that transition and a later accepted submit never
    replaces the delivered report. ``submit_error`` holds pi's text for the last
    rejected ``submit`` (ticket 78); a rejected submit is never a denial.
    ``validate`` is lime's own ``model_validate`` verdict for a submit's raw args
    (ticket 250): the emitted ``tool_result.ok`` reports whether lime accepts the
    envelope, not merely that pi accepted the call.
    """

    def __init__(self, model: str, on_event: Callable[[dict], None] | None,
                 validate: Callable[[dict], bool] | None = None):
        self.model = model
        self.on_event = on_event
        self.validate = validate
        self.session_id = ""
        self.text = ""
        self.error = ""
        self.settled = False
        self.denials: list[dict] = []
        self.assistant_after_last_denial = True
        self.usage = UsageBreakdown()
        self.context_tokens = 0
        self.turns = 0
        self.envelope: dict | None = None
        self.submit_error = ""
        self.has_tool_calls = False  # last assistant message had structured toolCall content parts
        self._submit_args: dict[str, dict] = {}
        self._accept_notified = False  # the once-only `submit_accepted` event has been emitted

    def feed(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        accepted_before = self.envelope is not None
        emitted = self._route(event)
        if self.on_event:
            self.on_event(event)
            for out in emitted:
                self.on_event(out)
            # once-only normalized acceptance notification (ticket 170): the stream owns
            # acceptance recognition; the orchestrator only closes the matching session.
            if not accepted_before and self.envelope is not None and not self._accept_notified:
                self._accept_notified = True
                self.on_event({"type": "submit_accepted"})

    def _route(self, event: dict) -> list[dict]:
        kind = event.get("type")
        if kind == "session":
            self.session_id = event.get("id", "")
        elif kind == "message_end":
            turn = self._assistant_message(event.get("message") or {})
            return [turn] if turn else []
        elif kind == "turn_start":
            self.turns += 1
        elif kind == "tool_execution_start" and event.get("toolName") == SUBMIT_TOOL:
            self._submit_args[str(event.get("toolCallId") or "")] = event.get("args")
        elif kind == "tool_execution_end":
            return [self._tool_end(event)]
        elif kind == "agent_settled":
            self.settled = True
        return []

    def _tool_end(self, event: dict) -> dict:
        """Normalize a `tool_execution_end` into the `tool_result` contract. For `submit`,
        ``ok`` is lime's envelope verdict: true only when this call's args became the delivered
        envelope AND lime's own ``model_validate`` accepts them (ticket 250) — never merely the
        harness flag of pi's acceptance."""
        tool = str(event.get("toolName") or "")
        call_id = str(event.get("toolCallId") or "")
        ok = False
        if tool == SUBMIT_TOOL:
            args = self._submit_args.pop(call_id, None)
            if event.get("isError"):
                # pi refused the call at the schema (ticket 78): the model DID call submit — keep pi's
                # own words for the correction; this is not a guard denial and must not cut the run
                self.submit_error = _result_text(event)
            elif isinstance(args, dict) and self.envelope is None:
                # first accepted submit wins (ticket 170); raw args, never the clipped
                # tool_call copy. An empty dict is still a dict (is not None, never truthiness).
                self.envelope = args
                self.assistant_after_last_denial = True  # a delivered envelope is the answer — terminate:true leaves no assistant message after it
                # lime's verdict, not pi's: an envelope lime would reject is not a success
                ok = self.validate(args) if self.validate else True
        else:
            ok = not event.get("isError")
            if not ok:
                reason = str(event.get("reason") or event.get("result") or "")
                self.denials.append({"tool": event.get("toolName", ""), "reason": reason})
                self.assistant_after_last_denial = False
        return {"type": "tool_result", "tool_call_id": call_id, "tool": tool, "ok": ok, "output": _tool_output(event)}

    @property
    def denial_cut_the_run(self) -> bool:
        # a terminating guard block leaves no assistant answer after the blocked
        # tool_execution_end (07-guard-probe.md Q4) — even when the stream still settles
        return bool(self.denials) and not self.assistant_after_last_denial

    def _assistant_message(self, message: dict) -> dict | None:
        if message.get("role") != "assistant":
            return None
        self.assistant_after_last_denial = True
        # Reset and check has_tool_calls from the last assistant message's content parts
        self.has_tool_calls = any(
            part.get("type") == "toolCall" for part in message.get("content") or []
        )
        self.error = str(message.get("errorMessage") or message.get("stopReason")) if message.get("stopReason") == "error" else ""
        usage = message.get("usage") or {}
        cost = usage.get("cost") or {}
        self.usage.input += usage.get("input", 0)
        self.usage.output += usage.get("output", 0)
        self.usage.cache_read += usage.get("cacheRead", 0)
        self.usage.cache_write += usage.get("cacheWrite", 0)
        self.usage.reasoning += usage.get("reasoning", 0)
        self.usage.total += usage.get("totalTokens", 0)
        self.usage.cost.input += cost.get("input", 0.0)
        self.usage.cost.output += cost.get("output", 0.0)
        self.usage.cost.cache_read += cost.get("cacheRead", 0.0)
        self.usage.cost.cache_write += cost.get("cacheWrite", 0.0)
        self.usage.cost.total += cost.get("total", 0.0)
        # totalTokens includes output — context occupancy is the input+cache sum
        self.context_tokens = usage.get("input", 0) + usage.get("cacheRead", 0) + usage.get("cacheWrite", 0)
        text = "".join(
            part.get("text", "") for part in message.get("content") or [] if part.get("type") == "text"
        )
        if text:
            self.text = text
        return {
            "type": "turn",
            "model": self.model,
            "usage": usage,
            "context_tokens": self.context_tokens,
            "cost": cost.get("total", 0.0),
        }


def _signal_ladder(proc) -> None:
    for sig, grace in ((signal.SIGINT, SIGINT_GRACE), (signal.SIGTERM, SIGTERM_GRACE)):
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)
        with contextlib.suppress(TimeoutExpired):
            proc.wait(timeout=grace)
            return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


def run(
    req: AgentRequest,
    on_event: Callable[[dict], None] | None = None,
    on_spawn: Callable[[int], None] | None = None,
    on_exit: Callable[[int], None] | None = None,
) -> AgentResult:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    if req.budget_file:  # the Guard's mailbox — guard.ts shells out to `lime guard` (spec §8, §9)
        env["LIME_BUDGET_FILE"] = req.budget_file
    if req.handoff_dir:
        env["LIME_HANDOFF_DIR"] = req.handoff_dir
    if req.handoff_path:
        env["LIME_HANDOFF_PATH"] = req.handoff_path
    if req.submit_schema:
        env["LIME_SUBMIT_SCHEMA"] = req.submit_schema
    env["LIME_TOOL_CHOICE"] = req.tool_choice
    if req.writes is None:  # unset Role policy — never inherit a stale one from the operator
        env.pop("LIME_WRITES", None)
    else:
        env["LIME_WRITES"] = json.dumps(req.writes)  # [] must round-trip as [] (read-only)
    if req.bash_deadline_s is None:  # only the builder's calls are capped — never inherit a stale value
        env.pop("LIME_BASH_DEADLINE_S", None)
    else:
        env["LIME_BASH_DEADLINE_S"] = str(req.bash_deadline_s)
    # The Guard selects its partial-delivery verb by runtime — force our own marker
    # immediately before spawning, overriding any inherited one.
    env["LIME_AGENT_RUNTIME"] = "pi"
    proc = Popen(build_command(req), cwd=req.cwd, env=env, stdin=DEVNULL, stdout=PIPE, stderr=PIPE)
    assert proc.stdout is not None and proc.stderr is not None
    stderr_pipe = proc.stderr
    if on_spawn:
        on_spawn(proc.pid)

    stderr_chunks: list[bytes] = []
    stderr_reader = threading.Thread(target=lambda: stderr_chunks.append(stderr_pipe.read()), daemon=True)
    stderr_reader.start()

    stream = _Stream(req.model, on_event, req.envelope_validator)
    stopped = {"timeout": False, "max_turns": False, "submit": False}
    signal_threads: list[threading.Thread] = []
    stop_lock = threading.Lock()
    ladder_started = False  # one bounded signal ladder per process, whatever the cause

    def stop(cause: str) -> None:
        """Record the cause and, for the first cause only, run the bounded ladder.
        timeout/max-turns/submit can all record, but never race a second ladder."""
        nonlocal ladder_started
        stopped[cause] = True
        with stop_lock:
            if ladder_started:
                return
            ladder_started = True
        thread = threading.Thread(target=_signal_ladder, args=(proc,), daemon=True)
        thread.start()
        signal_threads.append(thread)

    timer = threading.Timer(req.timeout_s, stop, ("timeout",))
    timer.start()
    returncode: int = -1
    try:
        with open(req.raw_output_path, "ab") as raw:
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8")
                raw.write(raw_line if raw_line.endswith(b"\n") else raw_line + b"\n")
                raw.flush()
                stream.feed(line)
                if stream.envelope is not None and not stopped["submit"]:
                    # act on the acceptance transition immediately: do not wait for
                    # agent_settled, EOF, another assistant answer, or the timer
                    timer.cancel()
                    stop("submit")
                if req.should_stop and not stopped["timeout"] and stream.envelope is None and req.should_stop():
                    timer.cancel()
                    stop("timeout")  # repeat stop (ticket 173): same status as the time ceiling
                if req.max_turns and stream.turns > req.max_turns and not stopped["max_turns"]:
                    stop("max_turns")
        returncode = proc.wait()
    except UnicodeDecodeError:
        if proc.returncode is None:
            _signal_ladder(proc)
            returncode = proc.wait()
        else:
            returncode = proc.returncode
        if stream.envelope is None:
            raise  # pre-acceptance invalid UTF-8 keeps the existing retry behavior
        # a delivered envelope survives trailing stream decoding failure
    finally:
        timer.cancel()
        if not ladder_started and proc.returncode is None:
            # an exception (interruption/persistence failure) escaped the read loop
            # before the process was reaped — clean it up, bounded, then re-raise
            _signal_ladder(proc)
            returncode = proc.wait()
        for thread in signal_threads:
            thread.join()
        stderr_reader.join(timeout=5)
        if on_exit:
            on_exit(proc.pid)

    if stream.envelope is not None:
        # an accepted envelope wins the adapter's terminal status: no stale
        # timeout/stream error even when the child exits from a signal or the timer
        status = "ok"
        for denial in stream.denials:
            logger.warning(
                "denial ignored because a report was delivered: tool=%s",
                denial.get("tool") or "unknown",
            )
    elif stopped["max_turns"]:
        status = "max_turns"
    elif stopped["timeout"]:
        status = "timeout"
    elif stream.denial_cut_the_run:
        status = "denied"
    elif stream.error:
        status = "error"
    elif stream.settled and returncode == 0:
        status = "ok"
    else:
        status = "error"

    if status == "max_turns":
        error = f"max_turns: {stream.turns} turns > cap {req.max_turns}"
    elif status == "timeout":
        error = f"timeout: {req.timeout_s}s elapsed"
    elif stream.envelope is not None:
        error = ""
    else:
        error = stream.error

    return AgentResult(
        status=status,
        text=stream.text,
        session_id=stream.session_id or req.session_id,
        returncode=returncode,
        usage=stream.usage,
        cost=stream.usage.cost.total,
        context_tokens=stream.context_tokens,
        context_window=context_window(req.model),
        denials=stream.denials,
        stderr_tail=b"".join(stderr_chunks).decode("utf-8", errors="replace")[-800:],
        error=error,
        has_tool_calls=stream.has_tool_calls,
        envelope=stream.envelope,
        submit_error=stream.submit_error,
    )
