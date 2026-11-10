"""Claude Code adapter (spec §4, assets/02-runtime-adapters.md)."""

import json
import logging
import os
import threading
from subprocess import DEVNULL, PIPE, Popen
from typing import Callable

from .agent_pi import _signal_ladder
from .data_types import AgentRequest, AgentResult, EnvelopeBase, UsageBreakdown

logger = logging.getLogger(__name__)

_EFFORT = {"off": "low", "minimal": "low"}


def effort(thinking: str) -> str:
    return _EFFORT.get(thinking, thinking)


def build_settings(req: AgentRequest) -> dict:
    # never disableAllHooks — it silences the guard passed in this very blob (07-guard-probe.md Q2)
    settings: dict = {"alwaysThinkingEnabled": True}
    if req.guards:
        settings["hooks"] = {
            "PreToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": g} for g in req.guards]}
            ]
        }
    return settings


def _prompt_with_attachments(req: AgentRequest) -> str:
    # cc has no native attachment arg — its `read` tool handles images, so the
    # reference paths are simply named up front (17-designer-role.md)
    if not req.attachments:
        return req.prompt
    return "Reference files to read first: " + ", ".join(req.attachments) + "\n\n" + req.prompt


def build_command(req: AgentRequest) -> list[str]:
    cmd = [
        "claude", "-p", _prompt_with_attachments(req), "--output-format", "stream-json", "--verbose",
        "--model", req.model,
        "--resume" if req.resume else "--session-id", req.session_id,
        "--system-prompt", req.system_prompt,
        "--effort", effort(req.thinking),
        "--permission-mode", "bypassPermissions",
        "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
        "--settings", json.dumps(build_settings(req)),
    ]
    if req.tools:
        cmd += ["--tools", ",".join(req.tools)]
    elif req.tools is not None:  # [] = no tools (None = runtime default, unchanged)
        cmd += ["--tools", ""]
    if req.max_turns:
        cmd += ["--max-turns", str(req.max_turns)]
    return cmd


def _block_text(content) -> str:
    """The text of a tool result block's `content`: a bare string, or the joined text parts of a list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


class _Stream:
    """Folds parsed stdout events into result state; emits one `turn` per model answer and
    one `tool_result` per tool result block on a `user` line."""

    def __init__(self, model: str, on_event: Callable[[dict], None] | None):
        self.model = model
        self.on_event = on_event
        self.session_id = ""
        self.text = ""
        self.subtype = ""
        self.denials: list[dict] = []
        self.usage = UsageBreakdown()
        self.context_tokens = 0
        self.context_window = 0
        self.cost = 0.0
        self._message_id = None

    def feed(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        emitted = self._route(event)
        if self.on_event:
            self.on_event(event)
            for out in emitted:
                self.on_event(out)

    def _route(self, event: dict) -> list[dict]:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self.session_id = event.get("session_id", "")
        elif kind == "assistant":
            turn = self._assistant_message(event.get("message") or {})
            return [turn] if turn else []
        elif kind == "user":
            return self._tool_results(event.get("message") or {})
        elif kind == "result":
            self._result(event)
        return []

    def _tool_results(self, message: dict) -> list[dict]:
        """One normalized `tool_result` per `tool_result` block on a `user` line (parallel calls)."""
        content = message.get("content")
        if not isinstance(content, list):
            return []
        results = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            results.append(
                {
                    "type": "tool_result",
                    "tool_call_id": str(block.get("tool_use_id") or ""),
                    "tool": "",
                    "ok": not block.get("is_error"),
                    "output": _block_text(block.get("content")),
                }
            )
        return results

    def _assistant_message(self, message: dict) -> dict | None:
        text = "".join(
            part.get("text", "") for part in message.get("content") or [] if part.get("type") == "text"
        )
        if text:
            self.text = text
        # one API answer is repeated per content block with the same id and usage — count it once
        if message.get("id") == self._message_id:
            return None
        self._message_id = message.get("id")
        usage = message.get("usage") or {}
        self.usage.input += usage.get("input_tokens", 0)
        self.usage.output += usage.get("output_tokens", 0)
        self.usage.cache_read += usage.get("cache_read_input_tokens", 0)
        self.usage.cache_write += usage.get("cache_creation_input_tokens", 0)
        self.usage.reasoning += (usage.get("output_tokens_details") or {}).get("thinking_tokens", 0)
        self.context_tokens = self._occupancy(usage)
        self.usage.total = self.usage.input + self.usage.output + self.usage.cache_read + self.usage.cache_write
        return {
            "type": "turn",
            "model": self.model,
            "usage": usage,
            "context_tokens": self.context_tokens,
            "cost": 0.0,  # subscription — the run total lands on the result event
        }

    def _result(self, event: dict) -> None:
        self.subtype = event.get("subtype", "")
        self.cost = event.get("total_cost_usd", 0.0)
        self.denials = event.get("permission_denials") or []
        if event.get("result"):
            self.text = event["result"]
        # context_tokens stays the last assistant message's sum — result.usage is run-cumulative
        windows = {k: int(v.get("contextWindow") or 0) for k, v in (event.get("modelUsage") or {}).items()}
        matched = [w for k, w in windows.items() if self.model in k]  # subagent models also get rows
        self.context_window = matched[0] if matched else max(windows.values(), default=0)

    @staticmethod
    def _occupancy(usage: dict) -> int:
        # output_tokens is not context occupancy — input + cache sum only
        return (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )


def run(
    req: AgentRequest,
    on_event: Callable[[dict], None] | None = None,
    on_spawn: Callable[[int], None] | None = None,
    on_exit: Callable[[int], None] | None = None,
) -> AgentResult:
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    env["CLAUDE_CODE_DISABLE_ADAPTATIVE_THINKING"] = "1"
    if req.budget_file:  # the Guard's mailbox — `lime guard` reads these (spec §8, §9)
        env["LIME_BUDGET_FILE"] = req.budget_file
    if req.handoff_dir:
        env["LIME_HANDOFF_DIR"] = req.handoff_dir
    if req.handoff_path:
        env["LIME_HANDOFF_PATH"] = req.handoff_path
    if req.writes is None:  # unset Role policy — never inherit a stale one from the operator
        env.pop("LIME_WRITES", None)
    else:
        env["LIME_WRITES"] = json.dumps(req.writes)  # [] must round-trip as [] (read-only)
    # The Guard selects its partial-delivery verb by runtime — force our own marker
    # immediately before spawning, overriding any inherited one.
    env["LIME_AGENT_RUNTIME"] = "claude_code"
    proc = Popen(build_command(req), cwd=req.cwd, env=env, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, text=True)
    assert proc.stdout is not None and proc.stderr is not None
    stderr_pipe = proc.stderr
    if on_spawn:
        on_spawn(proc.pid)

    stderr_chunks: list[str] = []
    stderr_reader = threading.Thread(target=lambda: stderr_chunks.append(stderr_pipe.read()), daemon=True)
    stderr_reader.start()

    stream = _Stream(req.model, on_event)
    stopped = {"timeout": False}
    signal_threads: list[threading.Thread] = []

    def stop(cause: str) -> None:
        stopped[cause] = True
        thread = threading.Thread(target=_signal_ladder, args=(proc,), daemon=True)
        thread.start()
        signal_threads.append(thread)

    timer = threading.Timer(req.timeout_s, stop, ("timeout",))
    timer.start()
    try:
        with open(req.raw_output_path, "a") as raw:
            for line in proc.stdout:
                raw.write(line if line.endswith("\n") else line + "\n")
                raw.flush()
                stream.feed(line)
                if req.should_stop and not stopped["timeout"] and req.should_stop():
                    timer.cancel()
                    stop("timeout")
        returncode = proc.wait()
    finally:
        timer.cancel()
    for thread in signal_threads:
        thread.join()
    stderr_reader.join(timeout=5)
    if on_exit:
        on_exit(proc.pid)

    if stopped["timeout"]:
        status = "timeout"
    elif stream.subtype == "error_max_turns":
        status = "max_turns"
    elif stream.subtype == "success":
        # exit code says nothing — a hook/rule denial still ends `success` with the list filled
        status = "denied" if stream.denials else "ok"
        if stream.denials:
            # a finished report in the text outranks the denial list (ticket 171): validate the
            # common envelope so the orchestrator can reach typed extraction downstream. The
            # parser is imported locally because agents.py imports this adapter (no cycle).
            try:
                from .agents import extract_envelope

                extract_envelope(stream.text, EnvelopeBase)
            except ValueError:
                pass
            else:
                status = "ok"
                for denial in stream.denials:
                    logger.warning(
                        "denial ignored because a report was delivered: tool=%s",
                        denial.get("tool_name") or "unknown",
                    )
    else:
        status = "error"

    stream.usage.cost.total = stream.cost
    return AgentResult(
        status=status,
        text=stream.text,
        session_id=stream.session_id,
        returncode=returncode,
        usage=stream.usage,
        cost=stream.cost,
        context_tokens=stream.context_tokens,
        context_window=stream.context_window,
        denials=stream.denials,
        stderr_tail="".join(stderr_chunks)[-800:],
    )
